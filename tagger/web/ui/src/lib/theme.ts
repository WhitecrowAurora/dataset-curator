export type Theme = 'dark' | 'light'

const KEY = 'tagger-theme'

/** 默认深色（与 tokens.css 的 :root 一致）。URL 上的 ?theme= 优先，方便对比与截图。 */
export function readTheme(): Theme {
  const q = new URLSearchParams(location.search).get('theme')
  if (q === 'light' || q === 'dark') return q
  try {
    const v = localStorage.getItem(KEY)
    if (v === 'light' || v === 'dark') return v
  } catch {
    // 隐私模式 / 禁用存储：不因为读不到偏好就白屏
  }
  return 'dark'
}

/** light 走 [data-theme="light"] 覆盖块；dark 落回 :root，不需要额外规则。 */
export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    // 同上，静默即可
  }
}
