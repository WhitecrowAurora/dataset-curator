import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// 只打包等宽字体（数字与路径要等宽对齐）；中文走系统字体，省掉十几兆切片。
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import './index.css'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
