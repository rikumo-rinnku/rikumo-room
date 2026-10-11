import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { existsSync, readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const tlsDir = fileURLToPath(new URL('../.dev/tls/', import.meta.url))
const httpsEnabled = existsSync(resolve(tlsDir, 'server.crt')) && existsSync(resolve(tlsDir, 'server.key'))

export default defineConfig({
  plugins: [vue(), {
    name: 'local-test-certificate',
    configureServer(server) {
      if (!httpsEnabled) return
      // Public CA certificate only. Private keys never leave .dev/tls.
      const download = createServer((request, response) => {
        if (request.url === '/rikumo-test-ca.cer') {
          response.writeHead(200, { 'Content-Type': 'application/x-x509-ca-cert', 'Content-Disposition': 'attachment; filename="rikumo-test-ca.cer"' })
          response.end(readFileSync(resolve(tlsDir, 'ca.cer')))
        } else {
          response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
          response.end('<meta name="viewport" content="width=device-width"><h2>本地语音测试</h2><p><a href="/rikumo-test-ca.cer">下载本地测试证书</a></p><p>安卓：在系统设置中搜索“安装证书”，选择 CA 证书，安装下载的文件。随后使用 HTTPS 地址访问项目。</p>')
        }
      })
      download.listen(5174, '0.0.0.0')
      download.on('error', (error) => server.config.logger.error(`Certificate download server: ${error.message}`))
      server.httpServer?.once('close', () => download.close())
    },
  }],
  server: {
    https: httpsEnabled ? { key: readFileSync(resolve(tlsDir, 'server.key')), cert: readFileSync(resolve(tlsDir, 'server.crt')) } : undefined,
    proxy: {
      '/api': 'http://localhost:8000',
      '/livekit': { target: 'http://localhost:7880', ws: true, rewrite: (path) => path.replace(/^\/livekit/, '') },
    },
  },
})
