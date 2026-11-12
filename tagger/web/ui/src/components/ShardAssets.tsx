/**
 * 本地已打包分片（parquet）清单。
 *
 * 为什么需要这个入口：封包产物是一堆 `pack-*-00000.parquet`，散在不同目录里。
 * 用户手里有这批文件，却不知道该把哪个路径喂给 NL 打标；而「本地分片」
 * 输入框只接受一个路径。这里把目录里的分片连**行数**一起列出来——
 * 行数就是"这片里有多少张图"，选之前就该知道。
 *
 * 行数从 parquet 的 footer 元数据读（O(1)），不加载数据本身；
 * 读不动的文件显示 "-"，不挡列表。
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { FolderOpen, FolderSearch, Loader2, X } from 'lucide-react'
import { api, type ArtifactInfo } from '../lib/api'
import { useFolderPicker } from './FolderPicker'

function fmtBytes(n: number): string {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

function fmtTime(sec: number): string {
  if (!sec) return '—'
  const d = new Date(sec * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function ShardAssets({
  open,
  startDir,
  onClose,
  onPick,
}: {
  open: boolean
  /** 打开时先看哪个目录；空 = work/out */
  startDir: string
  onClose: () => void
  onPick: (file: string) => void
}) {
  const [dir, setDir] = useState('')
  const [base, setBase] = useState('')
  const [files, setFiles] = useState<ArtifactInfo[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const folder = useFolderPicker()

  async function load(target: string) {
    const path = target.trim()
    if (!path) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.artifacts(path)
      setBase(res.path)
      setFiles(res.files)
    } catch (err) {
      setFiles([])
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
      setLoaded(true)
    }
  }

  // 打开时看一次。之后由「刷新」按钮触发——路径每敲一个字就发请求太吵。
  useEffect(() => {
    if (!open) return
    const start = startDir.trim() || 'work/out'
    setDir(start)
    setFiles([])
    setError(null)
    setLoaded(false)
    void load(start)
    // startDir 只在打开那一刻有意义
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

  // 必须挂到 body 上再定位。
  //
  // `fixed` 只相对**最近的带 transform / filter / backdrop-filter 的祖先**定位，
  // 而页面根节点用了入场动画（transform），于是这个"全屏遮罩"实际是相对
  // 整张长表单定位的——看着就像弹窗跑到页面中段、要往下拉才能看见。
  // 挂到 body 之后才是真正的视口居中。
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
          aria-label="已打包分片"
        >
          <div
            className="absolute inset-0"
            style={{
              backgroundColor: 'color-mix(in srgb, var(--lx-bg) 62%, transparent)',
              backdropFilter: 'blur(3px)',
            }}
          />
          <motion.div
            className="lx-sheet relative flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden"
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 4 }}
            transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          >
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-lx px-[18px] py-3">
              <div>
                <h3 className="text-[15px] font-semibold text-lx">已打包分片</h3>
                <p className="mt-0.5 text-[11px] text-lx-muted">
                  选一个 parquet 作为 NL 输入
                </p>
              </div>
              <button className="lx-btn-ghost-icon" onClick={onClose} aria-label="关闭">
                <X size={16} />
              </button>
            </header>

            <div className="shrink-0 space-y-2 border-b border-lx px-[18px] py-3">
              <div className="flex gap-2">
                <input
                  className="lx-input font-mono text-xs"
                  value={dir}
                  onChange={(e) => setDir(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void load(dir)
                  }}
                  placeholder="D:\work\out"
                  spellCheck={false}
                  aria-label="分片目录"
                />
                <button
                  type="button"
                  className="lx-btn-ghost inline-flex shrink-0 items-center gap-1.5 px-3"
                  disabled={busy}
                  onClick={() =>
                    folder.pick(dir || startDir || undefined, (path) => {
                      setDir(path)
                      void load(path)
                    })
                  }
                  title="浏览目录"
                >
                  <FolderSearch size={12} />
                  浏览
                </button>
                <button
                  type="button"
                  className="lx-btn-ghost inline-flex shrink-0 items-center gap-1.5 px-3"
                  disabled={busy || !dir.trim()}
                  onClick={() => void load(dir)}
                >
                  {busy ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <FolderOpen size={12} />
                  )}
                  读取
                </button>
              </div>
              {base && (
                <p className="font-mono text-[10px] leading-relaxed text-lx-dim">{base}</p>
              )}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-[18px] py-2">
              {busy && files.length === 0 && (
                <div className="flex items-center gap-2 py-6 text-[12px] text-lx-muted">
                  <Loader2 size={14} className="animate-spin" /> 读取分片清单…
                </div>
              )}
              {error && (
                <p className="py-3 text-[11px] leading-relaxed text-lx-danger">{error}</p>
              )}
              {loaded && !busy && !error && files.length === 0 && (
                <p className="py-3 text-[11px] leading-relaxed text-lx-muted">
                  这个目录里没有 .parquet。封包会在「数据打包 → 封包」里写出
                  pack-*.parquet；也可以直接填到它所在的目录再点「读取」。
                </p>
              )}
              {files.length > 0 && (
                <table className="w-full text-[11px]">
                  <thead className="text-[10px] uppercase tracking-wide text-lx-dim">
                    <tr>
                      <th className="py-1.5 text-left font-normal">分片</th>
                      <th className="w-16 py-1.5 text-right font-normal">行数</th>
                      <th className="w-20 py-1.5 text-right font-normal">大小</th>
                      <th className="w-32 py-1.5 text-right font-normal">修改时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {files.map((f) => (
                      <tr
                        key={f.name}
                        className="cursor-pointer border-t border-lx hover:bg-lx-accent-subtle"
                        onClick={() =>
                          onPick(`${base.replace(/[\\/]+$/, '')}/${f.name}`)
                        }
                        title="用它作为 NL 输入"
                      >
                        <td className="max-w-0 truncate py-1.5 pr-2 font-mono text-lx-secondary">
                          {f.name}
                        </td>
                        <td className="py-1.5 text-right font-mono tabular-nums text-lx-secondary">
                          {f.rows ?? '—'}
                        </td>
                        <td className="py-1.5 text-right font-mono tabular-nums text-lx-muted">
                          {fmtBytes(f.bytes)}
                        </td>
                        <td className="py-1.5 text-right font-mono tabular-nums text-lx-dim">
                          {fmtTime(f.mtime)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <footer className="flex shrink-0 items-center justify-between gap-2 border-t border-lx px-[18px] py-3">
              <span className="text-[11px] text-lx-dim">
                共 {files.length} 片{files.length ? `，${files.reduce((n, f) => n + (f.rows ?? 0), 0)} 行` : ''}
              </span>
              <div className="flex items-center gap-2">
                {files.length > 1 && (
                  <button
                    type="button"
                    className="lx-btn-primary"
                    onClick={() => onPick(base)}
                  >
                    用整个目录（{files.length} 片）
                  </button>
                )}
                <button type="button" className="lx-btn-ghost" onClick={onClose}>
                  关闭
                </button>
              </div>
            </footer>
            {folder.dialog}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
}
