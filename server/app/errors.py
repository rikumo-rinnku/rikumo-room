"""User-facing validation messages; never include submitted values."""

FIELD_LABELS = {
    "username": "用户名", "guest_id": "访客 ID", "invite_code": "邀请码",
    "password": "密码", "current_password": "当前密码", "new_password": "新密码",
    "reset_code": "密码重置码", "max_uses": "可注册人数", "expires_at": "过期时间",
    "name": "房间名称", "description": "房间简介", "start": "开始时间",
    "end": "结束时间", "limit": "每页条数", "offset": "分页位置",
}


def validation_message(errors: list[dict]) -> str:
    if not errors:
        return "填写的信息格式不正确，请检查后重试。"
    error = errors[0]
    location = error.get("loc", ())
    field = FIELD_LABELS.get(str(location[-1]) if location else "", "请求参数")
    kind = error.get("type", "")
    context = error.get("ctx", {})
    if kind == "missing":
        return f"请填写{field}。"
    if kind == "string_too_short":
        return f"{field}至少需要 {context.get('min_length')} 个字符。"
    if kind == "string_too_long":
        return f"{field}最多允许 {context.get('max_length')} 个字符。"
    if kind == "greater_than_equal":
        return f"{field}不能小于 {context.get('ge')}。"
    if kind == "less_than_equal":
        return f"{field}不能大于 {context.get('le')}。"
    if kind in {"int_parsing", "int_type", "int_from_float"}:
        return f"{field}必须是整数。"
    if kind.startswith("datetime") or kind.startswith("date_"):
        return f"{field}格式不正确，请重新选择日期和时间。"
    if kind == "json_invalid":
        return "提交的数据格式不正确，请刷新页面后重试。"
    if kind == "string_pattern_mismatch":
        return f"{field}包含不允许使用的字符。"
    return f"{field}格式不正确，请检查后重试。"
