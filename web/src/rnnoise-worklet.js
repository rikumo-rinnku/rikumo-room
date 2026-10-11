// RNNoise 2022.2.0 WASM ABI. One mono state, 48 kHz, 480-sample frames.
// Compile/fetch on the main thread; the audio thread only instantiates and runs.
class RnnoiseProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.disposed = false
    this.failed = false
    this.position = 0
    this.read = 0
    this.write = 480
    this.count = 480
    this.queue = new Float32Array(2048)
    this.frame = new Float32Array(480)
    this.port.onmessage = ({ data }) => { if (data === 'dispose') this.destroy() }
    try {
      if (sampleRate !== 48000) throw new Error('sample-rate')
      const instance = new WebAssembly.Instance(options.processorOptions.module, { env: {
        emscripten_memcpy_big: (destination, source, length) => {
          new Uint8Array(this.wasm.memory.buffer).copyWithin(destination, source, source + length)
          return destination
        },
        emscripten_resize_heap: bytes => {
          const memory = this.wasm.memory
          if (bytes > 64 * 1024 * 1024) return 0
          try { memory.grow(Math.max(0, Math.ceil((bytes - memory.buffer.byteLength) / 65536))); return 1 }
          catch { return 0 }
        },
        __assert_fail: () => { throw new Error('RNNoise assertion failed') },
      } })
      this.wasm = instance.exports
      this.wasm.emscripten_stack_init()
      this.wasm.__wasm_call_ctors()
      if (this.wasm.rnnoise_get_frame_size() !== 480) throw new Error('frame-size')
      this.state = this.wasm.rnnoise_create(0)
      this.input = this.wasm.malloc(480 * 4)
      this.output = this.wasm.malloc(480 * 4)
      if (!this.state || !this.input || !this.output) throw new Error('allocation')
      this.heap = new Float32Array(this.wasm.memory.buffer)
      this.port.postMessage('ready')
    } catch {
      this.failed = true
      this.destroy()
      this.port.postMessage('error')
    }
  }

  destroy() {
    this.disposed = true
    if (this.state) this.wasm.rnnoise_destroy(this.state)
    if (this.input) this.wasm.free(this.input)
    if (this.output) this.wasm.free(this.output)
    this.state = this.input = this.output = 0
  }

  process(inputs, outputs) {
    if (this.disposed || this.failed) return false
    const input = inputs[0]?.[0]
    const output = outputs[0]?.[0]
    if (!output) return true
    try {
      for (let i = 0; i < output.length; i++) {
        output[i] = this.count ? this.queue[this.read] : 0
        if (this.count) { this.read = (this.read + 1) % 2048; this.count-- }
        this.frame[this.position++] = (input?.[i] ?? 0) * 32768
        if (this.position === 480) {
          if (this.heap.buffer !== this.wasm.memory.buffer) this.heap = new Float32Array(this.wasm.memory.buffer)
          this.heap.set(this.frame, this.input / 4)
          this.wasm.rnnoise_process_frame(this.state, this.output, this.input)
          if (this.heap.buffer !== this.wasm.memory.buffer) this.heap = new Float32Array(this.wasm.memory.buffer)
          for (let j = 0; j < 480; j++) {
            const value = this.heap[this.output / 4 + j] / 32768
            if (!Number.isFinite(value)) throw new Error('invalid-output')
            this.queue[this.write] = value
            this.write = (this.write + 1) % 2048
          }
          this.count += 480
          this.position = 0
        }
      }
      return true
    } catch {
      this.failed = true
      this.destroy()
      this.port.postMessage('error')
      return false
    }
  }
}
registerProcessor('rikumo-rnnoise', RnnoiseProcessor)
