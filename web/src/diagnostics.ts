import type { Room, LocalAudioTrack } from 'livekit-client'

export type DiagnosticRow = {
  member: string; direction: string; rttMs: number | null; jitterMs: number | null;
  lossPercent: number | null; bitrateKbps: number | null; concealPercent: number | null;
  silentConcealPercent: number | null; accelerationPercent: number | null; decelerationPercent: number | null;
  bufferDelayMs: number | null; packetsDiscarded: number | null;
}
export type DiagnosticSnapshot = {
  time: string; state: string; context: string; microphone: Record<string, unknown> | null; rows: DiagnosticRow[];
}
type Counter = { timestamp: number; packets: number; lost: number; bytes: number; concealed: number; samples: number;
  silent: number; accelerated: number; decelerated: number; bufferDelay: number; bufferCount: number; discarded: number }
const number = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null
const ms = (value: unknown) => number(value) === null ? null : (value as number) * 1000
const ratio = (part: number, total: number) => total > 0 && part >= 0 ? Math.min(100, part / total * 100) : null

// Read-only statistics. Never connect audio nodes, change capture or store raw RTC reports.
export class CallDiagnostics {
  snapshots: DiagnosticSnapshot[] = []
  events: { time: string; action: string }[] = []
  private timer?: ReturnType<typeof setTimeout>
  private generation = 0
  private previous = new Map<string, Counter>()
  constructor(private update: (snapshot: DiagnosticSnapshot) => void) {}

  event(action: string) {
    this.events.push({ time: new Date().toISOString(), action })
    if (this.events.length > 200) this.events.shift()
  }

  start(room: Room, context: () => AudioContext | undefined, preferences: () => Record<string, unknown>) {
    this.stop()
    this.previous.clear()
    const generation = this.generation
    this.event('开始连接房间')
    const poll = async () => {
      if (generation !== this.generation) return
      const rows: DiagnosticRow[] = []
      let microphone: Record<string, unknown> | null = null
      const tracks: { member: string; direction: string; track: { getRTCStatsReport(): Promise<RTCStatsReport | undefined> } }[] = []
      room.localParticipant.audioTrackPublications.forEach(publication => {
        if (!publication.track) return
        const track = publication.track as LocalAudioTrack
        const settings = track.getSourceTrackSettings()
        microphone = {
          device: track.mediaStreamTrack.label, muted: publication.isMuted,
          sampleRate: settings.sampleRate ?? null, channelCount: settings.channelCount ?? null,
          noiseSuppression: settings.noiseSuppression ?? null, echoCancellation: settings.echoCancellation ?? null,
          autoGainControl: settings.autoGainControl ?? null, ...preferences(),
        }
        tracks.push({ member: '自己', direction: '发送', track })
      })
      room.remoteParticipants.forEach(participant => participant.audioTrackPublications.forEach(publication => {
        if (publication.track) tracks.push({ member: participant.name || '成员', direction: '接收', track: publication.track })
      }))
      const results = await Promise.allSettled(tracks.map(async item => ({ item, report: await item.track.getRTCStatsReport() })))
      if (generation !== this.generation) return
      const current = new Map<string, Counter>()
      for (const result of results) {
        if (result.status !== 'fulfilled') continue
        const { item, report } = result.value
        if (!report) continue
        report.forEach(stat => {
          const type = item.direction === '发送' ? 'outbound-rtp' : 'inbound-rtp'
          if (stat.type !== type || (stat.kind ?? stat.mediaType) !== 'audio') return
          const counter: Counter = {
            timestamp: stat.timestamp, packets: stat.packetsReceived ?? stat.packetsSent ?? 0,
            lost: stat.packetsLost ?? 0, bytes: stat.bytesReceived ?? stat.bytesSent ?? 0,
            concealed: stat.concealedSamples ?? 0, samples: stat.totalSamplesReceived ?? 0,
            silent: stat.silentConcealedSamples ?? 0, accelerated: stat.removedSamplesForAcceleration ?? 0,
            decelerated: stat.insertedSamplesForDeceleration ?? 0, bufferDelay: stat.jitterBufferDelay ?? 0,
            bufferCount: stat.jitterBufferEmittedCount ?? 0, discarded: stat.packetsDiscarded ?? 0,
          }
          const key = `${item.direction}:${stat.id}`
          const prev = this.previous.get(key)
          current.set(key, counter)
          const valid = prev && counter.timestamp > prev.timestamp && counter.packets >= prev.packets && counter.bytes >= prev.bytes
          const remote = stat.remoteId ? report.get(stat.remoteId) : undefined
          const transport = stat.transportId ? report.get(stat.transportId) : undefined
          const pair = transport?.selectedCandidatePairId ? report.get(transport.selectedCandidatePairId) : undefined
          rows.push({
            member: item.member, direction: item.direction,
            rttMs: ms(remote?.roundTripTime ?? pair?.currentRoundTripTime),
            jitterMs: ms(stat.jitter ?? remote?.jitter),
            // Sending-side receiver reports are cumulative, not local sent-packet deltas.
            lossPercent: item.direction === '发送' ? (number(remote?.fractionLost) !== null ? Math.max(0, Math.min(100, remote.fractionLost * 100)) : null)
              : valid ? ratio(counter.lost - prev.lost, counter.packets - prev.packets + counter.lost - prev.lost) : null,
            bitrateKbps: valid ? (counter.bytes - prev.bytes) * 8 / (counter.timestamp - prev.timestamp) : null,
            concealPercent: item.direction === '接收' && valid && number(stat.concealedSamples) !== null && number(stat.totalSamplesReceived) !== null
              ? ratio(counter.concealed - prev.concealed, counter.samples - prev.samples) : null,
            silentConcealPercent: item.direction === '接收' && valid && number(stat.silentConcealedSamples) !== null
              ? ratio(counter.silent - prev.silent, counter.samples - prev.samples) : null,
            accelerationPercent: item.direction === '接收' && valid && number(stat.removedSamplesForAcceleration) !== null
              ? ratio(counter.accelerated - prev.accelerated, counter.samples - prev.samples) : null,
            decelerationPercent: item.direction === '接收' && valid && number(stat.insertedSamplesForDeceleration) !== null
              ? ratio(counter.decelerated - prev.decelerated, counter.samples - prev.samples) : null,
            bufferDelayMs: valid && number(stat.jitterBufferDelay) !== null && counter.bufferCount > prev.bufferCount
              ? (counter.bufferDelay - prev.bufferDelay) / (counter.bufferCount - prev.bufferCount) * 1000 : null,
            packetsDiscarded: valid && number(stat.packetsDiscarded) !== null ? Math.max(0, counter.discarded - prev.discarded) : null,
          })
        })
      }
      this.previous = current
      const snapshot = { time: new Date().toISOString(), state: room.state, context: context()?.state ?? '未创建', microphone, rows }
      this.snapshots.push(snapshot)
      if (this.snapshots.length > 360) this.snapshots.shift()
      this.update(snapshot)
      this.timer = setTimeout(() => { void poll().catch(() => { this.event('诊断采集不可用'); this.stop() }) }, 5000)
    }
    void poll().catch(() => { this.event('诊断采集不可用'); this.stop() })
  }

  stop() { ++this.generation; clearTimeout(this.timer); this.timer = undefined; this.previous.clear() }

  download() {
    const content = { format: 2, build: 'problem-recording-v1-2026-10-07', exportedAt: new Date().toISOString(),
      browser: navigator.userAgent, note: 'RTT为网络往返时间，不是端到端语音延迟。接收丢包与补偿为采样区间比例；发送丢包为远端报告 fractionLost，没有数据时为空。空值代表暂无数据。',
      events: this.events, snapshots: this.snapshots }
    const url = URL.createObjectURL(new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url; link.download = `通话诊断-${Date.now()}.json`; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}
