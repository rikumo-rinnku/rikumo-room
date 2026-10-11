// Also translate responses from older servers during a rolling update.
const errorTranslations: Record<string, string> = {
  "Please sign in": "请先登录。",
  "Session expired": "登录状态已失效，请重新登录。",
  "Administrator access required": "此操作需要管理员权限。",
  "This guest session is ending": "访客资格已到期，请使用新的邀请链接重新加入。",
  "Super administrator access required": "此操作需要超级管理员权限。",
  "A current guest session is required": "访客资格已失效，请使用有效邀请链接重新加入。",
  "This invite is invalid, expired, or revoked": "邀请链接无效、已过期或已撤销。",
  "This guest ID is blocked": "该访客 ID 已被拉黑。",
  "This ID is unavailable": "该 ID 不可用，请更换。",
  "This invite has no registrations remaining": "该邀请的可注册人数已用完。",
  "This guest ID is already in use": "该访客 ID 已被使用，请更换。",
  "A current guest invite is required": "请通过有效的访客邀请链接加入。",
  "Guest ID is unavailable; use a current invite to join": "访客 ID 已失效，请使用有效邀请链接重新加入。",
  "Username or password is incorrect": "用户名或密码错误。",
  "This registration code is invalid or expired": "管理员邀请码无效、已过期、已撤销或注册名额已用完。",
  "This username is already in use": "该用户名已被使用，请更换。",
  "Username cannot be empty": "用户名不能为空。",
  "Guest IDs can contain at most 24 characters": "访客 ID 最多 24 个字符。",
  "Guest accounts do not use passwords": "访客账号无需设置密码。",
  "Current password is incorrect": "当前密码错误。",
  "No account changes supplied": "请填写需要修改的账号信息。",
  "Account not found": "账号不存在。",
  "Invalid username": "用户名格式不正确。",
  "No changes supplied": "请填写需要修改的内容。",
  "Room not found": "房间不存在或已删除。",
  "This guest may finish the current call but cannot join another room": "访客资格已到期，可以继续当前通话；加入其他房间需使用新的邀请链接。",
  "Could not create a unique invite code": "邀请码生成失败，请稍后重试。",
  "Invite not found": "邀请不存在。",
  "Invite expiration must be in the future": "过期时间必须晚于当前时间。",
  "Administrator not found": "管理员账号不存在。",
  "Reset code is invalid or expired": "密码重置码无效或已过期。",
  "Guest not found": "访客不存在。",
  "A room with this name already exists": "该房间名称已存在，请换一个名称。",
}

export function mediaErrorMessage(error: unknown): string {
  const name = error instanceof Error ? error.name : ''
  const message = error instanceof Error ? error.message : ''
  if (errorTranslations[message]) return errorTranslations[message]!
  if (name === 'NotAllowedError' || name === 'SecurityError') return '无法使用音频设备，请检查浏览器权限和系统麦克风权限。'
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return '未找到所选音频设备，请重新选择设备。'
  if (name === 'NotReadableError' || name === 'AbortError') return '无法打开音频设备，请检查设备连接或是否被其他程序占用。'
  if (name === 'ConnectionError') return '无法连接语音服务，请检查网络后重试。'
  if (/getusermedia.*permission|permission denied/i.test(message)) return '麦克风权限被拒绝，请在浏览器设置中允许使用麦克风。'
  if (message.includes('does not support')) return '当前浏览器不支持切换扬声器，请使用系统默认输出。'
  if (message.includes('engine not connected within timeout')) return '麦克风发布超时，请重新加入房间。'
  if (name === 'TypeError' && /fetch|network/i.test(message)) return '网络暂不可用，请检查连接后重试。'
  if (/no published audio track/i.test(message)) return '该用户尚未开启麦克风。'
  if (/structuredClone|could not be cloned/i.test(message)) return '音频状态处理失败，请重新加入房间。'
  if (/timeout|timed out/i.test(message)) return '操作超时，请检查网络后重试。'
  if (/[\u4e00-\u9fff]/.test(message)) return message
  return '操作失败，请稍后重试。'
}
