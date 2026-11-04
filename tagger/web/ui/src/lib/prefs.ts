/**
 * 本地偏好。放 localStorage 而不是后端配置：这些是"这台机器上这个浏览器
 * 看着舒服"的东西，跟服务端跑什么任务无关，也不该跟着配置文件漂移。
 */

export const PROGRESS_INTERVAL_KEY = 'tagger-progress-interval'

/** 0 = 不轮询（底部条整条隐藏） */
export const PROGRESS_INTERVALS = [0, 1000, 2000, 5000, 10000] as const

export const DEFAULT_PROGRESS_INTERVAL_MS = 5000

export function readProgressInterval(): number {
  try {
    const raw = localStorage.getItem(PROGRESS_INTERVAL_KEY)
    if (raw === null) return DEFAULT_PROGRESS_INTERVAL_MS
    const n = Number(raw)
    // 只认白名单里的值：手改坏了（负数、NaN）就退回默认，
    // 否则 setTimeout(负数) 会变成 0，变成疯狂轮询。
    return (PROGRESS_INTERVALS as readonly number[]).includes(n)
      ? n
      : DEFAULT_PROGRESS_INTERVAL_MS
  } catch {
    return DEFAULT_PROGRESS_INTERVAL_MS
  }
}

export function writeProgressInterval(ms: number): void {
  try {
    localStorage.setItem(PROGRESS_INTERVAL_KEY, String(ms))
  } catch {
    // 存不了就只在内存里生效，不影响使用
  }
}
