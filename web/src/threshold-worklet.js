// Only inserted when the threshold is enabled; no extra microphone capture.
class VoiceThreshold extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.threshold = 10 ** ((options.processorOptions?.threshold ?? -45) / 20)
    this.open = false
    this.hold = 0
    this.gain = 0
    this.meter = false
    this.frames = 0
    this.disposed = false
    this.port.onmessage = ({ data }) => {
      if (data.type === 'threshold' && Number.isFinite(data.value)) this.threshold = 10 ** (Math.max(-70, Math.min(-10, data.value)) / 20)
      if (data.type === 'meter') this.meter = Boolean(data.enabled)
      if (data.type === 'dispose') this.disposed = true
    }
  }

  process(inputs, outputs) {
    if (this.disposed) return false
    const input = inputs[0]
    const output = outputs[0]
    if (!output?.length) return true
    const length = output[0].length
    let power = 0
    for (let channel = 0; channel < (input?.length ?? 0); channel++) {
      let sum = 0
      for (let i = 0; i < length; i++) sum += input[channel][i] * input[channel][i]
      power = Math.max(power, sum / length)
    }
    const rms = Math.sqrt(power)
    // Fast opening, 4 dB hysteresis, 180 ms hold to retain word endings.
    if (rms >= (this.open ? this.threshold * 0.631 : this.threshold)) {
      this.open = true
      this.hold = sampleRate * 0.18
    } else {
      this.hold = Math.max(0, this.hold - length)
      if (!this.hold) this.open = false
    }
    const target = this.open ? 1 : 0
    const nextGain = this.gain + Math.sign(target - this.gain) * Math.min(Math.abs(target - this.gain), length / (sampleRate * (this.open ? 0.005 : 0.06)))
    const step = (nextGain - this.gain) / length
    for (let channel = 0; channel < output.length; channel++) {
      const source = input?.[channel] ?? input?.[0]
      let gain = this.gain
      for (let i = 0; i < length; i++) {
        gain += step
        output[channel][i] = (source?.[i] ?? 0) * gain
      }
    }
    this.gain = nextGain
    // No notifications while settings are hidden. At most 5 updates/second.
    this.frames = this.meter ? this.frames + length : 0
    if (this.meter && this.frames >= sampleRate / 5) {
      this.frames = 0
      this.port.postMessage({ db: Math.max(-90, 20 * Math.log10(Math.max(rms, 0.0000316))), open: this.open })
    }
    return true
  }
}
registerProcessor('rikumo-voice-threshold-v2', VoiceThreshold)
