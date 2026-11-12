import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { ArrowUp, FileText, Folder, Image as ImageIcon, Loader2, Package, X } from 'lucide-react'
import { api, type FsListing } from '../lib/api'
import { StatusChip } from './ui/launcher'
import { ErrorBox } from './ui/controls'

/** 极简输入浏览器。目录 + 当层的素材包（.zip / .tar / .tar.gz）：
 *  用户的真实动作是"找那个装着图的文件夹，或者直接指那个大包"。 */
export function FolderPicker({
  open,
  start,
  files,
  onClose,
  onPick,
}: {
  open: boolean
  start?: string
  /** 额外列出的文件扩展名（`json` / `parquet` …）。有些输入框收"目录或单个
   *  文件"，只列目录等于让用户回去手敲文件名。 */
  files?: string[]
  onClose: () => void
  onPick: (path: string) => void
}) {
  const [listing, setListing] = useState<FsListing | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 地址栏。与 listing.path 分开存：用户正敲到一半时不能被一次刷新覆盖。
  const [draft, setDraft] = useState('')
  // 数组进依赖每次渲染都是新引用，摊平成字符串只为了让 load 稳定。
  const extKey = (files ?? []).join(',')

  const load = useCallback(async (path: string) => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.fs(path, extKey ? extKey.split(',') : undefined)
      setListing(data)
      // 地址栏一律回显**实际列出来的**目录，而不是用户敲的那串：敲的是
      // `~/data`、带引号、或者一个还不存在的路径时，后端会规整/回退，
      // 地址栏留着原文就会出现"地址栏写 A、列表其实是 B"的分裂状态。
      setDraft(data.path)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [extKey])

  useEffect(() => {
    if (!open) return
    void load(start ?? '')
  }, [open, start, load])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      // 地址栏里按 Esc 是"放弃这次输入"，不该顺手把整个弹窗关掉——
      // 那个动作由输入框自己 preventDefault 后在这里跳过。
      if (e.key === 'Escape' && !e.defaultPrevented) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const empty =
    listing && listing.dirs.length === 0 && listing.zips.length === 0 && !listing.files?.length

  // 挂到 body 上，而不是留在页面 DOM 里：页面根节点带 .lx-page-enter（入场动画
  // 残留 transform），会让 position:fixed 相对那个整页高的容器定位，弹窗于是
  // 出现在"整页的中间"，用户在顶部时要往下滚才看得见。
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
              {/* 地址栏可编辑。以前这里是只读 <code>，只能靠"上一级"和切盘符
                  逐层点进去——云服务器上数据常在 /home/<user>/... 这种深路径，
                  从根翻下去要点十几次，用户宁可去别处复制粘贴。粘贴进来回车
                  就跳转，这是这类场景唯一真正省事的入口。 */}
              <input
                className="lx-input min-w-0 flex-1 font-mono text-xs"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    void load(draft)
                  } else if (e.key === 'Escape') {
                    // 放弃这次输入，回到当前目录；preventDefault 让上层
                    // 的全局监听知道"这个 Esc 已经有人处理了"。
                    e.preventDefault()
                    setDraft(listing?.path ?? '')
                  }
                }}
                placeholder="粘贴路径后回车跳转，例如 /data/datasets"
                spellCheck={false}
                aria-label="路径"
              />
              {listing && (
                <StatusChip tone={listing.images ? 'accent' : 'neutral'} icon={<ImageIcon size={11} />}>
                  {listing.images} 张
                </StatusChip>
              )}
            </div>

            {/* 落到了别处就说一声。用户敲的路径不存在（最典型：想指一个还
                没建的输出目录）时后端会退到最近的祖先，地址栏也已经回显成
                真实位置；不解释一句的话，他只会觉得"我输入的那个路径没生效"。 */}
            {listing?.fell_back_to && (
              <div className="shrink-0 border-b border-lx px-[18px] py-1.5 text-[11px] text-lx-warning md:px-7">
                那个路径不存在，已落到 {listing.fell_back_to}
              </div>
            )}

            {/* 盘符 / 挂载点切换：不靠"上一级"翻到根再猜，一步就能换盘。
                Linux 服务器上素材盘常在 /data、/mnt/xxx，从 `/` 往上翻永远
                翻不到，这一行就是唯一的出口，所以非 Windows 也给。
                **只有一项也照渲染**：以前按"一项就是噪音"藏掉，可一台只有
                `/` 的机器藏了这行，用户从 `/` 就再没有出口了——宁可多一行。 */}
            {listing && listing.drives.length > 0 && (
              <div className="flex shrink-0 flex-wrap items-center gap-1 border-b border-lx px-[18px] py-1.5 md:px-7">
                <span className="mr-1 shrink-0 text-[10px] text-lx-dim">位置</span>
                {listing.drives.map((d) => {
                  // Windows 上 "H:"；别的系统是挂载点原文（"/data"）
                  const letter = d.path.replace(/\\$/, '')
                  const active = listing.path.toLowerCase().startsWith(d.path.toLowerCase())
                  return (
                    <button
                      key={d.path}
                      type="button"
                      // 卷标被截断时靠它看全名
                      title={d.label ? `${d.label} (${d.path})` : d.path}
                      onClick={() => load(d.path)}
                      className={`max-w-[11rem] shrink-0 truncate rounded px-2 py-0.5 text-[11px] transition-colors ${
                        active
                          ? 'bg-[color-mix(in_srgb,var(--lx-accent)_16%,transparent)] text-lx-accent'
                          : 'text-lx-muted hover:bg-[color-mix(in_srgb,var(--lx-text)_6%,transparent)] hover:text-lx-secondary'
                      }`}
                    >
                      {d.label ? `${letter} · ${d.label}` : letter}
                    </button>
                  )
                })}
              </div>
            )}

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
                listing?.files?.map((f) => (
                  <button
                    key={f.path}
                    onClick={() => onPick(f.path)}
                    className="lx-sheet-row flex w-full items-center gap-2.5 px-[18px] py-2.5 text-left first:border-t-0 hover:bg-[color-mix(in_srgb,var(--lx-text)_3%,transparent)] md:px-7"
                    title="直接用这个文件作为输入"
                  >
                    <FileText size={15} className="shrink-0 text-lx-accent" />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-lx">{f.name}</span>
                    <span className="shrink-0 text-[11px] tabular-nums text-lx-muted">
                      {f.bytes >= 1073741824
                        ? `${(f.bytes / 1073741824).toFixed(2)} GB`
                        : `${Math.max(1, Math.round(f.bytes / 1024))} KB`}
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
                点目录进入，双击选定；地址栏可粘贴路径后回车跳转
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
    </AnimatePresence>,
    document.body,
  )
}

/**
 * 各页面「浏览」按钮的统一入口。
 *
 * 原本这里先试 Tagger.Shell 的系统对话框、失败再回退。实际跑下来，
 * 系统框在 WebView2 里不稳定，而且握手失败时要等一两秒才回退——那段时间
 * 界面什么都不发生，比"没有这个功能"更难受。所以现在就一条路径：
 * 直接开自绘窗口。它够快，也永远拿得到绝对路径。
 *
 * 返回值里的 `dialog` 必须渲染在页面上。
 */
export function useFolderPicker() {
  const [open, setOpen] = useState(false)
  const [start, setStart] = useState<string | undefined>(undefined)
  const [files, setFiles] = useState<string[] | undefined>(undefined)
  // 回调放 ref：窗口是异步打开的，不能依赖闭包里的旧 state。
  const pending = useRef<((path: string) => void) | null>(null)

  const pick = useCallback(
    (startPath: string | undefined, apply: (path: string) => void, exts?: string[]) => {
      pending.current = apply
      setStart(startPath)
      setFiles(exts)
      setOpen(true)
    },
    [],
  )

  const dialog = (
    <FolderPicker
      open={open}
      start={start}
      files={files}
      onClose={() => setOpen(false)}
      onPick={(path) => {
        setOpen(false)
        pending.current?.(path)
      }}
    />
  )

  return { pick, dialog }
}
