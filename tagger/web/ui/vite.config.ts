import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时前端跑 3010，后端由 `tagger web` 起在 27100。
// 走代理而不是 CORS：同源相对路径（/api、/ws）在打包后也能直接用，
// 少一套"开发能跑、构建完 404"的经典故障。
const BACKEND = process.env.TAGGER_API ?? 'http://127.0.0.1:27100'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3010,
    strictPort: true,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND.replace(/^http/, 'ws'), ws: true },
    },
  },
  build: {
    outDir: 'dist',
    // 某些受管环境会给 fs.rm 挂安全删除守卫，清空 dist 时直接抛错导致构建失败。
    // 关掉自动清空，改为覆盖同名产物；要彻底重来就手动删 dist。
    emptyOutDir: false,
    chunkSizeWarningLimit: 900,
  },
})
