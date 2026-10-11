"""Human-readable audit events; never include passwords or session tokens."""
import re

ACTION_LABELS = {
    "guest_kick": "踢出访客", "guest_ban": "拉黑访客", "guest_unban": "解除拉黑",
    "account_update": "修改账号", "password_change": "修改密码", "password_reset": "重置管理员密码",
    "admin_register": "管理员注册", "guest_invite": "生成访客邀请", "admin_invite": "生成管理员邀请",
    "invite_revoke": "撤销邀请", "room_create": "创建房间", "room_update": "修改房间",
    "room_delete": "删除房间", "participant_kick": "踢出房间", "participant_mute": "禁言成员",
    "login": "登录", "logout": "退出登录", "room_join": "加入房间", "room_leave": "退出房间",
    "read": "查询数据", "daily_reset": "每日访客清理", "system_error": "服务异常", "other": "其他操作",
    "mic_test_start": "开始服务器试麦", "mic_test_stop": "结束服务器试麦",
    "problem_recording_start": "开启问题记录", "problem_recording_stop": "停止问题记录",
    "problem_recording_report": "上报通话诊断",
}
CATEGORIES = {"user": "用户管理", "account": "账号变更", "invite": "邀请管理", "room": "房间管理",
              "auth": "登录访问", "system": "系统事件", "read": "数据查询", "other": "其他操作"}


def classify(method: str, path: str, status: int = 200, password: bool = False) -> dict:
    action, category, important = "other", "other", method != "GET"
    if method == "GET":
        action, category, important = "read", "read", False
    elif "/auth/" in path:
        category, important = "auth", False
        action = "logout" if path.endswith("/logout") else "login"
        if path.endswith("/admin/register"):
            action, category, important = "admin_register", "account", True
        elif path.endswith("/password-reset"):
            action, category, important = "password_reset", "account", True
    elif path.startswith("/api/guests/"):
        category = "user"
        action = "guest_kick" if path.endswith("/kick") else ("guest_unban" if method == "DELETE" else "guest_ban")
    elif path.startswith("/api/accounts/") or path == "/api/account/profile":
        action = ("password_reset" if path.startswith("/api/accounts/") else "password_change") if password else "account_update"
        category = "account"
    elif path.startswith("/api/admins/"):
        action, category = "password_reset", "account"
    elif path.startswith("/api/invites/"):
        category = "invite"
        action = "invite_revoke" if method == "DELETE" else ("admin_invite" if path.endswith("/admin") else "guest_invite")
    elif path.startswith("/api/rooms"):
        category = "room"
        if "/problem-recording" in path:
            action = "problem_recording_report" if path.endswith("/report") else "problem_recording_stop" if method == "DELETE" else "problem_recording_start"
            important = action != "problem_recording_report"
        elif path.endswith("/token"):
            action, important = "room_join", False
        elif path.endswith("/leave"):
            action, important = "room_leave", False
        elif path.endswith("/kick"):
            action = "participant_kick"
        elif path.endswith("/mute"):
            action = "participant_mute"
        elif method == "DELETE":
            action = "room_delete"
        elif method == "PATCH":
            action = "room_update"
        else:
            action = "room_create"
    if status >= 400:
        important = True
    return {"action": action, "category": category, "important": int(important), "label": ACTION_LABELS[action]}


def fallback_target(path: str) -> str:
    match = re.match(r"/api/(?:guests|accounts|admins|rooms|invites)/([^/]+)", path)
    return match[1] if match and match[1] not in {"guest", "admin"} else ""


def summary(event: dict, target: str, status: int) -> str:
    text = event["label"] + (f"：{target}" if target else "")
    if status >= 400 and event["action"] == "login":
        text += "（账号、密码或邀请无效）"
    elif status in (401, 403):
        text += "（登录失效或无权限）"
    return text
