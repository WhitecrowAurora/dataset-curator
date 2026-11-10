/**
 * 离线包的分片管理：看哪片已经打过，勾选这次只打哪几片。
 *
 * 为什么不是再接一个"选文件"弹窗：离线包动辄几十个分片，用户真正要判断的是
 * 「哪些还没打」。所以状态和勾选放在同一张表里——勾之前就看得到每片已经产出
 * 多少行，不用先跑一遍再回来对账。
 *
 * 进度从产物文件名反解（`artifacts-<包名>-…`），所以只对 jsonl 产物成立。
 * 产物是 parquet 形态时后端给 attributable=false，这里把话说清楚，而不是把
 * 整列显示成 0 让人以为一片都没打过。
 */
import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { CheckCheck, Loader2, RefreshCw, X } from 'lucide-react'
import { api, type PackShard, type PackShardScan } from '../lib/api'
import { StatusChip } from './ui/launcher'

function fmtBytes(n: number): string {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

const STATE_TEXT: Record<PackShard['state'], string> = {
  done: '已完成',
  partial: '打了一部分',
  none: '未开始',
}

const STATE_TONE: Record<PackShard['state'], 'success' | 'warning' | 'neutral'> = {
  done: 'success',
  partial: 'warning',
  none: 'neutral',
}

function progressText(s: PackShard): string {
  if (s.total_rows != null) return `${s.done_rows} / ${s.total_rows} 行`
  return s.done_rows > 0 ? `${s.done_rows} 行` : '—'
}

export function PackShards({
  open,
  packDir,
  packLabel,
  outDir,
  selected,
  onClose,
  onConfirm,
}: {
  open: boolean
  packDir: string
  packLabel: string
  /** 产物目录：分片状态是从这里的产物反解出来的 */
  outDir: string
  /** 已勾选的分片绝对路径（外部状态，打开时回填） */
  selected: string[]
  onClose: () => void
  onConfirm: (paths: string[]) => void
}) {
  const [scan, setScan] = useState<PackShardScan | null>(null)
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    if (!packDir.trim()) return
    setBusy(true)
    setError(null)
    try {
      setScan(await api.packShards(packDir, outDir))
    } catch (err) {
      setScan(null)
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  // 每次打开都重新读一次：刚跑完一片再打开，状态就该是新的。
  useEffect(() => {
    if (!open) return
    setChecked(new Set(selected))
    setScan(null)
    setError(null)
    void load()
    // 只在"打开"这一刻拉一次，之后由刷新按钮触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const shards = scan?.shards ?? []
  const totalBytes = useMemo(
    () => shards.filter((s) => checked.has(s.path)).reduce((n, s) => n + s.bytes, 0),
    [shards, checked],
  )

  const toggle = (path: string) =>
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  const pickUnfinished = () =>
    setChecked(new Set(shards.filter((s) => s.state !== 'done').map((s) => s.path)))

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[10000] flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) onClose()
          }}
          role="dialog"
          aria-modal="true"
          aria-label="离线包分片"
        >
          <div
            className="absolute inset-0"
            style={{
              backgroundColor: 'color-mix(in srgb, var(--lx-bg) 62%, transparent)',
              backdropFilter: 'blur(3px)',
            }}
          />
          <motion.div
            className="lx-sheet relative flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden"
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 4 }}
            transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          >
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-lx px-[18px] py-3">
              <div className="min-w-0">
                <h3 className="truncate text-[15px] font-semibold text-lx">分片管理</h3>
                <p className="mt-0.5 truncate text-[11px] text-lx-muted">
                  {packLabel || packDir}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <button
                  className="lx-btn-ghost-icon"
                  onClick={() => void load()}
                  disabled={busy}
                  aria-label="刷新"
                  title="重新统计"
                >
                  <RefreshCw size={15} className={busy ? 'animate-spin' : undefined} />
                </button>
                <button className="lx-btn-ghost-icon" onClick={onClose} aria-label="关闭">
                  <X size={16} />
                </button>
              </div>
            </header>

            <div className="shrink-0 space-y-1.5 border-b border-lx px-[18px] py-2.5">
              <p className="truncate font-mono text-[10px] text-lx-dim" title={outDir}>
                产物目录：{outDir || '未确定'}
              </p>
              <div className="flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  className="lx-btn-ghost px-2 py-1 text-[11px]"
                  onClick={() => setChecked(new Set(shards.map((s) => s.path)))}
                  disabled={!shards.length}
                >
                  全选
                </button>
                <button
                  type="button"
                  className="lx-btn-ghost px-2 py-1 text-[11px]"
                  onClick={pickUnfinished}
                  disabled={!shards.length}
                >
                  只选未完成
                </button>
                <button
                  type="button"
                  className="lx-btn-ghost px-2 py-1 text-[11px]"
                  onClick={() => setChecked(new Set())}
                  disabled={!checked.size}
                >
                  清空
                </button>
              </div>
              {scan && !scan.attributable && (
                <p className="text-[11px] leading-relaxed text-lx-warning">
                  产物目录里没有按分片命名的 jsonl，统计不到每片进度（parquet
                  产物按行数切、不按包切）。勾选仍然可用，只是状态一律显示未开始。
                </p>
              )}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-[18px] py-1">
              {busy && !scan && (
                <div className="flex items-center gap-2 py-6 text-[12px] text-lx-muted">
                  <Loader2 size={14} className="animate-spin" /> 统计分片…
                </div>
              )}
              {error && <p className="py-3 text-[11px] leading-relaxed text-lx-danger">{error}</p>}
              {!busy && !error && !shards.length && (
                <p className="py-3 text-[11px] leading-relaxed text-lx-muted">
                  这个离线包里没有 .parquet / .zip / .tar 分片。
                </p>
              )}
              {shards.length > 0 && (
                <table className="w-full text-[11px]">
                  <thead className="sticky top-0 bg-lx text-[10px] uppercase tracking-wide text-lx-dim">
                    <tr>
                      <th className="w-8 py-1.5 text-left font-normal">
                        <span className="sr-only">选择</span>
                      </th>
                      <th className="py-1.5 text-left font-normal">分片</th>
                      <th className="w-20 py-1.5 text-right font-normal">大小</th>
                      <th className="w-24 py-1.5 text-right font-normal">状态</th>
                      <th className="w-28 py-1.5 text-right font-normal">已产出</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shards.map((s) => (
                      <tr
                        key={s.path}
                        className="cursor-pointer border-t border-lx hover:bg-lx-accent-subtle"
                        onClick={() => toggle(s.path)}
                      >
                        <td className="py-1.5">
                          <input
                            type="checkbox"
                            className="accent-lx-accent"
                            checked={checked.has(s.path)}
                            onChange={() => toggle(s.path)}
                            onClick={(e) => e.stopPropagation()}
                            aria-label={s.name}
                          />
                        </td>
                        <td className="max-w-0 truncate py-1.5 pr-2 font-mono text-lx-secondary">
                          {s.name}
                        </td>
                        <td className="py-1.5 text-right font-mono tabular-nums text-lx-muted">
                          {fmtBytes(s.bytes)}
                        </td>
                        <td className="py-1.5 text-right">
                          <StatusChip tone={STATE_TONE[s.state]}>{STATE_TEXT[s.state]}</StatusChip>
                        </td>
                        <td className="py-1.5 text-right font-mono tabular-nums text-lx-dim">
                          {progressText(s)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <footer className="flex shrink-0 items-center justify-between gap-2 border-t border-lx px-[18px] py-3">
              <span className="text-[11px] text-lx-dim">
                共 {shards.length} 片
                {checked.size > 0 ? `，选中 ${checked.size} 片（${fmtBytes(totalBytes)}）` : '，未选 = 打整包'}
              </span>
              <div className="flex items-center gap-2">
                <button type="button" className="lx-btn-ghost" onClick={onClose}>
                  取消
                </button>
                <button
                  type="button"
                  className="lx-btn-primary inline-flex items-center gap-1.5"
                  onClick={() => {
                    onConfirm(Array.from(checked))
                    onClose()
                  }}
                >
                  <CheckCheck size={13} />
                  {checked.size > 0 ? `只打这 ${checked.size} 片` : '确定'}
                </button>
              </div>
            </footer>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
}
