"""Short-lived diagnostic sessions. Audio is never uploaded to the API."""
from __future__ import annotations

import json
import math
import secrets
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from livekit import api as livekit_api

WINDOW = 180
LEASE = 30
MIC_FIELDS = {
    "device", "speaker", "muted", "sampleRate", "channelCount", "noiseSuppression",
    "echoCancellation", "autoGainControl", "microphoneVolume", "listeningVolume",
    "listeningMuted", "contextSampleRate", "thresholdEnabled", "thresholdDb",
    "nativeDenoiseEnabled", "rnnoiseEnabled", "rawMicrophone", "echoCancellationEnabled",
    "autoGainEnabled", "inputGainEnabled", "microphoneProcessorEnabled",
}
ROW_FIELDS = {
    "member", "direction", "rttMs", "jitterMs", "lossPercent", "bitrateKbps", "concealPercent",
    "silentConcealPercent", "accelerationPercent", "decelerationPercent", "bufferDelayMs", "packetsDiscarded",
}


def register_problem_recording(app, require_user, require_manager, livekit_client, record_system_event):
    sessions: dict[str, dict] = {}

    def prune():
        now = time.monotonic()
        for room, session in list(sessions.items()):
            if session["lease"] < now:
                del sessions[room]
                record_system_event("problem_recording_stop", f"{session['owner_name']} 的问题记录因心跳中断自动结束", room)
        return now

    def session_for(room_id: str, session_id: str | None = None):
        prune()
        session = sessions.get(room_id)
        if session_id and (not session or session["id"] != session_id):
            raise HTTPException(404, "问题记录已停止，请重新开启。")
        return session

    async def members(room_id: str):
        lk = await livekit_client()
        try:
            result = await lk.room.list_participants(livekit_api.ListParticipantsRequest(room=room_id))
            return {p.identity: p.name for p in result.participants}
        except Exception as exc:
            raise HTTPException(503, "无法确认房间成员，请稍后重试。") from exc
        finally:
            await lk.aclose()

    async def require_member(room_id: str, user: dict):
        identity = f"{'guest' if user['role'] == 'guest' else 'user'}_{user['id']}"
        present = await members(room_id)
        if identity not in present:
            raise HTTPException(403, "请先加入该语音房间。")
        return identity, present

    def status(session):
        return {"id": session["id"], "owner_id": session["owner_id"], "owner_name": session["owner_name"],
                "started_at": session["started_at"], "window_seconds": WINDOW} if session else None

    @app.get("/api/rooms/{room_id}/problem-recording")
    async def recording_status(room_id: str, user: dict = Depends(require_user)):
        _, present = await require_member(room_id, user)
        session = session_for(room_id)
        if session and f"user_{session['owner_id']}" not in present:
            del sessions[room_id]
            return None
        return status(session)

    @app.post("/api/rooms/{room_id}/problem-recording")
    async def start_recording(room_id: str, user: dict = Depends(require_manager)):
        await require_member(room_id, user)
        if session_for(room_id):
            raise HTTPException(409, "该房间已有管理员开启问题记录。")
        if len(sessions) >= 20:
            raise HTTPException(429, "问题记录会话数量已达上限。")
        session = {"id": secrets.token_hex(16), "owner_id": user["id"], "owner_name": user["username"],
                   "started_at": datetime.now(timezone.utc).isoformat(), "lease": time.monotonic() + LEASE,
                   "reports": {}, "last_report": {}}
        sessions[room_id] = session
        return status(session)

    @app.delete("/api/rooms/{room_id}/problem-recording/{session_id}")
    async def stop_recording(room_id: str, session_id: str, user: dict = Depends(require_manager)):
        session = session_for(room_id, session_id)
        if session["owner_id"] != user["id"] and user["role"] != "superadmin":
            raise HTTPException(403, "仅开启者或超级管理员可以停止记录。")
        del sessions[room_id]
        return {"ok": True}

    @app.post("/api/rooms/{room_id}/problem-recording/{session_id}/report")
    async def report(room_id: str, session_id: str, request: Request, user: dict = Depends(require_user)):
        session = session_for(room_id, session_id)
        identity, _ = await require_member(room_id, user)
        # Awaiting LiveKit can let a stopped session expire. Revalidate before writing.
        if session_for(room_id, session_id) is not session:
            raise HTTPException(404, "问题记录已停止。")
        received = time.monotonic()
        if received - session["last_report"].get(identity, 0) < 2:
            raise HTTPException(429, "诊断上报过于频繁。")
        body = await request.body()
        if len(body) > 64000:
            raise HTTPException(413, "诊断数据过大。")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "诊断数据格式错误。")
        snapshot = payload.get("snapshot") or {}
        if not isinstance(snapshot, dict):
            raise HTTPException(400, "诊断数据格式错误。")
        def safe_fields(value, fields):
            if not isinstance(value, dict):
                return {}
            return {key: (item[:250] if isinstance(item, str) else item) for key, item in value.items()
                    if key in fields and isinstance(item, (str, bool, int, float, type(None)))
                    and (not isinstance(item, float) or math.isfinite(item))}
        clean = {"time": str(snapshot.get("time", ""))[:64], "state": str(snapshot.get("state", ""))[:40],
                 "context": str(snapshot.get("context", ""))[:40],
                 "microphone": safe_fields(snapshot.get("microphone"), MIC_FIELDS),
                 "rows": [safe_fields(row, ROW_FIELDS) for row in snapshot.get("rows", [])[:32]]
                 if isinstance(snapshot.get("rows"), list) else []}
        raw_events = payload.get("events")
        events = [{"time": str(event.get("time", ""))[:64], "action": str(event.get("action", ""))[:250]}
                  for event in raw_events[-30:] if isinstance(event, dict)] if isinstance(raw_events, list) else []
        entries = session["reports"].setdefault(identity, deque(maxlen=100))
        entries.append({"received_at": datetime.now(timezone.utc).isoformat(), "username": user["username"],
                        "snapshot": clean, "events": events, "build": str(payload.get("build", ""))[:80],
                        "browser": str(payload.get("browser", ""))[:250], "_age": received})
        while entries and entries[0]["_age"] < received - WINDOW - 10:
            entries.popleft()
        session["last_report"][identity] = received
        if session["owner_id"] == user["id"]:
            session["lease"] = received + LEASE
        # Keep all departed-member queues bounded too.
        for member, values in list(session["reports"].items()):
            while values and values[0]["_age"] < received - WINDOW - 10:
                values.popleft()
            if not values:
                del session["reports"][member]
        return {"ok": True}

    @app.get("/api/rooms/{room_id}/problem-recording/{session_id}/export")
    async def export(room_id: str, session_id: str, user: dict = Depends(require_manager)):
        session = session_for(room_id, session_id)
        await require_member(room_id, user)
        if session["owner_id"] != user["id"]:
            raise HTTPException(403, "仅开启记录的管理员可以导出。")
        now = time.monotonic()
        return {"session": status(session), "reports": {
            identity: [{key: value for key, value in entry.items() if key != "_age"}
                       for entry in entries if entry["_age"] >= now - WINDOW]
            for identity, entries in session["reports"].items()
        }}

    return prune
