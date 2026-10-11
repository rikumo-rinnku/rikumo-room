<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import type { Participant, RemoteTrack, RemoteAudioTrack, LocalAudioTrack, Room as LiveKitRoom } from 'livekit-client'
import VoiceControls from './VoiceControls.vue'
import { ProblemRecording, zipProblemFiles, recordingPlayer, type RecordingState, type RecordingSource } from './problemRecording'
import CallDiagnosticsPanel from './CallDiagnostics.vue'
import { CallDiagnostics, type DiagnosticSnapshot } from './diagnostics'
import { MicrophoneGainProcessor, playAudioCue } from './audio'
import { mediaErrorMessage } from './mediaErrors'
import {
  AudioLines,
  Ban,
  Check,
  CircleHelp,
  Copy,
  DoorOpen,
  Headphones,
  LogOut,
  MicOff,
  Plus,
  Settings2,
  Shield,
  Settings,
  Users,
  Volume2,
  X,
} from 'lucide-vue-next'
import { api, post, type InviteInfo, type ParticipantInfo, type RoomInfo, type User } from './api'

const user = ref<User | null>(null)
const rooms = ref<RoomInfo[]>([])
const activeRoom = ref<RoomInfo | null>(null)
// Keep SDK instances and their nested media options out of Vue's deep proxies.
// LiveKit clones capture options with structuredClone, which rejects proxies.
const liveRoom = shallowRef<LiveKitRoom | null>(null)
const activeSpeakers = ref(new Set<string>())
const localMuted = ref(true)
const microphoneBusy = ref(false)
const deviceSwitchBusy = ref(false)
const connectionReady = ref(false)
const callNotice = ref('')
const retryRoom = ref<RoomInfo | null>(null)
const playbackBlocked = ref(false)
const speakerSelectionSupported = typeof HTMLMediaElement !== 'undefined' && 'setSinkId' in HTMLMediaElement.prototype
  && typeof AudioContext !== 'undefined' && 'setSinkId' in AudioContext.prototype
let deviceChangeTimer: number | undefined
let deviceRefreshVersion = 0
let deviceChangePending = false
let callOperationVersion = 0
const microphoneVolume = ref(100)
const thresholdEnabled = ref(false)
const thresholdValue = ref(-45)
const thresholdBusy = ref(false)
const nativeDenoiseEnabled = ref(true)
const nativeDenoiseStatus = ref('')
const rnnoiseEnabled = ref(false)
const rawMicrophone = ref(false)
const echoCancellationEnabled = ref(true)
const autoGainEnabled = ref(true)
const inputGainEnabled = ref(true)
const needsMicrophoneProcessor = () => !rawMicrophone.value && (inputGainEnabled.value || rnnoiseEnabled.value || thresholdEnabled.value)
const capturePreferences = () => ({ echoCancellation: !rawMicrophone.value && echoCancellationEnabled.value,
  noiseSuppression: !rawMicrophone.value && nativeDenoiseEnabled.value, autoGainControl: !rawMicrophone.value && autoGainEnabled.value })
const denoiseBusy = ref(false)
const thresholdLevel = ref<number | null>(null)
const thresholdOpen = ref(false)
const audioSettingsVisible = ref(false)
const listeningVolume = ref(100)
const listeningMuted = ref(false)
const memberVolumes = ref<Record<string, number>>({})
const volumePanelIdentity = ref('')
const remoteAudioTracks = new Map<RemoteAudioTrack, string>()
let audioContext: AudioContext | undefined
let microphoneProcessor: MicrophoneGainProcessor | undefined
const connectionState = ref('')
const diagnosticsOpen = ref(false)
const diagnosticSnapshot = shallowRef<DiagnosticSnapshot | null>(null)
const diagnosticEventCount = ref(0)
const diagnostics = new CallDiagnostics(snapshot => { diagnosticSnapshot.value = snapshot; diagnosticEventCount.value = diagnostics.events.length })
function diagnosticEvent(action: string) { diagnostics.event(action); diagnosticEventCount.value = diagnostics.events.length }
interface RecordingSession { id: string; owner_id: number; owner_name: string; started_at: string; window_seconds: number }
const problemSession = ref<RecordingSession | null>(null)
const problemCache = ref<RecordingState>({ seconds: 0, bytes: 0, tracks: 0 })
const problemBusy = ref(false)
const problemSaving = ref(false)
const problemNotice = ref('')
const problemMember = ref('')
const problemKind = ref('电音')
const problemMarks = ref<Array<{ time: number; member: string; kind: string }>>([])
let problemRecorder: ProblemRecording | undefined
let problemPollTimer: number | undefined
let problemPolling = false
let problemFailures = 0
let problemGeneration = 0

function syncProblemSources() {
  const connection = liveRoom.value
  if (!problemRecorder || !connection) return
  const sources: RecordingSource[] = []
  connection.localParticipant.audioTrackPublications.forEach(publication => {
    if (publication.track) sources.push({ identity: connection.localParticipant.identity, name: user.value?.username || '自己',
      track: publication.track.mediaStreamTrack, muted: publication.isMuted, local: true })
  })
  connection.remoteParticipants.forEach(participant => participant.audioTrackPublications.forEach(publication => {
    if (publication.track) sources.push({ identity: participant.identity, name: participant.name || participant.identity,
      track: publication.track.mediaStreamTrack, muted: publication.isMuted, local: false })
  }))
  problemRecorder.sync(sources)
}

async function startProblemRecording() {
  const room = activeRoom.value
  if (!room || !liveRoom.value || !connectionReady.value || !isManager.value || problemBusy.value || problemSaving.value) return
  problemBusy.value = true
  ++problemGeneration
  problemFailures = 0
  problemNotice.value = ''
  let session: RecordingSession | undefined
  try {
    if (typeof MediaRecorder === 'undefined') throw new Error('此浏览器不支持问题录音，请使用桌面 Chrome。')
    session = await api<RecordingSession>(`/rooms/${room.id}/problem-recording`, post())
    if (activeRoom.value?.id !== room.id || !liveRoom.value) {
      await api(`/rooms/${room.id}/problem-recording/${session.id}`, { method: 'DELETE' }).catch(() => undefined)
      return
    }
    problemSession.value = session
    problemMarks.value = []
    problemRecorder = new ProblemRecording(state => { problemCache.value = state }, message => {
      problemNotice.value = message
      void stopProblemRecording()
    })
    syncProblemSources()
    diagnosticEvent('管理员开启三分钟语音循环缓存')
    void pollProblemRecording()
  } catch (error) {
    problemRecorder?.stop()
    problemRecorder = undefined
    if (session) await api(`/rooms/${room.id}/problem-recording/${session.id}`, { method: 'DELETE' }).catch(() => undefined)
    problemSession.value = null
    reportError(error)
  } finally { problemBusy.value = false }
}

async function stopProblemRecording() {
  ++problemGeneration
  const room = activeRoom.value
  const session = problemSession.value
  const owned = session?.owner_id === user.value?.id
  problemRecorder?.stop()
  problemRecorder = undefined
  problemCache.value = { seconds: 0, bytes: 0, tracks: 0 }
  problemMarks.value = []
  problemSession.value = null
  if (room && session && owned) {
    await api(`/rooms/${room.id}/problem-recording/${session.id}`, { method: 'DELETE', signal: AbortSignal.timeout(3000) }).catch(() => undefined)
    diagnosticEvent('管理员停止语音循环缓存并清空本机缓存')
  }
}

async function pollProblemRecording() {
  const room = activeRoom.value
  const connection = liveRoom.value
  if (!room || !connection || !connectionReady.value || problemPolling || problemBusy.value) return
  problemPolling = true
  const generation = problemGeneration
  try {
    const session = await api<RecordingSession | null>(`/rooms/${room.id}/problem-recording`, { signal: AbortSignal.timeout(4000) })
    if (liveRoom.value !== connection || activeRoom.value?.id !== room.id || generation !== problemGeneration) return
    if (problemRecorder && (!session || session.id !== problemSession.value?.id || session.owner_id !== user.value?.id)) {
      problemRecorder.stop(); problemRecorder = undefined
      problemCache.value = { seconds: 0, bytes: 0, tracks: 0 }
      problemNotice.value = '问题缓存会话已结束，本机缓存已清空。'
    }
    problemSession.value = session
    if (session) {
      syncProblemSources()
      // Upload only diagnostic metadata. Each participant submits only their own report.
      if (session.owner_id !== user.value?.id || problemRecorder) {
        const cutoff = Date.now() - 35000
        await api(`/rooms/${room.id}/problem-recording/${session.id}/report`, { ...post({
          snapshot: diagnosticSnapshot.value,
          events: diagnostics.events.filter(event => Date.parse(event.time) >= cutoff),
          build: 'problem-recording-v1-2026-10-07', browser: navigator.userAgent,
        }), signal: AbortSignal.timeout(4000) })
      }
    }
    problemFailures = 0
  } catch {
    if (liveRoom.value === connection && generation === problemGeneration) {
      problemFailures++
      if (problemRecorder && problemFailures >= 3) {
        problemNotice.value = '问题记录连接不可用，已停止缓存。正常通话继续。'
        await stopProblemRecording()
      }
    }
  } finally { problemPolling = false }
}

function markProblemRecording() {
  const member = people.value.find(person => person.identity === problemMember.value)?.name || problemMember.value || '未指定成员'
  problemMarks.value.push({ time: Date.now(), member, kind: problemKind.value })
  problemMarks.value = problemMarks.value.filter(mark => mark.time >= Date.now() - 180000).slice(-100)
  diagnosticEvent(`语音异常标记：${member} · ${problemKind.value}`)
  problemNotice.value = '已标记异常。'
}

async function saveProblemRecording() {
  const room = activeRoom.value
  const session = problemSession.value
  const recorder = problemRecorder
  if (!room || !session || !recorder || session.owner_id !== user.value?.id || problemSaving.value || problemBusy.value) return
  problemSaving.value = true
  problemNotice.value = '正在保存最近三分钟…'
  const at = Date.now()
  const recordedBy = { identity: currentIdentity.value, name: user.value?.username }
  const marks = [...problemMarks.value]
  const local = { browser: navigator.userAgent,
    snapshots: diagnostics.snapshots.filter(snapshot => Date.parse(snapshot.time) >= at - 180000 && Date.parse(snapshot.time) <= at),
    events: diagnostics.events.filter(event => Date.parse(event.time) >= at - 180000 && Date.parse(event.time) <= at) }
  const members = people.value.map(person => ({ identity: person.identity, name: person.name || person.identity }))
  try {
    const [audio, remote] = await Promise.all([recorder.snapshot(at),
      api<Record<string, unknown>>(`/rooms/${room.id}/problem-recording/${session.id}/export`, { signal: AbortSignal.timeout(10000) })
        .catch(() => ({ unavailable: true, note: '全员诊断汇总获取失败；包内保留记录者本机诊断。' }))])
    const manifest = { format: 1, build: 'problem-recording-v1-2026-10-07', room: { id: room.id, name: room.name },
      recorder: recordedBy, start: audio.start, end: audio.end,
      members, clips: audio.manifest, notes: audio.notes,
      marks: marks.filter(mark => mark.time >= audio.start && mark.time <= audio.end),
      note: '管理员接收轨道按成员分段录制，自己的录音为实际发送轨道；不包含个人收听音量调整。时间按本机时钟估算，片段切换可能存在细小间隙。静音、未发布或未订阅期间没有声音；未上报诊断的成员可能使用旧版本或已断开。' }
    const json = (value: unknown) => new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' })
    audio.files.push({ name: 'manifest.json', data: json(manifest) }, { name: 'diagnostics-recorder.json', data: json(local) },
      { name: 'diagnostics-members.json', data: json(remote) },
      { name: '回听录音.html', data: new Blob([recordingPlayer(audio.manifest)], { type: 'text/html;charset=utf-8' }) })
    const blob = await zipProblemFiles(audio.files)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url; link.download = `语音问题记录-${new Date(at).toISOString().replace(/[:.]/g, '-')}.zip`; link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 60000)
    problemNotice.value = '已下载。解压后打开“回听录音.html”查看各成员录音。'
    diagnosticEvent('保存最近三分钟语音问题记录')
  } catch (error) {
    problemNotice.value = error instanceof Error ? error.message : '保存问题记录失败。'
  } finally { problemSaving.value = false }
}

const errorMessage = ref('')
const busy = ref(false)
function inviteFromLocation() {
  const queryInvite = new URLSearchParams(location.search).get('invite')
  if (queryInvite) return queryInvite
  const pathInvite = decodeURIComponent(location.pathname.replace(/^\/+|\/+$/g, ''))
  return pathInvite && !pathInvite.includes('/') ? pathInvite : ''
}
function toLocalDateTimeInput(value: Date) {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

const inviteCode = ref(inviteFromLocation())
const adminInviteFromLocation = new URLSearchParams(location.search).get('admin_invite') ?? ''
const authMode = ref<'guest' | 'admin'>(inviteCode.value ? 'guest' : 'admin')
const guestId = ref('')
const adminUsername = ref('')
const adminPassword = ref('')
const isRegisteringAdmin = ref(Boolean(adminInviteFromLocation))
const adminInviteCode = ref(adminInviteFromLocation)
const latestAdminInviteCode = ref('')
const adminInviteLimit = ref(1)
const adminInviteExpiry = ref(toLocalDateTimeInput(new Date(Date.now() + 24 * 60 * 60 * 1000)))
const adminInviteMinExpiry = computed(() => toLocalDateTimeInput(new Date()))
const isResettingAdmin = ref(false)
const passwordResetCode = ref('')
const showCreateRoom = ref(false)
const showInvite = ref(false)
const newRoomName = ref('')
const creatingRoom = ref(false)
const newRoomDescription = ref('')
const inviteLimit = ref(5)
const latestInvite = ref('')
const creatingGuestInvite = ref(false)
const creatingAdminInvite = ref(false)
const inviteRows = ref<InviteInfo[]>([])
const adminInviteRows = ref<InviteInfo[]>([])
const bannedIds = ref<Array<{ guest_id: string; created_at: string }>>([])
const people = ref<ParticipantInfo[]>([])
const micDevices = ref<MediaDeviceInfo[]>([])
const speakerDevices = ref<MediaDeviceInfo[]>([])
const micDevice = ref('')
const speakerDevice = ref('')
const mobileMenuOpen = ref(false)
type SettingsSection = 'profile' | 'accounts' | 'invites' | 'logs'
const settingsSection = ref<SettingsSection>('profile')
const settingsOpen = ref(false)
const profileUsername = ref('')
const profileCurrentPassword = ref('')
const profileNewPassword = ref('')
const guestRows = ref<Array<{ id: number; username: string; created_at: string; pending_delete: number; room_names: string[]; room_state_known: boolean }>>([])
const accountRows = ref<Array<{ id: number; username: string; role: User['role']; created_at: string; pending_delete: number; has_password: number; room_names: string[]; room_state_known: boolean }>>([])
const accountNameDrafts = ref<Record<number, string>>({})
const accountPasswordDrafts = ref<Record<number, string>>({})
interface AuditRow { id: number; username: string; role: string; method: string; path: string; status_code: number; created_at: string; summary: string; target: string; action: string }
const auditRows = ref<AuditRow[]>([])
const auditActions = ref<Record<string, string>>({})
const auditFilters = ref({ include_all: false, action: '', actor: '', target: '', result: '', q: '', start: '', end: '' })
const auditPage = ref(1)
const auditTotal = ref(0)
const auditLoading = ref(false)
const auditPageCount = computed(() => Math.max(1, Math.ceil(auditTotal.value / 30)))
const moderationBusy = ref<number | null>(null)
const settingsNotice = ref('')
let refreshTimer: number | undefined
let roomRefreshVersion = 0
const departingParticipants = new Map<string, string>()

const isManager = computed(() => user.value?.role === 'admin' || user.value?.role === 'superadmin')
const isSuperadmin = computed(() => user.value?.role === 'superadmin')
const currentIdentity = ref('')

function reportError(error: unknown) {
  errorMessage.value = mediaErrorMessage(error)
  window.setTimeout(() => {
    if (errorMessage.value) errorMessage.value = ''
  }, 5000)
}

function handleAuthExpired() {
  if (!user.value) return
  user.value = null
  settingsOpen.value = false
  retryRoom.value = null
  void disconnectRoom().catch(() => undefined)
  reportError(new Error('登录状态已失效，请重新登录。'))
}

function guestInviteUrl(code: string | null | undefined) {
  return code ? `${location.origin}/${encodeURIComponent(code)}` : ''
}

function adminInviteUrl(code: string | null | undefined) {
  return code ? `${location.origin}/?admin_invite=${encodeURIComponent(code)}` : ''
}

function roleLabel(role: string) {
  return ({ superadmin: '超级管理员', admin: '管理员', guest: '访客', system: '系统', unknown: '未登录' } as Record<string, string>)[role] ?? '未知角色'
}

function guestRoomLabel(account: { room_names: string[]; room_state_known: boolean }) {
  const names = account.room_names ?? []
  return account.room_state_known ? (names.length ? `房间：${names.join('、')}` : '未在语音房间') : '房间状态暂不可用'
}

async function refreshUser() {
  try {
    user.value = await api<User>('/auth/me')
    await refreshRooms()
  } catch { /* A network outage must not discard a valid signed-in account. */ }
}

async function refreshRooms() {
  const version = ++roomRefreshVersion
  const latestRooms = await api<RoomInfo[]>('/rooms')
  if (version !== roomRefreshVersion) return
  rooms.value = latestRooms.map((room) => {
    const identity = departingParticipants.get(room.id)
    if (!identity) return room
    if (!room.participants?.some((participant) => participant.identity === identity)) {
      departingParticipants.delete(room.id)
      return room
    }
    // LiveKit can briefly return a departing participant before processing the leave signal.
    const participants = room.participants.filter((participant) => participant.identity !== identity)
    return { ...room, participants, participant_count: participants.length }
  })
  if (activeRoom.value) {
    activeRoom.value = rooms.value.find((room) => room.id === activeRoom.value?.id) ?? activeRoom.value
  }
}

async function loadDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return
  const version = ++deviceRefreshVersion
  const devices = await navigator.mediaDevices.enumerateDevices()
  if (version !== deviceRefreshVersion) return
  micDevices.value = devices.filter((device) => device.kind === 'audioinput')
  speakerDevices.value = devices.filter((device) => device.kind === 'audiooutput')
}

async function handleDeviceChange() {
  if (deviceSwitchBusy.value || microphoneBusy.value || busy.value || denoiseBusy.value || thresholdBusy.value) {
    deviceChangePending = true
    return
  }
  deviceChangePending = false
  try {
    await loadDevices()
    const connection = liveRoom.value
    const missingMic = micDevice.value && !micDevices.value.some((device) => device.deviceId === micDevice.value)
    if (missingMic || !micDevices.value.length) {
      micDevice.value = ''
      localMuted.value = true
      if (connection) {
        await connection.localParticipant.setMicrophoneEnabled(false)
        if (liveRoom.value !== connection) return
        callNotice.value = micDevices.value.length ? '麦克风已断开，请选择设备后重新开麦。' : '未检测到麦克风，接入设备后可重新开麦。'
      }
    }
    const missingSpeaker = speakerDevice.value && !speakerDevices.value.some((device) => device.deviceId === speakerDevice.value)
    if (missingSpeaker) {
      const changed = await switchDevice('audiooutput', '')
      if (connection && liveRoom.value === connection) callNotice.value = changed
        ? '原扬声器已断开，已切换到系统默认输出。' : '原扬声器已断开，请重新选择输出设备。'
    }
  } catch (error) { reportError(error) }
}

function scheduleDeviceRefresh() {
  if (deviceChangeTimer) window.clearTimeout(deviceChangeTimer)
  deviceChangeTimer = window.setTimeout(() => { void handleDeviceChange() }, 250)
}

function drainDeviceChanges() {
  if (deviceChangePending) scheduleDeviceRefresh()
}

function handleOffline() {
  if (!liveRoom.value) return
  connectionReady.value = false
  connectionState.value = '网络已断开'
  callNotice.value = '网络已断开，正在等待恢复。'
}

function handleOnline() {
  void refreshUser()
  if (liveRoom.value) {
    connectionReady.value = liveRoom.value.state === 'connected'
    connectionState.value = connectionReady.value ? '已连接' : '网络恢复中'
    callNotice.value = connectionReady.value ? '网络已恢复。' : '网络已恢复，正在恢复语音连接。'
  }
}

async function resumeAudioPlayback() {
  const connection = liveRoom.value
  if (!connection) return
  try {
    await getAudioContext().resume()
    await connection.startAudio()
    if (liveRoom.value !== connection) return
    playbackBlocked.value = !connection.canPlaybackAudio
    if (playbackBlocked.value) callNotice.value = '浏览器未允许播放声音，请再次点击恢复收听。'
    applyListeningVolume()
  } catch (error) { reportError(error) }
}

async function guestAccess() {
  if (!inviteCode.value.trim()) throw new Error('请先打开管理员发给你的邀请链接')
  user.value = await api<User>('/auth/guest/access', post({
    invite_code: inviteCode.value.trim(),
    guest_id: guestId.value.trim(),
  }))
  await refreshRooms()
}

async function adminAuth() {
  if (isResettingAdmin.value) {
    await api('/auth/admin/password-reset', post({
      reset_code: passwordResetCode.value.trim(),
      new_password: adminPassword.value,
    }))
    isResettingAdmin.value = false
    passwordResetCode.value = ''
    adminPassword.value = ''
    return
  }
  if (isRegisteringAdmin.value) {
    user.value = await api<User>('/auth/admin/register', post({
      invite_code: adminInviteCode.value.trim(),
      username: adminUsername.value.trim(),
      password: adminPassword.value,
    }))
  } else {
    user.value = await api<User>('/auth/admin/login', post({
      username: adminUsername.value.trim(),
      password: adminPassword.value,
    }))
  }
  await refreshRooms()
}

async function submitAuth() {
  busy.value = true
  errorMessage.value = ''
  try {
    if (authMode.value === 'guest') {
      await guestAccess()
    } else {
      await adminAuth()
    }
  } catch (error) {
    reportError(error)
  } finally {
    busy.value = false
  }
}

function getAudioContext() {
  if (!audioContext || audioContext.state === 'closed') {
    try { audioContext = new AudioContext({ sampleRate: 48000 }) }
    catch { audioContext = new AudioContext() }
  }
  return audioContext
}

function playControlCue(enabled: boolean, listening = false) {
  void playAudioCue(getAudioContext(), enabled, listening).catch(() => undefined)
}

function applyListeningVolume() {
  for (const [track, identity] of remoteAudioTracks) {
    track.setVolume(listeningMuted.value ? 0 : listeningVolume.value / 100 * (memberVolumes.value[identity] ?? 100) / 100)
  }
}

function toggleListening() {
  listeningMuted.value = !listeningMuted.value
  applyListeningVolume()
  playControlCue(!listeningMuted.value, true)
}

function setMicrophoneVolume(volume: number) {
  microphoneVolume.value = Math.max(0, Math.min(200, volume))
  microphoneProcessor?.setVolume(microphoneVolume.value)
}

async function setThresholdEnabled(enabled: boolean) {
  if (rawMicrophone.value) return
  const previous = thresholdEnabled.value
  await replaceMicrophoneSettings(() => { thresholdEnabled.value = enabled }, () => { thresholdEnabled.value = previous }, '收音阈值')
}

function setThresholdValue(value: number) {
  if (!Number.isFinite(value)) return
  thresholdValue.value = Math.round(Math.max(-70, Math.min(-10, value)))
  microphoneProcessor?.setThreshold(thresholdValue.value)
}

function setAudioSettingsVisible(visible: boolean) {
  audioSettingsVisible.value = visible
  microphoneProcessor?.setMeterVisible(visible)
}

async function setRnnoiseEnabled(enabled: boolean) {
  if (rawMicrophone.value) return
  const previous = rnnoiseEnabled.value
  await replaceMicrophoneSettings(() => { rnnoiseEnabled.value = enabled }, () => { rnnoiseEnabled.value = previous }, 'RNNoise 降噪')
}

async function setCaptureOption(option: 'echo' | 'auto' | 'gain', enabled: boolean) {
  if (rawMicrophone.value) return
  const preference = option === 'echo' ? echoCancellationEnabled : option === 'auto' ? autoGainEnabled : inputGainEnabled
  const previous = preference.value
  const label = option === 'echo' ? '回声消除' : option === 'auto' ? '自动增益' : '输入音量处理'
  await replaceMicrophoneSettings(() => { preference.value = enabled }, () => { preference.value = previous }, label)
}

async function applyNativeDenoise(track: LocalAudioTrack) {
  await track.applyConstraints({ ...track.constraints, ...capturePreferences() })
  const actual = track.getSourceTrackSettings().noiseSuppression
  nativeDenoiseStatus.value = actual === undefined ? '浏览器未报告实际降噪状态' : actual === nativeDenoiseEnabled.value ? '' : '浏览器未应用设置，可重新加入房间再试'
  diagnosticEvent(`原生降噪：请求 ${nativeDenoiseEnabled.value}，实际 ${actual ?? '未知'}`)
}

async function setNativeDenoiseEnabled(enabled: boolean) {
  if (rawMicrophone.value) return
  const previous = nativeDenoiseEnabled.value
  await replaceMicrophoneSettings(() => { nativeDenoiseEnabled.value = enabled }, () => { nativeDenoiseEnabled.value = previous }, '原生降噪')
}

function setListeningVolume(volume: number) {
  listeningVolume.value = Math.max(0, Math.min(200, Number.isFinite(volume) ? volume : 100))
  applyListeningVolume()
}

function setMemberVolume(identity: string, event: Event) {
  const volume = Number((event.target as HTMLInputElement).value)
  memberVolumes.value[identity] = Math.max(0, Math.min(200, Number.isFinite(volume) ? volume : 100))
  applyListeningVolume()
}

async function setRawMicrophone(enabled: boolean) {
  const previous = rawMicrophone.value
  await replaceMicrophoneSettings(() => { rawMicrophone.value = enabled }, () => { rawMicrophone.value = previous }, '原始麦克风直通')
}

async function replaceMicrophoneSettings(apply: () => void, rollback: () => void, label: string) {
  const connection = liveRoom.value
  if (!connection || !connectionReady.value || busy.value || microphoneBusy.value || deviceSwitchBusy.value || thresholdBusy.value || denoiseBusy.value) return
  microphoneBusy.value = true
  const wasMuted = !connection.localParticipant.isMicrophoneEnabled
  let nextTrack: LocalAudioTrack | undefined
  try {
    const { Track } = await import('livekit-client')
    const oldTrack = connection.localParticipant.getTrackPublication(Track.Source.Microphone)?.track
    // Silence the old publication before replacing capture; never publish two microphones.
    if (oldTrack) {
      await connection.localParticipant.setMicrophoneEnabled(false)
      localMuted.value = true
      await connection.localParticipant.unpublishTrack(oldTrack, true)
    }
    microphoneProcessor = undefined
    thresholdLevel.value = null
    thresholdOpen.value = false
    if (liveRoom.value !== connection) return
    apply()
    if (oldTrack) {
      const { track, processor } = await captureMicrophone(connection)
      nextTrack = track
      await track.mute()
      if (liveRoom.value !== connection) { track.stop(); return }
      await connection.localParticipant.publishTrack(track, { source: Track.Source.Microphone })
      if (liveRoom.value !== connection) { track.stop(); return }
      microphoneProcessor = processor
      if (!wasMuted) await setMicrophoneEnabled(connection, true)
      localMuted.value = !connection.localParticipant.isMicrophoneEnabled
    }
    diagnosticEvent(`${label}设置已应用`)
    syncProblemSources()
  } catch (error) {
    // A failed replacement remains muted. A later unmute captures the previous mode.
    if (nextTrack) {
      await nextTrack.mute().catch(() => undefined)
      await connection.localParticipant.unpublishTrack(nextTrack, true).catch(() => undefined)
      nextTrack.stop()
    }
    if (liveRoom.value === connection) {
      rollback()
      microphoneProcessor = undefined
      localMuted.value = true
      reportError(error)
      callNotice.value = '收音模式切换失败，麦克风保持静音。请重新开麦。'
    }
  } finally {
    microphoneBusy.value = false
    drainDeviceChanges()
  }
}

async function setMicrophoneEnabled(connection: LiveKitRoom, enabled: boolean) {
  const { Track } = await import('livekit-client')
  const publication = connection.localParticipant.getTrackPublication(Track.Source.Microphone)
  if (publication || !enabled) {
    // Device selection already updates the track; muted tracks apply the pending
    // selection on unmute. Do not restart capture a second time here.
    await connection.localParticipant.setMicrophoneEnabled(enabled)
    if (enabled && !rawMicrophone.value && publication?.track && publication.track.getSourceTrackSettings().noiseSuppression !== nativeDenoiseEnabled.value) {
      await applyNativeDenoise(publication.track as LocalAudioTrack).catch(() => { nativeDenoiseStatus.value = '降噪设置未应用，请重新加入房间' })
    }
    diagnosticEvent(enabled ? '麦克风已开启' : '麦克风已关闭')
    return
  }
  const { track, processor } = await captureMicrophone(connection)
  try {
    if (liveRoom.value !== connection) { track.stop(); return }
    await connection.localParticipant.publishTrack(track, { source: Track.Source.Microphone })
    diagnosticEvent('麦克风发布成功')
    syncProblemSources()
    if (liveRoom.value === connection) microphoneProcessor = processor
  } catch (error) {
    diagnosticEvent('麦克风发布失败')
    track.stop()
    throw error
  }
}

async function captureMicrophone(connection: LiveKitRoom) {
  const { TrackEvent, createLocalAudioTrack } = await import('livekit-client')
  // One capture and processor are used for the normal room microphone.
  const context = getAudioContext()
  await context.resume()
  diagnosticEvent('开始采集麦克风')
  const track = await createLocalAudioTrack({
    deviceId: micDevice.value || 'default', ...capturePreferences(),
  })
  track.on(TrackEvent.Ended, () => {
    if (liveRoom.value !== connection) return
    diagnosticEvent('麦克风采集结束或设备断开')
    localMuted.value = true
    callNotice.value = '麦克风已断开，请选择可用设备后重新开麦。'
    void connection.localParticipant.setMicrophoneEnabled(false).catch(reportError)
    scheduleDeviceRefresh()
  })
  try {
    if (liveRoom.value !== connection) throw new Error('通话已结束。')
    const settings = track.getSourceTrackSettings()
    const requested = capturePreferences()
    nativeDenoiseStatus.value = settings.noiseSuppression === undefined ? '浏览器未报告实际降噪状态' : settings.noiseSuppression === requested.noiseSuppression ? '' : '浏览器未应用所选原生降噪'
    diagnosticEvent(`采集设置：回声消除 ${settings.echoCancellation ?? '未知'}，自动增益 ${settings.autoGainControl ?? '未知'}，原生降噪 ${settings.noiseSuppression ?? '未知'}`)
    if (!needsMicrophoneProcessor()) {
      diagnosticEvent('麦克风直接发送：未安装自定义处理器')
      return { track, processor: undefined }
    }
    track.setAudioContext(context)
    const processor = new MicrophoneGainProcessor(microphoneVolume.value, diagnosticEvent, {
      enabled: thresholdEnabled.value, value: thresholdValue.value, meter: audioSettingsVisible.value,
      onLevel: (db, open) => { thresholdLevel.value = db; thresholdOpen.value = open },
      onFailure: message => { thresholdEnabled.value = false; callNotice.value = message },
    }, {
      enabled: rnnoiseEnabled.value,
      onFailure: message => { rnnoiseEnabled.value = false; callNotice.value = message },
    })
    processor.setGainEnabled(inputGainEnabled.value)
    await track.setProcessor(processor)
    if (liveRoom.value !== connection) throw new Error('通话已结束。')
    return { track, processor }
  } catch (error) {
    diagnosticEvent('麦克风采集或发布失败')
    track.stop()
    throw error
  }
}

function onTrackSubscribed(track: RemoteTrack, _publication: unknown, participant: Participant) {
  if (track.kind !== 'audio') return
  const audioTrack = track as RemoteAudioTrack
  remoteAudioTracks.set(audioTrack, participant.identity)
  const node = track.attach()
  node.autoplay = true
  node.dataset.remoteAudio = 'true'
  document.body.appendChild(node)
  applyListeningVolume()
  syncProblemSources()
}

function onTrackUnsubscribed(track: RemoteTrack) {
  if (track.kind === 'audio') {
    remoteAudioTracks.delete(track as RemoteAudioTrack)
    track.detach().forEach((node) => node.remove())
    syncProblemSources()
  }
}

function updateSpeakers(speakers: Participant[]) {
  activeSpeakers.value = new Set(speakers.map((participant) => participant.identity))
  people.value = people.value.map((person) => ({
    ...person,
    is_speaking: activeSpeakers.value.has(person.identity),
  }))
}

async function refreshPeople() {
  const roomId = activeRoom.value?.id
  const connection = liveRoom.value
  if (!roomId || !connection) return
  try {
    const participants = await api<ParticipantInfo[]>(`/rooms/${roomId}/participants`)
    if (liveRoom.value !== connection || activeRoom.value?.id !== roomId) return
    people.value = participants.map((person) => {
      const participant = person.identity === connection.localParticipant.identity
        ? connection.localParticipant : connection.remoteParticipants.get(person.identity)
      return {
        ...person,
        audio_muted: participant ? ![...participant.audioTrackPublications.values()].some((track) => !track.isMuted) : person.audio_muted,
        is_speaking: activeSpeakers.value.has(person.identity),
      }
    })
  } catch {
    // The LiveKit room events keep the call usable while the roster refreshes.
  }
}

async function joinRoom(room: RoomInfo) {
  // Returning from settings to the current call must not reconnect the media session.
  settingsOpen.value = false
  mobileMenuOpen.value = false
  if (activeRoom.value?.id === room.id && liveRoom.value) return
  if (busy.value) return
  if (!user.value || user.value.pending_delete) return
  if (!window.isSecureContext) {
    callNotice.value = '手机语音需要 HTTPS 安全连接，请安装本地测试证书并使用 HTTPS 地址。'
    retryRoom.value = null
    return
  }
  void getAudioContext().resume().catch(() => undefined)
  busy.value = true
  errorMessage.value = ''
  retryRoom.value = null
  callNotice.value = ''
  let connection: LiveKitRoom | undefined
  let operationVersion = 0
  try {
    await disconnectRoom()
    operationVersion = ++callOperationVersion
    const grant = await api<{ token: string; server_url: string }>(`/rooms/${room.id}/token`, post())
    if (operationVersion !== callOperationVersion || !user.value) return
    const { Room, RoomEvent, DisconnectReason } = await import('livekit-client')
    if (operationVersion !== callOperationVersion || !user.value) return
    connection = new Room({
      adaptiveStream: false,
      dynacast: false,
      // Web Audio gain supports amplification beyond HTMLMediaElement's 0–1 volume range.
      webAudioMix: true,
      audioCaptureDefaults: capturePreferences(),
    })
    departingParticipants.delete(room.id)
    ++roomRefreshVersion
    liveRoom.value = connection
    diagnostics.start(connection, () => audioContext, () => ({
      device: micDevices.value.find(device => device.deviceId === micDevice.value)?.label || '默认麦克风',
      microphoneVolume: microphoneVolume.value, listeningVolume: listeningVolume.value, listeningMuted: listeningMuted.value,
      speaker: speakerDevices.value.find(device => device.deviceId === speakerDevice.value)?.label || '默认扬声器',
      contextSampleRate: audioContext?.sampleRate ?? null,
      thresholdEnabled: thresholdEnabled.value, thresholdDb: thresholdValue.value,
      nativeDenoiseEnabled: nativeDenoiseEnabled.value, rnnoiseEnabled: rnnoiseEnabled.value,
      rawMicrophone: rawMicrophone.value,
      echoCancellationEnabled: echoCancellationEnabled.value, autoGainEnabled: autoGainEnabled.value,
      inputGainEnabled: inputGainEnabled.value, microphoneProcessorEnabled: needsMicrophoneProcessor(),
    }))
    activeRoom.value = room
    connectionState.value = '连接中'
    connection.on(RoomEvent.TrackSubscribed, onTrackSubscribed)
    connection.on(RoomEvent.TrackUnsubscribed, onTrackUnsubscribed)
    connection.on(RoomEvent.ActiveSpeakersChanged, updateSpeakers)
    connection.on(RoomEvent.ParticipantConnected, () => void refreshPeople())
    connection.on(RoomEvent.ParticipantDisconnected, () => void refreshPeople())
    connection.on(RoomEvent.TrackPublished, () => void refreshPeople())
    connection.on(RoomEvent.TrackUnpublished, () => void refreshPeople())
    connection.on(RoomEvent.TrackMuted, () => void refreshPeople())
    connection.on(RoomEvent.TrackUnmuted, () => void refreshPeople())
    connection.on(RoomEvent.LocalTrackPublished, () => {
      if (liveRoom.value !== connection) return
      localMuted.value = !connection!.localParticipant.isMicrophoneEnabled
      syncProblemSources()
      void refreshPeople()
    })
    const syncLocalMicrophone = () => {
      if (liveRoom.value !== connection) return
      localMuted.value = !connection!.localParticipant.isMicrophoneEnabled
      syncProblemSources()
    }
    connection.on(RoomEvent.TrackMuted, syncLocalMicrophone)
    connection.on(RoomEvent.TrackUnmuted, syncLocalMicrophone)
    const currentConnection = connection
    const recovering = () => {
      if (liveRoom.value !== currentConnection) return
      diagnosticEvent('连接开始恢复')
      connectionReady.value = false
      connectionState.value = '网络恢复中'
      callNotice.value = '语音连接中断，正在自动重连。'
    }
    connection.on(RoomEvent.Reconnecting, recovering)
    connection.on(RoomEvent.SignalReconnecting, recovering)
    connection.on(RoomEvent.Reconnected, () => {
      if (liveRoom.value !== currentConnection) return
      diagnosticEvent('连接恢复成功')
      connectionReady.value = true
      connectionState.value = '已连接'
      currentIdentity.value = currentConnection.localParticipant.identity
      localMuted.value = !currentConnection.localParticipant.isMicrophoneEnabled
      callNotice.value = '语音连接已恢复。'
      applyListeningVolume()
      void refreshPeople()
      scheduleDeviceRefresh()
    })
    connection.on(RoomEvent.AudioPlaybackStatusChanged, () => {
      if (liveRoom.value === currentConnection) playbackBlocked.value = !currentConnection.canPlaybackAudio
    })
    connection.on(RoomEvent.MediaDevicesError, (error) => {
      if (liveRoom.value === currentConnection) {
        diagnosticEvent('浏览器报告设备错误')
        reportError(error)
      }
    })
    connection.on(RoomEvent.Disconnected, (reason) => {
      if (liveRoom.value !== currentConnection) return
      diagnosticEvent(`连接断开（原因编号 ${reason ?? '未知'}）`)
      if (reason === DisconnectReason.PARTICIPANT_REMOVED) {
        callNotice.value = '你已被管理员移出房间。'
      } else if (reason === DisconnectReason.DUPLICATE_IDENTITY) {
        callNotice.value = '此账号已在其他页面进入房间，当前通话已结束。'
      } else if (reason === DisconnectReason.ROOM_DELETED || reason === DisconnectReason.ROOM_CLOSED) {
        callNotice.value = '房间已关闭，当前通话已结束。'
      } else if (reason === DisconnectReason.CLIENT_INITIATED) {
        callNotice.value = '通话已结束。'
      } else {
        callNotice.value = reason === DisconnectReason.SERVER_SHUTDOWN
          ? '语音服务正在重启，稍后可重新加入房间。' : '语音连接未能恢复，可重新加入房间。'
        if (!user.value?.pending_delete) retryRoom.value = room
      }
      void disconnectRoom().catch(reportError)
    })
    let serverUrl = grant.server_url
    const configuredUrl = new URL(serverUrl)
    if (import.meta.env.DEV && ['localhost', '127.0.0.1', '[::1]'].includes(configuredUrl.hostname)) {
      const proxyUrl = new URL('/livekit', location.href)
      proxyUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      serverUrl = proxyUrl.toString()
    }
    await connection.connect(serverUrl, grant.token)
    if (liveRoom.value !== connection) return
    diagnosticEvent('房间连接成功')
    currentIdentity.value = connection.localParticipant.identity
    connectionReady.value = true
    playbackBlocked.value = !connection.canPlaybackAudio
    connectionState.value = '已连接'
    await refreshPeople()
    await loadDevices()
    if (liveRoom.value !== connection) return
    if (micDevice.value && !micDevices.value.some((device) => device.deviceId === micDevice.value)) {
      micDevice.value = ''
      localMuted.value = true
      callNotice.value = '原麦克风不可用，请选择设备后开麦。'
    }
    if (speakerDevice.value) await switchDevice('audiooutput', speakerDevice.value)
    if (liveRoom.value !== connection) return
    try {
      await setMicrophoneEnabled(connection, !localMuted.value)
    } catch (error) {
      if (liveRoom.value !== connection) return
      localMuted.value = true
      reportError(error) // Stay in the room as a listener when microphone access fails.
    }
  } catch (error) {
    // A publication may reject after its room was already closed. Ignore that old operation.
    if (connection && liveRoom.value !== connection) return
    if (operationVersion && operationVersion !== callOperationVersion) return
    if (user.value && !user.value.pending_delete) retryRoom.value = room
    callNotice.value = '连接房间失败，请检查网络后重试。'
    diagnosticEvent('房间连接失败')
    reportError(error)
    await disconnectRoom()
  } finally {
    busy.value = false
    drainDeviceChanges()
  }
}

async function disconnectRoom(clearSelection = true) {
  ++callOperationVersion
  const oldRoom = liveRoom.value
  void stopProblemRecording()
  diagnostics.stop()
  if (oldRoom) diagnosticEvent('结束当前通话并释放连接')
  const oldActiveRoom = activeRoom.value
  if (oldRoom && oldActiveRoom) {
    ++roomRefreshVersion // Discard requests started before this local departure.
    const identity = oldRoom.localParticipant.identity
    if (identity) {
      departingParticipants.set(oldActiveRoom.id, identity)
      rooms.value = rooms.value.map((room) => {
        if (room.id !== oldActiveRoom.id) return room
        const participants = room.participants?.filter((participant) => participant.identity !== identity)
        return { ...room, participants, participant_count: participants?.length ?? Math.max(0, (room.participant_count ?? 0) - 1) }
      })
    }
  }
  liveRoom.value = null
  connectionReady.value = false
  playbackBlocked.value = false
  currentIdentity.value = ''
  microphoneProcessor = undefined
  thresholdLevel.value = null
  thresholdOpen.value = false
  audioSettingsVisible.value = false
  remoteAudioTracks.clear()
  volumePanelIdentity.value = ''
  if (clearSelection) {
    activeRoom.value = null
    people.value = []
  }
  activeSpeakers.value = new Set()
  connectionState.value = ''
  if (oldRoom) {
    oldRoom.removeAllListeners()
    oldRoom.remoteParticipants.forEach((participant) => {
      participant.trackPublications.forEach((publication) => {
        if (publication.track?.kind === 'audio') publication.track.detach().forEach((node) => node.remove())
      })
    })
    await oldRoom.disconnect()
  }
  if (oldActiveRoom && user.value?.pending_delete) {
    await api(`/rooms/${oldActiveRoom.id}/leave`, post()).catch(() => undefined)
    user.value = null
  }
  if (oldRoom && user.value) await refreshRooms().catch(() => undefined)
}

async function leaveRoom() {
  retryRoom.value = null
  callNotice.value = ''
  await disconnectRoom()
}

async function toggleMicrophone() {
  const connection = liveRoom.value
  if (thresholdBusy.value || denoiseBusy.value) return
  if (!connection || microphoneBusy.value || deviceSwitchBusy.value || busy.value) return
  if (!connectionReady.value || connection.state !== 'connected') {
    reportError(new Error('语音连接尚未恢复，请稍后再试。'))
    return
  }
  microphoneBusy.value = true
  try {
    const enabled = !connection.localParticipant.isMicrophoneEnabled
    void getAudioContext().resume().catch(() => undefined)
    await setMicrophoneEnabled(connection, enabled)
    if (liveRoom.value !== connection) return
    localMuted.value = !connection.localParticipant.isMicrophoneEnabled
    playControlCue(!localMuted.value)
    await loadDevices()
  } catch (error) {
    if (liveRoom.value === connection) {
      diagnosticEvent('麦克风开关操作失败')
      localMuted.value = !connection.localParticipant.isMicrophoneEnabled
      const message = error instanceof Error ? error.message : ''
      reportError(message.includes('engine not connected within timeout')
        ? new Error('麦克风发布超时，请离开房间后重新加入。')
        : error)
    }
  } finally {
    microphoneBusy.value = false
    drainDeviceChanges()
  }
}

async function switchDevice(kind: 'audioinput' | 'audiooutput', value: string): Promise<boolean> {
  if (thresholdBusy.value || denoiseBusy.value) return false
  if (deviceSwitchBusy.value || microphoneBusy.value) return false
  const connection = liveRoom.value
  if (connection && !connectionReady.value) {
    reportError(new Error('语音连接恢复后再切换设备。'))
    return false
  }
  if (kind === 'audiooutput' && !speakerSelectionSupported) return false
  const previous = kind === 'audioinput' ? micDevice.value : speakerDevice.value
  if (kind === 'audioinput') micDevice.value = value
  else speakerDevice.value = value
  deviceSwitchBusy.value = true
  diagnosticEvent(kind === 'audioinput' ? '开始切换麦克风' : '开始切换扬声器')
  try {
    if (connection) {
      const success = await connection.switchActiveDevice(kind, value || 'default')
      if (!success) throw new Error('设备切换未成功，请重新选择。')
      if (liveRoom.value !== connection) return false
      if (kind === 'audiooutput') {
        const context = getAudioContext() as AudioContext & { setSinkId?: (id: string) => Promise<void> }
        await context.setSinkId?.(value || 'default').catch(() => undefined)
      }
      localMuted.value = !connection.localParticipant.isMicrophoneEnabled
      applyListeningVolume()
      await loadDevices()
    }
    diagnosticEvent(kind === 'audioinput' ? '麦克风切换成功' : '扬声器切换成功')
    return true
  } catch (error) {
    diagnosticEvent(kind === 'audioinput' ? '麦克风切换失败' : '扬声器切换失败')
    if (!connection || liveRoom.value === connection) {
      if (kind === 'audioinput') micDevice.value = previous
      else speakerDevice.value = previous
      reportError(error)
    }
    return false
  } finally {
    deviceSwitchBusy.value = false
    drainDeviceChanges()
  }
}

async function createRoom() {
  if (!newRoomName.value.trim() || creatingRoom.value) return
  if (rooms.value.some((room) => room.name.toLowerCase() === newRoomName.value.trim().toLowerCase())) {
    reportError(new Error('该房间名称已存在，请换一个名称。'))
    return
  }
  creatingRoom.value = true
  try {
    await api('/rooms', post({ name: newRoomName.value.trim(), description: newRoomDescription.value.trim() }))
    newRoomName.value = ''
    newRoomDescription.value = ''
    showCreateRoom.value = false
    await refreshRooms()
  } catch (error) {
    reportError(error)
  } finally {
    creatingRoom.value = false
  }
}

async function createInvite() {
  creatingGuestInvite.value = true
  try {
    const created = await api<InviteInfo>('/invites/guest', post({ max_uses: inviteLimit.value }))
    latestInvite.value = `${location.origin}/${encodeURIComponent(created.invite_code ?? '')}`
    await refreshInvites()
  } catch (error) {
    reportError(error)
  } finally {
    creatingGuestInvite.value = false
  }
}

async function refreshInvites() {
  if (!isManager.value) return
  try {
    inviteRows.value = await api<InviteInfo[]>('/invites/guest')
  } catch {
    inviteRows.value = []
  }
}

async function refreshBans() {
  if (!isManager.value) return
  try {
    bannedIds.value = await api<Array<{ guest_id: string; created_at: string }>>('/bans')
  } catch {
    bannedIds.value = []
  }
}

async function openSettings() {
  settingsOpen.value = true
  settingsSection.value = 'profile'
  profileUsername.value = user.value?.username ?? ''
  profileCurrentPassword.value = ''
  profileNewPassword.value = ''
  settingsNotice.value = ''
  mobileMenuOpen.value = false
  await refreshSettingsData()
}

async function refreshSettingsData() {
  if (!isManager.value) return
  await Promise.all([refreshGuests(), refreshInvites(), refreshBans()])
  if (isSuperadmin.value) await Promise.all([refreshAccounts(), refreshAdminInvites(), refreshAuditLogs()])
}

async function refreshGuests() {
  try { guestRows.value = await api<typeof guestRows.value>('/guests') } catch { guestRows.value = [] }
}

async function refreshAccounts() {
  try {
    accountRows.value = await api<typeof accountRows.value>('/accounts')
    accountNameDrafts.value = Object.fromEntries(accountRows.value.map((account) => [account.id, account.username]))
  } catch { accountRows.value = [] }
}

async function refreshAuditLogs(page = 1) {
  auditLoading.value = true
  try {
    const query = new URLSearchParams({ include_all: String(auditFilters.value.include_all), page: String(page), page_size: '30' })
    for (const [key, value] of Object.entries(auditFilters.value)) {
      if (key !== 'include_all' && value) query.set(key, key === 'start' || key === 'end' ? new Date(String(value)).toISOString() : String(value))
    }
    const data = await api<{ items: AuditRow[]; total: number; actions: Record<string, string> }>(`/audit-logs?${query}`)
    auditRows.value = data.items
    auditTotal.value = data.total
    auditActions.value = data.actions
    auditPage.value = page
  } catch (error) { reportError(error) } finally { auditLoading.value = false }
}

function clearAuditFilters() {
  auditFilters.value = { include_all: false, action: '', actor: '', target: '', result: '', q: '', start: '', end: '' }
  void refreshAuditLogs()
}

async function refreshAdminInvites() {
  if (!isSuperadmin.value) return
  try { adminInviteRows.value = await api<InviteInfo[]>('/invites/admin') } catch { adminInviteRows.value = [] }
}

async function saveProfile() {
  try {
    const updated = await api<User>('/account/profile', { ...post({
      username: profileUsername.value.trim(),
      current_password: profileCurrentPassword.value || undefined,
      new_password: profileNewPassword.value || undefined,
    }), method: 'PATCH' })
    user.value = { ...user.value!, ...updated }
    profileCurrentPassword.value = ''
    profileNewPassword.value = ''
    settingsNotice.value = '资料已保存'
  } catch (error) { reportError(error) }
}

async function saveAccount(accountId: number) {
  if (accountPasswordDrafts.value[accountId] && accountPasswordDrafts.value[accountId]!.length < 4) {
    reportError(new Error('密码至少 4 个字符'))
    return
  }
  try {
    await api(`/accounts/${accountId}`, { method: 'PATCH', body: JSON.stringify({
      username: accountNameDrafts.value[accountId],
      new_password: accountPasswordDrafts.value[accountId] || undefined,
    }) })
    accountPasswordDrafts.value[accountId] = ''
    settingsNotice.value = '账号已更新'
    await refreshAccounts()
  } catch (error) { reportError(error) }
}

async function revokeInvite(id: number) {
  try {
    await api(`/invites/${id}`, { method: 'DELETE' })
    await Promise.all([refreshInvites(), refreshAdminInvites()])
  } catch (error) {
    reportError(error)
  }
}

async function copyInvite() {
  if (!latestInvite.value) return
  await navigator.clipboard.writeText(latestInvite.value)
}

async function createAdminInvite() {
  creatingAdminInvite.value = true
  try {
    const result = await api<InviteInfo>('/invites/admin', post({
      max_uses: adminInviteLimit.value,
      expires_at: new Date(adminInviteExpiry.value).toISOString(),
    }))
    latestAdminInviteCode.value = result.invite_code ?? ''
    await refreshAdminInvites()
  } catch (error) {
    reportError(error)
  } finally {
    creatingAdminInvite.value = false
  }
}

async function copyText(value: string) {
  await navigator.clipboard.writeText(value)
}

async function kickParticipant(identity: string) {
  if (!activeRoom.value) return
  try {
    await api(`/rooms/${activeRoom.value.id}/participants/${encodeURIComponent(identity)}/kick`, post())
    await refreshPeople()
  } catch (error) {
    reportError(error)
  }
}

async function banGuest(identity: string) {
  const person = people.value.find((entry) => entry.identity === identity)
  const guestId = person?.name ?? ''
  if (!guestId) return
  try {
    await api(`/guests/${encodeURIComponent(guestId)}/ban`, post())
    await refreshBans()
    await refreshPeople()
  } catch (error) {
    reportError(error)
  }
}

async function unbanGuest(guestId: string) {
  try {
    await api(`/guests/${encodeURIComponent(guestId)}/ban`, { method: 'DELETE' })
    await refreshBans()
  } catch (error) {
    reportError(error)
  }
}

async function banGuestAccount(guestId: string) {
  try {
    const result = await api<{ pending_disconnect?: boolean }>(`/guests/${encodeURIComponent(guestId)}/ban`, post())
    settingsNotice.value = result.pending_disconnect ? '已拉黑，语音断开将在后台重试' : '已拉黑并结束通话'
    await refreshSettingsData()
  } catch (error) { reportError(error) }
}

async function kickGuestAccount(accountId: number) {
  moderationBusy.value = accountId
  try {
    await api(`/guests/${accountId}/kick`, post())
    settingsNotice.value = '已踢出当前语音房间，访客仍可重新加入'
    await refreshSettingsData()
    await refreshRooms()
  } catch (error) { reportError(error) } finally { moderationBusy.value = null }
}

async function muteParticipant(identity: string) {
  if (!activeRoom.value) return
  try {
    await api(`/rooms/${activeRoom.value.id}/participants/${encodeURIComponent(identity)}/mute`, post())
    await refreshPeople()
  } catch (error) {
    reportError(error)
  }
}

async function signOut() {
  retryRoom.value = null
  callNotice.value = ''
  await disconnectRoom()
  await api('/auth/logout', post()).catch(() => undefined)
  user.value = null
}

async function archiveRoom(room: RoomInfo) {
  try {
    await api(`/rooms/${room.id}`, { method: 'DELETE' })
    if (activeRoom.value?.id === room.id) await disconnectRoom()
    await refreshRooms()
  } catch (error) {
    reportError(error)
  }
}

watch(user, (current, previous) => {
  if (user.value?.pending_delete && activeRoom.value) {
    connectionState.value = '通话结束后游客账号将清除'
  }
  if (current?.id !== previous?.id) {
    void refreshInvites()
    void refreshBans()
  }
})

onMounted(() => {
  try {
    const saved = JSON.parse(localStorage.getItem('rikumo-audio-volumes') || '{}')
    if (typeof saved.microphone === 'number' && Number.isFinite(saved.microphone)) setMicrophoneVolume(saved.microphone)
    if (typeof saved.listening === 'number' && Number.isFinite(saved.listening)) setListeningVolume(saved.listening)
  } catch { /* Browser storage is optional. */ }
  try {
    const saved = JSON.parse(localStorage.getItem('rikumo-threshold-v2') || '{}')
    if (typeof saved.enabled === 'boolean') thresholdEnabled.value = saved.enabled
    if (typeof saved.value === 'number') setThresholdValue(saved.value)
  } catch { /* Optional browser preference. */ }
  try {
    const saved = JSON.parse(localStorage.getItem('rikumo-denoise-v1') || '{}')
    if (typeof saved.native === 'boolean') nativeDenoiseEnabled.value = saved.native
    if (typeof saved.rnnoise === 'boolean') rnnoiseEnabled.value = saved.rnnoise
  } catch { /* Optional browser preference. */ }
  try {
    const saved = JSON.parse(localStorage.getItem('rikumo-processing-v1') || '{}')
    if (typeof saved.echo === 'boolean') echoCancellationEnabled.value = saved.echo
    if (typeof saved.auto === 'boolean') autoGainEnabled.value = saved.auto
    if (typeof saved.gain === 'boolean') inputGainEnabled.value = saved.gain
  } catch { /* Optional browser preference. */ }
  problemPollTimer = window.setInterval(() => { void pollProblemRecording() }, 5000)
  window.addEventListener('rikumo:auth-expired', handleAuthExpired)
  window.addEventListener('offline', handleOffline)
  window.addEventListener('online', handleOnline)
  navigator.mediaDevices?.addEventListener('devicechange', scheduleDeviceRefresh)
  void refreshUser()
  void loadDevices().catch(reportError)
  refreshTimer = window.setInterval(() => {
    if (!user.value) return
    void api<User>('/auth/me').then((latest) => { user.value = latest }).catch(() => undefined)
    void refreshRooms().catch(() => undefined)
  }, 8000)
})

watch([microphoneVolume, listeningVolume], () => {
  try {
    localStorage.setItem('rikumo-audio-volumes', JSON.stringify({ microphone: microphoneVolume.value, listening: listeningVolume.value }))
  } catch { /* Audio controls remain usable when browser storage is unavailable. */ }
})

watch([thresholdEnabled, thresholdValue], () => {
  diagnosticEvent(`收音阈值设置：${thresholdEnabled.value ? '开启' : '关闭'}，${thresholdValue.value} dBFS`)
  try { localStorage.setItem('rikumo-threshold-v2', JSON.stringify({ enabled: thresholdEnabled.value, value: thresholdValue.value })) }
  catch { /* Threshold controls work without browser storage. */ }
})

watch([echoCancellationEnabled, autoGainEnabled, inputGainEnabled], () => {
  diagnosticEvent(`音频处理设置：回声消除 ${echoCancellationEnabled.value}，自动增益 ${autoGainEnabled.value}，输入音量 ${inputGainEnabled.value}`)
  try { localStorage.setItem('rikumo-processing-v1', JSON.stringify({ echo: echoCancellationEnabled.value, auto: autoGainEnabled.value, gain: inputGainEnabled.value })) }
  catch { /* Preferences work without browser storage. */ }
})

watch([nativeDenoiseEnabled, rnnoiseEnabled], () => {
  diagnosticEvent(`降噪设置：原生 ${nativeDenoiseEnabled.value ? '开启' : '关闭'}，RNNoise ${rnnoiseEnabled.value ? '开启' : '关闭'}`)
  try { localStorage.setItem('rikumo-denoise-v1', JSON.stringify({ native: nativeDenoiseEnabled.value, rnnoise: rnnoiseEnabled.value })) }
  catch { /* Audio controls work without browser storage. */ }
})

onUnmounted(() => {
  window.clearInterval(problemPollTimer)
  window.removeEventListener('rikumo:auth-expired', handleAuthExpired)
  window.removeEventListener('offline', handleOffline)
  window.removeEventListener('online', handleOnline)
  navigator.mediaDevices?.removeEventListener('devicechange', scheduleDeviceRefresh)
  if (deviceChangeTimer) window.clearTimeout(deviceChangeTimer)
  if (refreshTimer) window.clearInterval(refreshTimer)
  void disconnectRoom().finally(() => { void audioContext?.close() })
})
</script>

<template>
  <main class="app-shell">
    <section v-if="!user" class="auth-screen">
      <div class="auth-side">
        <div class="brand brand-large"><span class="brand-mark"><AudioLines :size="22" /></span><span>璃雲の部屋</span></div>
        <div class="auth-orbit orbit-one"></div><div class="auth-orbit orbit-two"></div>
      </div>
      <div class="auth-form-side">
        <div class="mobile-brand"><span class="brand-mark"><AudioLines :size="20" /></span> 璃雲の部屋</div>
        <div class="auth-card">
          <div class="auth-card-head">
            <h2>{{ authMode === 'guest' ? '进入语音空间' : (isRegisteringAdmin ? '创建管理员账号' : '管理员登录') }}</h2>
            <p>{{ authMode === 'guest' ? '输入 ID 加入语音空间；已有 ID 可继续登录。' : '使用管理员账号继续。' }}</p>
          </div>
          <div class="auth-tabs auth-tabs-single">
            <button v-if="inviteCode" :class="{ selected: authMode === 'guest' }" @click="authMode = 'guest'">访客加入</button>
            <button v-if="!inviteCode" :class="{ selected: authMode === 'admin' }" @click="authMode = 'admin'">管理员登录</button>
          </div>
          <form class="auth-fields" @submit.prevent="submitAuth">
            <template v-if="authMode === 'guest'">
              <label class="field-label" for="guest-id">你的 ID</label>
              <div class="input-wrap"><Users :size="17" /><input id="guest-id" v-model="guestId" autocomplete="username" placeholder="请输入 ID（支持中文）" minlength="1" maxlength="24" required /></div>
              <div class="invite-recognized"><Check :size="15" /> 已读取邀请链接；ID 支持中文，最长 24 个字符</div>
              <p class="form-hint"><CircleHelp :size="14" /> 游客 ID 每天凌晨 4 点清理；之后需使用管理员发来的当日有效邀请链接重新加入。</p>
            </template>
            <template v-else>
              <template v-if="isResettingAdmin">
                <label class="field-label" for="reset-code">一次性重置码</label>
                <div class="input-wrap"><Shield :size="17" /><input id="reset-code" v-model="passwordResetCode" autocomplete="one-time-code" placeholder="输入超级管理员提供的重置码" required /></div>
              </template>
              <template v-else>
                <template v-if="isRegisteringAdmin">
                  <label class="field-label" for="admin-code">管理员注册邀请码</label>
                  <div class="input-wrap"><Shield :size="17" /><input id="admin-code" v-model="adminInviteCode" placeholder="输入超级管理员提供的邀请码" required /></div>
                </template>
                <label class="field-label" for="admin-name">用户名</label>
                <div class="input-wrap"><Users :size="17" /><input id="admin-name" v-model="adminUsername" autocomplete="username" placeholder="管理员用户名（支持中文）" minlength="1" maxlength="48" required /></div>
                <p v-if="isRegisteringAdmin" class="form-hint">用户名支持中文；密码至少 4 个字符。</p>
              </template>
              <label class="field-label" for="admin-password">{{ isResettingAdmin ? '设置新密码' : '密码' }}</label>
              <div class="input-wrap"><Shield :size="17" /><input id="admin-password" v-model="adminPassword" type="password" :autocomplete="isResettingAdmin || isRegisteringAdmin ? 'new-password' : 'current-password'" :placeholder="isResettingAdmin || isRegisteringAdmin ? '至少 4 个字符' : '请输入密码'" :minlength="isResettingAdmin || isRegisteringAdmin ? 4 : 1" required /></div>
              <button v-if="!isRegisteringAdmin && !isResettingAdmin" class="text-button register-link" type="button" @click="isRegisteringAdmin = true">使用注册邀请码创建管理员账号</button>
              <button v-if="!isRegisteringAdmin && !isResettingAdmin" class="text-button register-link" type="button" @click="isResettingAdmin = true">使用一次性重置码设置新密码</button>
              <button v-if="isRegisteringAdmin || isResettingAdmin" class="text-button register-link" type="button" @click="isRegisteringAdmin = false; isResettingAdmin = false">返回管理员登录</button>
            </template>
            <button class="primary-button auth-submit" type="submit" :disabled="busy">
              <span>{{ busy ? '正在连接…' : (authMode === 'guest' ? (inviteCode ? '加入房间列表' : '继续') : (isRegisteringAdmin ? '创建账号' : isResettingAdmin ? '设置新密码' : '登录')) }}</span><span class="button-arrow">↗</span>
            </button>
          </form>
          <div class="auth-privacy"><span class="privacy-dot"></span> 语音实时传输；问题记录开启时会显示提醒</div>
        </div>
        <div class="auth-copyright">璃雲の部屋</div>
      </div>
    </section>

    <section v-else class="workspace">
      <aside class="sidebar" :class="{ 'sidebar-open': mobileMenuOpen }">
        <div class="side-top">
          <div class="brand"><span class="brand-mark"><AudioLines :size="18" /></span><span>璃雲の部屋</span></div>
          <button class="icon-button mobile-only" aria-label="关闭菜单" @click="mobileMenuOpen = false"><X :size="18" /></button>
        </div>

        <div class="side-section-head"><span>语音房间</span><button v-if="isManager" class="mini-icon" title="创建房间" @click="showCreateRoom = true"><Plus :size="15" /></button></div>
        <div class="room-list">
          <div v-if="rooms.length === 0" class="empty-rooms">还没有房间</div>
          <div v-for="room in rooms" :key="room.id" class="room-row" :class="{ active: activeRoom?.id === room.id }" role="button" tabindex="0" @click="joinRoom(room); mobileMenuOpen = false" @keydown.enter="joinRoom(room)">
            <span class="room-hash">#</span><span class="room-name">{{ room.name }}</span><span v-if="room.participant_count" class="room-count">{{ room.participant_count }}</span>
            <button v-if="isManager" class="room-delete" title="删除房间" @click.stop="archiveRoom(room)"><X :size="13" /></button>
          </div>
        </div>
        <div class="sidebar-grow"></div>

        <div class="sidebar-settings"><button class="settings-entry" :class="{ selected: settingsOpen }" title="设置" @click="openSettings"><Settings :size="17" /><span>设置</span></button></div>

        <div class="user-panel">
          <div class="user-avatar" :class="user.role">{{ user.username.slice(0, 1).toUpperCase() }}</div>
          <div class="user-meta"><strong>{{ user.username }}</strong><small>{{ user.role === 'guest' ? (user.pending_delete ? '通话结束后清除' : '普通成员') : user.role === 'superadmin' ? '超级管理员' : '管理员' }}</small></div>
          <button class="icon-button" title="退出登录" @click="signOut"><LogOut :size="16" /></button>
        </div>
      </aside>

      <div v-if="mobileMenuOpen" class="mobile-scrim" @click="mobileMenuOpen = false"></div>

      <section class="main-panel">
        <header class="topbar">
          <button class="icon-button mobile-only" aria-label="打开菜单" @click="mobileMenuOpen = true"><Settings2 :size="18" /></button>
          <div class="breadcrumbs"><strong>{{ settingsOpen ? '设置' : (activeRoom?.name ?? '语音大厅') }}</strong></div>
          <div class="topbar-right"><button v-if="diagnosticSnapshot && !liveRoom" class="text-button" @click="diagnosticsOpen = !diagnosticsOpen">通话诊断</button><button class="icon-button" aria-label="帮助"><CircleHelp :size="17" /></button></div>
        </header>
        <div v-if="problemSession" class="problem-recording-banner" role="status"><span class="recording-dot"></span>{{ problemSession.owner_name }} 正在缓存本房间最近三分钟语音，仅保存问题记录时导出。</div>
        <div v-if="callNotice || playbackBlocked" class="call-notice" role="status" aria-live="polite">
          <span>{{ playbackBlocked ? '浏览器暂停了声音播放，请点击恢复收听。' : callNotice }}</span>
          <button v-if="playbackBlocked && liveRoom" class="text-button" @click="resumeAudioPlayback">恢复收听</button>
          <button v-else-if="retryRoom && !liveRoom && !user?.pending_delete" class="text-button" :disabled="busy" @click="joinRoom(retryRoom)">{{ busy ? '连接中…' : '重新加入' }}</button>
          <button v-if="!playbackBlocked" class="icon-button" aria-label="关闭提示" @click="callNotice = ''; retryRoom = null"><X :size="14" /></button>
        </div>

        <div v-if="settingsOpen" class="settings-view">
          <div class="settings-heading"><button class="text-button" @click="settingsOpen = false">返回语音大厅</button><h1>设置</h1></div>
          <div class="settings-layout">
            <nav class="settings-nav" aria-label="设置分类">
              <button :class="{ selected: settingsSection === 'profile' }" @click="settingsSection = 'profile'">个人资料</button>
              <button v-if="isManager" :class="{ selected: settingsSection === 'accounts' }" @click="settingsSection = 'accounts'; void refreshSettingsData()">账号管理</button>
              <button v-if="isManager" :class="{ selected: settingsSection === 'invites' }" @click="settingsSection = 'invites'; void refreshInvites()">邀请管理</button>
              <button v-if="isSuperadmin" :class="{ selected: settingsSection === 'logs' }" @click="settingsSection = 'logs'; void refreshAuditLogs()">操作日志</button>
            </nav>
            <section class="settings-content">
              <template v-if="settingsSection === 'profile'">
                <h2>个人资料</h2><p>修改当前账号的用户名和密码。</p>
                <form class="settings-form" @submit.prevent="saveProfile">
                  <label class="field-label" for="profile-username">{{ user.role === 'guest' ? '访客 ID' : '用户名' }}</label>
                  <input id="profile-username" v-model="profileUsername" class="plain-input" minlength="1" :maxlength="user.role === 'guest' ? 24 : 48" required />
                  <template v-if="user.role !== 'guest'"><label class="field-label" for="profile-current-password">当前密码</label><input id="profile-current-password" v-model="profileCurrentPassword" class="plain-input" type="password" autocomplete="current-password" placeholder="修改密码时填写" /><label class="field-label" for="profile-new-password">新密码</label><input id="profile-new-password" v-model="profileNewPassword" class="plain-input" type="password" autocomplete="new-password" minlength="4" placeholder="至少 4 个字符" /></template>
                  <p v-else class="settings-hint">访客账号使用 ID 和当日邀请链接登录，没有密码。</p>
                  <button class="primary-button" type="submit">保存资料</button><span v-if="settingsNotice" class="settings-notice">{{ settingsNotice }}</span>
                </form>
              </template>
              <template v-else-if="settingsSection === 'accounts'">
                <h2>账号管理</h2>
                <template v-if="isSuperadmin">
                  <p>查看账号并修改用户名或设置新密码。密码经过加密保存，无法查看原密码。</p>
                  <div class="settings-account-list"><div v-for="account in accountRows" :key="account.id" class="settings-account-row managed-account-row"><div class="settings-account-meta"><strong>{{ account.username }}</strong><small>{{ roleLabel(account.role) }}<span v-if="account.pending_delete"> · 通话结束后清除</span></small><small v-if="account.role === 'guest'">{{ guestRoomLabel(account) }}</small></div><template v-if="account.role !== 'superadmin'"><div class="account-edit-fields"><input v-model="accountNameDrafts[account.id]" class="plain-input" aria-label="用户名" /><input v-if="account.role === 'admin'" v-model="accountPasswordDrafts[account.id]" class="plain-input" type="password" minlength="4" placeholder="输入新密码（至少 4 个字符）" /></div><div class="account-actions"><button class="secondary-button" @click="saveAccount(account.id)">{{ accountPasswordDrafts[account.id] ? '修改密码' : '保存用户名' }}</button><template v-if="account.role === 'guest'"><button class="secondary-button" :disabled="moderationBusy === account.id || !account.room_state_known || !account.room_names?.length" @click="kickGuestAccount(account.id)">踢出</button><button class="text-button danger-text" @click="banGuestAccount(account.username)">拉黑</button></template></div></template><small v-else>请在个人资料中修改</small></div></div>
                </template>
                <template v-else>
                  <p>查看普通用户并管理封禁状态。</p>
                  <div class="settings-account-list"><div v-for="guest in guestRows" :key="guest.id" class="settings-account-row guest-management-row"><div class="settings-account-meta"><strong>{{ guest.username }}</strong><small>{{ guest.pending_delete ? '通话结束后清除' : '访客' }} · {{ guestRoomLabel(guest) }}</small></div><div class="account-actions"><button class="secondary-button" :disabled="moderationBusy === guest.id || !guest.room_state_known || !guest.room_names?.length" @click="kickGuestAccount(guest.id)">踢出</button><button class="text-button danger-text" @click="banGuestAccount(guest.username)">拉黑</button></div></div><p v-if="!guestRows.length" class="settings-hint">暂无普通用户</p></div>
                </template>
                <p v-if="settingsNotice" class="settings-notice" role="status">{{ settingsNotice }}</p>
                <div class="settings-account-list"><h3>已拉黑 ID</h3><div v-for="ban in bannedIds" :key="ban.guest_id" class="settings-account-row"><strong>{{ ban.guest_id }}</strong><button class="text-button" @click="unbanGuest(ban.guest_id); void refreshGuests()">解除拉黑</button></div><p v-if="!bannedIds.length" class="settings-hint">暂无拉黑账号</p></div>
              </template>
              <template v-else-if="settingsSection === 'invites'">
                <h2>邀请管理</h2><p>设置邀请链接的注册人数和有效时间。</p>
                <div class="invite-type-panel">
                  <h3>普通用户邀请</h3><p>邀请链接每天凌晨 4 点失效。</p>
                  <div class="invite-create-grid">
                    <div class="invite-field"><label class="field-label" for="settings-invite-limit">可注册人数</label><input id="settings-invite-limit" v-model.number="inviteLimit" class="plain-input" type="number" min="1" max="100" /></div>
                    <div class="invite-field"><label class="field-label">过期时间</label><div class="plain-input invite-fixed-expiry">每天凌晨 4 点</div></div>
                    <button class="secondary-button invite-create-button" :disabled="creatingGuestInvite" @click="createInvite">{{ creatingGuestInvite ? '正在生成…' : '生成邀请链接' }}</button>
                  </div>
                  <div v-if="latestInvite" class="generated-invite"><span>{{ latestInvite }}</span><button class="icon-button" title="复制邀请链接" @click="copyInvite"><Copy :size="15" /></button></div>
                  <div class="invite-history"><div v-for="row in inviteRows" :key="row.id" class="settings-account-row"><div class="settings-account-meta"><strong>{{ row.invite_code || '旧邀请码无法再次显示' }}</strong><small>{{ row.uses ?? 0 }}/{{ row.max_uses }} 已注册 · {{ row.revoked ? '已撤销' : new Date(row.expires_at).toLocaleString('zh-CN') + ' 到期' }}</small><small v-if="row.invite_code" class="invite-url">{{ guestInviteUrl(row.invite_code) }}</small></div><button v-if="row.invite_code" class="text-button" @click="copyText(guestInviteUrl(row.invite_code))">复制链接</button><button v-if="!row.revoked" class="text-button" @click="revokeInvite(row.id)">撤销</button></div></div>
                </div>
                <div v-if="isSuperadmin" class="invite-type-panel">
                  <h3>管理员邀请码</h3><p>每个邀请码可以单独设置注册人数和过期时间。</p>
                  <div class="invite-create-grid">
                    <div class="invite-field"><label class="field-label" for="admin-invite-limit">可注册人数</label><input id="admin-invite-limit" v-model.number="adminInviteLimit" class="plain-input" type="number" min="1" max="100" required /></div>
                    <div class="invite-field"><label class="field-label" for="admin-invite-expiry">过期时间</label><input id="admin-invite-expiry" v-model="adminInviteExpiry" class="plain-input" type="datetime-local" :min="adminInviteMinExpiry" required /></div>
                    <button class="secondary-button invite-create-button" :disabled="creatingAdminInvite" @click="createAdminInvite">{{ creatingAdminInvite ? '正在生成…' : '生成邀请码' }}</button>
                  </div>
                  <div v-if="latestAdminInviteCode" class="generated-invite"><span>邀请码：{{ latestAdminInviteCode }}<br />链接：{{ adminInviteUrl(latestAdminInviteCode) }}</span><button class="icon-button" title="复制管理员注册链接" @click="copyText(adminInviteUrl(latestAdminInviteCode))"><Copy :size="15" /></button></div>
                  <div class="invite-history"><div v-for="row in adminInviteRows" :key="row.id" class="settings-account-row"><div class="settings-account-meta"><strong>{{ row.invite_code || '旧邀请码无法再次显示' }}</strong><small>{{ row.uses ?? 0 }}/{{ row.max_uses }} 已注册 · {{ row.revoked ? '已撤销' : new Date(row.expires_at).toLocaleString('zh-CN') + ' 到期' }}</small><small v-if="row.invite_code" class="invite-url">{{ adminInviteUrl(row.invite_code) }}</small></div><button v-if="row.invite_code" class="text-button" @click="copyText(adminInviteUrl(row.invite_code))">复制链接</button><button v-if="!row.revoked" class="text-button" @click="revokeInvite(row.id)">撤销</button></div></div>
                </div>
              </template>
              <template v-else>
                <h2>操作日志</h2><p>默认显示重要操作；登录访问、查询等后台记录可切换到全部记录查看。</p>
                <form class="audit-filters" @submit.prevent="refreshAuditLogs()">
                  <label>显示范围<select v-model="auditFilters.include_all" class="plain-input"><option :value="false">重要操作</option><option :value="true">全部记录</option></select></label>
                  <label>操作类型<select v-model="auditFilters.action" class="plain-input"><option value="">全部操作</option><option v-for="(label, action) in auditActions" :key="action" :value="action">{{ label }}</option></select></label>
                  <label>开始时间<input v-model="auditFilters.start" class="plain-input" type="datetime-local" /></label><label>结束时间<input v-model="auditFilters.end" class="plain-input" type="datetime-local" /></label>
                  <label>操作人<input v-model="auditFilters.actor" class="plain-input" placeholder="用户名" /></label><label>操作对象<input v-model="auditFilters.target" class="plain-input" placeholder="用户、房间或邀请码" /></label>
                  <label>结果<select v-model="auditFilters.result" class="plain-input"><option value="">全部结果</option><option value="success">成功</option><option value="failure">失败</option></select></label>
                  <label>搜索<input v-model="auditFilters.q" class="plain-input" placeholder="搜索用户名、房间、邀请码或操作" maxlength="200" /></label>
                  <div class="audit-filter-actions"><button class="secondary-button" type="submit" :disabled="auditLoading">{{ auditLoading ? '查询中…' : '查询' }}</button><button class="text-button" type="button" @click="clearAuditFilters">重置筛选</button></div>
                </form>
                <div class="settings-log-list"><div class="settings-log-row settings-log-head"><span>时间</span><span>操作人</span><span>操作</span><span>结果</span></div><div v-for="entry in auditRows" :key="entry.id" class="settings-log-row"><span>{{ new Date(entry.created_at).toLocaleString('zh-CN') }}</span><span>{{ entry.username }}（{{ roleLabel(entry.role) }}）</span><details><summary>{{ entry.summary }}</summary><div class="audit-detail">对象：{{ entry.target || '—' }}<br />接口：{{ entry.method }} {{ entry.path || '后台任务' }}<br />状态码：{{ entry.status_code }}</div></details><span :class="entry.status_code >= 400 ? 'danger-text' : 'settings-notice'">{{ entry.status_code >= 400 ? '失败' : '成功' }}</span></div><p v-if="!auditRows.length" class="settings-hint">暂无符合条件的日志</p></div>
                <div class="audit-pagination"><span>共 {{ auditTotal }} 条 · 第 {{ auditPage }}/{{ auditPageCount }} 页</span><button class="secondary-button" :disabled="auditPage <= 1 || auditLoading" @click="refreshAuditLogs(auditPage - 1)">上一页</button><button class="secondary-button" :disabled="auditPage >= auditPageCount || auditLoading" @click="refreshAuditLogs(auditPage + 1)">下一页</button></div>
              </template>
            </section>
          </div>
        </div>
        <div v-else-if="!activeRoom" class="lobby-view">
          <div class="lobby-intro">
            <h1>语音大厅</h1>
            <p>选择一个房间加入语音通话。</p>
          </div>
          <div class="section-title"><div><h2>语音房间 <span>{{ rooms.length.toString().padStart(2, '0') }}</span></h2></div><button v-if="isManager" class="secondary-button" @click="showCreateRoom = true"><Plus :size="16" /> 创建房间</button></div>
          <div v-if="rooms.length" class="room-cards">
            <button v-for="(room, index) in rooms" :key="room.id" class="room-card" @click="joinRoom(room)">
              <div class="room-card-top"><span class="room-card-glyph">{{ ['雲', '月', '風', '星'][index % 4] }}</span><span class="live-count"><span class="live-dot"></span>{{ room.participant_count || 0 }} 人在线</span></div>
              <div class="room-card-content"><h3>{{ room.name }}</h3><p>{{ room.description || '暂无简介' }}</p></div>
              <div class="room-card-bottom"><span>加入语音</span><span class="card-arrow">↗</span></div>
            </button>
          </div>
          <div v-else class="empty-card"><div class="empty-symbol"><Headphones :size="24" /></div><h3>暂无语音房间</h3><p>管理员创建房间后即可加入。</p><button v-if="isManager" class="secondary-button" @click="showCreateRoom = true"><Plus :size="16" /> 创建房间</button></div>
        </div>

        <div v-else class="call-view">
          <div class="call-room-head"><div><h1>{{ activeRoom.name }}</h1><p>{{ activeRoom.description || '语音通话中' }}</p></div><button class="secondary-button leave-top" @click="leaveRoom()"><DoorOpen :size="16" /> 离开房间</button></div>
            <div class="participant-section"><div class="section-title compact"><div><h2>房间成员 <span>{{ people.length.toString().padStart(2, '0') }}</span></h2></div><button class="icon-button" title="刷新成员" @click="refreshPeople"><Users :size="17" /></button></div>
            <div v-if="people.length" class="participant-grid">
              <div v-for="person in people" :key="person.identity" class="participant-card" :class="{ speaking: person.is_speaking }">
                <div class="participant-avatar" :class="person.identity.startsWith('guest_') ? 'guest' : 'admin'">{{ person.name?.slice(0, 1) || '?' }}</div>
                <div class="participant-meta"><strong>{{ person.name || person.identity }}</strong><small>{{ person.identity === currentIdentity ? '你' : (person.identity.startsWith('guest_') ? '普通成员' : '团队成员') }}</small></div>
                <div v-if="person.is_speaking" class="speaking-bars"><i></i><i></i><i></i></div>
                <MicOff v-if="person.identity === currentIdentity ? localMuted : person.audio_muted" class="participant-audio-state" :size="14" aria-label="麦克风已关闭" />
                <button v-if="person.identity !== currentIdentity" class="participant-more" :title="`调整 ${person.name || person.identity} 的收听音量`" :aria-label="`调整 ${person.name || person.identity} 的收听音量`" :aria-expanded="volumePanelIdentity === person.identity" @click="volumePanelIdentity = volumePanelIdentity === person.identity ? '' : person.identity"><Volume2 :size="15" /></button>
                <template v-if="isManager && person.identity !== currentIdentity">
                  <button v-if="person.identity.startsWith('guest_')" class="participant-more" :disabled="person.audio_muted" :title="person.audio_muted ? '对方麦克风已关闭或静音' : '禁言'" @click="muteParticipant(person.identity)"><MicOff :size="13" /></button>
                  <button v-if="person.identity.startsWith('guest_')" class="participant-more danger-action" title="封禁此 ID 并踢出" @click="banGuest(person.identity)"><Ban :size="13" /></button>
                  <button class="participant-more" title="踢出房间" @click="kickParticipant(person.identity)"><X :size="14" /></button>
                </template>
                <label v-if="person.identity !== currentIdentity && volumePanelIdentity === person.identity" class="member-volume">成员音量<input type="range" min="0" max="200" step="1" :value="memberVolumes[person.identity] ?? 100" :aria-label="`${person.name || person.identity} 的收听音量`" @input="setMemberVolume(person.identity, $event)" /><output>{{ memberVolumes[person.identity] ?? 100 }}%</output></label>
              </div>
            </div>
            <div v-else class="waiting-card"><span class="waiting-pulse"></span><span>你已经在房间里，其他人加入后会出现在这里。</span></div>
          </div>
        </div>
        <CallDiagnosticsPanel v-if="diagnosticsOpen" :snapshot="diagnosticSnapshot" :event-count="diagnosticEventCount" :is-manager="isManager && !!liveRoom && connectionReady" :recording="!!problemSession" :recording-owned="problemSession?.owner_id === user?.id" :recording-busy="problemBusy" :recording-saving="problemSaving" :recording-seconds="problemCache.seconds" :recording-bytes="problemCache.bytes" :recording-tracks="problemCache.tracks" :recording-notice="problemNotice" :recording-members="people" v-model:recording-member="problemMember" v-model:recording-kind="problemKind" @recording-start="startProblemRecording" @recording-stop="stopProblemRecording" @recording-save="saveProblemRecording" @recording-mark="markProblemRecording" @close="diagnosticsOpen = false" @mark="diagnosticEvent('用户标记出现电音')" @download="diagnostics.download()" />
        <VoiceControls v-if="activeRoom && liveRoom" :echo-cancellation-enabled="echoCancellationEnabled" :auto-gain-enabled="autoGainEnabled" :input-gain-enabled="inputGainEnabled" @echo-cancellation-enabled="setCaptureOption('echo', $event)" @auto-gain-enabled="setCaptureOption('auto', $event)" @input-gain-enabled="setCaptureOption('gain', $event)" :raw-microphone="rawMicrophone" @raw-microphone="setRawMicrophone" :mic-muted="localMuted" :mic-busy="busy || microphoneBusy || deviceSwitchBusy || thresholdBusy || denoiseBusy || !connectionReady" :devices-busy="busy || microphoneBusy || deviceSwitchBusy || thresholdBusy || denoiseBusy || !connectionReady" :speaker-supported="speakerSelectionSupported" :mic-volume="microphoneVolume" :listen-muted="listeningMuted" :listen-volume="listeningVolume" :mic-devices="micDevices" :speaker-devices="speakerDevices" :mic-device="micDevice" :speaker-device="speakerDevice" :status="connectionState" :threshold-enabled="thresholdEnabled" :threshold-value="thresholdValue" :threshold-busy="thresholdBusy || denoiseBusy" :native-denoise-enabled="nativeDenoiseEnabled" :native-denoise-status="nativeDenoiseStatus" :rnnoise-enabled="rnnoiseEnabled" :denoise-busy="denoiseBusy" @native-denoise-enabled="setNativeDenoiseEnabled" @rnnoise-enabled="setRnnoiseEnabled" :threshold-level="thresholdLevel" :threshold-open="thresholdOpen" @threshold-enabled="setThresholdEnabled" @threshold-value="setThresholdValue" @settings-visibility="setAudioSettingsVisible" @diagnostics="diagnosticsOpen = !diagnosticsOpen" @toggle-mic="toggleMicrophone" @toggle-listen="toggleListening" @mic-volume="setMicrophoneVolume" @listen-volume="setListeningVolume" @mic-device="switchDevice('audioinput', $event)" @speaker-device="switchDevice('audiooutput', $event)" @leave="leaveRoom()" />
      </section>

      <aside v-if="activeRoom && !settingsOpen" class="details-panel">
        <div class="details-head"><span>房间信息</span><button class="icon-button" title="房间设置"><Settings2 :size="16" /></button></div>
        <div class="details-room"><div class="details-glyph">{{ activeRoom.name.slice(0, 1) }}</div><h2>{{ activeRoom.name }}</h2><p>{{ activeRoom.description || '暂无简介' }}</p></div>
        <div class="details-divider"></div><div class="details-subhead"><span>当前成员</span><span>{{ people.length }}</span></div>
        <div class="details-members"><div v-for="person in people" :key="person.identity" class="details-member"><span class="tiny-avatar" :class="person.identity.startsWith('guest_') ? 'guest' : 'admin'">{{ person.name?.slice(0, 1) || '?' }}</span><span>{{ person.name }}</span><span v-if="person.is_speaking" class="speaking-dot"></span></div></div>
        <div class="details-footer"><span class="privacy-dot"></span> 不录音，不保存语音</div>
      </aside>
    </section>

    <div v-if="showCreateRoom" class="modal-backdrop" @click.self="showCreateRoom = false"><form class="modal-card" @submit.prevent="createRoom"><button class="modal-close" type="button" @click="showCreateRoom = false"><X :size="18" /></button><h2>创建语音房间</h2><p>房间将对所有成员显示。</p><label class="field-label" for="room-name">房间名称</label><input id="room-name" v-model="newRoomName" class="plain-input" placeholder="请输入房间名称" maxlength="48" required /><label class="field-label" for="room-description">简介 <span>可选</span></label><input id="room-description" v-model="newRoomDescription" class="plain-input" placeholder="输入房间简介" maxlength="200" /><button class="primary-button modal-submit" type="submit" :disabled="creatingRoom">{{ creatingRoom ? '创建中…' : '创建房间' }} <span>↗</span></button></form></div>

    <div v-if="showInvite" class="modal-backdrop" @click.self="showInvite = false"><div class="modal-card invite-modal"><button class="modal-close" @click="showInvite = false"><X :size="18" /></button><h2>邀请普通用户</h2><p>链接不绑定房间，每位受邀用户可自行选择房间。链接每天凌晨 4 点失效。</p><div class="invite-limit"><label class="field-label" for="invite-limit">可注册人数</label><input id="invite-limit" v-model.number="inviteLimit" class="plain-input" type="number" min="1" max="100" /></div><button class="primary-button modal-submit" @click="createInvite">生成邀请链接 <span>↗</span></button><div v-if="latestInvite" class="generated-invite"><span>{{ latestInvite }}</span><button class="icon-button" title="复制邀请链接" @click="copyInvite"><Copy :size="15" /></button></div><div v-if="inviteRows.length" class="invite-list"><div v-for="row in inviteRows" :key="row.id" class="invite-item"><span><strong>{{ row.uses }}/{{ row.max_uses }}</strong> 已注册 · {{ row.revoked ? '已撤销' : '有效' }}</span><button v-if="!row.revoked" class="text-button" @click="revokeInvite(row.id)">撤销</button></div></div></div></div>

    <div v-if="errorMessage" class="toast-error"><span class="toast-dot"></span>{{ errorMessage }}<button @click="errorMessage = ''"><X :size="14" /></button></div>
    <div id="remote-audio" class="remote-audio" aria-hidden="true"></div>
  </main>
</template>

<style>
.problem-recording-banner{display:flex;align-items:center;gap:8px;padding:10px 24px;background:#252029;border-bottom:1px solid var(--line);font-size:12px;color:var(--soft)}.recording-dot{width:7px;height:7px;border-radius:50%;background:#ff9ba5;flex-shrink:0}
.sidebar-settings{margin-top:auto;padding-top:12px;border-top:1px solid var(--line)}
.call-notice{display:flex;align-items:center;gap:12px;padding:12px 22px;background:#1d2825;border-bottom:1px solid var(--line);color:#bcddca;font-size:12px;flex-shrink:0}.call-notice>span{flex:1}.call-notice>.text-button{white-space:nowrap}.call-notice>button:disabled{opacity:.5;cursor:wait}
.settings-entry{width:100%;height:38px;display:flex;align-items:center;gap:10px;padding:0 9px;border:0;border-radius:8px;background:transparent;color:#aeb1bc;font:inherit;font-size:12px;text-align:left;cursor:pointer}
.settings-entry:hover,.settings-entry.selected{background:#ffffff0a;color:#e9e9ee}
.settings-view{height:calc(100vh - 55px);overflow:auto;padding:31px clamp(20px,4vw,54px) 38px}
.settings-heading{margin-bottom:21px}.settings-heading>.text-button{padding:0;margin-bottom:12px}.settings-heading h1{margin:0;font-size:25px}
.settings-layout{display:grid;grid-template-columns:170px minmax(0,1fr);gap:26px;max-width:1080px}
.settings-nav{display:flex;flex-direction:column;gap:4px}.settings-nav button{border:0;border-radius:8px;background:transparent;color:var(--muted);padding:10px 12px;text-align:left;font:inherit;font-size:12px;cursor:pointer}.settings-nav button:hover,.settings-nav button.selected{background:#ffffff0a;color:#e9e9ee}
.settings-content{min-width:0;padding:23px;border:1px solid var(--line);border-radius:12px;background:#151720}.settings-content h2{margin:0 0 7px;font-size:18px}.settings-content>p,.settings-content>template>p{margin:0 0 20px;color:var(--muted);font-size:12px;line-height:1.7}.settings-content h3{margin:20px 0 8px;font-size:13px}.settings-form{display:flex;flex-direction:column;max-width:430px}.settings-form .field-label{margin-top:14px}.settings-form .primary-button{align-self:flex-start;margin-top:20px}.settings-hint,.settings-notice{color:var(--muted);font-size:11px;line-height:1.7}.settings-notice{margin-top:10px;color:var(--accent)}
.settings-account-list{margin-top:18px;border-top:1px solid var(--line)}.settings-account-row{display:grid;grid-template-columns:minmax(100px,1fr) minmax(110px,1.1fr) minmax(150px,1.3fr) auto;align-items:center;gap:10px;padding:11px 0;border-bottom:1px solid var(--line)}.settings-account-row>.plain-input{height:36px}.settings-account-meta{display:flex;min-width:0;flex-direction:column;gap:4px}.settings-account-meta strong,.settings-account-row>strong{overflow:hidden;text-overflow:ellipsis;font-size:12px}.settings-account-meta small{color:var(--muted);font-size:10px}.settings-account-row>.secondary-button{min-height:34px;padding:0 12px}.settings-account-list>.settings-hint{padding:12px 0}.settings-account-list>.settings-account-row:has(>strong){grid-template-columns:1fr auto}
.settings-invite-limit{max-width:180px;margin-bottom:12px}.settings-admin-invite{margin-top:27px;padding-top:18px;border-top:1px solid var(--line)}.settings-admin-invite>p{color:var(--muted);font-size:11px}
.settings-admin-invite-fields{max-width:280px;margin-bottom:12px}.settings-admin-invite-fields .field-label{display:block;margin-top:12px}
.invite-url{overflow-wrap:anywhere;color:var(--accent)!important}
.generated-invite>span{white-space:normal;overflow-wrap:anywhere}
.invite-type-panel{margin-top:18px;padding:18px;border:1px solid var(--line);border-radius:10px;background:#191b24}.invite-type-panel>h3{margin:0 0 5px;font-size:14px}.invite-type-panel>p{margin:0 0 16px;color:var(--muted);font-size:11px;line-height:1.6}
.invite-create-grid{display:grid;grid-template-columns:minmax(140px,190px) minmax(180px,240px) auto;align-items:end;gap:12px}.invite-field{min-width:0}.invite-field .field-label{display:block;margin:0 0 7px}.invite-field .plain-input{height:42px}.invite-create-button{height:42px;min-width:130px;justify-content:center;align-self:end;white-space:nowrap}
.invite-create-button:disabled{opacity:.6;cursor:wait}
.invite-fixed-expiry{display:flex;align-items:center;color:var(--soft);font-size:12px}
.invite-history{margin-top:17px;border-top:1px solid var(--line)}.invite-history .settings-account-row{grid-template-columns:minmax(0,1fr) auto auto}.invite-history .settings-account-row:last-child{border-bottom:0}
.settings-log-list{overflow:auto}.settings-log-row{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr) minmax(0,1.8fr) 36px;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);font-size:11px;color:#c3c5cd}.settings-log-row>span,.settings-log-row details{min-width:0;overflow-wrap:anywhere}.settings-log-head{color:var(--muted);font-weight:600}
.danger-text{color:#ff9ba5}
.participant-more:disabled{opacity:.4;cursor:default}
.managed-account-row{grid-template-columns:minmax(110px,1fr) minmax(160px,1.4fr) auto}.account-edit-fields{display:flex;flex-direction:column;gap:8px}.account-actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.guest-management-row{grid-template-columns:minmax(0,1fr) auto}.account-actions button:disabled{opacity:.45;cursor:default}
.audit-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:19px 0}.audit-filters label{display:flex;flex-direction:column;gap:6px;color:var(--soft);font-size:11px}.audit-filter-actions{display:flex;gap:14px;align-items:center;grid-column:1/-1}.audit-pagination{display:flex;align-items:center;gap:12px;margin-top:17px;font-size:11px;color:var(--muted)}.audit-pagination>span{margin-right:auto}.audit-detail{padding-top:9px;color:var(--muted);line-height:1.8;overflow-wrap:anywhere}.settings-log-row details>summary{cursor:pointer;line-height:1.7}.settings-log-row .settings-notice{margin-top:0}
@media(max-width:760px){.managed-account-row,.guest-management-row{grid-template-columns:1fr}.audit-filters{grid-template-columns:1fr}.audit-pagination{flex-wrap:wrap}}
@media(max-width:760px){.settings-view{height:calc(100dvh - 55px);padding:24px 16px}.settings-layout{grid-template-columns:1fr;gap:13px}.settings-nav{flex-direction:row;overflow:auto}.settings-nav button{white-space:nowrap}.settings-content{padding:17px}.settings-account-row{grid-template-columns:1fr}.settings-account-row>.plain-input{width:100%}.settings-account-list>.settings-account-row:has(>strong){grid-template-columns:1fr auto}}
@media(max-width:850px){.invite-create-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.invite-create-button{grid-column:1 / -1;justify-self:start}.invite-history .settings-account-row{grid-template-columns:minmax(0,1fr) auto}.invite-history .settings-account-row>.text-button:last-child{grid-column:2}}
</style>
