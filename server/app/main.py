from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import sqlite3
import string
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from livekit import api as livekit_api
from pwdlib import PasswordHash

from .config import get_settings
from .errors import validation_message
from .database import connect, initialize_database
from .problem_recording import register_problem_recording
from .audit import classify, fallback_target, summary, ACTION_LABELS, CATEGORIES
from .schemas import (
    AdminLogin,
    AdminRegister,
    AccountUpdate,
    AdminInviteCreate,
    GuestLogin,
    GuestRegister,
    InviteCreate,
    PasswordReset,
    ProfileUpdate,
    RoomCreate,
    RoomUpdate,
)

settings = get_settings()
password_hash = PasswordHash.recommended()
timezone_info = ZoneInfo(settings.app_timezone)
logger = logging.getLogger("rikumo_room")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or now_utc()).isoformat()


def record_system_event(action: str, description: str, target: str = "", status: int = 200) -> None:
    with connect() as db:
        db.execute(
            """INSERT INTO audit_logs(username,role,method,path,status_code,created_at,
               action,category,target,summary,important) VALUES('系统','system','SYSTEM','',?,?,?,?,?,?,1)""",
            (status, stamp(), action, "system", target, description),
        )


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, token_digest(raw)


def issue_invite_code() -> tuple[str, str]:
    raw = "".join(secrets.choice(string.ascii_letters) for _ in range(6))
    return raw, token_digest(raw)


def app_now() -> datetime:
    return datetime.now(timezone_info)


def reset_day(value: datetime | None = None) -> date:
    current = (value or app_now()).astimezone(timezone_info)
    return current.date() if current.time() >= time(4, 0) else current.date() - timedelta(days=1)


def next_reset(value: datetime | None = None) -> datetime:
    current = (value or app_now()).astimezone(timezone_info)
    target_date = current.date()
    if current.time() >= time(4, 0):
        target_date += timedelta(days=1)
    return datetime.combine(target_date, time(4, 0), timezone_info)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def require_user(request: Request) -> dict:
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        raise HTTPException(401, "请先登录。")
    with connect() as db:
        row = db.execute(
            """SELECT users.id, users.username, users.role, users.pending_delete,
                      sessions.expires_at
               FROM sessions JOIN users ON users.id = sessions.user_id
               WHERE sessions.token_hash = ?""",
            (token_digest(raw),),
        ).fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) <= now_utc():
        raise HTTPException(401, "登录状态已失效，请重新登录。")
    user = dict(row)
    request.state.audit_user = user
    return user


def require_manager(user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "superadmin"):
        raise HTTPException(403, "此操作需要管理员权限。")
    if user["pending_delete"]:
        raise HTTPException(403, "访客资格已到期，请使用新的邀请链接重新加入。")
    return user


def require_superadmin(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "superadmin":
        raise HTTPException(403, "此操作需要超级管理员权限。")
    return user


def require_active_guest(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "guest" or user["pending_delete"]:
        raise HTTPException(403, "访客资格已失效，请使用有效邀请链接重新加入。")
    return user


def set_session(response: Response, raw: str, expires: datetime) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        raw,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        expires=expires,
        path="/",
    )


def create_session(user_id: int, role: str) -> tuple[str, datetime]:
    raw, digest = issue_token()
    now = now_utc()
    if role == "guest":
        expiry = now + timedelta(hours=settings.guest_session_hours)
    else:
        expiry = now + timedelta(days=settings.admin_session_days)
    with connect() as db:
        if role == "guest":
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(?,?,?,?)",
            (digest, user_id, stamp(expiry), stamp(now)),
        )
    return raw, expiry


def guest_identity(user_id: int) -> str:
    return f"guest_{user_id}"


def clean_guest(user_id: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM users WHERE id = ? AND role = 'guest'", (user_id,))


async def livekit_client() -> livekit_api.LiveKitAPI:
    return livekit_api.LiveKitAPI(
        settings.livekit_api_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    )


async def active_identities() -> set[str]:
    with connect() as db:
        room_names = [row["id"] for row in db.execute("SELECT id FROM rooms WHERE archived = 0")]
    if not room_names:
        return set()
    lk = await livekit_client()
    active: set[str] = set()
    try:
        active_rooms = await lk.room.list_rooms(livekit_api.ListRoomsRequest())
        active_room_names = {room.name for room in active_rooms.rooms}
        for room_name in room_names:
            if room_name not in active_room_names:
                continue
            try:
                participants = await lk.room.list_participants(
                    livekit_api.ListParticipantsRequest(room=room_name)
                )
                active.update(p.identity for p in participants.participants)
            except Exception:
                raise
    finally:
        await lk.aclose()
    return active


async def apply_daily_reset() -> None:
    active = await active_identities()
    today = reset_day().isoformat()
    with connect() as db:
        guests = db.execute("SELECT id FROM users WHERE role = 'guest'").fetchall()
        for guest in guests:
            user_id = guest["id"]
            if guest_identity(user_id) in active:
                db.execute("UPDATE users SET pending_delete = 1 WHERE id = ?", (user_id,))
            else:
                db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.execute("DELETE FROM invites WHERE expires_at <= ? OR kind = 'guest'", (stamp(),))
        db.execute(
            "INSERT INTO app_state(key, value) VALUES('last_reset_day', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (today,),
        )
    retained = sum(guest_identity(guest["id"]) in active for guest in guests)
    record_system_event("daily_reset", f"每日访客清理：清除 {len(guests) - retained} 人，延后清理 {retained} 人")


async def reap_pending_guests() -> None:
    with connect() as db:
        pending = [row["id"] for row in db.execute(
            "SELECT id FROM users WHERE role = 'guest' AND pending_delete = 1"
        )]
    if not pending:
        return
    active = await active_identities()
    for user_id in pending:
        if guest_identity(user_id) not in active:
            clean_guest(user_id)


async def enforce_bans() -> None:
    with connect() as db:
        banned = {row["livekit_identity"] for row in db.execute(
            "SELECT livekit_identity FROM banned_ids"
        )}
        room_names = [row["id"] for row in db.execute("SELECT id FROM rooms WHERE archived = 0")]
    if not banned or not room_names:
        return
    lk = await livekit_client()
    try:
        for room_name in room_names:
            participants = await lk.room.list_participants(
                livekit_api.ListParticipantsRequest(room=room_name)
            )
            for participant in participants.participants:
                if participant.identity in banned:
                    await lk.room.remove_participant(
                        livekit_api.RoomParticipantIdentity(
                            room=room_name, identity=participant.identity
                        )
                    )
    finally:
        await lk.aclose()


async def scheduler() -> None:
    last_error_logged = None
    while True:
        try:
            cleanup_problem_recordings()
            expected_reset = reset_day().isoformat()
            with connect() as db:
                row = db.execute("SELECT value FROM app_state WHERE key = 'last_reset_day'").fetchone()
            if not row or row["value"] < expected_reset:
                await apply_daily_reset()
            await reap_pending_guests()
            await enforce_bans()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep the app available and retry on the next interval if LiveKit is offline.
            logger.exception("Background guest cleanup failed; it will retry")
            if last_error_logged is None or now_utc() - last_error_logged > timedelta(minutes=1):
                record_system_event("system_error", "访客清理或拉黑处理异常，后台将重试", status=500)
                last_error_logged = now_utc()
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    with connect() as db:
        owner = db.execute("SELECT id FROM users WHERE role = 'superadmin'").fetchone()
        if not owner:
            db.execute(
                """INSERT INTO users(username, role, password_hash, created_at)
                   VALUES(?, 'superadmin', ?, ?)""",
                (
                    settings.superadmin_username,
                    password_hash.hash(settings.superadmin_password),
                    stamp(),
                ),
            )
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Rikumo Room API", version="0.1.0", lifespan=lifespan)
cleanup_problem_recordings = register_problem_recording(app, require_user, require_manager, livekit_client, record_system_event)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": validation_message(exc.errors())})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def audit_requests(request: Request, call_next):
    method = request.method.upper()
    path = request.url.path
    payload = {}
    if method in {"POST", "PATCH", "PUT"}:
        try:
            body = await request.json()
            if isinstance(body, dict):
                payload = {key: body[key] for key in ("username", "guest_id", "name") if key in body}
                payload["password_changed"] = bool(body.get("new_password"))
        except (ValueError, TypeError):
            pass
    target = str(payload.get("guest_id") or payload.get("username") or payload.get("name") or fallback_target(path))[:200]
    with connect() as db:
        parts = path.split("/")
        if len(parts) > 3 and parts[2] in {"accounts", "admins", "rooms", "invites"}:
            table = {"accounts": "users", "admins": "users", "rooms": "rooms", "invites": "invites"}[parts[2]]
            column = {"users": "username", "rooms": "name", "invites": "invite_code"}[table]
            row = db.execute(f"SELECT {column} AS name FROM {table} WHERE id = ?", (parts[3],)).fetchone()
            if row and row["name"]:
                target = row["name"]
            if len(parts) > 5 and parts[4] == "participants":
                identity = parts[5]
                account_id = identity.split("_")[-1]
                person = db.execute("SELECT username FROM users WHERE id = ?", (account_id,)).fetchone()
                target = f"{person['username'] if person else identity} · {target}"
        elif path.startswith("/api/guests/") and path.endswith("/kick"):
            row = db.execute("SELECT username FROM users WHERE id = ?", (parts[3],)).fetchone()
            if row:
                target = row["username"]
    try:
        response = await call_next(request)
    except Exception:
        actor = getattr(request.state, "audit_user", {})
        event = classify(method, path, 500, payload.get("password_changed", False))
        with connect() as db:
            db.execute(
                """INSERT INTO audit_logs(user_id,username,role,method,path,status_code,created_at,
                   action,category,target,summary,important) VALUES(?,?,?,?,?,500,?,?,?,?,?,1)""",
                (actor.get("id"), actor.get("username", "未登录用户"), actor.get("role", "unknown"),
                 method, path, stamp(), event["action"], event["category"], target, summary(event, target, 500)),
            )
        record_system_event("system_error", "服务异常", path, 500)
        raise
    access_paths = {
        "/api/auth/admin/login", "/api/auth/admin/register",
        "/api/auth/guest/access", "/api/auth/guest/login",
    }
    if "/problem-recording/" in path and path.endswith("/report") and response.status_code < 400:
        # Diagnostic heartbeats are transient metadata, not business audit operations.
        return response
    if path.startswith("/api/") and (
        method in {"POST", "PUT", "PATCH", "DELETE"}
        or path in access_paths
        or (method == "GET" and path in {
            "/api/guests", "/api/accounts", "/api/audit-logs",
            "/api/invites/guest", "/api/invites/admin", "/api/admins", "/api/bans",
        })
    ):
        user = getattr(request.state, "audit_user", None)
        if not user and request.cookies.get(settings.session_cookie_name):
            with connect() as db:
                row = db.execute(
                    """SELECT users.id, users.username, users.role FROM sessions
                       JOIN users ON users.id = sessions.user_id
                       WHERE sessions.token_hash = ?""",
                    (token_digest(request.cookies[settings.session_cookie_name]),),
                ).fetchone()
                user = dict(row) if row else None
        user = user or getattr(request.state, "audit_user", None) or {}
        target = getattr(request.state, "audit_target", target)
        event = classify(method, path, response.status_code, payload.get("password_changed", False))
        with connect() as db:
            db.execute(
                """INSERT INTO audit_logs(user_id, username, role, method, path, status_code, created_at,
                   action,category,target,summary,important) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user.get("id"), user.get("username", str(payload.get("username") or payload.get("guest_id") or "未登录用户")), user.get("role", "unknown"),
                 method, path, response.status_code, stamp(), event["action"], event["category"], target,
                 summary(event, target, response.status_code), event["important"]),
            )
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def public_config() -> dict[str, str]:
    return {"livekit_url": settings.livekit_public_url, "title": "璃雲の部屋"}


@app.post("/api/auth/guest/access")
def guest_access(payload: GuestRegister, response: Response, request: Request) -> dict:
    guest_id = payload.guest_id
    now = stamp()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        invite = db.execute(
            """SELECT * FROM invites WHERE token_hash = ? AND kind = 'guest'
               AND revoked = 0 AND expires_at > ?""",
            (token_digest(payload.invite_code), now),
        ).fetchone()
        if not invite:
            raise HTTPException(400, "邀请链接无效、已过期或已撤销。")
        if db.execute("SELECT 1 FROM banned_ids WHERE guest_id = ?", (guest_id,)).fetchone():
            raise HTTPException(403, "该访客 ID 已被拉黑。")
        existing = db.execute(
            "SELECT id, username, role, pending_delete FROM users WHERE username = ? COLLATE NOCASE",
            (guest_id,),
        ).fetchone()
        if existing:
            if existing["role"] != "guest" or existing["pending_delete"]:
                raise HTTPException(409, "该 ID 不可用，请更换。")
            user_id = existing["id"]
            guest_id = existing["username"]
        else:
            if invite["uses"] >= invite["max_uses"]:
                raise HTTPException(400, "该邀请的可注册人数已用完。")
            try:
                cursor = db.execute(
                    """INSERT INTO users(username, role, created_at)
                       VALUES(?, 'guest', ?)""",
                    (guest_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "该访客 ID 已被使用，请更换。") from exc
            db.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (invite["id"],))
            user_id = cursor.lastrowid
    raw, expires = create_session(user_id, "guest")
    set_session(response, raw, expires)
    request.state.audit_user = {"id": user_id, "username": guest_id, "role": "guest"}
    return {"id": user_id, "username": guest_id, "role": "guest"}


@app.post("/api/auth/guest/register")
def guest_register(payload: GuestRegister, response: Response, request: Request) -> dict:
    return guest_access(payload, response, request)


@app.post("/api/auth/guest/login")
def guest_login(payload: GuestLogin, response: Response, request: Request) -> dict:
    guest_id = payload.guest_id
    with connect() as db:
        invite = db.execute(
            """SELECT 1 FROM invites WHERE token_hash = ? AND kind = 'guest'
               AND revoked = 0 AND expires_at > ?""",
            (token_digest(payload.invite_code), stamp()),
        ).fetchone()
        if not invite:
            raise HTTPException(400, "请通过有效的访客邀请链接加入。")
        user = db.execute(
            """SELECT id, username, role, pending_delete FROM users
               WHERE username = ? COLLATE NOCASE AND role = 'guest'""",
            (guest_id,),
        ).fetchone()
        if not user or user["pending_delete"]:
            raise HTTPException(401, "访客 ID 已失效，请使用有效邀请链接重新加入。")
        if db.execute("SELECT 1 FROM banned_ids WHERE guest_id = ?", (guest_id,)).fetchone():
            raise HTTPException(403, "该访客 ID 已被拉黑。")
    raw, expires = create_session(user["id"], "guest")
    set_session(response, raw, expires)
    request.state.audit_user = dict(user)
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@app.post("/api/auth/admin/login")
def admin_login(payload: AdminLogin, response: Response, request: Request) -> dict:
    with connect() as db:
        user = db.execute(
            """SELECT id, username, role, password_hash FROM users
               WHERE username = ? COLLATE NOCASE AND role IN ('admin', 'superadmin')""",
            (payload.username,),
        ).fetchone()
    if not user or not user["password_hash"] or not password_hash.verify(
        payload.password, user["password_hash"]
    ):
        raise HTTPException(401, "用户名或密码错误。")
    raw, expires = create_session(user["id"], user["role"])
    set_session(response, raw, expires)
    request.state.audit_user = dict(user)
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@app.post("/api/auth/admin/register")
def admin_register(payload: AdminRegister, response: Response, request: Request) -> dict:
    now = stamp()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        invite = db.execute(
            """SELECT * FROM invites WHERE token_hash = ? AND kind = 'admin'
               AND revoked = 0 AND expires_at > ? AND uses < max_uses""",
            (token_digest(payload.invite_code), now),
        ).fetchone()
        if not invite:
            raise HTTPException(400, "管理员邀请码无效、已过期、已撤销或注册名额已用完。")
        try:
            cursor = db.execute(
                """INSERT INTO users(username, role, password_hash, created_at)
                   VALUES(?, 'admin', ?, ?)""",
                (payload.username, password_hash.hash(payload.password), now),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该用户名已被使用，请更换。") from exc
        db.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (invite["id"],))
        user_id = cursor.lastrowid
    raw, expires = create_session(user_id, "admin")
    set_session(response, raw, expires)
    request.state.audit_user = {"id": user_id, "username": payload.username, "role": "admin"}
    return {"id": user_id, "username": payload.username, "role": "admin"}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    raw = request.cookies.get(settings.session_cookie_name)
    if raw:
        with connect() as db:
            row = db.execute(
            """SELECT users.id, users.username, users.role, users.pending_delete
                   FROM sessions JOIN users ON users.id = sessions.user_id
                   WHERE sessions.token_hash = ?""",
                (token_digest(raw),),
            ).fetchone()
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(raw),))
        if row and row["role"] == "guest" and row["pending_delete"]:
            clean_guest(row["id"])
        if row:
            request.state.audit_user = dict(row)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "pending_delete": bool(user["pending_delete"]),
    }


@app.patch("/api/account/profile")
def update_profile(
    request: Request,
    payload: ProfileUpdate,
    user: dict = Depends(require_user),
) -> dict:
    username = payload.username.strip()
    if not username:
        raise HTTPException(400, "用户名不能为空。")
    if user["role"] == "guest" and len(username) > 24:
        raise HTTPException(400, "访客 ID 最多 24 个字符。")
    if payload.new_password and user["role"] == "guest":
        raise HTTPException(400, "访客账号无需设置密码。")
    with connect() as db:
        row = db.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        if payload.new_password:
            if not row["password_hash"] or not payload.current_password or not password_hash.verify(
                payload.current_password, row["password_hash"]
            ):
                raise HTTPException(400, "当前密码错误。")
        try:
            db.execute(
                "UPDATE users SET username = ? WHERE id = ?", (username, user["id"])
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该用户名已被使用，请更换。") from exc
        if payload.new_password:
            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash.hash(payload.new_password), user["id"]),
            )
            raw_session = request.cookies.get(settings.session_cookie_name)
            db.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                (user["id"], token_digest(raw_session) if raw_session else ""),
            )
    return {"id": user["id"], "username": username, "role": user["role"]}


@app.get("/api/guests")
async def list_guests(user: dict = Depends(require_manager)) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, username, created_at, pending_delete
               FROM users WHERE role = 'guest' ORDER BY username COLLATE NOCASE"""
        ).fetchall()
    return await attach_guest_rooms([dict(row) for row in rows])


@app.get("/api/accounts")
async def list_accounts(user: dict = Depends(require_superadmin)) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, username, role, created_at, pending_delete,
                      CASE WHEN password_hash IS NULL THEN 0 ELSE 1 END AS has_password
               FROM users ORDER BY CASE role WHEN 'superadmin' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
               username COLLATE NOCASE"""
        ).fetchall()
    return await attach_guest_rooms([dict(row) for row in rows])


@app.patch("/api/accounts/{account_id}")
def update_account(
    account_id: int,
    payload: AccountUpdate,
    user: dict = Depends(require_superadmin),
) -> dict[str, bool]:
    if payload.username is None and payload.new_password is None:
        raise HTTPException(400, "请填写需要修改的账号信息。")
    with connect() as db:
        target = db.execute(
            "SELECT id, role FROM users WHERE id = ?", (account_id,)
        ).fetchone()
        if not target or target["role"] == "superadmin":
            raise HTTPException(404, "账号不存在。")
        if payload.new_password and target["role"] != "admin":
            raise HTTPException(400, "访客账号无需设置密码。")
        if payload.username is not None:
            username = payload.username.strip()
            if not username or (target["role"] == "guest" and len(username) > 24):
                raise HTTPException(400, "用户名格式不正确。")
            try:
                db.execute(
                    "UPDATE users SET username = ? WHERE id = ?", (username, account_id)
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "该用户名已被使用，请更换。") from exc
        if payload.new_password:
            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash.hash(payload.new_password), account_id),
            )
            db.execute("DELETE FROM sessions WHERE user_id = ?", (account_id,))
    return {"ok": True}


@app.get("/api/audit-logs")
def list_audit_logs(
    user: dict = Depends(require_superadmin),
    include_all: bool = False,
    action: str = Query(default="", max_length=64),
    actor: str = Query(default="", max_length=200),
    target: str = Query(default="", max_length=200),
    q: str = Query(default="", max_length=200),
    result: str = Query(default="", pattern="^(|success|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
) -> dict:
    clauses, values = [], []
    if not include_all:
        clauses.append("important = 1")
    if action:
        clauses.append("action = ?")
        values.append(action)
    for column, value in (("username", actor), ("target", target)):
        if value:
            clauses.append(f"{column} LIKE ?")
            values.append(f"%{value}%")
    if q:
        clauses.append("(summary LIKE ? OR target LIKE ? OR username LIKE ?)")
        values.extend([f"%{q}%"] * 3)
    if result:
        clauses.append("status_code < 400" if result == "success" else "status_code >= 400")
    dates = []
    for value in (start, end):
        dates.append(value.replace(tzinfo=timezone_info).astimezone(timezone.utc) if value and value.tzinfo is None else value.astimezone(timezone.utc) if value else None)
    if dates[0] and dates[1] and dates[0] > dates[1]:
        raise HTTPException(400, "开始时间不能晚于结束时间")
    for op, value in ((">=", dates[0]), ("<=", dates[1])):
        if value:
            clauses.append(f"created_at {op} ?")
            values.append(stamp(value))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM audit_logs" + where, values).fetchone()[0]
        rows = db.execute("SELECT * FROM audit_logs" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                          [*values, page_size, (page - 1) * page_size]).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size,
            "actions": ACTION_LABELS, "categories": CATEGORIES}


@app.get("/api/rooms")
async def list_rooms(user: dict = Depends(require_user)) -> list[dict]:
    with connect() as db:
        rooms = [dict(row) for row in db.execute(
            "SELECT id, name, description, position FROM rooms WHERE archived = 0 ORDER BY position, name"
        )]
    lk = await livekit_client()
    try:
        for room in rooms:
            try:
                participants = await lk.room.list_participants(
                    livekit_api.ListParticipantsRequest(room=room["id"])
                )
                room["participants"] = [
                    {"identity": p.identity, "name": p.name, "is_speaking": False}
                    for p in participants.participants
                ]
                room["participant_count"] = len(room["participants"])
            except Exception:
                room["participants"] = []
                room["participant_count"] = 0
    finally:
        await lk.aclose()
    return rooms


@app.post("/api/rooms")
def create_room(payload: RoomCreate, user: dict = Depends(require_manager)) -> dict:
    room_id = secrets.token_urlsafe(10).replace("-", "a").replace("_", "b")
    try:
        with connect() as db:
            db.execute(
                """INSERT INTO rooms(id, name, description, created_by, created_at)
                   VALUES(?,?,?,?,?)""",
                (room_id, payload.name.strip(), payload.description.strip(), user["id"], stamp()),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "该房间名称已存在，请换一个名称。") from exc
    return {"id": room_id, "name": payload.name.strip(), "description": payload.description.strip()}


@app.patch("/api/rooms/{room_id}")
def update_room(room_id: str, payload: RoomUpdate, user: dict = Depends(require_manager)) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(400, "请填写需要修改的内容。")
    columns = {"name": "name", "description": "description", "archived": "archived"}
    assignments = ", ".join(f"{columns[key]} = ?" for key in values)
    params = [int(value) if key == "archived" else value for key, value in values.items()]
    params.append(room_id)
    with connect() as db:
        cursor = db.execute(f"UPDATE rooms SET {assignments} WHERE id = ?", params)
        if not cursor.rowcount:
            raise HTTPException(404, "房间不存在或已删除。")
        room = db.execute(
            "SELECT id, name, description, archived FROM rooms WHERE id = ?", (room_id,)
        ).fetchone()
    return dict(room)


@app.delete("/api/rooms/{room_id}")
async def delete_room(room_id: str, user: dict = Depends(require_manager)) -> dict[str, bool]:
    with connect() as db:
        room = db.execute("SELECT id FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not room:
        raise HTTPException(404, "房间不存在或已删除。")
    lk = await livekit_client()
    try:
        await lk.room.delete_room(livekit_api.DeleteRoomRequest(room=room_id))
    finally:
        await lk.aclose()
    with connect() as db:
        db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
    return {"ok": True}


@app.post("/api/rooms/{room_id}/token")
def room_token(room_id: str, user: dict = Depends(require_user)) -> dict[str, str]:
    if user["role"] == "guest" and user["pending_delete"]:
        raise HTTPException(403, "访客资格已到期，可以继续当前通话；加入其他房间需使用新的邀请链接。")
    with connect() as db:
        room = db.execute(
            "SELECT id, name FROM rooms WHERE id = ? AND archived = 0", (room_id,)
        ).fetchone()
    if not room:
        raise HTTPException(404, "房间不存在或已删除。")
    identity = guest_identity(user["id"]) if user["role"] == "guest" else f"user_{user['id']}"
    token = (
        livekit_api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(user["username"])
        .with_grants(
            livekit_api.VideoGrants(
                room_join=True,
                room=room_id,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_ttl(timedelta(minutes=10))
        .to_jwt()
    )
    return {"token": token, "server_url": settings.livekit_public_url, "room_name": room["name"]}


@app.post("/api/rooms/{room_id}/leave")
def leave_room(room_id: str, user: dict = Depends(require_user)) -> dict[str, bool]:
    identity = guest_identity(user["id"]) if user["role"] == "guest" else f"user_{user['id']}"
    # The client disconnects from LiveKit first; this endpoint cleans a post-reset guest record.
    if user["role"] == "guest" and user["pending_delete"]:
        clean_guest(user["id"])
    return {"ok": True, "room": bool(room_id), "identity": bool(identity)}


@app.get("/api/rooms/{room_id}/participants")
async def room_participants(room_id: str, user: dict = Depends(require_user)) -> list[dict]:
    with connect() as db:
        if not db.execute("SELECT 1 FROM rooms WHERE id = ? AND archived = 0", (room_id,)).fetchone():
            raise HTTPException(404, "房间不存在或已删除。")
    lk = await livekit_client()
    try:
        result = await lk.room.list_participants(livekit_api.ListParticipantsRequest(room=room_id))
        return [
            {"identity": p.identity, "name": p.name, "tracks": [t.sid for t in p.tracks],
             "audio_muted": not any(t.type == 0 and not t.muted for t in p.tracks)}
            for p in result.participants
        ]
    except Exception:
        return []
    finally:
        await lk.aclose()


@app.post("/api/invites/guest")
def create_guest_invite(
    request: Request,
    payload: InviteCreate, user: dict = Depends(require_manager)
) -> dict:
    expires = next_reset().astimezone(timezone.utc)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for _ in range(20):
            raw, digest = issue_invite_code()
            if not db.execute("SELECT 1 FROM invites WHERE token_hash = ?", (digest,)).fetchone():
                break
        else:
            raise HTTPException(503, "邀请码生成失败，请稍后重试。")
        cursor = db.execute(
            """INSERT INTO invites(token_hash, invite_code, kind, max_uses, expires_at, created_by, created_at)
               VALUES(?, ?, 'guest', ?, ?, ?, ?)""",
            (digest, raw, payload.max_uses, stamp(expires), user["id"], stamp()),
        )
    request.state.audit_target = raw
    return {
        "id": cursor.lastrowid,
        "invite_code": raw,
        "expires_at": stamp(expires),
        "max_uses": payload.max_uses,
    }


@app.get("/api/invites/guest")
def list_guest_invites(user: dict = Depends(require_manager)) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, invite_code, max_uses, uses, expires_at, revoked, created_at
               FROM invites WHERE kind = 'guest' ORDER BY id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/invites/admin")
def list_admin_invites(user: dict = Depends(require_superadmin)) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, invite_code, max_uses, uses, expires_at, revoked, created_at
               FROM invites WHERE kind = 'admin' ORDER BY id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


@app.delete("/api/invites/{invite_id}")
def revoke_invite(invite_id: int, user: dict = Depends(require_manager)) -> dict[str, bool]:
    with connect() as db:
        invite = db.execute("SELECT kind FROM invites WHERE id = ?", (invite_id,)).fetchone()
        if not invite or (invite["kind"] == "admin" and user["role"] != "superadmin"):
            raise HTTPException(404, "邀请不存在。")
        cursor = db.execute("UPDATE invites SET revoked = 1 WHERE id = ?", (invite_id,))
    if not cursor.rowcount:
        raise HTTPException(404, "邀请不存在。")
    return {"ok": True}


@app.post("/api/invites/admin")
def create_admin_invite(
    request: Request,
    payload: AdminInviteCreate, user: dict = Depends(require_superadmin)
) -> dict:
    expires = payload.expires_at
    if expires is None:
        expires = next_reset()
    elif expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone_info)
    if expires <= now_utc():
        raise HTTPException(400, "过期时间必须晚于当前时间。")
    expires = expires.astimezone(timezone.utc)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for _ in range(20):
            raw, digest = issue_invite_code()
            if not db.execute("SELECT 1 FROM invites WHERE token_hash = ?", (digest,)).fetchone():
                break
        else:
            raise HTTPException(503, "邀请码生成失败，请稍后重试。")
        cursor = db.execute(
            """INSERT INTO invites(token_hash, invite_code, kind, max_uses, expires_at, created_by, created_at)
               VALUES(?, ?, 'admin', ?, ?, ?, ?)""",
            (digest, raw, payload.max_uses, stamp(expires), user["id"], stamp()),
        )
    request.state.audit_target = raw
    return {"id": cursor.lastrowid, "invite_code": raw, "expires_at": stamp(expires)}


@app.post("/api/admins/{admin_id}/password-reset")
def create_password_reset(admin_id: int, user: dict = Depends(require_superadmin)) -> dict:
    raw, digest = issue_token()
    expires = now_utc() + timedelta(minutes=30)
    with connect() as db:
        target = db.execute(
            "SELECT id FROM users WHERE id = ? AND role = 'admin'", (admin_id,)
        ).fetchone()
        if not target:
            raise HTTPException(404, "管理员账号不存在。")
        db.execute("DELETE FROM sessions WHERE user_id = ?", (admin_id,))
        db.execute(
            """INSERT INTO password_resets(token_hash, user_id, expires_at, created_by, created_at)
               VALUES(?,?,?,?,?)""",
            (digest, admin_id, stamp(expires), user["id"], stamp()),
        )
    return {"reset_code": raw, "expires_at": stamp(expires)}


@app.post("/api/auth/admin/password-reset")
def finish_password_reset(payload: PasswordReset) -> dict[str, bool]:
    with connect() as db:
        reset = db.execute(
            """SELECT * FROM password_resets WHERE token_hash = ? AND used = 0 AND expires_at > ?""",
            (token_digest(payload.reset_code), stamp()),
        ).fetchone()
        if not reset:
            raise HTTPException(400, "密码重置码无效或已过期。")
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (
            password_hash.hash(payload.new_password), reset["user_id"]
        ))
        db.execute("UPDATE password_resets SET used = 1 WHERE token_hash = ?", (reset["token_hash"],))
        db.execute("DELETE FROM sessions WHERE user_id = ?", (reset["user_id"],))
    return {"ok": True}


@app.get("/api/admins")
def list_admins(user: dict = Depends(require_superadmin)) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT id, username, role, created_at FROM users WHERE role = 'admin' ORDER BY username"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/rooms/{room_id}/participants/{identity}/kick")
async def kick_participant(
    room_id: str, identity: str, user: dict = Depends(require_manager)
) -> dict[str, bool]:
    lk = await livekit_client()
    try:
        await lk.room.remove_participant(
            livekit_api.RoomParticipantIdentity(room=room_id, identity=identity)
        )
    finally:
        await lk.aclose()
    return {"ok": True}


@app.post("/api/rooms/{room_id}/participants/{identity}/mute")
async def mute_participant(
    room_id: str, identity: str, user: dict = Depends(require_manager)
) -> dict[str, bool]:
    lk = await livekit_client()
    try:
        participant = await lk.room.get_participant(
            livekit_api.RoomParticipantIdentity(room=room_id, identity=identity)
        )
        audio_tracks = [track for track in participant.tracks if track.type == 0]
        if not audio_tracks:
            # Joining with the microphone off creates no audio track. Already silent
            # is a successful no-op, including when the roster is briefly out of date.
            return {"ok": True, "already_silent": True}
        for track in audio_tracks:
            if track.muted:
                continue
            await lk.room.mute_published_track(
                livekit_api.MuteRoomTrackRequest(
                    room=room_id,
                    identity=identity,
                    track_sid=track.sid,
                    muted=True,
                )
            )
    finally:
        await lk.aclose()
    return {"ok": True}


async def attach_guest_rooms(accounts: list[dict]) -> list[dict]:
    locations = {}
    known = True
    lk = await livekit_client()
    try:
        with connect() as db:
            names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM rooms")}
        active_rooms = await lk.room.list_rooms(livekit_api.ListRoomsRequest())
        for room in active_rooms.rooms:
            participants = await lk.room.list_participants(livekit_api.ListParticipantsRequest(room=room.name))
            for participant in participants.participants:
                locations.setdefault(participant.identity, []).append(names.get(room.name, room.name))
    except Exception:
        known = False
    finally:
        await lk.aclose()
    for account in accounts:
        account["room_names"] = locations.get(guest_identity(account["id"]), [])
        account["room_state_known"] = known
    return accounts


async def remove_guest_from_rooms(account_id: int) -> list[str]:
    removed = []
    identity = guest_identity(account_id)
    with connect() as db:
        names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM rooms")}
    lk = await livekit_client()
    try:
        active_rooms = await lk.room.list_rooms(livekit_api.ListRoomsRequest())
        for room in active_rooms.rooms:
            participants = await lk.room.list_participants(livekit_api.ListParticipantsRequest(room=room.name))
            if any(person.identity == identity for person in participants.participants):
                await lk.room.remove_participant(livekit_api.RoomParticipantIdentity(room=room.name, identity=identity))
                removed.append(names.get(room.name, room.name))
    finally:
        await lk.aclose()
    return removed


@app.post("/api/guests/{account_id}/kick")
async def kick_guest(account_id: int, request: Request, user: dict = Depends(require_manager)) -> dict:
    with connect() as db:
        target = db.execute("SELECT id,username FROM users WHERE id = ? AND role = 'guest'", (account_id,)).fetchone()
    if not target:
        raise HTTPException(404, "访客不存在")
    removed = await remove_guest_from_rooms(account_id)
    if not removed:
        raise HTTPException(409, "访客当前不在语音房间")
    request.state.audit_target = f"{target['username']} · {', '.join(removed)}"
    return {"ok": True, "removed_rooms": len(removed)}


@app.post("/api/guests/{guest_id}/ban")
async def ban_guest(guest_id: str, user: dict = Depends(require_manager)) -> dict[str, bool]:
    normalized = guest_id.strip()
    with connect() as db:
        guest = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE AND role = 'guest'",
            (normalized,),
        ).fetchone()
        if not guest:
            raise HTTPException(404, "访客不存在。")
        db.execute(
            """INSERT OR IGNORE INTO banned_ids(guest_id, livekit_identity, created_by, created_at)
               VALUES(?,?,?,?)""",
            (normalized, guest_identity(guest["id"]), user["id"], stamp()),
        )
        db.execute("DELETE FROM users WHERE id = ?", (guest["id"],))
    try:
        await remove_guest_from_rooms(guest["id"])
    except Exception:
        logger.exception("Guest banned; voice disconnection will be retried")
        record_system_event("system_error", "访客已拉黑，语音断开将在后台重试", normalized, 500)
        return {"ok": True, "pending_disconnect": True}
    return {"ok": True}


@app.delete("/api/guests/{guest_id}/ban")
def unban_guest(guest_id: str, user: dict = Depends(require_manager)) -> dict[str, bool]:
    with connect() as db:
        db.execute("DELETE FROM banned_ids WHERE guest_id = ? COLLATE NOCASE", (guest_id.strip(),))
    return {"ok": True}


@app.get("/api/bans")
def list_bans(user: dict = Depends(require_manager)) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT guest_id, created_at FROM banned_ids ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]
