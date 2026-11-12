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
  Download,
  FolderOpen,
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
import { ENDPOINT_PRESETS, endpointPresetOf } from '../lib/endpoint'
import { PageHeader, SectionPanel, StatusChip } from '../components/ui/launcher'
import { QuantizeDialog } from '../components/QuantizeDialog'
import { GgufConvertDialog } from '../components/GgufConvertDialog'
import { QuantizeMenu, type QuantPick } from '../components/QuantizeMenu'
import { ErrorBox } from '../components/ui/controls'
import { useFolderPicker } from '../components/FolderPicker'

// 模型条目里的 precision = "默认用哪份权重"。两条链路的取值域不是一回事：
//   vLLM 三档是**加载方式**（同一份权重，不换文件）；
//   GGUF 五档换的是**权重文件本身**（q4/q6 就是那两个 .gguf）。
// 必须都列出来：自定义模型填完路径后，如果这里只有 vLLM 那几档，就永远选不到
// 自己压出来的 Q4/Q6 —— 用户报的"添加自定义模型没法选"就是这个。
const VLLM_PRECISIONS = ['bf16', 'fp8', 'w4a16'] as const
const GGUF_TIER_PRECISIONS = ['f16', 'q4', 'q5', 'q6', 'q8'] as const
const PRECISION_LABELS: Record<string, string> = {
  bf16: 'BF16',
  fp8: 'FP8',
  w4a16: 'W4A16（AWQ/GPTQ）',
  f16: 'F16（完整 GGUF）',
  q4: 'Q4_K_M',
  q5: 'Q5_K_M',
  q6: 'Q6_K',
  q8: 'Q8_0',
  // 老清单里留下的写法（tiers.yaml 还有 int4）：不给一条 option 的话，
  // 下拉框会对不上 value、显示成别的档，用户以为精度被改了。
  int4: 'int4（旧档，等同 W4A16）',
  int8: 'int8（旧档）',
  fp16: 'fp16（旧档）',
  fp32: 'fp32（旧档）',
  auto: 'auto（按显存自动）',
  awq: 'awq（旧档，等同 W4A16）',
  gptq: 'gptq（旧档，等同 W4A16）',
}
const STANDARD_PRECISIONS: readonly string[] = [...VLLM_PRECISIONS, ...GGUF_TIER_PRECISIONS]
/** 这一档到底是"加载方式"还是"GGUF 文件档"：只有后者要看本地有没有压出来 */
const isGgufTier = (p: string) => (GGUF_TIER_PRECISIONS as readonly string[]).includes(p)

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
  // 打开位置失败（服务端没桌面环境）时显示的路径。空 = 没失败。
  const [openHint, setOpenHint] = useState('')
  const key = 'key' in entry ? entry.key : entry.backend
  const download = entry.download
  const running = isRunning(download)
  // NL 的"权重"既可能是仓库名也可能是本地目录；tag 的权重目录在新建任务页另填
  const path = 'key' in entry ? entry.path : entry.repo
  // 有文件 ≠ 下完了：取消/报错都会留下半截。只有非 partial 才算下完。
  const downloaded = entry.exists && !entry.partial
  // 下完 ≠ 跑得起来：VLM 下的就是原始权重，GGUF 档要等「本地量化」压出来。
  // 所以这里不再拿 gguf 有没有来判"下没下"，只把它当"还差哪一步"的提示。
  const missing = entry.missing_files ?? []
  // 有文件 ≠ 有原始权重：目录里只躺着量化产物（.gguf）时 exists 也是 true。
  // 以前这就被显示成"原始权重已下载"，然后点「本地量化」又说找不到输入。
  const rawReady = 'raw_ready' in entry ? Boolean(entry.raw_ready) : true
  const canUseLocal =
    kind === 'vlm' && !!onUseLocalDir && downloaded && rawReady && path !== entry.local_abs

  async function act(label: string, fn: () => Promise<ModelCatalog | void>) {
    setPending(label)
    onError('')
    setOpenHint('')
    try {
      const res = await fn()
      if (res) onRefresh(res)
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err))
    } finally {
      setPending('')
    }
  }

  /**
   * 「打开位置」：窗口弹不出来（服务端没桌面）时把路径摆出来。
   *
   * 不塞进 onError：那是会拦住后续操作的红字，而"窗口没弹"不影响模型能不能用。
   */
  async function reveal(withPath: () => Promise<{ path: string; opened: boolean; detail: string }>) {
    setPending('reveal')
    onError('')
    setOpenHint('')
    try {
      const r = await withPath()
      if (!r.opened) setOpenHint(`${r.detail}：${r.path}`)
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
            {/* 起步几秒的连接建立期不算 ETA：那时候的速度差一个数量级，
                按它算出来是"剩 35 小时"，看着像卡死。 */}
            {download.eta != null ? (
              <span className="text-lx-dim">剩 {Math.max(0, Math.round(download.eta))}s</span>
            ) : (
              (download.elapsed ?? 0) < 5 && <span className="text-lx-dim">测速中…</span>
            )}
            <span className="truncate text-lx-dim">{download.current}</span>
          </>
        ) : downloaded ? (
          <>
            <span className={rawReady ? 'text-lx-success' : 'text-lx-warning'}>
              {kind === 'tag' ? '本地已有' : rawReady ? '原始权重已下载' : '目录里只有量化后的 GGUF'}
            </span>
            <span className="font-mono tabular-nums text-lx-muted">
              {fmtBytes(entry.local_bytes)}
            </span>
            <span className="truncate font-mono text-[10px] text-lx-dim" title={entry.local_abs}>
              {entry.local_dir}
            </span>
            {/* 窗口弹不出来时的兜底：路径本身就是答案，给个能选中的文本 */}
            {openHint && (
              <span className="select-all break-all font-mono text-[10px] text-lx-dim">
                {openHint}
              </span>
            )}
            {!rawReady && (
              <span className="text-lx-dim">
                没有 config.json / safetensors —— 直接跑没问题；要再走一遍本地量化得先下原始权重
              </span>
            )}
            {/* 原始权重到位 ≠ 能跑：llama.cpp 只吃 GGUF，还得先本地量化。
                这里点名缺哪份，而不是让人对着"下完了还报错"去猜。
                ★ 只判 llama.cpp 这条路。W4A16（vLLM）是 AWQ/GPTQ 的
                safetensors，和 GGUF 不是一套东西——文案不写清的话，压完
                W4A16 的人会以为量化白做了（实测反馈过）。 */}
            {missing.length > 0 && (
              <span className="text-lx-warning">
                还没压 GGUF（llama.cpp 用），缺 {missing.join('、')}
              </span>
            )}
            {download?.status === 'error' && (
              <span className="text-lx-warning">{download.message}</span>
            )}
          </>
        ) : (
          <>
            <span className="text-lx-dim">
              {entry.partial
                ? '上次没下完，点下载会接着下'
                : download?.status === 'error'
                  ? download.message
                  : entry.download_repo
                    ? '未下载'
                    : '这条没有可下载的仓库（权重是本地目录）'}
            </span>
            <span className="truncate font-mono text-[10px] text-lx-dim" title={entry.local_abs}>
              {entry.local_dir}
            </span>
            {openHint && (
              <span className="select-all break-all font-mono text-[10px] text-lx-dim">
                {openHint}
              </span>
            )}
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
        ) : downloaded ? (
          <>
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
              disabled={!!pending}
              onClick={() => void reveal(() => api.revealLocalDir(kind, key))}
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
            {/* 下载入口在"目录里有文件"时也要留着：
                · 目录里只剩量化产物（没有 safetensors）→ 这是唯一能把原始权重
                  弄回来的入口，不给人就等于卡死在这个状态里；
                · 已经有原始权重 → 就是"重新下载/校验补漏"（下完的文件按大小
                  跳过，缺的和半截的才真下），比"先删 16G 再下"温和得多。 */}
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
              disabled={!!pending || !entry.download_repo}
              title={
                entry.download_repo
                  ? rawReady
                    ? `从 ${entry.download_repo} 重新下载：已下完的文件会跳过，只补缺的`
                    : `从 ${entry.download_repo} 下载原始权重`
                  : '没有可下载的仓库'
              }
              onClick={() => {
                // 没原始权重时点下去是真要拉十几 G，先问一句；
                // 已经有的时候基本是幂等补齐，不打断人。
                if (!rawReady && !window.confirm(
                  `从 ${entry.download_repo} 下载原始权重？要十几 G（原有权重不受影响）。`,
                )) return
                void act('download', async () => {
                  await onPrepare?.()
                  await api.startDownload(kind, key, kind === 'vlm' ? 'raw' : '')
                  // 同上：起完立刻回读，进度条马上出来
                  return await api.models()
                })
              }}
            >
              <Download size={12} />
              {pending === 'download'
                ? '启动中…'
                : rawReady
                  ? '重新下载'
                  : '下载原始权重'}
            </button>
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
                onClick={() => void reveal(() => api.revealLocalDir(kind, key))}
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
            {/* 下载只有一种内容：`path` 指的原始权重（safetensors + config +
                tokenizer）。网上的成品 GGUF 一律不下——量化档只能由下面的
                「本地量化」现场压出来。 */}
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]"
              disabled={!!pending || !entry.download_repo}
              title={
                entry.download_repo
                  ? `从 ${entry.download_repo} 下载原始权重`
                  : '没有可下载的仓库'
              }
              onClick={() =>
                void act('download', async () => {
                  await onPrepare?.()
                  await api.startDownload(kind, key, kind === 'vlm' ? 'raw' : '')
                  // ★ 起完立刻回读一次。轮询是靠"某一行已经在下载"才启动的
                  // （见下面的 downloading），不主动刷的话这一行永远停在
                  // "未下载"：点一次像没反应，得再点一次/手动刷新才动。
                  return await api.models()
                })
              }
            >
              <Download size={12} />
              {pending === 'download'
                ? '启动中…'
                : entry.partial
                  ? '继续下载'
                  : kind === 'vlm'
                    ? '下载原始权重'
                    : '下载'}
            </button>
          </>
        )}
      </div>
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
  // 下载源（镜像站）：跟代理是替代关系——设了镜像就不用再走梯子。
  const [endpointPick, setEndpointPick] = useState('')
  const [endpointDraft, setEndpointDraft] = useState('')
  const [endpointMsg, setEndpointMsg] = useState('')
  const [endpointBusy, setEndpointBusy] = useState(false)
  // 大文件下载连接数：镜像站总速随连接数线性涨，改一次 16G 差好几个小时。
  const [conns, setConns] = useState(4)
  const [connsBusy, setConnsBusy] = useState(false)
  // 折叠：每行默认只露"这是什么 + 下载/量化"，六七个字段全铺开时一屏看不到两个模型。
  const [openVlm, setOpenVlm] = useState<number | null>(null)
  // 量化弹窗：与新建任务页共用同一套 W4A16 流程，不另写一份。
  // 带上菜单里点的算法，弹窗里就是那一档，不用再选一次。
  const [quantFor, setQuantFor] = useState<{ key: string; method: string } | null>(null)
  // GGUF 压缩弹窗：与 W4A16 是两条独立的产物（.gguf vs safetensors），
  // 产物不同所以弹窗仍分开，入口统一在「量化」菜单里。
  const [convertFor, setConvertFor] = useState<{ key: string; target: string } | null>(null)

  const apply = (d: ModelCatalog, syncProxy = false) => {
    setCatalog(d)
    setVlm(d.vlm.map((m) => ({ ...m })))
    setTag(d.tag.map((t) => ({ ...t })))
    if (syncProxy) {
      setProxy(d.proxy)
      setEndpointPick(endpointPresetOf(d.endpoint))
      setEndpointDraft(d.endpoint)
      setConns(d.conns ?? 4)
    }
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
              // 这几个也属于"本地文件现状"：不跟着刷的话，下完原始权重仍显示
              // "只有量化后的 GGUF"（反过来也一样），要手动刷新才变。
              missing_files: f.missing_files,
              gguf_ready: f.gguf_ready,
              raw_ready: f.raw_ready,
              ready: f.ready,
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
              missing_files: f.missing_files,
              ready: f.ready,
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

  const folder = useFolderPicker()

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
        // 新条目按默认引擎（llama.cpp）的默认档起步：f16 = 完整权重
        precision: 'f16',
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

  async function saveEndpoint(url: string) {
    setEndpointBusy(true)
    setEndpointMsg('')
    try {
      const res = await api.saveEndpoint(url)
      apply(res.catalog, true)
      setEndpointMsg(`已保存，当前生效：${res.effective}`)
    } catch (err) {
      setEndpointMsg(err instanceof Error ? err.message : String(err))
    } finally {
      setEndpointBusy(false)
    }
  }

  async function saveConns(n: number) {
    setConnsBusy(true)
    setEndpointMsg('')
    try {
      const res = await api.saveConns(n)
      apply(res.catalog, true)
      setEndpointMsg(`连接数已设为 ${res.conns}，下次下载生效`)
    } catch (err) {
      setEndpointMsg(err instanceof Error ? err.message : String(err))
    } finally {
      setConnsBusy(false)
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
        description="决定新建任务能选到哪些模型"
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
        description="出自然语言的权重"
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
                  {/* 两条量化路合成一个入口：它们其实是"同一个问题的两种答案"
                      （给 llama.cpp 出 GGUF、给 vLLM 出 W4A16），并排摆着只会
                      让人以为要各点一次。菜单里按引擎分组、各自列档位。
                      ★ 这个按钮必须常驻：以前 GGUF 那条只挂在"还没下载"的工具栏
                      里，下完权重就消失，用户以为压不了。 */}
                  <QuantizeMenu
                    disabled={!m.exists || !!m.partial}
                    hint="先下载原始权重，才能量化"
                    onPick={(pick: QuantPick) => {
                      if (pick.route === 'gguf') setConvertFor({ key: m.key, target: pick.target })
                      else setQuantFor({ key: m.key, method: pick.method })
                    }}
                  />
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
                  {/* 不在两组里的写法（老清单的 int4、或手写的怪值）也要留一条，
                      否则 value 对不上，下拉框显示成别的档 */}
                  {!STANDARD_PRECISIONS.includes(m.precision) && (
                    <option value={m.precision}>
                      {PRECISION_LABELS[m.precision] ?? m.precision}
                    </option>
                  )}
                  <optgroup label="vLLM（加载方式，不换文件）">
                    {VLLM_PRECISIONS.map((p) => (
                      <option key={p} value={p}>
                        {PRECISION_LABELS[p]}
                      </option>
                    ))}
                  </optgroup>
                  <optgroup label="llama.cpp（GGUF 档，换的是权重文件）">
                    {GGUF_TIER_PRECISIONS.map((p) => (
                      <option key={p} value={p}>
                        {PRECISION_LABELS[p]}
                        {/* 按"文件真的在"算，不是按配置里登记了就算：
                            登记过但文件被删/改名的档，标"已压"只会骗人 */}
                        {m.gguf_ready?.includes(p) ? ' · 已压' : ''}
                      </option>
                    ))}
                  </optgroup>
                </select>
                {/* 选了没压过的那一档，启动才会报缺文件；在这儿先提醒一句，
                    别让人启动失败后再回来猜是哪份权重没做。 */}
                {isGgufTier(m.precision) && !m.gguf_ready?.includes(m.precision) && (
                  <span className="mt-1 block text-[10.5px] leading-relaxed text-lx-dim">
                    本地还没有这一档，先用行首的「量化」压一份
                  </span>
                )}
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
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-[11px]"
                    value={m.local_dir}
                    spellCheck={false}
                    placeholder="留空 = models/nl/<key>"
                    onChange={(e) => patchVlm(i, { local_dir: e.target.value })}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-2.5"
                    onClick={() => folder.pick(m.local_dir || undefined, (p) => patchVlm(i, { local_dir: p }))}
                    aria-label="浏览下载目录"
                  >
                    <FolderOpen size={14} />
                  </button>
                </div>
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
        description="下载目录可填进新建任务的「权重目录」"
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
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-[11px]"
                    value={t.local_dir}
                    spellCheck={false}
                    placeholder="留空 = models/<后端名>"
                    onChange={(e) => patchTag(i, { local_dir: e.target.value })}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-2.5"
                    onClick={() => folder.pick(t.local_dir || undefined, (p) => patchTag(i, { local_dir: p }))}
                    aria-label="浏览下载目录"
                  >
                    <FolderOpen size={14} />
                  </button>
                </div>
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
        title="下载网络"
        description="只作用于本页的权重下载"
        trailing={
          <span className="text-[11px] text-lx-dim">
            {proxy ? <span className="text-lx-accent">{proxy}</span> : '未设置'}
          </span>
        }
      >
        <div className="flex flex-col gap-2">
          {/* 下载源：设了镜像就不必再挂梯子——梯子按流量计费，16G 一份权重
              走代理是很难受的；镜像站国内直连通常也更快。 */}
          <div className="flex flex-wrap items-end gap-2">
            <label className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="text-[11px] text-lx-muted">下载源</span>
              <select
                className="lx-select text-xs"
                value={endpointPick}
                onChange={(e) => {
                  const v = e.target.value
                  setEndpointPick(v)
                  setEndpointMsg('')
                  if (v === 'custom') setEndpointDraft(catalog?.endpoint || 'https://')
                }}
              >
                {ENDPOINT_PRESETS.map((p) => (
                  <option key={p.key || 'default'} value={p.key}>
                    {p.label}
                  </option>
                ))}
                <option value="custom">自定义…</option>
              </select>
            </label>
            {endpointPick === 'custom' && (
              <label className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="text-[11px] text-lx-muted">地址</span>
                <input
                  className="lx-input font-mono text-xs"
                  spellCheck={false}
                  value={endpointDraft}
                  placeholder="https://hf-mirror.com"
                  onChange={(e) => {
                    setEndpointDraft(e.target.value)
                    setEndpointMsg('')
                  }}
                />
              </label>
            )}
            {/* 连接数：镜像站实测"单条约 0.35 MB/s、总速随连接数线性涨"，
                16.3 G 在 4 条下要 4 小时、16 条约 45 分钟。改完下次下载生效。 */}
            <label className="flex w-[84px] shrink-0 flex-col gap-1">
              <span className="text-[11px] text-lx-muted">连接数</span>
              <select
                className="lx-select text-xs"
                value={String(conns)}
                disabled={connsBusy}
                onChange={(e) => {
                  const n = Number(e.target.value)
                  setConns(n)
                  void saveConns(n)
                }}
              >
                {[1, 4, 8, 16, 24, 32].map((n) => (
                  <option key={n} value={n}>
                    {n === 1 ? '1（单连接）' : n}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="lx-btn-primary px-2.5 py-1.5 text-[11px]"
              disabled={endpointBusy}
              onClick={() =>
                void saveEndpoint(endpointPick === 'custom' ? endpointDraft : endpointPick)
              }
            >
              {endpointBusy ? '保存中…' : '保存下载源'}
            </button>
          </div>
          <p className="text-[11px] text-lx-dim">
            当前生效：<span className="font-mono">{catalog?.effective_endpoint ?? '—'}</span>
            {catalog?.env_endpoint && '（来自环境变量 HF_ENDPOINT；页面设置优先）'}
          </p>
          {endpointMsg && <p className="text-[11px] text-lx-muted">{endpointMsg}</p>}

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
              socks5 需先装 socksio：env\python\python.exe -m pip install socksio
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
          modelKey={quantFor.key}
          initialMethod={quantFor.method}
          onCancel={() => setQuantFor(null)}
          onUse={() => {
            // 量化完成后重拉清单：quantized_path 变了，下面"权重精度"才有得选。
            setQuantFor(null)
            api.models().then((d) => apply(d)).catch(fail)
          }}
        />
      )}

      {convertFor && (
        <GgufConvertDialog
          open
          modelKey={convertFor.key}
          initialTarget={convertFor.target}
          onClose={() => {
            setConvertFor(null)
            // 压完 gguf / gguf_quants 会写回覆盖文件，"缺哪个文件"的提示要跟着变
            api.models().then((d) => apply(d)).catch(fail)
          }}
        />
      )}

      {folder.dialog}
    </div>
  )
}
