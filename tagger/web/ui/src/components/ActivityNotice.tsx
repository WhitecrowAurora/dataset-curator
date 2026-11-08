import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { CheckCircle2, Copy, TriangleAlert, X } from 'lucide-react'

import { api, type FinishedActivity } from '../lib/api'

/**
 * 任务收尾时的弹窗通知。
 *
 * 为什么需要：打标一跑就是几小时，用户切去干别的，回来只看到底栏空了——
 * 到底跑完了、还是失败了、失败在哪一步，全都没有痕迹。
 *
 * 两条数据来源，因为两类活的形态不同：
 * 1. **打标 / 数据任务**：从底栏的活动列表里"消失"就是收尾了，再拉一次
 *    /api/jobs/{id} 拿真实结局（成功数、失败数、错误文本）。
 * 2. **量化 / 依赖安装**：没有独立任务页，后端在 /api/activities 的
 *    finished 里直接给结局。
 *
 * 去重靠 localStorage：第一次打开界面时，把已存在的历史结局全部记为"看过"，
 * 否则用户一开页面就会被昨天那个任务弹一脸。
 */

const SEEN_KEY = 'tagger.seen.finished'
const SEEN_MAX = 200

type Notice = {
  key: string
  title: string
  ok: boolean
  detail: string
  lines: string[]
  error: string
  href: string
}

function loadSeen(): Set<string> {
  try {
    const raw = localStorage.getItem(SEEN_KEY)
    return new Set(raw ? (JSON.parse(raw) as string[]) : [])
  } catch {
    return new Set()
  }
}

function saveSeen(seen: Set<string>): void {
  try {
    // 只留最近的若干条，避免 localStorage 无限长
    const list = Array.from(seen).slice(-SEEN_MAX)
    localStorage.setItem(SEEN_KEY, JSON.stringify(list))
  } catch {
    /* 隐私模式下写不进去：不致命，最多重复弹一次 */
  }
}

function fmtEta(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`
  return `${(s / 3600).toFixed(1)} 小时`
}

export function ActivityNotice({
  interval = 4000,
  onOpen,
}: {
  interval?: number
  onOpen?: (href: string) => void
}) {
  const [queue, setQueue] = useState<Notice[]>([])
  const [copied, setCopied] = useState(false)
  const seen = useRef<Set<string>>(loadSeen())
  const prevRunning = useRef<Map<string, string> | null>(null)
  const primed = useRef(false)

  useEffect(() => {
    let alive = true
    let timer = 0

    const tick = async () => {
      try {
        const data = await api.activities()
        if (!alive) return
        const running = new Map<string, string>()
        for (const item of data.items) running.set(item.id, item.title)

        // 第一轮只记录现状：此时"在跑的"没有一个是刚结束的。
        if (!primed.current) {
          primed.current = true
          for (const item of data.finished ?? []) seen.current.add(item.id)
          saveSeen(seen.current)
          prevRunning.current = running
          timer = window.setTimeout(tick, interval)
          return
        }

        const fresh: Notice[] = []

        // ---- 1. 从底栏消失 = 结束，去问任务要真实结局
        const before = prevRunning.current ?? new Map<string, string>()
        for (const [id, title] of before) {
          if (running.has(id)) continue
          try {
            const snap = await api.job(id)
            if (!alive) return
            const counts = snap.counts
            const lines = [
              `${counts.done} 张完成${counts.skipped ? ` · ${counts.skipped} 张跳过` : ''}` +
                `${counts.failed ? ` · ${counts.failed} 张失败` : ''}`,
              `用时 ${fmtEta(snap.elapsed_s)}${
                snap.rate_per_s > 0 ? ` · 平均 ${snap.rate_per_s.toFixed(2)} 张/秒` : ''
              }`,
            ]
            fresh.push({
              key: `${id}:${snap.status}`,
              title,
              ok: snap.status === 'done',
              detail: snap.out_dir || '',
              lines,
              error: snap.error || '',
              href: `/run/${id}`,
            })
          } catch {
            /* 任务记录可能已经清掉：不弹，静默 */
          }
        }

        // ---- 2. 量化 / 依赖安装：后端直接给结局
        for (const item of (data.finished ?? []) as FinishedActivity[]) {
          const key = `${item.id}:${item.finished_at ?? ''}`
          if (seen.current.has(key) || seen.current.has(item.id)) continue
          seen.current.add(key)
          seen.current.add(item.id)
          const lines: string[] = []
          if (item.elapsed_s > 0) lines.push(`用时 ${fmtEta(item.elapsed_s)}`)
          if (item.total > 0) lines.push(`${item.done}/${item.total}`)
          fresh.push({
            key,
            title: item.title,
            ok: item.ok,
            detail: item.detail || '',
            lines,
            error: item.error || '',
            href: item.href,
          })
        }
        saveSeen(seen.current)
        prevRunning.current = running

        if (fresh.length) setQueue((q) => [...q, ...fresh])
        timer = window.setTimeout(
          tick,
          data.items.length > 0 ? interval : Math.max(interval, 8000),
        )
      } catch {
        if (alive) timer = window.setTimeout(tick, Math.max(interval, 8000))
      }
    }

    void tick()
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [interval])

  const current = queue[0]
  if (!current) return null

  const close = () => {
    setCopied(false)
    setQueue((q) => q.slice(1))
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(
        [
          `${current.title}：${current.ok ? '完成' : '失败'}`,
          current.detail,
          ...current.lines,
          current.error,
        ]
          .filter(Boolean)
          .join('\n'),
      )
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* 剪贴板不可用（无 https / 无权限）：按钮保持原样，不报错 */
    }
  }

  return createPortal(
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-[12000] flex items-end justify-center p-4 sm:items-center"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onMouseDown={(e) => {
          if (e.target === e.currentTarget) close()
        }}
      >
        <motion.div
          className="w-full max-w-lg rounded-lx-lg border border-lx bg-lx-surface shadow-2xl"
          initial={{ y: 16, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: 8, opacity: 0 }}
        >
          <div className="flex items-start justify-between gap-3 border-b border-lx px-4 py-3">
            <div className="flex items-start gap-2">
              {current.ok ? (
                <CheckCircle2 size={16} className="mt-0.5 text-lx-success" />
              ) : (
                <TriangleAlert size={16} className="mt-0.5 text-lx-danger" />
              )}
              <div>
                <div className="text-sm font-medium text-lx-primary">
                  {current.title}
                  {current.ok ? ' 已结束' : ' 失败'}
                </div>
                {current.detail && (
                  <div className="mt-0.5 break-all text-[11px] text-lx-muted">
                    {current.detail}
                  </div>
                )}
              </div>
            </div>
            <button
              type="button"
              className="lx-btn-icon"
              onClick={close}
              aria-label="关闭"
            >
              <X size={14} />
            </button>
          </div>

          <div className="px-4 py-3">
            {current.lines.length > 0 && (
              <ul className="space-y-1 text-[12px] text-lx-secondary">
                {current.lines.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            )}

            {/* 错误原文照抄，不翻译：翻译过的错误没法搜、也没法贴给别人。 */}
            {current.error && (
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10.5px] leading-relaxed text-lx-danger">
                {current.error}
              </pre>
            )}

            {queue.length > 1 && (
              <p className="mt-2 text-[11px] text-lx-muted">
                还有 {queue.length - 1} 条同样的通知，关掉这条会接着显示。
              </p>
            )}
          </div>

          <div className="flex items-center justify-between gap-2 border-t border-lx px-4 py-2.5">
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px]"
              onClick={() => void copy()}
            >
              <Copy size={11} />
              {copied ? '已复制' : '复制详情'}
            </button>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="lx-btn-ghost px-2.5 py-1 text-[11px]"
                onClick={close}
              >
                知道了
              </button>
              {current.href && (
                <button
                  type="button"
                  className="lx-btn-primary px-2.5 py-1 text-[11px]"
                  onClick={() => {
                    onOpen?.(current.href)
                    close()
                  }}
                >
                  查看详情
                </button>
              )}
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>,
    document.body,
  )
}
