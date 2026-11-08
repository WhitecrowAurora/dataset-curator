/**
 * 模型管理：NL 模型（Qwen3-VL 4B/8B…）与 tag 分类头的可选清单 + 权重下载。
 *
 * 两件事分开看：
 * 1. 清单 —— 保存时只把与 tiers.yaml 不同的条目写进 configs/models.json，
 *    所以以后改 tiers.yaml 仍然生效，不会被这个页面冻结成一份旧快照。
 * 2. 权重 —— 下载到每行自己的目录（默认 models/<key>），进度与网速从
 *    /api/models 的 download 字段轮询；下完就地变成"删除 / 打开位置"。
 *    删除只允许删 models/ 之内的路径（后端有守卫），别把 local_dir 指到素材上。
 */
import { useEffect, useState, type ReactNode } from 'react'
import {
  Boxes,
  ChevronDown,
  ChevronUp,
  Cpu,
  Download,
  FolderOpen,
  Gauge,
  Plus,
  RotateCcw,
  Save,
  Tags,
  Trash2,
  X,
} from 'lucide-react'
import {
  api,
  type DownloadState,
  type ModelCatalog,
  type TagModelEntry,
  type VlmModelEntry,
} from '../lib/api'
import { PageHeader, SectionPanel, StatusChip } from '../components/ui/launcher'
import { QuantizeDialog } from '../components/QuantizeDialog'
import { GgufConvertDialog } from '../components/GgufConvertDialog'
import { ErrorBox } from '../components/ui/controls'

const PRECISIONS = ['int4', 'int8', 'bf16', 'fp16', 'fp32']

function fmtBytes(n: number): string {
  if (!n || n < 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = n
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(unit === 0 || value >= 100 ? 0 : 1)} ${units[unit]}`
}

const isRunning = (d: DownloadState | null | undefined) =>
  !!d && (d.status === 'queued' || d.status === 'running')

function Field({
  label,
  className = '',
  children,
}: {
  label: string
  className?: string
  children: ReactNode
}) {
  return (
    <label className={`flex min-w-0 flex-col gap-1 ${className}`}>
      <span className="text-[11px] text-lx-muted">{label}</span>
      {children}
    </label>
  )
}

type RowEntry = VlmModelEntry | TagModelEntry

/** 一行底部的"本地权重"区：下载 / 进度条 / 删除 / 打开位置。 */
function LocalFilesBar({
  kind,
  entry,
  onRefresh,
  onUseLocalDir,
  onPrepare,
  onError,
}: {
  kind: 'vlm' | 'tag'
  entry: RowEntry
  onRefresh: (cat: ModelCatalog) => void
  /** 只给 NL 行：把权重从仓库名换成这个本地目录 */
  onUseLocalDir?: () => void
  /** 点下载前先把表单存了：否则刚改的下载目录不生效，文件会下到旧位置 */
  onPrepare?: () => Promise<void>
  onError: (msg: string) => void
}) {
  const [pending, setPending] = useState('')
  // 要下哪一档 GGUF。空 = 按配置清单下（一般是好几档一起）；选了就只下这一档；
  // 'raw' = 整仓库原始权重（safetensors），专供"本地量化"用。
  const [tier, setTier] = useState('')
  const [convertOpen, setConvertOpen] = useState(false)
  const key = 'key' in entry ? entry.key : entry.backend
  const download = entry.download
  const running = isRunning(download)
  // NL 的"权重"既可能是仓库名也可能是本地目录；tag 的权重目录在新建任务页另填
  const path = 'key' in entry ? entry.path : entry.repo
  const tiers = 'download_tiers' in entry ? entry.download_tiers ?? [] : []
  // 有文件 ≠ 下完了：取消/报错会留下半截。只有非 partial 才算"本地已有"。
  // 也不等于跑得起来：下错仓库时目录里全是 safetensors，缺 gguf 照样起不来。
  const missing = entry.missing_files ?? []
  const ready = entry.exists && !entry.partial && entry.ready !== false
  const canUseLocal =
    kind === 'vlm' && !!onUseLocalDir && ready && path !== entry.local_abs

  async function act(label: string, fn: () => Promise<ModelCatalog | void>) {
    setPending(label)
    onError('')
    try {
      const res = await fn()
      if (res) onRefresh(res)
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err))
    } finally {
      setPending('')
    }
  }

  return (
    <div className="md:col-span-2 flex flex-wrap items-center justify-between gap-2 border-t border-lx pt-2">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 text-[11px]">
        {running && download ? (
          <>
            <div className="h-1.5 w-32 shrink-0 overflow-hidden rounded-full bg-lx-surface-raised">
              <div
                className="h-full rounded-full bg-lx-accent transition-[width] duration-300"
                style={{ width: `${Math.max(2, download.percent)}%` }}
              />
            </div>
            <span className="font-mono tabular-nums text-lx">
              {download.percent.toFixed(1)}%
            </span>
            <span className="font-mono tabular-nums text-lx-muted">
              {fmtBytes(download.bytes_done)} / {fmtBytes(download.bytes_total)}
            </span>
            <span className="font-mono tabular-nums text-lx-accent">
              {fmtBytes(download.speed)}/s
            </span>
            {download.eta != null && (
              <span className="text-lx-dim">剩 {Math.max(0, Math.round(download.eta))}s</span>
            )}
            <span className="truncate text-lx-dim">{download.current}</span>
          </>
        ) : ready ? (
          <>
            <span className="text-lx-success">本地已有</span>
            <span className="font-mono tabular-nums text-lx-muted">
              {fmtBytes(entry.local_bytes)}
            </span>
            <span className="truncate font-mono text-[10px] text-lx-dim" title={entry.local_abs}>
              {entry.local_dir}
            </span>
            {download?.status === 'error' && (
              <span className="text-lx-warning">{download.message}</span>
            )}
          </>
        ) : (
          <>
            <span className={missing.length ? 'text-lx-warning' : 'text-lx-dim'}>
              {entry.partial
                ? '上次没下完，点下载会接着下'
                : missing.length
                  ? `已下载，但缺 ${missing.join('、')}——启动会报权重不存在`
                  : download?.status === 'error'
                    ? download.message
                    : entry.download_repo
                      ? '未下载'
                      : '这条没有可下载的仓库（权重是本地目录）'}
            </span>
            <span className="truncate font-mono text-[10px] text-lx-dim" title={entry.local_abs}>
              {entry.local_dir}
            </span>
          </>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-1">
        {running ? (
          <button
            type="button"
            className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
            disabled={!!pending}
            onClick={() =>
              void act('cancel', async () => {
                await api.cancelDownload(kind, key)
              })
            }
          >
            <X size={12} />
            取消
          </button>
        ) : ready ? (
          <>
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
              disabled={!!pending}
              onClick={() => void act('reveal', async () => {
                await api.revealLocalDir(kind, key)
              })}
            >
              <FolderOpen size={12} />
              打开位置
            </button>
            {canUseLocal && (
              <button
                type="button"
                className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                title="把权重从仓库名改成这个本地目录，之后起服务不再联网"
                onClick={() => onUseLocalDir?.()}
              >
                改用本地目录
              </button>
            )}
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px] text-lx-warning"
              disabled={!!pending}
              onClick={() => {
                if (!window.confirm(`删除 ${entry.local_dir} 里的权重文件？删完要重新下载。`)) return
                void act('delete', () => api.deleteLocalFiles(kind, key))
              }}
            >
              <Trash2 size={12} />
              删除
            </button>
          </>
        ) : (
          <>
            {/* 半截的目录也给"打开位置"，方便确认下到哪一步了 */}
            {entry.exists && (
              <button
                type="button"
                className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                disabled={!!pending}
                onClick={() =>
                  void act('reveal', async () => {
                    await api.revealLocalDir(kind, key)
                  })
                }
              >
                <FolderOpen size={12} />
                打开位置
              </button>
            )}
            {entry.exists && (
              <button
                type="button"
                className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px] text-lx-warning"
                disabled={!!pending}
                onClick={() => {
                  if (!window.confirm(`删除 ${entry.local_dir} 里没下完的文件？`)) return
                  void act('delete', () => api.deleteLocalFiles(kind, key))
                }}
              >
                <Trash2 size={12} />
                删除
              </button>
            )}
            {/* 档位下拉：一个 GGUF 仓库里挂着十来个档（20G+），默认只下配置
                清单里那几份。想只要 Q6 就选它，省一半流量和磁盘。 */}
            {tiers.length > 0 && (
              <select
                className="lx-input px-1.5 py-1 text-[11px]"
                value={tier}
                disabled={!!pending}
                onChange={(e) => setTier(e.target.value)}
                aria-label="要下载的量化档"
                title="只下这一档 + 配套 mmproj；不选则按配置清单下"
              >
                <option value="">全部档位</option>
                {tiers.map((t) => (
                  <option key={t.key} value={t.key}>
                    {t.label}
                  </option>
                ))}
                {/* 原始权重：给「本地量化」当输入。体积大得多，所以不放进
                    "全部档位"里顺手带上。 */}
                {kind === 'vlm' && (
                  <option value="raw">原始权重（用于本地量化）</option>
                )}
              </select>
            )}
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
              disabled={!!pending || !entry.download_repo}
              title={entry.download_repo ? `从 ${entry.download_repo} 下载` : '没有可下载的仓库'}
              onClick={() =>
                void act('download', async () => {
                  await onPrepare?.()
                  await api.startDownload(kind, key, tier)
                })
              }
            >
              <Download size={12} />
              {pending === 'download' ? '启动中…' : entry.partial ? '继续下载' : '下载'}
            </button>
            {/* 本地量化：原始权重 → GGUF。放在下载旁边，因为它是"下完原始
                权重之后"的下一步，用户不会想到去别的页面找。 */}
            {kind === 'vlm' && (
              <button
                type="button"
                className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                disabled={!!pending}
                title="用原始权重在本地压出 GGUF（Q4_K_M / Q6_K）"
                onClick={() => setConvertOpen(true)}
              >
                <Cpu size={12} />
                本地量化
              </button>
            )}
          </>
        )}
      </div>
      {kind === 'vlm' && (
        <GgufConvertDialog
          open={convertOpen}
          modelKey={key}
          onClose={() => setConvertOpen(false)}
        />
      )}
    </div>
  )
}

export function Models() {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [vlm, setVlm] = useState<VlmModelEntry[]>([])
  const [tag, setTag] = useState<TagModelEntry[]>([])
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState('')
  // 下载代理：与模型表分开保存（改代理不该顺带重写整张表）
  const [proxy, setProxy] = useState('')
  const [proxyMsg, setProxyMsg] = useState('')
  const [proxyOk, setProxyOk] = useState<boolean | null>(null)
  const [proxyBusy, setProxyBusy] = useState('')
  // 折叠：每行默认只露"这是什么 + 下载/量化"，六七个字段全铺开时一屏看不到两个模型。
  const [openVlm, setOpenVlm] = useState<number | null>(null)
  // 量化弹窗：与新建任务页共用同一套 W4A16 流程，不另写一份。
  const [quantFor, setQuantFor] = useState<string | null>(null)

  const apply = (d: ModelCatalog, syncProxy = false) => {
    setCatalog(d)
    setVlm(d.vlm.map((m) => ({ ...m })))
    setTag(d.tag.map((t) => ({ ...t })))
    if (syncProxy) setProxy(d.proxy)
    setDirty(false)
  }

  const fail = (err: unknown) =>
    setError(err instanceof Error ? err.message : String(err))

  useEffect(() => {
    api.models().then((d) => apply(d, true)).catch(fail)
  }, [])

  // 下载期间的轮询：只刷"本地文件 + 进度"这几个字段，不覆盖用户正在编辑的
  // 名字/路径，也不清 dirty —— 否则一边下载一边改名，1 秒后就被轮询吞掉。
  const mergeLocal = (fresh: ModelCatalog) => {
    setVlm((prev) =>
      prev.map((row) => {
        const f = fresh.vlm.find((x) => x.key === row.key)
        return f
          ? {
              ...row,
              local_abs: f.local_abs,
              exists: f.exists,
              partial: f.partial,
              local_bytes: f.local_bytes,
              download: f.download,
              download_repo: f.download_repo,
            }
          : row
      }),
    )
    setTag((prev) =>
      prev.map((row) => {
        const f = fresh.tag.find((x) => x.backend === row.backend)
        return f
          ? {
              ...row,
              local_abs: f.local_abs,
              exists: f.exists,
              partial: f.partial,
              local_bytes: f.local_bytes,
              download: f.download,
              download_repo: f.download_repo,
            }
          : row
      }),
    )
  }

  const downloading =
    vlm.some((m) => isRunning(m.download)) || tag.some((t) => isRunning(t.download))
  useEffect(() => {
    if (!downloading) return
    const timer = setInterval(() => {
      api.models().then(mergeLocal).catch(() => undefined)
    }, 1200)
    return () => clearInterval(timer)
    // mergeLocal 每次渲染都会重建，但它只读 setState，不需要进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloading])

  const patchVlm = (index: number, patch: Partial<VlmModelEntry>) => {
    setVlm((prev) => prev.map((m, i) => (i === index ? { ...m, ...patch } : m)))
    setSaved('')
    setDirty(true)
  }

  const patchTag = (index: number, patch: Partial<TagModelEntry>) => {
    setTag((prev) => prev.map((t, i) => (i === index ? { ...t, ...patch } : t)))
    setSaved('')
    setDirty(true)
  }

  const addVlm = () => {
    setVlm((prev) => [
      ...prev,
      {
        key: '',
        path: '',
        precision: 'int4',
        family: 'qwen3vl',
        revision: null,
        quantized_path: null,
        note: '',
        source: 'managed',
        tiers: [],
        local_dir: '',
        local_abs: '',
        exists: false,
        partial: false,
        local_bytes: 0,
        download_repo: '',
        download: null,
      },
    ])
    setSaved('')
    setDirty(true)
  }

  const addTag = () => {
    setTag((prev) => [
      ...prev,
      {
        backend: '',
        repo: '',
        note: '',
        source: 'managed',
        local_dir: '',
        local_abs: '',
        exists: false,
        partial: false,
        local_bytes: 0,
        download_repo: '',
        download: null,
      },
    ])
    setSaved('')
    setDirty(true)
  }

  async function save() {
    setBusy(true)
    setError(null)
    setSaved('')
    try {
      apply(await api.saveModels({ vlm, tag }))
      setSaved('已保存，新建任务页会按新清单刷新')
    } catch (err) {
      fail(err)
    } finally {
      setBusy(false)
    }
  }

  // 下载前先保存：目录是按 key 存在覆盖文件里的，不存就等于"下了但页面不知道下了"。
  // 保存失败直接抛出去，下载不启动——总比下到旧目录、然后显示"未下载"强。
  const prepareDownload = async () => {
    apply(await api.saveModels({ vlm, tag }))
  }

  async function saveProxy(url: string) {
    setProxyBusy('save')
    setProxyMsg('')
    setProxyOk(null)
    try {
      const res = await api.saveProxy(url)
      apply(res.catalog, true)
      setProxyMsg(res.proxy ? `已保存：${res.proxy}` : '已清空，之后直连')
      setProxyOk(true)
    } catch (err) {
      setProxyMsg(err instanceof Error ? err.message : String(err))
      setProxyOk(false)
    } finally {
      setProxyBusy('')
    }
  }

  async function testProxy(url: string) {
    setProxyBusy('test')
    setProxyMsg('')
    setProxyOk(null)
    try {
      const res = await api.testProxy(url)
      setProxyOk(res.ok)
      setProxyMsg(
        res.ok
          ? `通：${res.via} → ${res.endpoint ?? 'HF'}，${res.latency_ms} ms`
          : `${res.via || '直连'} 不通：${res.error ?? '未知错误'}`,
      )
    } catch (err) {
      setProxyOk(false)
      setProxyMsg(err instanceof Error ? err.message : String(err))
    } finally {
      setProxyBusy('')
    }
  }

  // 配置里有、现在被隐藏掉的条目：给一键恢复，否则删了就只剩手写 JSON 一条路
  const hiddenVlm = (catalog?.base_vlm ?? []).filter((b) => !vlm.some((v) => v.key === b.key))
  const hiddenTag = (catalog?.base_tag ?? []).filter(
    (b) => !tag.some((t) => t.backend === b.backend),
  )

  return (
    <div className="mx-auto lx-page-enter flex max-w-5xl flex-col gap-3.5">
      <PageHeader
        icon={<Boxes size={17} />}
        eyebrow="可选清单"
        title="模型管理"
        description="决定新建任务能选到哪些模型；破限版默认排最前——原版遇 NSFW 图会整批漏标且不报错。"
        badges={
          catalog && (
            <StatusChip tone={catalog.overlay_exists ? 'accent' : 'neutral'}>
              {catalog.overlay_exists ? '已叠加本机清单' : '跟随配置默认'}
            </StatusChip>
          )
        }
        actions={
          <>
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1.5"
              disabled={busy}
              onClick={() => {
                setError(null)
                api.models().then(apply).catch(fail)
              }}
            >
              <RotateCcw size={13} />
              重新读取
            </button>
            <button
              type="button"
              className="lx-btn-primary inline-flex items-center gap-1.5"
              disabled={busy || !dirty}
              onClick={() => void save()}
            >
              <Save size={13} />
              {busy ? '保存中…' : dirty ? '保存' : '没有改动'}
            </button>
          </>
        }
      />

      {error && <ErrorBox error={error} title="出错了" />}
      {saved && !error && <p className="px-1 text-[12px] text-lx-success">{saved}</p>}

      <SectionPanel
        title="NL 模型"
        description="出自然语言的权重（Qwen-VL 类）；名字就是起服务时的 --model。"
        trailing={
          <button
            type="button"
            className="lx-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px]"
            onClick={addVlm}
          >
            <Plus size={12} />
            新增
          </button>
        }
      >
        <div>
          {vlm.map((m, i) => (
            <div key={`${m.key}-${i}`} className="border-b border-lx py-2 last:border-b-0">
              {/* 行头只回答两个问题：这是什么、本地有没有。其余六七个字段
                  收进展开里——它们改一次就够，却每次都占满一屏。 */}
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  className="min-w-0 flex-1 cursor-pointer text-left"
                  onClick={() => setOpenVlm(openVlm === i ? null : i)}
                >
                  <div className="flex items-center gap-1.5 truncate text-[12.5px] font-medium text-lx">
                    <span className="truncate">{m.note || m.key}</span>
                    {m.tiers.length > 0 && (
                      <span className="shrink-0 rounded-lx-sm border border-lx px-1 py-px text-[9.5px] font-normal text-lx-muted">
                        分档在用
                      </span>
                    )}
                  </div>
                  <div className="truncate font-mono text-[10.5px] text-lx-dim">
                    {m.key} · {m.path}
                  </div>
                </button>
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                    title="在本机把权重量化成 4bit（W4A16），跑起来省显存"
                    onClick={() => setQuantFor(m.key)}
                  >
                    <Gauge size={12} />
                    量化
                  </button>
                  <button
                    type="button"
                    className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                    onClick={() => setOpenVlm(openVlm === i ? null : i)}
                  >
                    {openVlm === i ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                    {openVlm === i ? '收起' : '编辑'}
                  </button>
                  <button
                    type="button"
                    className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                    disabled={m.tiers.length > 0 || vlm.length <= 1}
                    title={
                      m.tiers.length > 0
                        ? `分档 ${m.tiers.join('/')} 还在用它，先改 tiers.yaml`
                        : vlm.length <= 1
                          ? '至少保留一个 NL 模型'
                          : '从清单里移除'
                    }
                    onClick={() => {
                      setVlm((prev) => prev.filter((_, k) => k !== i))
                      setDirty(true)
                      setSaved('')
                    }}
                  >
                    <Trash2 size={12} />
                    移除
                  </button>
                </div>
              </div>

              <div className="mt-1.5">
                <LocalFilesBar
                  kind="vlm"
                  entry={m}
                  onError={setError}
                  onRefresh={mergeLocal}
                  onPrepare={prepareDownload}
                  onUseLocalDir={() => patchVlm(i, { path: m.local_abs })}
                />
              </div>

              {openVlm === i && (
                <div className="mt-2 grid gap-2 md:grid-cols-2">
              <Field label="名字（key）">
                <input
                  className="lx-input font-mono text-xs"
                  value={m.key}
                  spellCheck={false}
                  placeholder="qwen3vl-4b"
                  onChange={(e) => patchVlm(i, { key: e.target.value })}
                />
              </Field>
              <Field label="权重（HF 仓库或本地目录）">
                <input
                  className="lx-input font-mono text-xs"
                  value={m.path}
                  spellCheck={false}
                  placeholder="Qwen/Qwen3-VL-4B-Instruct"
                  onChange={(e) => patchVlm(i, { path: e.target.value })}
                />
              </Field>
              <Field label="精度">
                <select
                  className="lx-select"
                  value={m.precision}
                  onChange={(e) => patchVlm(i, { precision: e.target.value })}
                >
                  {PRECISIONS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="家族（决定指令模板）">
                <input
                  className="lx-input font-mono text-xs"
                  value={m.family}
                  spellCheck={false}
                  placeholder="qwen3vl"
                  onChange={(e) => patchVlm(i, { family: e.target.value })}
                />
              </Field>
              <Field label="备注（下拉里显示）" className="md:col-span-2">
                <input
                  className="lx-input text-xs"
                  value={m.note}
                  placeholder="例如：无审查版，8B"
                  onChange={(e) => patchVlm(i, { note: e.target.value })}
                />
              </Field>
              <Field label="下载到" className="md:col-span-2">
                <input
                  className="lx-input font-mono text-[11px]"
                  value={m.local_dir}
                  spellCheck={false}
                  placeholder="留空 = models/nl/<key>"
                  onChange={(e) => patchVlm(i, { local_dir: e.target.value })}
                />
              </Field>
                </div>
              )}

              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10.5px] text-lx-dim">
                <span
                  className={`rounded-lx-sm border px-1.5 py-px ${
                    m.source === 'managed'
                      ? 'border-lx-accent text-lx-accent'
                      : 'border-lx text-lx-muted'
                  }`}
                >
                  {m.source === 'managed' ? '本机改过' : '来自配置'}
                </span>
                {m.tiers.length > 0 && <span>分档：{m.tiers.join(' / ')}</span>}
              </div>
            </div>
          ))}
        </div>

        {hiddenVlm.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-lx pt-2 text-[11px] text-lx-muted">
            <span>配置里有、当前隐藏：</span>
            {hiddenVlm.map((b) => (
              <button
                key={b.key}
                type="button"
                className="lx-btn-ghost px-2 py-0.5 text-[11px]"
                onClick={() => {
                  setVlm((prev) => [...prev, { ...b }])
                  setDirty(true)
                }}
              >
                {b.key} 恢复
              </button>
            ))}
          </div>
        )}
      </SectionPanel>

      <SectionPanel
        title="Tag 模型"
        description="下完的目录可填进新建任务的「权重目录」（pixai 走 HF 缓存，下载仅留副本）。"
        trailing={
          <button
            type="button"
            className="lx-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px]"
            onClick={addTag}
          >
            <Plus size={12} />
            新增
          </button>
        }
      >
        <div>
          {tag.map((t, i) => (
            <div key={`${t.backend}-${i}`} className="grid gap-2 border-b border-lx py-3 last:border-b-0 md:grid-cols-2">
              <Field label="后端">
                <input
                  className="lx-input font-mono text-xs"
                  value={t.backend}
                  spellCheck={false}
                  placeholder="wd-eva02"
                  onChange={(e) => patchTag(i, { backend: e.target.value })}
                />
              </Field>
              <Field label="权重仓库">
                <input
                  className="lx-input font-mono text-xs"
                  value={t.repo}
                  spellCheck={false}
                  placeholder="ashen-sensored/wd-eva02-tagger-2026-canary"
                  onChange={(e) => patchTag(i, { repo: e.target.value })}
                />
              </Field>
              <Field label="备注" className="md:col-span-2">
                <input
                  className="lx-input text-xs"
                  value={t.note}
                  placeholder="可选"
                  onChange={(e) => patchTag(i, { note: e.target.value })}
                />
              </Field>
              <Field label="下载到" className="md:col-span-2">
                <input
                  className="lx-input font-mono text-[11px]"
                  value={t.local_dir}
                  spellCheck={false}
                  placeholder="留空 = models/<后端名>"
                  onChange={(e) => patchTag(i, { local_dir: e.target.value })}
                />
              </Field>

              <LocalFilesBar
                kind="tag"
                entry={t}
                onError={setError}
                onRefresh={mergeLocal}
                onPrepare={prepareDownload}
              />

              <div className="md:col-span-2 flex items-center justify-between gap-2">
                <span className="text-[11px] text-lx-dim">
                  {t.source === 'managed' ? '本机改过' : '来自配置'}
                </span>
                <button
                  type="button"
                  className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
                  disabled={tag.length <= 1}
                  onClick={() => {
                    setTag((prev) => prev.filter((_, k) => k !== i))
                    setDirty(true)
                    setSaved('')
                  }}
                >
                  <Trash2 size={12} />
                  移除
                </button>
              </div>
            </div>
          ))}
        </div>

        {hiddenTag.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-lx pt-2 text-[11px] text-lx-muted">
            <span>配置里有、当前隐藏：</span>
            {hiddenTag.map((b) => (
              <button
                key={b.backend}
                type="button"
                className="lx-btn-ghost px-2 py-0.5 text-[11px]"
                onClick={() => {
                  setTag((prev) => [...prev, { ...b }])
                  setDirty(true)
                }}
              >
                {b.backend} 恢复
              </button>
            ))}
          </div>
        )}
      </SectionPanel>

      <SectionPanel
        title="下载代理"
        description="只作用于本页的权重下载。留空 = 直连。"
        trailing={
          <span className="text-[11px] text-lx-dim">
            {proxy ? <span className="text-lx-accent">{proxy}</span> : '未设置'}
          </span>
        }
      >
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-end gap-2">
            <label className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="text-[11px] text-lx-muted">代理地址</span>
              <input
                className="lx-input font-mono text-xs"
                value={proxy}
                spellCheck={false}
                placeholder={
                  catalog?.socks_supported
                    ? 'http://127.0.0.1:7890 或 socks5://127.0.0.1:1080'
                    : 'http://127.0.0.1:7890（装 socksio 才支持 socks5）'
                }
                onChange={(e) => {
                  setProxy(e.target.value)
                  setProxyMsg('')
                  setProxyOk(null)
                }}
              />
            </label>
            <button
              type="button"
              className="lx-btn-ghost px-2.5 py-1.5 text-[11px]"
              disabled={!!proxyBusy}
              onClick={() => void testProxy(proxy)}
            >
              {proxyBusy === 'test' ? '测试中…' : '测试连通'}
            </button>
            <button
              type="button"
              className="lx-btn-primary px-2.5 py-1.5 text-[11px]"
              disabled={!!proxyBusy}
              onClick={() => void saveProxy(proxy)}
            >
              {proxyBusy === 'save' ? '保存中…' : '保存代理'}
            </button>
            {proxy && (
              <button
                type="button"
                className="lx-btn-ghost px-2.5 py-1.5 text-[11px]"
                disabled={!!proxyBusy}
                onClick={() => void saveProxy('')}
              >
                清空（直连）
              </button>
            )}
          </div>

          {!catalog?.socks_supported && (
            <p className="text-[11px] text-lx-warning">
              socks5 需要 socksio：env\python\python.exe -m pip install socksio
            </p>
          )}
          {proxyMsg && (
            <p className={`text-[11px] ${proxyOk ? 'text-lx-success' : 'text-lx-warning'}`}>
              {proxyMsg}
            </p>
          )}
        </div>
      </SectionPanel>

      <p className="px-1 text-[11px] leading-relaxed text-lx-dim">
        <Tags size={12} className="mr-1 inline align-[-2px]" />
        只存与配置不同的条目 → {catalog?.overlay ?? 'configs/models.json'}
      </p>

      {quantFor && (
        <QuantizeDialog
          open
          modelKey={quantFor}
          onCancel={() => setQuantFor(null)}
          onUse={() => {
            // 量化完成后重拉清单：quantized_path 变了，下面"权重精度"才有得选。
            setQuantFor(null)
            api.models().then((d) => apply(d)).catch(fail)
          }}
        />
      )}
    </div>
  )
}
