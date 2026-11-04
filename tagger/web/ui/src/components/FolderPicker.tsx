import { useCallback, useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ArrowUp, Folder, Image as ImageIcon, Loader2, Package, X } from 'lucide-react'
import { api, type FsListing } from '../lib/api'
import { StatusChip } from './ui/launcher'
import { ErrorBox } from './ui/controls'

/** 极简输入浏览器。目录 + 当层的 .zip 素材包：
 *  用户的真实动作是"找那个装着图的文件夹，或者直接指那个大包"。 */
export function FolderPicker({
  open,
  start,
  onClose,
  onPick,
}: {
  open: boolean
  start?: string
  onClose: () => void
  onPick: (path: string) => void
}) {
  const [listing, setListing] = useState<FsListing | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (path: string) => {
    setLoading(true)
    setError(null)
    try {
      setListing(await api.fs(path))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    void load(start ?? '')
  }, [open, start, load])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const empty = listing && listing.dirs.length === 0 && listing.zips.length === 0

  return (
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
          aria-label="选择输入目录或素材包"
        >
          <div
            className="absolute inset-0"
            style={{
              backgroundColor: 'color-mix(in srgb, var(--lx-bg) 62%, transparent)',
              backdropFilter: 'blur(3px)',
            }}
          />

          <motion.div
            className="lx-sheet relative flex h-[70vh] w-full max-w-2xl flex-col overflow-hidden"
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 4 }}
            transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          >
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-lx px-[18px] py-3 md:px-7">
              <h3 className="text-[15px] font-semibold text-lx">选择输入</h3>
              <button className="lx-btn-ghost-icon" onClick={onClose} aria-label="关闭">
                <X size={16} />
              </button>
            </header>

            <div className="flex shrink-0 items-center gap-2 border-b border-lx px-[18px] py-2.5 md:px-7">
              <button
                type="button"
                className="lx-btn-ghost-icon"
                disabled={!listing?.parent}
                onClick={() => listing?.parent && load(listing.parent)}
                title="上一级"
                aria-label="上一级"
              >
                <ArrowUp size={14} />
              </button>
              <code className="min-w-0 flex-1 truncate font-mono text-xs text-lx-muted">
                {listing?.path ?? '…'}
              </code>
              {listing && (
                <StatusChip tone={listing.images ? 'accent' : 'neutral'} icon={<ImageIcon size={11} />}>
                  {listing.images} 张
                </StatusChip>
              )}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto">
              {loading && (
                <div className="flex items-center gap-2 px-5 py-4 text-xs text-lx-muted">
                  <Loader2 size={14} className="animate-spin" /> 读取中…
                </div>
              )}
              {error && (
                <div className="p-4">
                  <ErrorBox error={error} tone="danger" title="这个目录打不开" />
                </div>
              )}
              {!loading && !error && empty && (
                <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center text-sm text-lx-muted">
                  <Folder size={20} />
                  这一层没有子目录，也没有 .zip 素材包
                </div>
              )}
              {!loading &&
                listing?.zips.map((z) => (
                  <button
                    key={z.path}
                    onClick={() => onPick(z.path)}
                    className="lx-sheet-row flex w-full items-center gap-2.5 px-[18px] py-2.5 text-left first:border-t-0 hover:bg-[color-mix(in_srgb,var(--lx-text)_3%,transparent)] md:px-7"
                    title="直接用这个素材包作为输入"
                  >
                    <Package size={15} className="shrink-0 text-lx-warning" />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-lx">{z.name}</span>
                    <span className="shrink-0 text-[11px] tabular-nums text-lx-muted">
                      {(z.bytes / 1073741824).toFixed(2)} GB
                    </span>
                  </button>
                ))}
              {!loading &&
                listing?.dirs.map((d) => (
                  <button
                    key={d.path}
                    onDoubleClick={() => onPick(d.path)}
                    onClick={() => load(d.path)}
                    className="lx-sheet-row flex w-full items-center gap-2.5 px-[18px] py-2.5 text-left first:border-t-0 hover:bg-[color-mix(in_srgb,var(--lx-text)_3%,transparent)] md:px-7"
                  >
                    <Folder
                      size={15}
                      className={d.images || d.zips ? 'shrink-0 text-lx-accent' : 'shrink-0 text-lx-muted'}
                    />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-lx">{d.name}</span>
                    {(d.images > 0 || d.zips > 0) && (
                      <span className="shrink-0 text-[11px] tabular-nums text-lx-muted">
                        {d.images > 0 ? `${d.images} 张` : ''}
                        {d.images > 0 && d.zips > 0 ? ' · ' : ''}
                        {d.zips > 0 ? `${d.zips} 个包` : ''}
                      </span>
                    )}
                  </button>
                ))}
            </div>

            <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-lx px-[18px] py-3 md:px-7">
              <span className="text-[11px] text-lx-muted">
                点目录进去，双击直接选定；点 .zip 直接用它当输入
              </span>
              <div className="flex gap-2">
                <button type="button" className="lx-btn-ghost" onClick={onClose}>
                  取消
                </button>
                <button
                  type="button"
                  className="lx-btn-primary"
                  disabled={!listing}
                  onClick={() => listing && onPick(listing.path)}
                >
                  就用这个目录
                </button>
              </div>
            </footer>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
