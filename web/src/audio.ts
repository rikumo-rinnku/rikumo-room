import type { AudioProcessorOptions, Track, TrackProcessor } from 'livekit-client'
import thresholdModuleUrl from './threshold-worklet.js?url&no-inline'
import rnnoiseModuleUrl from './rnnoise-worklet.js?url&no-inline'
import rnnoiseWasmUrl from '@shiguredo/rnnoise-wasm/dist/rnnoise.wasm?url&no-inline'
import rnnoiseSimdUrl from '@shiguredo/rnnoise-wasm/dist/rnnoise_simd.wasm?url&no-inline'

type ThresholdOptions = { enabled: boolean; value: number; meter: boolean;
  onLevel: (db: number | null, open: boolean) => void; onFailure: (message: string) => void }
const thresholdModules = new WeakMap<AudioContext, Promise<void>>()
const rnnoiseModules = new WeakMap<AudioContext, Promise<void>>()
let rnnoiseBinary: Promise<WebAssembly.Module> | undefined
function loadRnnoiseBinary() {
  if (!rnnoiseBinary) {
    rnnoiseBinary = (async () => {
      const simd = WebAssembly.validate(new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,123,3,2,1,0,10,10,1,8,0,65,0,253,15,253,98,11]))
      const response = await fetch(simd ? rnnoiseSimdUrl : rnnoiseWasmUrl, { signal: AbortSignal.timeout(15000) })
      if (!response.ok) throw new Error('RNNoise 模型下载失败。')
      return WebAssembly.compile(await response.arrayBuffer())
    })()
    void rnnoiseBinary.catch(() => { rnnoiseBinary = undefined })
  }
  return rnnoiseBinary
}
type DenoiseOptions = { enabled: boolean; onFailure: (message: string) => void }

// A gain node processes outgoing audio without playing the microphone locally.
export class MicrophoneGainProcessor implements TrackProcessor<Track.Kind.Audio, AudioProcessorOptions> {
  name = 'microphone-volume'
  processedTrack?: MediaStreamTrack
  private source?: MediaStreamAudioSourceNode
  private captureTrack?: MediaStreamTrack
  get captureIsLive() { return this.captureTrack?.readyState === 'live' }
  private gainEnabled = true
  private gain?: GainNode
  private destination?: MediaStreamAudioDestinationNode
  private context?: AudioContext
  private gate?: AudioWorkletNode
  private gateRevision = 0
  private denoise?: AudioWorkletNode
  private denoiseRevision = 0
  private cancelDenoiseReady?: () => void
  constructor(private volume: number, private diagnosticEvent?: (action: string) => void,
    private threshold?: ThresholdOptions, private denoiseOptions?: DenoiseOptions) {}

  private connectChain() {
    if (!this.source || !this.gain) return
    this.source.disconnect()
    this.denoise?.disconnect()
    this.gate?.disconnect()
    let tail: AudioNode = this.source
    if (this.denoise) tail = tail.connect(this.denoise)
    if (this.gate) tail = tail.connect(this.gate)
    tail.connect(this.gain)
  }

  private removeDenoise() {
    this.cancelDenoiseReady?.()
    this.cancelDenoiseReady = undefined
    const node = this.denoise
    this.denoise = undefined
    if (node) {
      node.onprocessorerror = null
      node.port.onmessage = null
      node.port.postMessage('dispose')
      node.disconnect()
      node.port.close()
      this.connectChain()
    }
  }

  async setRnnoiseEnabled(enabled: boolean) {
    if (!this.denoiseOptions) return
    this.denoiseOptions.enabled = enabled
    const revision = ++this.denoiseRevision
    if (!enabled) { this.removeDenoise(); this.diagnosticEvent?.('RNNoise 已关闭，已绕过处理节点'); return }
    const context = this.context
    if (!context || !this.source || this.denoise) return
    try {
      if (context.sampleRate !== 48000 || !context.audioWorklet) throw new Error('unsupported')
      let loading = rnnoiseModules.get(context)
      if (!loading) {
        loading = context.audioWorklet.addModule(rnnoiseModuleUrl)
        rnnoiseModules.set(context, loading)
        void loading.catch(() => { rnnoiseModules.delete(context) })
      }
      const [, module] = await Promise.all([loading, loadRnnoiseBinary()])
      if (revision !== this.denoiseRevision || context !== this.context || !this.source) return
      const node = new AudioWorkletNode(context, 'rikumo-rnnoise', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        channelCount: 1, channelCountMode: 'explicit', processorOptions: { module },
      })
      // Leave the original audio path connected until WASM state is ready.
      this.denoise = node
      await new Promise<void>((resolve, reject) => {
        const timeout = window.setTimeout(() => finish(new Error('initialization-timeout')), 5000)
        const finish = (error?: Error) => {
          window.clearTimeout(timeout)
          this.cancelDenoiseReady = undefined
          node.port.onmessage = null
          node.onprocessorerror = null
          error ? reject(error) : resolve()
        }
        this.cancelDenoiseReady = () => finish(new Error('cancelled'))
        node.port.onmessage = ({ data }) => { if (data === 'ready') finish(); else if (data === 'error') finish(new Error('initialization')) }
        node.onprocessorerror = () => finish(new Error('processor'))
      })
      if (revision !== this.denoiseRevision || context !== this.context) return
      const fail = () => {
        if (this.denoise !== node) return
        ++this.denoiseRevision
        this.removeDenoise()
        this.denoiseOptions!.enabled = false
        const message = 'RNNoise 处理异常，已关闭并恢复原收音路径。'
        this.diagnosticEvent?.(message)
        this.denoiseOptions!.onFailure(message)
      }
      node.onprocessorerror = fail
      node.port.onmessage = ({ data }) => { if (data === 'error') fail() }
      this.connectChain()
      this.diagnosticEvent?.('RNNoise 已启用（48 kHz，单声道）')
    } catch {
      if (revision !== this.denoiseRevision) return
      this.removeDenoise()
      this.denoiseOptions.enabled = false
      const message = 'RNNoise 无法启用，已保留原收音路径。请检查浏览器支持与模型加载。'
      this.diagnosticEvent?.(message)
      this.denoiseOptions.onFailure(message)
    }
  }

  setThreshold(value: number) {
    if (!this.threshold) return
    this.threshold.value = Math.max(-70, Math.min(-10, value))
    this.gate?.port.postMessage({ type: 'threshold', value: this.threshold.value })
  }

  setMeterVisible(visible: boolean) {
    if (!this.threshold) return
    this.threshold.meter = visible
    this.gate?.port.postMessage({ type: 'meter', enabled: visible })
    if (!visible) this.threshold.onLevel(null, false)
  }

  private removeGate() {
    const gate = this.gate
    this.gate = undefined
    if (gate) {
      gate.onprocessorerror = null
      gate.port.onmessage = null
      gate.port.postMessage({ type: 'dispose' })
      gate.disconnect()
      gate.port.close()
      this.connectChain()
    }
    this.threshold?.onLevel(null, false)
  }

  async setThresholdEnabled(enabled: boolean) {
    if (!this.threshold) return
    this.threshold.enabled = enabled
    const revision = ++this.gateRevision
    if (!enabled) {
      this.removeGate()
      this.diagnosticEvent?.('收音阈值关闭，已绕过处理节点')
      return
    }
    const context = this.context
    if (!context || !this.source || !this.gain || this.gate) return
    try {
      if (!context.audioWorklet) throw new Error('收音阈值需要浏览器音频处理支持。')
      let loading = thresholdModules.get(context)
      if (!loading) {
        loading = context.audioWorklet.addModule(thresholdModuleUrl)
        thresholdModules.set(context, loading)
        void loading.catch(() => { thresholdModules.delete(context) })
      }
      await loading
      if (revision !== this.gateRevision || this.context !== context || !this.source || !this.gain) return
      const gate = new AudioWorkletNode(context, 'rikumo-voice-threshold-v2', {
        numberOfInputs: 1, numberOfOutputs: 1,
        processorOptions: { threshold: this.threshold.value },
      })
      this.gate = gate
      gate.port.onmessage = ({ data }) => {
        if (this.gate !== gate || !this.threshold?.meter || !Number.isFinite(data?.db)) return
        this.threshold.onLevel(Math.max(-90, Math.min(0, data.db)), Boolean(data.open))
      }
      gate.onprocessorerror = () => {
        if (this.gate !== gate) return
        ++this.gateRevision
        this.removeGate()
        this.threshold!.enabled = false
        const message = '收音阈值处理异常，已关闭阈值并恢复普通收音。'
        this.diagnosticEvent?.(message)
        this.threshold!.onFailure(message)
      }
      gate.port.postMessage({ type: 'meter', enabled: this.threshold.meter })
      // No parallel audio path: replace the direct connection only after loading.
      this.connectChain()
      this.diagnosticEvent?.('收音阈值处理已启用')
    } catch {
      if (revision !== this.gateRevision) return
      this.removeGate()
      this.threshold.enabled = false
      const message = '收音阈值不可用，已恢复普通收音。'
      this.diagnosticEvent?.(message)
      this.threshold.onFailure(message)
    }
  }

  setGainEnabled(enabled: boolean) {
    this.gainEnabled = enabled
    this.setVolume(this.volume)
  }

  setVolume(volume: number) {
    this.volume = Math.max(0, Math.min(200, volume))
    if (this.gain && this.context) this.gain.gain.setTargetAtTime((this.gainEnabled ? this.volume / 100 : 1), this.context.currentTime, 0.015)
  }

  async init(options: AudioProcessorOptions) {
    this.context = options.audioContext
    if (!this.context || this.context.state === 'closed') {
      throw new Error('音频处理尚未就绪，请重新加入房间。')
    }
    this.source = this.context.createMediaStreamSource(new MediaStream([options.track]))
    this.captureTrack = options.track
    this.gain = this.context.createGain()
    this.gain.gain.value = this.gainEnabled ? this.volume / 100 : 1
    this.destination = this.context.createMediaStreamDestination()
    this.source.connect(this.gain).connect(this.destination)
    this.processedTrack = this.destination.stream.getAudioTracks()[0]
    this.diagnosticEvent?.('麦克风处理器初始化')
    if (this.denoiseOptions?.enabled) await this.setRnnoiseEnabled(true)
    if (this.threshold?.enabled) await this.setThresholdEnabled(true)
  }

  async restart(options: AudioProcessorOptions) {
    // LiveKit's capture restart supplies the new source without audioContext.
    // Keep the output track and gain alive while replacing only the microphone.
    if (!this.context || this.context.state === 'closed' || !this.gain || !this.destination) {
      throw new Error('音频处理尚未就绪，请重新加入房间。')
    }
    const nextSource = this.context.createMediaStreamSource(new MediaStream([options.track]))
    this.source?.disconnect()
    this.source = nextSource
    this.captureTrack = options.track
    this.connectChain()
    this.diagnosticEvent?.('麦克风采集源已替换')
  }

  async destroy() {
    ++this.gateRevision
    ++this.denoiseRevision
    this.removeDenoise()
    this.removeGate()
    this.diagnosticEvent?.('麦克风处理器释放')
    this.source?.disconnect()
    this.gain?.disconnect()
    this.processedTrack?.stop()
    this.source = undefined
    this.captureTrack = undefined
    this.gain = undefined
    this.destination = undefined
    this.processedTrack = undefined
    this.context = undefined
  }
}

export async function playAudioCue(context: AudioContext, enabled: boolean, listening: boolean) {
  await context.resume()
  const start = context.currentTime
  const gain = context.createGain()
  gain.gain.setValueAtTime(0, start)
  gain.gain.linearRampToValueAtTime(0.045, start + 0.012)
  gain.gain.exponentialRampToValueAtTime(0.001, start + 0.17)
  gain.connect(context.destination)
  const oscillator = context.createOscillator()
  oscillator.type = 'sine'
  const low = listening ? 480 : 620
  const high = listening ? 720 : 930
  oscillator.frequency.setValueAtTime(enabled ? low : high, start)
  oscillator.frequency.setValueAtTime(enabled ? high : low, start + 0.075)
  oscillator.connect(gain)
  oscillator.onended = () => { oscillator.disconnect(); gain.disconnect() }
  oscillator.start(start)
  oscillator.stop(start + 0.18)
}
