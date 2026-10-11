export type RecordingSource = { identity: string; name: string; track: MediaStreamTrack; muted: boolean; local: boolean }
export type RecordingState = { seconds: number; bytes: number; tracks: number }
type Clip = { identity: string; name: string; local: boolean; start: number; end: number; blob: Blob }
type Segment = { recorder: MediaRecorder; start: number; end?: number; done: Promise<void>; finish: () => void; parts: Blob[] }
type Source = { input: MediaStreamTrack; clone: MediaStreamTrack; info: RecordingSource; segment?: Segment }
export type PackageFile = { name: string; data: Blob }
const WINDOW = 180000
const SEGMENT = 30000
const LIMIT = 64 * 1024 * 1024

// Tap existing published/received tracks only. Never change the sending processor or capture another microphone.
export class ProblemRecording {
  readonly startedAt = Date.now()
  private sources = new Map<string, Source>()
  private clips: Clip[] = []
  private issues: { time: number; message: string }[] = []
  private timer: number
  private closed = false
  constructor(private onState: (state: RecordingState) => void, private onFailure: (message: string) => void) {
    if (typeof MediaRecorder === 'undefined') throw new Error('此浏览器不支持问题录音。请使用桌面 Chrome。')
    this.timer = window.setInterval(() => this.tick(), 1000)
  }

  sync(incoming: RecordingSource[]) {
    if (this.closed) return
    const found = new Set<string>()
    for (const info of incoming) {
      if (this.closed) return
      if (info.track.readyState !== 'live') continue
      found.add(info.identity)
      const current = this.sources.get(info.identity)
      if (current?.input === info.track) {
        current.info = info
        current.clone.enabled = !info.muted && info.track.enabled
        continue
      }
      if (current) this.remove(info.identity)
      const source: Source = { input: info.track, clone: info.track.clone(), info }
      source.clone.enabled = !info.muted && info.track.enabled
      this.sources.set(info.identity, source)
      this.begin(source)
    }
    for (const identity of this.sources.keys()) if (!found.has(identity)) this.remove(identity)
    this.tick()
  }

  private issue(message: string) {
    this.issues.push({ time: Date.now(), message })
    if (this.issues.length > 200) this.issues.shift()
  }

  private begin(source: Source) {
    if (this.closed) return
    try {
      const mimeType = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(value => MediaRecorder.isTypeSupported(value))
      if (!mimeType) throw new Error('unsupported-format')
      const recorder = new MediaRecorder(new MediaStream([source.clone]), { mimeType, audioBitsPerSecond: 48000 })
      let finish!: () => void
      const done = new Promise<void>(resolve => { finish = resolve })
      const segment: Segment = { recorder, start: Date.now(), done, finish, parts: [] }
      source.segment = segment
      recorder.ondataavailable = ({ data }) => { if (data.size && !this.closed) segment.parts.push(data) }
      recorder.onerror = () => {
        this.issue(`${source.info.name} 的录音出现错误`)
        this.onFailure('录音出现错误，问题缓存已停止；正常通话不受此操作影响。')
        this.stop()
        finish()
      }
      recorder.onstop = () => {
        if (!this.closed && segment.parts.length) {
          this.clips.push({ identity: source.info.identity, name: source.info.name, local: source.info.local,
            start: segment.start, end: segment.end ?? Date.now(), blob: new Blob(segment.parts, { type: mimeType }) })
          this.prune()
        }
        segment.parts = []
        finish()
      }
      // Complete recording containers per segment, not arbitrary unplayable timeslice fragments.
      recorder.start()
      source.segment = segment
    } catch {
      this.onFailure('无法开始问题录音，已停止缓存。请使用桌面 Chrome。')
      this.stop()
    }
  }

  private closeSegment(source: Source, at = Date.now()) {
    const segment = source.segment
    source.segment = undefined
    if (!segment) return Promise.resolve()
    segment.end = at
    if (segment.recorder.state !== 'inactive') segment.recorder.stop()
    else segment.finish()
    return segment.done
  }

  private rotate(source: Source, at: number) {
    const old = source.segment
    source.segment = undefined
    // Start the successor before stopping its predecessor to avoid a deliberate gap.
    this.begin(source)
    if (!old) return Promise.resolve()
    old.end = at
    if (old.recorder.state !== 'inactive') old.recorder.stop()
    else old.finish()
    return old.done
  }

  private remove(identity: string) {
    const source = this.sources.get(identity)
    if (!source) return
    this.sources.delete(identity)
    void this.closeSegment(source)
    source.clone.stop()
  }

  private prune() {
    const cutoff = Date.now() - WINDOW
    this.clips = this.clips.filter(clip => clip.end > cutoff)
    const bytes = this.clips.reduce((total, clip) => total + clip.blob.size, 0)
    if (bytes > LIMIT) {
      this.onFailure('问题缓存超过 64 MB，已停止并清空；请缩小测试房间后重新开启。')
      this.stop()
    }
  }

  private tick() {
    if (this.closed) return
    const now = Date.now()
    for (const source of this.sources.values()) {
      if (this.closed) return
      source.clone.enabled = !source.info.muted && source.input.enabled
      if (source.input.readyState !== 'live') { this.remove(source.info.identity); continue }
      if (source.segment && now - source.segment.start >= SEGMENT) void this.rotate(source, now)
    }
    this.prune()
    if (!this.closed) this.onState({ seconds: Math.min(180, Math.floor((now - this.startedAt) / 1000)),
      bytes: this.clips.reduce((total, clip) => total + clip.blob.size, 0), tracks: this.sources.size })
  }

  async snapshot(at: number) {
    if (this.closed) throw new Error('问题记录已停止。')
    let timeout: number | undefined
    try {
      await Promise.race([
        Promise.all([...this.sources.values()].map(source => this.rotate(source, at))),
        new Promise<never>((_, reject) => { timeout = window.setTimeout(() => reject(new Error('录音片段保存超时，请重试。')), 10000) }),
      ])
    } finally { window.clearTimeout(timeout) }
    if (this.closed) throw new Error('缓存已停止，未导出录音。')
    const cutoff = Math.max(this.startedAt, at - WINDOW)
    const selected = this.clips.filter(clip => clip.end > cutoff && clip.start < at && clip.end <= at)
    if (!selected.length) throw new Error('尚无可保存的语音，请开麦通话后重试。')
    const files: PackageFile[] = []
    const manifest: Record<string, unknown>[] = []
    const notes = this.issues.filter(issue => issue.time >= cutoff && issue.time <= at)
    const identities = [...new Set(selected.map(clip => clip.identity))]
    for (const [member, identity] of identities.entries()) {
      const clips = selected.filter(clip => clip.identity === identity).sort((a, b) => a.start - b.start)
      for (const [index, clip] of clips.entries()) {
        let blob = clip.blob
        let start = clip.start
        let extension = blob.type.includes('ogg') ? 'ogg' : blob.type.includes('mp4') ? 'm4a' : 'webm'
        if (start < cutoff) {
          // Decode only the boundary segment. Do not export audio preceding the requested window.
          try {
            blob = await trimAudio(blob, (cutoff - start) / 1000)
            start = cutoff
            extension = 'wav'
          } catch {
            notes.push({ time: at, message: `${clip.name} 的窗口边界片段无法裁剪，已跳过该片段` })
            continue
          }
        }
        const name = `audio/member-${member + 1}/segment-${index + 1}.${extension}`
        files.push({ name, data: blob })
        manifest.push({ file: name, identity, name: clip.name, local: clip.local, start, end: clip.end,
          offsetSeconds: (start - cutoff) / 1000, mimeType: blob.type })
        // Yield between members/segments; packaging itself runs in a Worker.
        await new Promise(resolve => window.setTimeout(resolve, 0))
      }
    }
    if (!files.length) throw new Error('没有可播放的录音片段，请重试。')
    return { files, manifest, notes, start: cutoff, end: at }
  }

  stop() {
    if (this.closed) return
    this.closed = true
    window.clearInterval(this.timer)
    for (const source of this.sources.values()) {
      void this.closeSegment(source)
      source.clone.stop()
    }
    this.sources.clear()
    this.clips = []
    this.issues = []
  }
}

async function trimAudio(blob: Blob, skipSeconds: number) {
  const decoder = new OfflineAudioContext(1, 1, 48000)
  const audio = await decoder.decodeAudioData(await blob.arrayBuffer())
  const first = Math.min(audio.length, Math.ceil(skipSeconds * audio.sampleRate))
  const length = audio.length - first
  if (!length) throw new Error('empty-boundary')
  const bytes = new ArrayBuffer(44 + length * 2)
  const view = new DataView(bytes)
  const word = (offset: number, value: string) => { for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i)) }
  word(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); word(8, 'WAVE'); word(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, audio.sampleRate, true); view.setUint32(28, audio.sampleRate * 2, true)
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); word(36, 'data'); view.setUint32(40, length * 2, true)
  // Export mono, averaging channels only in the saved boundary file.
  for (let i = 0; i < length; i++) {
    let value = 0
    for (let channel = 0; channel < audio.numberOfChannels; channel++) value += audio.getChannelData(channel)[first + i]
    value = Math.max(-1, Math.min(1, value / audio.numberOfChannels))
    view.setInt16(44 + i * 2, value < 0 ? value * 32768 : value * 32767, true)
  }
  return new Blob([bytes], { type: 'audio/wav' })
}

export function zipProblemFiles(files: PackageFile[]) {
  return new Promise<Blob>((resolve, reject) => {
    const worker = new Worker(new URL('./problem-zip-worker.ts', import.meta.url), { type: 'module' })
    const timeout = window.setTimeout(() => finish(undefined, '录音打包超时，请重试。'), 60000)
    const finish = (blob?: Blob, error?: string) => {
      window.clearTimeout(timeout); worker.terminate()
      if (blob) resolve(blob); else reject(new Error(error || '录音打包失败。'))
    }
    worker.onmessage = ({ data }) => finish(data.blob, data.error)
    worker.onerror = () => finish(undefined, '录音打包失败。')
    worker.postMessage({ files })
  })
}

export function recordingPlayer(manifest: Record<string, unknown>[]) {
  const data = JSON.stringify(manifest).replace(/</g, '\\u003c')
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>语音问题记录</title>
<style>body{background:#101118;color:#ddd;font:14px system-ui;padding:24px;max-width:900px;margin:auto}section{background:#1b1e27;border:1px solid #303440;border-radius:12px;padding:16px;margin:16px 0}h2{font-size:16px;color:#a9edc1}audio{display:block;width:100%;margin:8px 0 16px}small{color:#9b9eaa}</style>
<h1>语音问题记录</h1><p>解压后打开本页面，按成员回听片段。时间为保存窗口内的位置；缺失或无法录制的部分请查看 manifest.json。</p><main id="clips"></main>
<script>const clips=${data};let current;const groups=new Map();for(const clip of clips){let section=groups.get(clip.identity);if(!section){section=document.createElement('section');let h=document.createElement('h2');h.textContent=clip.name+(clip.local?'（记录者发送轨道）':'（记录者接收轨道）');section.append(h);document.getElementById('clips').append(section);groups.set(clip.identity,section)}const label=document.createElement('small');label.textContent='窗口内 '+clip.offsetSeconds.toFixed(1)+' 秒 · '+new Date(clip.start).toLocaleTimeString();const audio=document.createElement('audio');audio.controls=true;audio.preload='none';audio.src=clip.file;audio.onplay=()=>{if(current&&current!==audio)current.pause();current=audio};section.append(label,audio)}</script></html>`
}
