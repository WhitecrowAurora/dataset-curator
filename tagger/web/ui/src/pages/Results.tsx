import { useCallback, useEffect, useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  ImageOff,
  Images,
  RefreshCw,
  Search,
  TriangleAlert,
  X,
} from 'lucide-react'
import { api, type ResultItem, type ResultsPage } from '../lib/api'
import { clamp01, fmtCount } from '../lib/format'
import { cn } from '../lib/cn'
import { PageHeader, StatusChip } from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'
import { ProgressBar } from '../components/ui/ProgressBar'

const PAGE_SIZES = [24, 48, 96]
const RATING_ORDER = ['general', 'sensitive', 'questionable', 'explicit']

export function Results({ jobId, onBack }: { jobId: string; onBack: () => void }) {
  const [data, setData] = useState<ResultsPage | null>(null)
  const [offset, setOffset] = useState(0)
  const [limit, setLimit] = useState(24)
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [onlyFailed, setOnlyFailed] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<ResultItem | null>(null)

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(query.trim()), 250)
    return () => window.clearTimeout(t)
  }, [query])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.results(jobId, { offset, limit, q: debounced, only_failed: onlyFailed }))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [jobId, offset, limit, debounced, onlyFailed])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    setOffset(0)
  }, [debounced, onlyFailed, limit])

  const pages = data ? Math.max(1, Math.ceil(data.total / limit)) : 1
  const page = Math.floor(offset / limit) + 1

  const avgTags = useMemo(() => {
    if (!data?.items.length) return null
    const sum = data.items.reduce((acc, it) => acc + it.tags.length, 0)
    return sum / data.items.length
  }, [data])

  return (
    <div className="mx-auto lx-page-enter flex max-w-[1440px] flex-col gap-3.5">
      <PageHeader
        icon={<Images size={17} />}
        eyebrow={`任务 ${jobId.slice(0, 8)}`}
        title="打标结果"
        description={
          data
            ? `共 ${fmtCount(data.total)} 条${avgTags !== null ? ` · 本页平均 ${avgTags.toFixed(1)} 个 tag` : ''}`
            : '正在读取产物…'
        }
        badges={
          data?.failed_total ? (
            <StatusChip tone="danger" icon={<TriangleAlert size={11} />}>
              失败 {fmtCount(data.failed_total)} 张
            </StatusChip>
          ) : undefined
        }
        actions={
          <>
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1.5"
              onClick={() => void load()}
              disabled={loading}
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : undefined} />
              刷新
            </button>
            <button type="button" className="lx-btn-ghost" onClick={onBack}>
              回进度
            </button>
          </>
        }
      />

      {error && <ErrorBox error={error} title="读不到产物" />}

      <section className="lx-sheet flex flex-wrap items-center gap-2 px-[18px] py-3 md:px-7">
        <div className="relative min-w-[200px] flex-1">
          <Search
            size={13}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-lx-muted"
          />
          <input
            className="lx-input pl-8"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="按 image_id 过滤（支持模糊）"
            spellCheck={false}
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-lx-muted transition hover:text-lx"
              aria-label="清空搜索"
            >
              <X size={13} />
            </button>
          )}
        </div>

        <button
          type="button"
          onClick={() => setOnlyFailed((v) => !v)}
          className={cn(
            'inline-flex items-center gap-1.5 text-[12px]',
            onlyFailed ? 'lx-btn-danger' : 'lx-btn-ghost',
          )}
          title="只看没有产物的图"
          aria-pressed={onlyFailed}
        >
          <TriangleAlert size={13} /> 只看失败
          {data?.failed_total ? (
            <span className="tabular-nums opacity-80">{fmtCount(data.failed_total)}</span>
          ) : null}
        </button>

        <select
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          className="lx-select !w-auto text-xs"
          aria-label="每页数量"
        >
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n} / 页
            </option>
          ))}
        </select>
      </section>

      {!data || (data.items.length === 0 && !loading) ? (
        <section className="lx-sheet flex flex-col items-center justify-center gap-2 px-6 py-16 text-center">
          <ImageOff size={22} className="text-lx-muted" />
          <p className="text-sm text-lx-secondary">
            {debounced || onlyFailed ? '这个筛选下没有结果' : '还没有产物'}
          </p>
          <p className="text-xs text-lx-muted">
            {debounced || onlyFailed ? '换个关键词，或把「只看失败」关掉' : '回到运行页看看是不是还没跑'}
          </p>
        </section>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {data.items.map((item) => (
            <button
              key={item.image_id}
              type="button"
              onClick={() => setSelected(item)}
              className="group relative overflow-hidden rounded-lx border border-lx bg-lx-surface text-left transition-all duration-200 hover:-translate-y-0.5 hover:border-lx-hover hover:shadow-[0_10px_24px_-10px_rgba(0,0,0,0.7)]"
            >
              <div className="relative aspect-square overflow-hidden bg-[color-mix(in_srgb,var(--lx-bg)_70%,var(--lx-surface))]">
                {item.thumb === false ? (
                  /* VLM 任务拿不到原图（分片流式读，没有原图索引），所以卡片
                     正面直接放 NL 文案——比一个空缩略图占位有用。 */
                  <div className="flex h-full flex-col justify-center p-2.5">
                    {item.ok ? (
                      <p className="line-clamp-6 text-[11px] leading-relaxed text-lx-secondary">
                        {item.caption || '（这条没有文案）'}
                      </p>
                    ) : (
                      <p className="line-clamp-6 text-[11px] leading-relaxed text-lx-danger">
                        {item.error || '失败'}
                      </p>
                    )}
                  </div>
                ) : item.ok ? (
                  <img
                    src={api.thumbUrl(jobId, item.image_id, 360)}
                    alt={item.image_id}
                    loading="lazy"
                    className="h-full w-full object-cover opacity-0 transition-all duration-500 group-hover:scale-[1.04] [&.ready]:opacity-100"
                    onLoad={(e) => e.currentTarget.classList.add('ready')}
                  />
                ) : (
                  <div className="flex h-full flex-col items-center justify-center gap-1 text-lx-danger">
                    <ImageOff size={18} />
                    <span className="text-[11px]">无产物</span>
                  </div>
                )}
                {item.tags.length > 0 && (
                  <span className="absolute right-1.5 top-1.5 rounded-[4px] bg-black/55 px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-white/85 backdrop-blur">
                    {item.tags.length}
                  </span>
                )}
              </div>

              <div className="space-y-1.5 p-2.5">
                <div className="truncate font-mono text-[11px] text-lx-secondary" title={item.image_id}>
                  {item.image_id}
                </div>
                <div className="flex flex-wrap gap-1 overflow-hidden">
                  {item.tags.slice(0, 2).map(([t, p]) => (
                    <span
                      key={t}
                      className="max-w-full truncate rounded-[4px] bg-lx-surface-raised px-1.5 py-0.5 font-mono text-[10px] text-lx-secondary"
                      title={`${t} ${p.toFixed(2)}`}
                    >
                      {t}
                    </span>
                  ))}
                  {item.tags.length > 2 && (
                    <span className="rounded-[4px] px-1 py-0.5 text-[10px] text-lx-muted">
                      +{item.tags.length - 2}
                    </span>
                  )}
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      {data && data.total > limit && (
        <div className="flex flex-wrap items-center justify-between gap-3 px-1 pb-2 text-xs text-lx-muted">
          <span className="tabular-nums">
            第 {page} / {pages} 页 · {fmtCount(data.total)} 条
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-3 py-1.5 text-[12px]"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              <ChevronLeft size={13} /> 上一页
            </button>
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-3 py-1.5 text-[12px]"
              disabled={offset + limit >= data.total}
              onClick={() => setOffset(offset + limit)}
            >
              下一页 <ChevronRight size={13} />
            </button>
          </div>
        </div>
      )}

      <AnimatePresence>
        {selected && (
          <DetailDrawer jobId={jobId} item={selected} onClose={() => setSelected(null)} />
        )}
      </AnimatePresence>
    </div>
  )
}

function DetailDrawer({
  jobId,
  item,
  onClose,
}: {
  jobId: string
  item: ResultItem
  onClose: () => void
}) {
  const [copied, setCopied] = useState<'tag' | 'caption' | null>(null)
  const sorted = useMemo(() => [...item.tags].sort((a, b) => b[1] - a[1]), [item.tags])
  const rating = RATING_ORDER.filter((k) => k in item.rating)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function copy(text: string, what: 'tag' | 'caption') {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(what)
      window.setTimeout(() => setCopied(null), 1400)
    } catch {
      setCopied(null)
    }
  }

  return (
    <motion.div
      className="fixed inset-0 z-[9000] flex justify-end"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.15 }}
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="absolute inset-0"
        style={{ backgroundColor: 'color-mix(in srgb, var(--lx-bg) 58%, transparent)', backdropFilter: 'blur(2px)' }}
      />

      <motion.aside
        className="relative flex h-full w-full max-w-md flex-col border-l border-lx bg-lx-surface"
        initial={{ x: 40, opacity: 0 }}
        animate={{ x: 0, opacity: 1 }}
        exit={{ x: 40, opacity: 0 }}
        transition={{ type: 'spring', stiffness: 380, damping: 32 }}
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-lx px-[18px] py-3.5 md:px-6">
          <div className="min-w-0">
            <h3 className="truncate font-mono text-[13px] text-lx" title={item.image_id}>
              {item.image_id}
            </h3>
            {item.rel && (
              <p className="mt-0.5 truncate text-[11px] text-lx-muted" title={item.rel}>
                {item.rel}
              </p>
            )}
          </div>
          <button className="lx-btn-ghost-icon" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-[18px] py-4 md:px-6">
          {/* thumb === false 是 VLM 任务：分片流式读，没有原图可回头取，
              渲染 <img> 只会得到一串 404 破图。 */}
          {item.thumb === false ? null : item.ok ? (
            <img
              src={api.thumbUrl(jobId, item.image_id, 720)}
              alt={item.image_id}
              className="mb-4 w-full rounded-lx border border-lx object-contain"
            />
          ) : (
            <div className="mb-4 flex items-center gap-2 rounded-lx border border-lx-danger bg-lx-danger-subtle px-3 py-2 text-xs text-lx-danger">
              <ImageOff size={13} /> 这张没有产物
            </div>
          )}

          {!item.ok && item.error && (
            <div className="mb-5 rounded-lx border border-lx-danger bg-lx-danger-subtle px-3 py-2 text-xs text-lx-danger">
              {item.error}
            </div>
          )}

          {item.caption && (
            <div className="mb-5">
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <span className="text-[11px] uppercase tracking-wide text-lx-muted">
                  NL 文案 · {item.caption.length} 字
                </span>
                <button
                  type="button"
                  className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                  onClick={() => void copy(item.caption ?? '', 'caption')}
                >
                  {copied === 'caption' ? <Check size={12} /> : <Copy size={12} />}
                  {copied === 'caption' ? '已复制' : '复制文案'}
                </button>
              </div>
              <p className="whitespace-pre-wrap rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 text-[12.5px] leading-relaxed text-lx-secondary">
                {item.caption}
              </p>
            </div>
          )}

          {rating.length > 0 && (
            <div className="mb-5 space-y-2">
              <div className="text-[11px] uppercase tracking-wide text-lx-muted">
                rating（独立 sigmoid，不归一）
              </div>
              {rating.map((k) => (
                <div key={k} className="flex items-center gap-2">
                  <span className="w-[86px] shrink-0 text-[11px] text-lx-muted">{k}</span>
                  <ProgressBar
                    percent={clamp01(item.rating[k]) * 100}
                    tone={clamp01(item.rating[k]) > 0.5 ? 'accent' : 'neutral'}
                    className="flex-1"
                  />
                  <span className="w-9 shrink-0 text-right font-mono text-[11px] tabular-nums text-lx-muted">
                    {item.rating[k].toFixed(2)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {sorted.length > 0 && (
            <>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[11px] uppercase tracking-wide text-lx-muted">
                  {sorted.length} 个 tag
                </span>
                <button
                  type="button"
                  className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                  onClick={() => void copy(item.tag, 'tag')}
                >
                  {copied === 'tag' ? <Check size={12} /> : <Copy size={12} />}
                  {copied === 'tag' ? '已复制' : '复制 tag 串'}
                </button>
              </div>

              <ul className="space-y-0.5">
                {sorted.map(([name, prob]) => (
                  <li
                    key={name}
                    className="flex items-center gap-2 rounded-lx-sm px-1 py-1 transition-colors hover:bg-[color-mix(in_srgb,var(--lx-text)_4%,transparent)]"
                  >
                    <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-lx-secondary" title={name}>
                      {name}
                    </span>
                    <ProgressBar percent={clamp01(prob) * 100} className="w-16 shrink-0" />
                    <span className="w-9 shrink-0 text-right font-mono text-[11px] tabular-nums text-lx-muted">
                      {prob.toFixed(2)}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        <footer className="shrink-0 border-t border-lx px-[18px] py-3 text-[11px] text-lx-muted md:px-6">
          后端 {item.tag_backend || '—'}
        </footer>
      </motion.aside>
    </motion.div>
  )
}
