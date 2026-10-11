// Package off the UI thread. ZIP uses stored entries because audio is already compressed.
const table = new Uint32Array(256)
for (let i = 0; i < 256; i++) {
  let value = i
  for (let j = 0; j < 8; j++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1
  table[i] = value >>> 0
}
const crc32 = (bytes: Uint8Array) => {
  let crc = 0xffffffff
  for (const byte of bytes) crc = table[(crc ^ byte) & 255] ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}
self.onmessage = async ({ data }: MessageEvent<{ files: { name: string; data: Blob }[] }>) => {
  try {
    const chunks: BlobPart[] = []
    const directory: Uint8Array[] = []
    let offset = 0
    const now = new Date()
    const date = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate()
    const time = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1)
    for (const file of data.files) {
      const name = new TextEncoder().encode(file.name)
      const size = file.data.size
      const crc = crc32(new Uint8Array(await file.data.arrayBuffer()))
      const local = new Uint8Array(30 + name.length)
      const l = new DataView(local.buffer)
      l.setUint32(0, 0x04034b50, true); l.setUint16(4, 20, true); l.setUint16(6, 0x800, true)
      l.setUint16(10, time, true); l.setUint16(12, date, true); l.setUint32(14, crc, true)
      l.setUint32(18, size, true); l.setUint32(22, size, true); l.setUint16(26, name.length, true)
      local.set(name, 30)
      chunks.push(local, file.data)
      const central = new Uint8Array(46 + name.length)
      const c = new DataView(central.buffer)
      c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true)
      c.setUint16(8, 0x800, true); c.setUint16(12, time, true); c.setUint16(14, date, true)
      c.setUint32(16, crc, true); c.setUint32(20, size, true); c.setUint32(24, size, true)
      c.setUint16(28, name.length, true); c.setUint32(42, offset, true)
      central.set(name, 46); directory.push(central)
      offset += local.byteLength + size
    }
    const directorySize = directory.reduce((total, entry) => total + entry.byteLength, 0)
    const end = new Uint8Array(22)
    const e = new DataView(end.buffer)
    e.setUint32(0, 0x06054b50, true); e.setUint16(8, directory.length, true); e.setUint16(10, directory.length, true)
    e.setUint32(12, directorySize, true); e.setUint32(16, offset, true)
    self.postMessage({ blob: new Blob([...chunks, ...directory, end], { type: 'application/zip' }) })
  } catch {
    self.postMessage({ error: '问题记录打包失败，请稍后再试。' })
  }
}
