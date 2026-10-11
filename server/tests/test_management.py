import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import main
from app.audit import classify, summary
from app.config import get_settings
from app.database import connect, initialize_database
from app.schemas import AccountUpdate


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        config = get_settings().model_copy(update={"database_path": str(Path(self.folder.name) / "test.sqlite3")})
        self.config_patch = patch("app.database.get_settings", return_value=config)
        self.config_patch.start()
        initialize_database()
        self.owner = {"id": 1, "username": "站长", "role": "superadmin", "pending_delete": 0}
        self.admin = {"id": 2, "username": "管理员甲", "role": "admin", "pending_delete": 0}
        self.guest = {"id": 3, "username": "访客甲", "role": "guest", "pending_delete": 0}
        with connect() as db:
            for account in (self.owner, self.admin, self.guest):
                db.execute("INSERT INTO users(id,username,role,created_at) VALUES(?,?,?,?)",
                           (account["id"], account["username"], account["role"], main.stamp()))

    def tearDown(self):
        self.config_patch.stop()
        self.folder.cleanup()

    def logs(self, **kwargs):
        options = dict(user=self.owner, include_all=False, action="", actor="", target="", q="",
                       result="", start=None, end=None, page=1, page_size=30)
        options.update(kwargs)
        return main.list_audit_logs(**options)

    def insert_event(self, method, path, status, target="", created="2026-09-30T07:00:00+00:00"):
        event = classify(method, path, status)
        with connect() as db:
            db.execute("""INSERT INTO audit_logs(username,role,method,path,status_code,created_at,
                       action,category,target,summary,important) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       ("管理员甲", "admin", method, path, status, created, event["action"], event["category"],
                        target, summary(event, target, status), event["important"]))

    def test_importance_filters_and_pagination_cover_history(self):
        self.insert_event("GET", "/api/accounts", 200)
        self.insert_event("POST", "/api/auth/guest/access", 200, "访客甲")
        self.insert_event("POST", "/api/guests/访客甲/ban", 200, "访客甲")
        self.insert_event("POST", "/api/auth/admin/login", 401, "管理员甲")
        self.assertEqual(self.logs()["total"], 2)
        self.assertEqual(self.logs(include_all=True)["total"], 4)
        matched = self.logs(action="guest_ban", actor="管理员甲", target="访客甲", q="拉黑", result="success",
                            start=main.datetime.fromisoformat("2026-09-30T06:59:00+00:00"),
                            end=main.datetime.fromisoformat("2026-09-30T07:01:00+00:00"))
        self.assertEqual(matched["total"], 1)
        self.assertIn("拉黑访客", matched["items"][0]["summary"])
        self.assertEqual(self.logs(result="failure")["total"], 1)
        first, second = self.logs(page_size=1), self.logs(page_size=1, page=2)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertEqual(self.logs(q="不存在")["total"], 0)

    def test_historical_migration_hides_queries(self):
        with connect() as db:
            db.execute("""INSERT INTO audit_logs(username,role,method,path,status_code,created_at)
                          VALUES('旧用户','admin','GET','/api/invites/guest',200,?)""", (main.stamp(),))
        initialize_database()
        self.assertEqual(self.logs()["total"], 0)
        self.assertEqual(self.logs(include_all=True)["items"][0]["summary"], "查询数据")

    def test_privileged_dependencies_and_guest_only_kick(self):
        for guard, user in ((main.require_manager, self.guest), (main.require_superadmin, self.admin)):
            with self.assertRaises(HTTPException) as raised:
                guard(user)
            self.assertEqual(raised.exception.status_code, 403)
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.kick_guest(2, Request({"type": "http", "headers": []}), self.admin))
        self.assertEqual(raised.exception.status_code, 404)

    def test_kick_keeps_session_ban_revokes_and_disconnects(self):
        main.create_session(3, "guest")
        with patch("app.main.remove_guest_from_rooms", new_callable=AsyncMock, return_value=["测试房间"]) as remove:
            request = Request({"type": "http", "headers": []})
            asyncio.run(main.kick_guest(3, request, self.admin))
            self.assertIn("测试房间", request.state.audit_target)
            with connect() as db:
                self.assertIsNotNone(db.execute("SELECT 1 FROM users WHERE id=3").fetchone())
                self.assertIsNotNone(db.execute("SELECT 1 FROM sessions WHERE user_id=3").fetchone())
            asyncio.run(main.ban_guest("访客甲", self.admin))
            self.assertEqual(remove.await_count, 2)
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM users WHERE id=3").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sessions WHERE user_id=3").fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM banned_ids WHERE guest_id='访客甲'").fetchone())

    def test_password_change_audit_never_stores_password(self):
        secret = "仅在测试中使用的密码"
        content = json.dumps({"new_password": secret}).encode()
        async def receive():
            return {"type": "http.request", "body": content, "more_body": False}
        request = Request({"type": "http", "method": "PATCH", "path": "/api/accounts/2",
                           "query_string": b"", "headers": []}, receive)
        async def call_next(req):
            req.state.audit_user = self.owner
            result = main.update_account(2, AccountUpdate(new_password=secret), self.owner)
            return JSONResponse(result)
        asyncio.run(main.audit_requests(request, call_next))
        row = self.logs()["items"][0]
        self.assertEqual(row["action"], "password_reset")
        self.assertEqual(row["target"], "管理员甲")
        self.assertNotIn(secret, json.dumps(row, ensure_ascii=False))
        with connect() as db:
            stored = db.execute("SELECT password_hash FROM users WHERE id=2").fetchone()[0]
        self.assertTrue(main.password_hash.verify(secret, stored))

    def test_service_failure_keeps_failed_operation_and_system_event(self):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        request = Request({"type": "http", "method": "POST", "path": "/api/guests/3/kick",
                           "query_string": b"", "headers": []}, receive)
        async def call_next(req):
            req.state.audit_user = self.admin
            raise RuntimeError("media service unavailable")
        with self.assertRaises(RuntimeError):
            asyncio.run(main.audit_requests(request, call_next))
        actions = {row["action"] for row in self.logs(result="failure")["items"]}
        self.assertEqual(actions, {"guest_kick", "system_error"})


if __name__ == "__main__":
    unittest.main()
