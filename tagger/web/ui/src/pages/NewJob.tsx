import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Boxes,
  Cloud,
  CloudOff,
  Cpu,
  FolderOpen,
  FolderSearch,
  Gauge,
  HardDrive,
  Layers,
  Package,
  Play,
  RefreshCw,
  Server,
  Sparkles,
  Square,
  Tags,
  TriangleAlert,
} from 'lucide-react'
import {
  api,
  ApiError,
  type Defaults,
  type Health,
  type JobSpec,
  type NlApiProbe,
  type PlanPreview,
  type VllmPlan,
  type VllmProbe,
  type VllmState,
} from '../lib/api'
import { basename, fmtCount } from '../lib/format'
import { PageHeader, SectionPanel, SettingRow, StatusChip } from '../components/ui/launcher'
import { ChoiceGrid, ErrorBox, ToggleRow } from '../components/ui/controls'
import { OverviewFigure, OverviewFigureRow } from '../components/ui/OverviewFigures'
import { FolderPicker } from '../components/FolderPicker'
import { MultiGpuPanel } from '../components/MultiGpuPanel'
import { QuantizeDialog } from '../components/QuantizeDialog'
import { ShardAssets } from '../components/ShardAssets'

const BACKENDS = [
  { value: 'wd-eva02', label: 'wd-eva02（本地权重，推荐）' },
  { value: 'pixai', label: 'pixai tagger' },
  { value: 'cl-tagger-v2', label: 'cl-tagger-v2' },
  { value: 'onnx', label: 'onnx' },
]

// vlm 引擎下 tag 可以完全交给 VLM 自己出（不加载本地后端）
const BACKENDS_VLM = [
  { value: 'none', label: 'VLM 自己出 tag（不加载本地后端）' },
  ...BACKENDS,
]

const PRECISIONS = ['bf16', 'tf32', 'fp32'] as const
// VLM 权重精度。与上面那个 tag 分类头的 fp32/tf32/bf16 是两件事，别混用：
//   bf16  → 不传 --quantization，按权重自带 dtype 加载（最稳，显存最贵）
//   fp8   → --quantization fp8，vLLM 在线量化（Ada 及以上才支持）
//   w4a16 → 4bit 权重 + 16bit 激活，必须有预量化的 AWQ/GPTQ 权重
const VLLM_PRECISIONS = ['bf16', 'fp8', 'w4a16'] as const
const VLLM_PRECISION_LABELS: Record<(typeof VLLM_PRECISIONS)[number], string> = {
  bf16: 'BF16',
  fp8: 'FP8',
  w4a16: 'W4A16',
}

// NL 的本地输入只认分片文件（.parquet / .zip / .tar），或"装着分片的目录"。
// 平铺的图片目录是新人最容易踩的坑：任务能创建，开跑第一秒就报
// "里没有 .zip 分发包"，而页面上看起来只是"跑失败了"。
function looksLikeShard(path: string): boolean {
  return /\.(parquet|zip|tar|tgz|tar\.gz)$/i.test(path.trim())
}
const DEVICES = ['auto', 'cuda:0', 'cuda:1', 'cpu'] as const
const BATCHES = ['1', '2', '4', '8', '16'] as const
// 预处理位置。解码都在 CPU，区别只在缩放/归一化：pil = CPU（现状），
// tensor = 上卡。上卡要 CUDA，且只有"解码比前向重"时才有收益。
const PREPROCESS = ['pil', 'tensor'] as const
const PREPROCESS_LABELS: Record<(typeof PREPROCESS)[number], string> = {
  pil: 'CPU（默认）',
  tensor: 'GPU',
}
const LAYOUTS = ['flat', 'mirror'] as const
// 打标方式（页首三选）。与后端的映射：
//   tag  → engine=local（只加载 tag 分类头）
//   nl   → engine=vlm + output_mode=nl + backend=none
//   both → engine=vlm + output_mode=both（tag 分类头与 VLM 并行，backend=none 则 tag 也由 VLM 出）
// 单模式下后端会被强制关掉，否则 run_client 会多写一份没人要的 tag。见 jobs.vlm_backend_for。
const MODES = ['tag', 'nl', 'both'] as const
const ARTIFACT_FORMATS = ['parquet', 'json'] as const
/**
 * NL 推理来源。local = 本机/自建服务（要起进程、有环境指纹）；
 * openai / anthropic = 外部 API（不加载权重、不用起服务、没有指纹）。
 * 文案与默认地址一律由 /api/defaults 的 vlm_providers 给，前端不留第二份
 * 标签表——两份文案迟早会分叉。
 */
const VLM_PROVIDERS = ['local', 'openai', 'anthropic'] as const
type VlmProvider = (typeof VLM_PROVIDERS)[number]

/**
 * 硬件保护的一个数字阈值。范围与后端 `jobs.GUARD_KNOBS` 一一对应。
 *
 * 内部留一份 draft 是刻意的：受控数字框每敲一个键就 clamp，会把"82"敲成
 * "8" 的瞬间改成下限 40，用户看到的是输入框自己跳数；所以编辑期只存字符串，
 * 失焦/回车才提交并夹到区间里。
 */
function GuardKnob({
  label,
  hint,
  suffix,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string
  hint?: string
  suffix?: string
  value: number
  min: number
  max: number
  step: number
  onChange: (v: number) => void
}) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => setDraft(String(value)), [value])

  const commit = (raw: string) => {
    const n = Number(raw)
    if (raw.trim() === '' || !Number.isFinite(n)) {
      setDraft(String(value))
      return
    }
    const clamped = Math.min(max, Math.max(min, n))
    setDraft(String(clamped))
    if (clamped !== value) onChange(clamped)
  }

  return (
    <label className="flex flex-col gap-1">
      <span className="flex items-center justify-between gap-2 text-[11px] text-lx-secondary">
        <span className="truncate">{label}</span>
        <span className="flex shrink-0 items-baseline gap-1">
          <input
            type="number"
            className="lx-input w-20 px-1.5 py-1 text-right font-mono text-[11px] tabular-nums"
            min={min}
            max={max}
            step={step}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={(e) => commit(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commit((e.target as HTMLInputElement).value)
            }}
            aria-label={label}
          />
          {suffix && <span className="text-[10px] text-lx-dim">{suffix}</span>}
        </span>
      </span>
      {hint && <span className="text-[10px] leading-snug text-lx-dim">{hint}</span>}
    </label>
  )
}

function vllmStatusLabel(s: VllmState | null): string {
  if (!s) return '未知'
  if (s.status === 'ready') return '运行中'
  if (s.status === 'starting') return '启动中'
  if (s.status === 'stopping') return '停止中'
  if (s.status === 'failed') return '启动失败'
  return '未启动'
}

export function NewJob({
  active,
  onStarted,
  onOpenModels,
}: {
  /** 当前是否在这个页面。切回来自动刷新一次模型清单（模型管理页可能刚改过） */
  active: boolean
  onStarted: (id: string) => void
  onOpenModels: () => void
}) {
  const [defaults, setDefaults] = useState<Defaults | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [mode, setMode] = useState<(typeof MODES)[number]>('tag')
  const [inputDir, setInputDir] = useState('')
  const [outDir, setOutDir] = useState('')
  const [backend, setBackend] = useState('wd-eva02')
  const [modelDir, setModelDir] = useState('')
  const [device, setDevice] = useState<string>('auto')
  const [precision, setPrecision] = useState<string>('bf16')
  const [batch, setBatch] = useState(4)
  const [preprocess, setPreprocess] = useState<string>('pil')
  const [threshold, setThreshold] = useState(0.35)
  const [limit, setLimit] = useState(0)
  const [force, setForce] = useState(false)
  const [recursive, setRecursive] = useState(true)
  const [includeZips, setIncludeZips] = useState(true)
  const [includeLoose, setIncludeLoose] = useState(true)
  const [picker, setPicker] = useState(false)
  const [scan, setScan] = useState<{ path: string; images: number; zips: number } | null>(null)
  const [preview, setPreview] = useState<PlanPreview | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState<string | null>(null)

  // ---- NL 模型（mode = nl / both）
  const [vlmUrl, setVlmUrl] = useState('http://127.0.0.1:8000')
  const [vlmModel, setVlmModel] = useState('tagger')
  // 推理来源。apiBase/apiKey 只在 openai / anthropic 下用：前者留空按来源
  // 取默认域名，后者只在提交那一次请求里走一趟（快照里会被抹成 ***）。
  const [vlmProvider, setVlmProvider] = useState<VlmProvider>('local')
  const [apiBase, setApiBase] = useState('')
  const [apiKey, setApiKey] = useState('')
  // 探测结果与「记住密钥」。存档只回脱敏值：浏览器的内存里永远只有用户
  // 刚敲进去的那一份明文，后端不会把存过的 key 再发下来。
  const [apiProbe, setApiProbe] = useState<NlApiProbe | null>(null)
  const [apiProbeBusy, setApiProbeBusy] = useState(false)
  const [rememberKey, setRememberKey] = useState(false)
  // undefined = 还没动过，用 defaults 里的存档；null = 明确没有。
  const [savedKeyOverride, setSavedKeyOverride] = useState<
    { base: string; key_masked: string } | null | undefined
  >(undefined)
  // 0 = 跟服务端 max_num_seqs 走。30M 量级下把这里填成 1 等于让机器闲置着跑，
  // 所以默认交给服务端决定，而不是写一个看起保守的数。
  const [concurrency, setConcurrency] = useState(0)
  const [temperature, setTemperature] = useState(0.2)
  const [topP, setTopP] = useState(0.9)
  const [maxTokens, setMaxTokens] = useState(0)
  // 描述长度引导（字符）。两个都填才生效；空 = 按 token 区间派生。
  // 这与 maxTokens 是两件事：maxTokens 是"写不下就截断"的硬上限。
  const [captionLo, setCaptionLo] = useState(0)
  const [captionHi, setCaptionHi] = useState(0)
  const [timeoutS, setTimeoutS] = useState(90)
  const [maxPixels, setMaxPixels] = useState(0)
  const [seed, setSeed] = useState('')
  // 同一张图累计失败多少次后跳过。0 = 不限制。
  const [maxFailures, setMaxFailures] = useState(3)
  // 质检判定线与重打次数。0 = 跟配置走（pass 3.4 / drop 1.8 / 重打 2 次）。
  // 重打一次就是一次完整推理，30M 量级下这几个数直接决定跑几天。
  const [qualityPass, setQualityPass] = useState(0)
  const [qualityDrop, setQualityDrop] = useState(0)
  const [qualityAttempts, setQualityAttempts] = useState(0)
  // 重试阶梯：每次重打把温度抬多少 / 抬到哪，像素预算乘几倍 / 乘到哪。
  // 0 = 跟配置走。抬分辨率要求服务端 max_pixels 留了余量，否则会被钳回去。
  const [retryTempStep, setRetryTempStep] = useState(0)
  const [retryTempCap, setRetryTempCap] = useState(0)
  const [retryPixelsScale, setRetryPixelsScale] = useState(0)
  const [retryPixelsCap, setRetryPixelsCap] = useState(0)
  // L2 语义打分（图文 CLIP）。开启后质检能抓"句子通顺但跑题"，
  // 但打分不是线程安全的 → 并发被摁到 1，整条链路慢一个数量级。
  const [clipOn, setClipOn] = useState(false)
  const [clipMin, setClipMin] = useState(0)
  // CLIP 权重与预训练来源。空 = 跟配置。换权重等于换打分口径。
  const [clipModel, setClipModel] = useState('')
  const [clipPretrained, setClipPretrained] = useState('')
  // L1 规则层。用字符串存：空串 = 跟配置（null），0 是合法值
  // （最短 0 = 不设下限；重复率 0 = 一个 4-gram 都不许重复）。
  const [minChars, setMinChars] = useState('')
  const [maxChars, setMaxChars] = useState('')
  const [minCoverage, setMinCoverage] = useState('')
  const [maxRepeat, setMaxRepeat] = useState('')
  // 重试升级：重试用尽仍不合格时打到第二个服务（另一套更大的权重）。
  // 空 = 不升级。两个都得填，只填一个等于没开。
  const [escalateUrl, setEscalateUrl] = useState('')
  const [escalateModel, setEscalateModel] = useState('')
  // file:// 传图：省掉 base64 的 33% 体积与编码 CPU，要求服务端放行产物目录
  const [useFileUrl, setUseFileUrl] = useState(false)

  // ---- 本机 vLLM 服务（NL 模式的前置；远程服务只能用「探测」看连通性）
  const [vllm, setVllm] = useState<VllmState | null>(null)
  const [vllmPort, setVllmPort] = useState(8000)
  // '' = 按显存自动落档。这里选的是**权重**（tiers.yaml 的 models 段），
  // 与下面那个「模型名」（--served-model-name，接口路由名）是两件事。
  // 推理引擎：vllm | llamacpp。Windows 原生只有后者能上 GPU，
  // 且两者指纹不同（换引擎等于换一代数据），所以它跟着服务一起锁。
  const [vllmEngine, setVllmEngine] = useState('vllm')
  // 这个引擎在本机能不能跑。下拉一改就重新问：快照里的 local_ok 只按
  // "当前引擎"算，而还没启动时它的答案来自默认引擎，切换后必须重查。
  const [engineCap, setEngineCap] = useState<{ ok: boolean; reason: string } | null>(null)
  useEffect(() => {
    let alive = true
    api
      .vllmCapability(vllmEngine)
      .then((r) => alive && setEngineCap({ ok: r.ok, reason: r.reason }))
      .catch(() => alive && setEngineCap(null))
    return () => {
      alive = false
    }
  }, [vllmEngine])
  const [vllmModelKey, setVllmModelKey] = useState('')
  // '' = 按显存自动匹配。分档决定 max_model_len / 默认并发 / 精度下限，
  // 与选哪套权重正交（24G 卡上想跑 4B 也可以强行留 low-16g，KV 更省）。
  const [vllmTier, setVllmTier] = useState('')
  // 权重精度覆盖分档表。默认 BF16：它是唯一不需要额外硬件能力、也不依赖
  // 量化权重的档，前几批数据实际也是以 bf16 加载的。换精度等于换一代数据，
  // 会进 provenance（model_id 里带 precision）。
  const [vllmPrecision, setVllmPrecision] =
    useState<(typeof VLLM_PRECISIONS)[number]>('bf16')
  // 切到 W4A16 时先问一句：它需要预量化权重，直接切只会得到"回退 BF16"。
  const [quantOpen, setQuantOpen] = useState(false)
  // 已打包分片清单（parquet 资产）。打开时以当前输入为起点，
  // 选中的路径直接填回「本地分片」。
  const [assetsOpen, setAssetsOpen] = useState(false)
  // 量化完成后 overlay 变了，但精度值可能没变（已在 w4a16），靠这个手动踢一下
  // plan 预览重新解析。
  const [planTick, setPlanTick] = useState(0)
  // 0 = 用分档表的默认值。并发调大而单步 token 预算不跟上时，吞吐根本不会涨
  // （一步塞满后剩下的请求只能等下一步），所以这两个要摆在一起。
  const [vllmSeqs, setVllmSeqs] = useState(0)
  const [vllmBatched, setVllmBatched] = useState(0)
  const [vllmGpuMem, setVllmGpuMem] = useState(0)
  // file:// 放行根目录（--allowed-local-media-path）。留空 = 服务端只收
  // base64。填了之后客户端才能开 --use-file-url 省掉 33% 体积与编码 CPU。
  const [vllmMediaRoot, setVllmMediaRoot] = useState('')
  const [vllmBusy, setVllmBusy] = useState(false)
  const [vllmError, setVllmError] = useState<string | null>(null)
  // 多卡（数据并行）：每张卡一个服务进程。perCardSeqs 同时是"每卡开几个
  // slot"和"每槽并发多少条"——这两个数必须一致：服务开 8 个 slot 而任务
  // 只发 2 条，等于把卡闲着一半。
  const [multiGpu, setMultiGpu] = useState(false)
  const [perCardSeqs, setPerCardSeqs] = useState(0)
  // 这组参数会解析成什么（分档/权重/max_model_len/警告）。点启动之前就看到，
  // 省掉"选了 8B 才发现掉档 / OOM"那几分钟。
  const [planPreview, setPlanPreview] = useState<VllmPlan | null>(null)
  const [probe, setProbe] = useState<VllmProbe | null>(null)
  const [probeBusy, setProbeBusy] = useState(false)

  // ---- HF 输入（engine="vlm"）。令牌只在提交那次请求里走一趟
  const [hfRepo, setHfRepo] = useState('')
  const [hfToken, setHfToken] = useState('')
  const [hfRevision, setHfRevision] = useState('main')
  const [hfAvailable, setHfAvailable] = useState<string[] | null>(null)
  const [hfFiles, setHfFiles] = useState<string[]>([])
  const [hfBusy, setHfBusy] = useState(false)
  // auto = 有 parquet 就逐行流式（不落盘）；tar 分片要下到本地，打完一片删一片
  const [hfFormat, setHfFormat] = useState<'auto' | 'parquet' | 'webdataset'>('auto')
  // 预取：下载与打标并行。0 = 严格串行（每片都要空等下载完）
  const [hfPrefetch, setHfPrefetch] = useState(1)
  // 多线程下载（IDM 式分段并发）：国内单条 TCP 常被限速，靠多段并发跑满带宽。
  // 关掉就等价于 hf_conns=1，服务端不支持 Range 时也会自动退回单连接。
  const [hfMulti, setHfMulti] = useState(true)
  const [hfConns, setHfConns] = useState(4)
  // 给产物留的磁盘余量（GB）。0 = 自动（最大分片的 30%，不少于 2 GB）
  const [hfReserveGb, setHfReserveGb] = useState(0)
  // 结果缓存：同图同参复用上次产出。默认关：首轮命中率为 0，而算图哈希有成本
  const [cacheOn, setCacheOn] = useState(false)
  // 目标盘剩余空间（字节）。填预取/余量时最想知道的就是这个数。
  const [diskFree, setDiskFree] = useState<number | null>(null)

  // ---- 产物形态（engine="vlm"）
  const [artifactFormat, setArtifactFormat] = useState<(typeof ARTIFACT_FORMATS)[number]>('parquet')
  const [parquetRows, setParquetRows] = useState(2000)

  // ---- tag 后端补充（两种引擎通用）
  const [keepCategories, setKeepCategories] = useState<string[]>([])
  const [catThresholds, setCatThresholds] = useState<Record<string, string>>({})
  const [modelRepo, setModelRepo] = useState('')
  // 自定义仓库名时才显示输入框；默认状态下模型名来自配置，看得见但不用手抄。
  const [tagModelCustom, setTagModelCustom] = useState(false)
  const [guard, setGuard] = useState(true)
  // 开跑前按"预计产出 + 20% 冗余"判盘。只对本地引擎有效（VLM 走流式写出）。
  const [checkSpace, setCheckSpace] = useState(true)
  // 硬件保护的阈值。初值来自 /api/defaults（= tiers.yaml 的 limits 段），
  // 只放 guard 真认的那几项；磁盘两项归「盘位预算」，闭环的 kp/ki 留在配置里。
  const [guardPolicy, setGuardPolicy] = useState<Record<string, number>>({})
  const setKnob = (key: string, value: number) =>
    setGuardPolicy((prev) => ({ ...prev, [key]: value }))

  // ---- 命名
  const [artist, setArtist] = useState(true)
  const [artistDepth, setArtistDepth] = useState(0)
  const [artistInject, setArtistInject] = useState(true)
  // VLM 路径的画师名来自数据本身（parquet 的 artist 列 / 分片目录名），
  // 不是从素材路径反推，所以单独一套开关。
  // 默认关**是刻意的**：配置里 artist.enabled 一直是 false，打开会改变
  // .nl 文本内容（等于换一代数据）。想注入的人在这里显式开。
  const [vlmArtist, setVlmArtist] = useState(false)
  const [vlmArtistInject, setVlmArtistInject] = useState(true)
  const [layout, setLayout] = useState<(typeof LAYOUTS)[number]>('flat')
  const [seqWidth, setSeqWidth] = useState(6)

  const isVlm = mode !== 'tag'
  const hasTag = mode !== 'nl'
  const useHf = isVlm && hfRepo.trim() !== ''
  // API 模式：不加载本地权重、不用起服务、没有环境指纹，也没有 file:// 直传。
  // 本机服务那一整块面板在 API 模式下不渲染（不是灰掉），因为其中一半字段
  // 对 API 毫无意义，灰着摆着只会让人以为"配置没生效"。
  const isApiMode = isVlm && vlmProvider !== 'local'
  const providerList = defaults?.vlm_providers ?? []
  // 默认地址与"密钥去哪个环境变量找"一律读后端给的那份，前端不留第二份常量
  const providerBase = (p: VlmProvider) =>
    providerList.find((o) => o.value === p)?.base || ''
  const providerEnvKey = (p: VlmProvider) =>
    providerList.find((o) => o.value === p)?.env_key || ''
  // 标签以 /api/defaults 为准；只有 defaults 还没回来时用到这份兜底。
  const providerLabel = (p: VlmProvider) =>
    providerList.find((o) => o.value === p)?.label ||
    (p === 'openai' ? 'OpenAI 兼容 API' : p === 'anthropic' ? 'Anthropic API' : '本机 / 自建服务')
  // defaults 晚到（或用户先选了 API）时补上该来源的默认地址。空着会让提交
  // 按钮莫名置灰，而其实后端本来就会按来源取默认域名。
  useEffect(() => {
    if (vlmProvider === 'local' || apiBase.trim()) return
    const fallback = defaults?.vlm_providers?.find((o) => o.value === vlmProvider)?.base
    if (fallback) setApiBase(fallback)
  }, [vlmProvider, apiBase, defaults])

  // 地址指向本机 = 应该由这个页面来起服务；指向别处就只有「探测」能告诉我们状态。
  const localVlmUrl = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?\/?$/i.test(
    vlmUrl.trim(),
  )
  // 并发填 0 时的实际生效值。规则与 client.resolve_concurrency 一致：
  // 本机按指纹里的 max_num_seqs（封顶 32，读不到就是 4），API 模式按该来源
  // 的建议值（云端限的是每分钟请求数，与显存无关）。
  // 页面上只是显示，真正解析在后端——两处不一致时以后端为准。
  const providerMeta = (p: VlmProvider) => providerList.find((o) => o.value === p)
  const savedKey =
    savedKeyOverride !== undefined ? savedKeyOverride : (providerMeta(vlmProvider)?.saved ?? null)
  const effectiveSeqs = (() => {
    if (concurrency > 0) return Math.max(1, concurrency)
    if (isApiMode) return Math.max(1, providerMeta(vlmProvider)?.concurrency ?? 4)
    const raw = Number(vllm?.fingerprint?.max_num_seqs ?? 0)
    if (!Number.isFinite(raw) || raw <= 0) return 4
    return Math.max(1, Math.min(32, Math.round(raw)))
  })()

  // tiers.yaml 的 models 段就是可选权重的唯一真相源：页面不写死名字，
  // 配置里加一个（比如换成本地量化版）下拉里就多一项。
  const vllmModels = defaults?.vllm_models ?? []
  const vllmTiers = defaults?.vllm_tiers ?? []
  // 「打标方式」的副标题跟着配置走：tiers.yaml 的 models 段加一个，这里就多一个。
  // 写死 "Qwen3-VL" 的话，换权重那天页面就开始说谎。
  const localModelHint = vllmModels.length
    ? vllmModels.map((m) => m.key).join(' / ')
    : 'Qwen3-VL 4B/8B'
  // API 模式下列举不出对方的模型清单，就显示"来源 · 已填的模型 id"。
  const vlmModelHint = isApiMode
    ? `${providerLabel(vlmProvider)}${vlmModel.trim() ? ` · ${vlmModel.trim()}` : ''}`
    : localModelHint
  const vllmLocked = vllm?.status === 'starting' || vllm?.status === 'ready'

  // 质检的配置默认值。输入框里的 0 = 不覆盖，占位文案必须显示真实数字，
  // 所以这些常量只能来自 /api/defaults，不能写死在 JSX 里。
  const qDef = defaults?.quality
  const qPassDef = qDef?.pass_score ?? 3.4
  const qDropDef = qDef?.drop_score ?? 1.8
  const qAttemptsDef = qDef?.max_attempts ?? 2
  const qTempStepDef = qDef?.retry_temp_step ?? 0.15
  const qTempCapDef = qDef?.retry_temp_cap ?? 0.7
  const qPixelsScaleDef = qDef?.retry_pixels_scale ?? 1.5
  const qPixelsCapDef = qDef?.retry_pixels_cap ?? 1048576
  const qClipMinDef = qDef?.clip_min_score ?? 0.22
  // 服务端是否放行了 file://：只有本机自己拉起的服务才可能。
  // 指纹里的 local_media_root 是服务端实际参数，比表单更可信。
  const allowedMediaRoot =
    String((vllm?.fingerprint as Record<string, unknown> | null)?.local_media_root ?? '') ||
    vllm?.overrides?.local_media_root ||
    ''

  // tag 侧的模型表按后端分组（配置里就是这么写的）：切后端不能拿错仓库，
  // 所以这里按 backend → repo 反查，不做"全部模型混在一个列表"。
  const tagModelPresets = Object.entries(defaults?.tag_models ?? {})
  const tagRepoForBackend = defaults?.tag_models?.[backend] ?? ''
  const backendOfRepo = (repo: string) =>
    tagModelPresets.find(([, r]) => r === repo)?.[0] ?? ''
  // 「同时」下 tag 也可以交给 VLM 自己出，那种情况下别显示一个不存在的后端名
  const tagBackendLabel = backend === 'none' ? 'VLM 自己出 tag' : backend

  // 切打标方式。「同时」里可以把 tag 交给 VLM（backend=none），切到 Tag 打标时
  // 这个值不成立（本地引擎没有"none"后端），退回配置默认。
  const changeMode = (next: (typeof MODES)[number]) => {
    setMode(next)
    if (next === 'tag' && backend === 'none') setBackend(defaults?.backend ?? 'wd-eva02')
  }

  // 切推理来源。换来源必须换默认地址（拿 OpenAI 的地址去打 Anthropic 只会
  // 404），并且把本机默认的路由名 tagger 清掉——API 侧不认这个名字。
  const changeProvider = (next: VlmProvider) => {
    setVlmProvider(next)
    setProbe(null)
    setApiProbe(null)
    setSavedKeyOverride(undefined)
    const saved = providerList.find((o) => o.value === next)?.saved
    // 记住过的地址优先：用户当初填的是自建网关，换回来还得是同一个。
    setApiBase(next === 'local' ? '' : (saved?.base || providerBase(next)))
    if (next !== 'local' && vlmModel.trim() === 'tagger') setVlmModel('')
  }

  // 取消勾选 = 忘掉存档。不这么做的话密钥会永远留在盘上，而页面上已经
  // 看不出它还存着——"我明明取消了"是最难查的一类问题。
  const toggleRemember = async (checked: boolean) => {
    setRememberKey(checked)
    if (checked || !savedKey) return
    try {
      const r = await api.saveNlApiKey({
        provider: vlmProvider,
        base: '',
        key: '',
        remember: false,
      })
      setSavedKeyOverride(r.saved)
    } catch {
      // 删存档失败不该挡住提交；下次进页面还会显示"已记住"。
    }
  }

  // W4A16 不是运行时开关：没有量化权重时选了也只会回退 BF16。所以先弹窗
  // 问"现在量化吗"，用户取消就回到默认的 BF16（见 QuantizeDialog 的说明）。
  const choosePrecision = (v: (typeof VLLM_PRECISIONS)[number]) => {
    if (v === 'w4a16') {
      setQuantOpen(true)
      return
    }
    setVllmPrecision(v)
  }

  const effectiveOut = useMemo(() => {
    if (outDir) return outDir
    if (useHf) return 'work/out'
    if (!inputDir) return ''
    const parts = inputDir.replace(/[\\/]+$/, '').split(/[\\/]/)
    const name = parts.pop() ?? 'out'
    const stem = name.replace(/\.zip$/i, '')
    return `${parts.join('/')}/${stem}-tags`
  }, [inputDir, outDir, useHf])

  // 首次挂载灌配置默认值；之后每次切回本页只刷新"可选清单"（模型可能刚在
  // 模型管理页加过），不动用户已经填好的路径与参数。
  const initedRef = useRef(false)
  useEffect(() => {
    if (!active) return
    api
      .defaults()
      .then((d) => {
        setDefaults(d)
        if (initedRef.current) return
        initedRef.current = true
        setBackend(d.backend)
        setModelDir(d.model_dir)
        setDevice(d.device)
        setPrecision(d.precision)
        setBatch(d.batch)
        setThreshold(d.threshold)
        setLayout(d.layout)
        setArtist(d.artist)
        setArtistDepth(d.artist_depth)
        setArtistInject(d.artist_inject)
        setSeqWidth(d.seq_width)
        setGuardPolicy({ ...(d.guard ?? {}) })
        api.health().then(setHealth).catch(() => setHealth(null))
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
  }, [active])

  // 本机 vLLM 的状态。只选 VLM 引擎时才轮询：首次加载要几分钟，
  // 页面上必须能看见"启动中/就绪/失败 + 日志尾巴"，否则只有一个转不完的圈。
  useEffect(() => {
    // 只在"这一页可见 + 选了 NL"时轮询：页面常驻挂载（切到模型管理不卸载），
    // 不加 active 的话会在后台一直问服务状态。
    if (!active || !isVlm) return
    let alive = true
    const tick = () =>
      api
        .vllm()
        .then((s) => {
          if (!alive) return
          setVllm(s)
          setVllmPort(s.port)
          // 服务在跑时精度以服务端实际参数为准：页面刷新后本地 state 会回到
          // 默认 BF16，而服务可能正跑着 FP8。只在 running 时同步——停止后
          // 用户还要接着改，不能被上一轮的旧值反复覆盖。
          const runningPrecision = s.overrides?.precision
          if (
            (s.status === 'ready' || s.status === 'starting') &&
            runningPrecision &&
            (VLLM_PRECISIONS as readonly string[]).includes(runningPrecision)
          ) {
            setVllmPrecision(runningPrecision as (typeof VLLM_PRECISIONS)[number])
          }
          // 本机服务就绪而表单还指着默认地址时，自动对齐端口——用户改了
          // 端口却忘了同步表单，症状是任务跑起来全部超时。
          if (s.status === 'ready' && s.url) setVlmUrl(s.url)
        })
        .catch(() => alive && setVllm(null))
    tick()
    const t = window.setInterval(tick, 4000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [active, isVlm])

  // 参数一改就重算"会起成什么"。防抖是为了别让每个按键都去调一次
  // nvidia-smi（服务端按参数组合缓存 30 秒，来回切不会真的重算）。
  useEffect(() => {
    if (!isVlm) return
    let alive = true
    const timer = window.setTimeout(() => {
      api
        .vllmPlan({
          model: vllmModelKey || undefined,
          tier: vllmTier || undefined,
          precision: vllmPrecision,
          max_num_seqs: vllmSeqs || undefined,
          max_num_batched_tokens: vllmBatched || undefined,
          gpu_mem: vllmGpuMem || undefined,
        })
        .then((p) => alive && setPlanPreview(p))
        .catch(() => alive && setPlanPreview(null))
    }, 400)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [isVlm, vllmModelKey, vllmTier, vllmPrecision, planTick, vllmSeqs, vllmBatched, vllmGpuMem])

  // 服务起着的时候，真正在跑的是服务端那组参数，不是表单里正在编辑的这组。
  // 两者混着显示，就会出现"页面说 4B、实际跑 8B"这种最难查的错。
  const activePlan: VllmPlan | null =
    vllm && (vllm.status === 'ready' || vllm.status === 'starting' || vllm.status === 'stopping')
      ? vllm.plan
      : planPreview

  // 引擎探测优先：快照里的 local_ok 是按"当前引擎"算的，而服务没起时
  // 那个"当前引擎"还是默认值。切换下拉后必须用新查回来的那份。
  const capOk = engineCap ? engineCap.ok : vllm?.local_ok
  const capReason = (engineCap ? engineCap.reason : vllm?.local_reason) || ''

  async function startVllm() {
    setVllmBusy(true)
    setVllmError(null)
    try {
      const s = await api.vllmStart(vllmPort, 900, {
        engine: vllmEngine,
        // 空/0 不传 = 用分档表；显式传 0 会被后端当成非法值
        model: vllmModelKey || undefined,
        tier: vllmTier || undefined,
        precision: vllmPrecision,
        // 服务端路由名必须与任务表单里的「模型名」一致，否则请求报"模型不存在"
        served_name: vlmModel.trim() || undefined,
        max_num_seqs: vllmSeqs || undefined,
        max_num_batched_tokens: vllmBatched || undefined,
        gpu_mem: vllmGpuMem || undefined,
        // 非空才加 --allowed-local-media-path。放行目录必须覆盖产物目录，
        // 否则 file:// 请求每张都被 400 拒掉（报的是"路径不被允许"）。
        local_media_root: vllmMediaRoot.trim() || undefined,
      })
      setVllm(s)
      setVlmUrl(s.url)
    } catch (err) {
      setVllmError(err instanceof Error ? err.message : String(err))
    } finally {
      setVllmBusy(false)
    }
  }

  async function stopVllm() {
    setVllmBusy(true)
    setVllmError(null)
    try {
      setVllm(await api.vllmStop())
    } catch (err) {
      setVllmError(err instanceof Error ? err.message : String(err))
    } finally {
      setVllmBusy(false)
    }
  }

  async function probeVlm() {
    setProbeBusy(true)
    try {
      setProbe(await api.vllmProbe(vlmUrl))
    } catch (err) {
      setProbe({
        url: vlmUrl,
        ok: false,
        version: '',
        models: [],
        error: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setProbeBusy(false)
    }
  }

  // API 模式的"探测"：发一张 8×8 的图走一次真实请求。这是唯一能同时回答
  // "地址通不通 / 密钥对不对 / 模型名认不认 / 看不看得见图"的办法——分四次
  // 试错的话，每次都要等 30 秒超时。
  async function probeApi() {
    setApiProbeBusy(true)
    try {
      setApiProbe(
        await api.probeNlApi({
          provider: vlmProvider,
          base: apiBase.trim(),
          model: vlmModel.trim(),
          key: apiKey,
        }),
      )
    } catch (err) {
      setApiProbe({
        ok: false,
        provider: vlmProvider,
        base: apiBase,
        model: vlmModel,
        latency_ms: 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        text: '',
        error: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setApiProbeBusy(false)
    }
  }

  // 随手看一眼"这个输入里有多少张"：用户在按开始之前最想知道的就是这个，
  // 而它决定了这次要跑几分钟。HF 仓库输入没有本地目录可扫，跳过。
  useEffect(() => {
    // NL 模式喂的是分片包，/api/fs 只列目录：对着一个 .zip 或散图目录都能
    // 拿到 404，然后在控制台留下一串红字。这个数字在 NL 下也不用显示。
    if (isVlm || !inputDir || useHf) {
      setScan(null)
      return
    }
    let alive = true
    api
      .fs(inputDir)
      .then((l) => alive && setScan({ path: l.path, images: l.images, zips: l.zips.length }))
      .catch(() => alive && setScan(null))
    return () => {
      alive = false
    }
  }, [isVlm, inputDir, useHf])

  // 命名预览：命名规则/画师开关一变就重算。这是"跑完几百万张才发现名字
  // 不对"的唯一防线，所以它跟着输入参数一起失效、一起刷新。
  useEffect(() => {
    // 同理：preview 是"逐张命名"的预览，NL 模式没有这套命名规则。
    if (isVlm || !inputDir.trim() || useHf) {
      setPreview(null)
      setPreviewError(null)
      return
    }
    let alive = true
    const timer = window.setTimeout(() => {
        api
        .preview({
          path: inputDir,
          layout,
          artist,
          artist_depth: artistDepth,
          recursive,
          include_zips: includeZips,
          include_loose: includeLoose,
          limit: 0,
          seq_width: seqWidth,
        })
        .then((p) => {
          if (!alive) return
          setPreview(p)
          setPreviewError(null)
        })
        .catch((err) => {
          if (!alive) return
          setPreview(null)
          setPreviewError(err instanceof Error ? err.message : String(err))
        })
    }, 350)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [isVlm, inputDir, layout, artist, artistDepth, recursive, includeZips, includeLoose,
      seqWidth, useHf])

  function toggleKeep(value: string) {
    setKeepCategories((prev) =>
      prev.includes(value) ? prev.filter((c) => c !== value) : [...prev, value],
    )
  }

  function toggleHfFile(name: string) {
    setHfFiles((prev) =>
      prev.includes(name) ? prev.filter((f) => f !== name) : [...prev, name],
    )
  }

  // 目标盘剩余空间：决定"能预取几片"的那个数，开跑前就该看得见。
  useEffect(() => {
    if (!useHf) {
      setDiskFree(null)
      return
    }
    let alive = true
    api
      .diskFree(outDir.trim() || '.')
      .then((res) => {
        if (alive) setDiskFree(res.free_bytes)
      })
      .catch(() => {
        if (alive) setDiskFree(null)
      })
    return () => {
      alive = false
    }
  }, [useHf, outDir])

  async function loadHfFiles() {
    if (!hfRepo.trim()) return
    setHfBusy(true)
    setError(null)
    try {
      const res = await api.hfFiles(
        hfRepo.trim(),
        hfToken,
        hfRevision || 'main',
        hfFormat,
      )
      setHfAvailable(res.files)
      setHfFiles([]) // 不勾 = 处理全部
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setHfBusy(false)
    }
  }

  async function submit() {
    setBusy(true)
    setError(null)
    setConflict(null)
    try {
      const thresholds = Object.fromEntries(
        Object.entries(catThresholds)
          .filter(([, v]) => v !== '' && !Number.isNaN(Number(v)))
          .map(([k, v]) => [k, Number(v)]),
      )
      const shared = {
        out_dir: outDir,
        device,
        precision,
        batch,
        preprocess,
        threshold,
        limit,
        force,
        keep_categories: keepCategories,
        thresholds,
        model_repo: modelRepo,
        guard,
        check_space: checkSpace,
        // 关掉保护就不必把阈值发过去（发了也不生效，只会让快照里多一份噪声）
        guard_policy: guard ? guardPolicy : {},
      }
      const spec: Partial<JobSpec> = isVlm
        ? {
            ...shared,
            engine: 'vlm',
            // HF 仓库与本地分片包二选一；填了 repo 就走 HF
            input_dir: useHf ? '' : inputDir,
            // 只有「同时」才挂 tag 分类头；「NL 打标」的后端必须 none，
            // 否则 run_client 会把它算出的 tag 一起写进产物。
            backend: hasTag ? backend : 'none',
            model_dir: modelDir,
            hf_repo: hfRepo.trim(),
            hf_token: hfToken,
            hf_revision: hfRevision || 'main',
            hf_files: hfFiles,
            hf_format: hfFormat,
            hf_prefetch: hfPrefetch,
            hf_conns: hfMulti ? hfConns : 1,
            hf_reserve_gb: hfReserveGb,
            cache: cacheOn,
            // 推理来源。API 模式不需要本地服务地址，也不吃 file:// 直传
            // （云端读不到本机路径）；地址留空时后端按来源取默认域名。
            vlm_provider: vlmProvider,
            api_base: vlmProvider === 'local' ? '' : apiBase.trim(),
            api_key: vlmProvider === 'local' ? '' : apiKey,
            url: vlmUrl,
            vlm_model: vlmModel.trim(),
            output_mode: mode,
            temperature,
            top_p: topP,
            max_tokens: maxTokens,
            // 两个都填才发，否则走默认派生（400-1024）
            caption_chars: captionLo > 0 && captionHi > captionLo
              ? [captionLo, captionHi]
              : null,
            timeout_s: timeoutS,
            concurrency,
            // 多卡：地址不从这里来（后端直接取多卡服务组的地址分槽），
            // 只有每卡并发要传，否则服务开 8 个 slot、任务只发 2 条。
            multi_gpu: multiGpu,
            per_card_seqs: multiGpu ? perCardSeqs : 0,
            max_pixels: maxPixels,
            seed: seed === '' ? null : Number(seed),
            max_failures: maxFailures,
            quality_pass_score: qualityPass,
            quality_drop_score: qualityDrop,
            quality_max_attempts: qualityAttempts,
            // 重试阶梯与 L2：0 = 跟配置走，只有用户真的动过才发。
            quality_retry_temp_step: retryTempStep,
            quality_retry_temp_cap: retryTempCap,
            quality_retry_pixels_scale: retryPixelsScale,
            quality_retry_pixels_cap: retryPixelsCap,
            quality_enable_clip: clipOn,
            quality_clip_min_score: clipMin,
            quality_clip_model: clipModel.trim(),
            quality_clip_pretrained: clipPretrained.trim(),
            // 空串 = 跟配置；0 是合法值，所以不能用 0 表示"没填"
            quality_min_chars: minChars.trim() === '' ? null : Number(minChars),
            quality_max_chars: maxChars.trim() === '' ? null : Number(maxChars),
            quality_min_tag_coverage: minCoverage.trim() === '' ? null : Number(minCoverage),
            quality_max_repeat_ratio: maxRepeat.trim() === '' ? null : Number(maxRepeat),
            escalate_url: escalateUrl.trim(),
            escalate_model: escalateModel.trim(),
            use_file_url: vlmProvider === 'local' && useFileUrl,
            // 画师名来自数据本身（parquet 的 artist 列 / 分片的目录名），
            // 这两个开关决定它写不写进 .nl/.tag 文本。
            artist: vlmArtist,
            artist_inject: vlmArtist && vlmArtistInject,
            artifact_format: artifactFormat,
            parquet_rows: parquetRows,
          }
        : {
            ...shared,
            engine: 'local',
            input_dir: inputDir,
            // 本地引擎没有 "none" 后端；从「同时」切过来时兜一层
            backend: backend === 'none' ? (defaults?.backend ?? 'wd-eva02') : backend,
            model_dir: modelDir,
            recursive,
            include_zips: includeZips,
            include_loose: includeLoose,
            artist,
            artist_depth: artistDepth,
            artist_inject: artistInject,
            layout,
            seq_width: seqWidth,
          }
      // 勾了「记住」才落盘。失败不挡提交：存不下顶多下次重贴，任务照样跑。
      if (isApiMode && rememberKey && apiKey.trim()) {
        try {
          const saved = await api.saveNlApiKey({
            provider: vlmProvider,
            base: apiBase.trim(),
            key: apiKey,
            remember: true,
          })
          setSavedKeyOverride(saved.saved)
        } catch {
          // 密钥存档失败与任务无关，不打断提交。
        }
      }
      const snap = await api.createJob(spec)
      onStarted(snap.id)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // 409 = 已有任务在跑。顺手刷新 health，好把"去看那个任务"的链接点亮。
        setConflict(err.message)
        api.health().then(setHealth).catch(() => undefined)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  const gpus = health?.gpus ?? []
  const total = preview?.total ?? 0
  // 实测（4070 Ti SUPER / bf16 / batch=4）：800 张 32s 跑完，端到端约 40ms/张。
  const estimateS = !isVlm && preview ? Math.max(1, Math.round((total * 40) / 1000)) : null
  const missingInput = isVlm ? !hfRepo.trim() && !inputDir.trim() : !inputDir.trim()
  // API 模式的两个必需项。缺了就在按钮上拦住，而不是让后端 400——
  // 这两项填错的报错信息（"模型不存在" / "401"）都很难和"表单没填全"区分。
  const apiIncomplete = isApiMode && (!apiBase.trim() || !vlmModel.trim())
  const backendOptions = isVlm ? BACKENDS_VLM : BACKENDS
  const keepOptions = defaults?.keep_categories ?? []

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<FolderSearch size={17} />}
        eyebrow={mode === 'both' ? 'Tag + NL' : mode === 'nl' ? 'NL 打标' : 'Tag 打标'}
        title="新建打标任务"
        description={
          mode === 'both'
            ? 'tag 与 NL 一起产，两个文件落到同一批产物里。'
            : mode === 'nl'
              ? '只产 .nl 自然语言描述。'
              : '只产 .tag。'
        }
        actions={
          <>
            <button type="button" className="lx-btn-ghost" onClick={() => setPicker(true)}>
              <FolderOpen size={14} className="mr-1.5 inline" />
              浏览输入
            </button>
            <button
              type="button"
              className="lx-btn-primary lx-launch-glow inline-flex items-center gap-1.5"
              disabled={missingInput || apiIncomplete || busy}
              title={apiIncomplete ? 'API 模式要填地址和模型名' : undefined}
              onClick={() => void submit()}
            >
              <Play size={14} />
              {busy ? '创建中…' : '开始打标'}
            </button>
          </>
        }
      />

      {error && <ErrorBox error={error} title={hfBusy ? '读取文件列表失败' : '创建任务失败'} />}
      {conflict && (
        <ErrorBox
          tone="warning"
          title="已有任务在跑"
          error={`${conflict}${
            health?.running_job ? '\n点左侧「运行中」可以去看那个任务。' : ''
          }`}
        />
      )}

      <div className="grid gap-3.5 lg:grid-cols-[minmax(0,1fr)_318px]">
        <div className="flex min-w-0 flex-col gap-3.5">
          <SectionPanel title="打标方式">
            <ChoiceGrid
              options={MODES}
              value={mode}
              onChange={changeMode}
              className="grid-cols-3"
              renderLabel={(v) => (v === 'tag' ? 'Tag 打标' : v === 'nl' ? 'NL 打标' : '同时')}
              ariaLabel="打标方式"
            />
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-lx pt-3">
              <span className="text-[11px] text-lx-dim">
                {mode === 'tag'
                  ? `tag 分类头（${tagBackendLabel}）· 出 .tag`
                  : mode === 'nl'
                    ? `${vlmModelHint} · 出 .nl`
                    : `tag 分类头 + ${vlmModelHint} · 两个文件都出`}
              </span>
              <button
                type="button"
                className="lx-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px]"
                onClick={onOpenModels}
              >
                <Layers size={12} />
                模型管理
              </button>
            </div>
          </SectionPanel>

          <SectionPanel title="输入">
            {isVlm && (
              <>
                <SettingRow
                  label="HF 团队仓库"
                  description="org/name；留空用下面的本地分片包"
                  icon={<Cloud size={14} />}
                >
                  <div className="flex gap-2">
                    <input
                      className="lx-input font-mono text-xs"
                      value={hfRepo}
                      onChange={(e) => setHfRepo(e.target.value)}
                      placeholder="my-team/image-shards"
                      spellCheck={false}
                    />
                    <button
                      type="button"
                      className="lx-btn-ghost shrink-0 px-3"
                      disabled={!hfRepo.trim() || hfBusy}
                      onClick={() => void loadHfFiles()}
                    >
                      {hfBusy ? '读取中…' : '查看文件列表'}
                    </button>
                  </div>
                </SettingRow>

                {hfRepo.trim() !== '' && (
                  <>
                    <SettingRow label="访问令牌" help="有 read 权限的 token；只在提交时用一次，不落盘">
                      <input
                        className="lx-input font-mono text-xs"
                        type="password"
                        value={hfToken}
                        onChange={(e) => setHfToken(e.target.value)}
                        placeholder="hf_..."
                        spellCheck={false}
                      />
                    </SettingRow>

                    <SettingRow label="分支" help="默认 main">
                      <input
                        className="lx-input font-mono text-xs"
                        value={hfRevision}
                        onChange={(e) => setHfRevision(e.target.value)}
                        placeholder="main"
                        spellCheck={false}
                      />
                    </SettingRow>

                    <SettingRow
                      label="仓库格式"
                      help="auto = 有 parquet 就逐行流式（不落盘）；tar 分片要先下到本地，打完一片删一片"
                    >
                      <select
                        className="lx-input"
                        value={hfFormat}
                        onChange={(e) =>
                          setHfFormat(
                            e.target.value as 'auto' | 'parquet' | 'webdataset',
                          )
                        }
                      >
                        <option value="auto">自动</option>
                        <option value="parquet">parquet 流式（不落盘）</option>
                        <option value="webdataset">tar 分片（落盘，打完即删）</option>
                      </select>
                    </SettingRow>

                    {hfFormat !== 'parquet' && (
                      <>
                        <SettingRow
                          label="多线程下载"
                          help="把一个分片切成多段并发拉取（IDM 式的分段下载）。开着更吃带宽，也更容易跑满国内常见的单连接限速"
                        >
                          <label className="flex cursor-pointer items-center gap-2 text-xs text-lx-secondary">
                            <input
                              type="checkbox"
                              className="accent-lx-accent"
                              checked={hfMulti}
                              onChange={(e) => setHfMulti(e.target.checked)}
                            />
                            启用（文件小于 16 MB 或服务端不支持 Range 时自动退回单连接）
                          </label>
                        </SettingRow>
                        {hfMulti && (
                          <SettingRow
                            label="并发线程数"
                            help="单个分片同时开几条连接。4~8 条最实用；再多只是在抢同一份带宽，还会让丢包重传的代价变大"
                          >
                            <input
                              type="number"
                              className="lx-input font-mono tabular-nums"
                              min={2}
                              max={16}
                              value={hfConns}
                              onChange={(e) =>
                                setHfConns(
                                  Math.max(2, Math.min(16, Number(e.target.value) || 2)),
                                )
                              }
                            />
                          </SettingRow>
                        )}
                        <SettingRow
                          label="预取分片"
                          help="下载与打标并行，下载时间藏进上一片的处理里；0 = 严格串行。磁盘余量不够时会自动暂停下载"
                        >
                          <input
                            type="number"
                            className="lx-input font-mono tabular-nums"
                            min={0}
                            max={8}
                            value={hfPrefetch}
                            onChange={(e) =>
                              setHfPrefetch(
                                Math.max(0, Math.min(8, Number(e.target.value) || 0)),
                              )
                            }
                          />
                        </SettingRow>
                        <SettingRow
                          label="磁盘余量（GB）"
                          help="给产物留的空间；0 = 自动（最大分片的 30%，不少于 2 GB）。空间不够就停下载，不会写爆盘"
                          description={
                            diskFree === null
                              ? undefined
                              : `目标盘剩余 ${(diskFree / 1024 ** 3).toFixed(1)} GB`
                          }
                        >
                          <input
                            type="number"
                            className="lx-input font-mono tabular-nums"
                            min={0}
                            step={1}
                            value={hfReserveGb || ''}
                            placeholder="0"
                            onChange={(e) =>
                              setHfReserveGb(Math.max(0, Number(e.target.value) || 0))
                            }
                          />
                        </SettingRow>
                      </>
                    )}

                    {hfAvailable !== null && (
                      <SettingRow
                        label="文件"
                        description={
                          hfFiles.length
                            ? `只处理勾选的 ${hfFiles.length} / ${hfAvailable.length} 个分片`
                            : `不勾 = 处理全部 ${hfAvailable.length} 个分片`
                        }
                      >
                        {hfAvailable.length === 0 ? (
                          <p className="text-xs text-lx-warning">
                            {hfFormat === 'parquet'
                              ? '仓库里没有 *.parquet 分片。'
                              : hfFormat === 'webdataset'
                                ? '仓库里没有 *.tar 分片。'
                                : '仓库里没有 *.parquet 也没有 *.tar 分片。'}
                          </p>
                        ) : (
                          <div className="lx-sheet-inset max-h-44 space-y-1 overflow-y-auto p-2">
                            {hfAvailable.map((f) => (
                              <label
                                key={f}
                                className="flex cursor-pointer items-center gap-2 rounded-lx-sm px-1.5 py-1 font-mono text-[11px] hover:bg-lx-accent-subtle"
                              >
                                <input
                                  type="checkbox"
                                  checked={hfFiles.includes(f)}
                                  onChange={() => toggleHfFile(f)}
                                  className="accent-lx-accent"
                                />
                                <span className="min-w-0 flex-1 truncate text-lx-secondary" title={f}>
                                  {f}
                                </span>
                              </label>
                            ))}
                          </div>
                        )}
                      </SettingRow>
                    )}
                  </>
                )}

                <SettingRow
                  label="本地分片（可选）"
                  help="parquet（HF datasets 流式）/ .zip / .tar（WebDataset）都收，也可以给装着分片的目录。填了 HF 仓库就忽略这里"
                  icon={<HardDrive size={14} />}
                >
                  <div className="flex gap-2">
                    <input
                      className="lx-input"
                      value={inputDir}
                      onChange={(e) => setInputDir(e.target.value)}
                      placeholder="D:\shards\pack-00000.parquet"
                      spellCheck={false}
                    />
                    <button
                      type="button"
                      className="lx-btn-ghost shrink-0 px-3"
                      onClick={() => setPicker(true)}
                    >
                      浏览
                    </button>
                  </div>
                  <button
                    type="button"
                    className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-lx-muted underline-offset-2 hover:text-lx-secondary hover:underline"
                    onClick={() => setAssetsOpen(true)}
                  >
                    <Layers size={11} />
                    查看本机已打包的 parquet（带行数）
                  </button>
                  {/* 不阻断，只提醒：目录也可能装的是分片文件，凭路径判断不了。
                      但"直接选图片目录"这条死路必须提前说出来。 */}
                  {inputDir.trim() !== '' && !looksLikeShard(inputDir) && (
                    <p className="mt-1.5 text-[11px] leading-relaxed text-lx-warning">
                      这不像分片文件。如果它是一堆散图的目录：NL 只读分片里的图，散图不会被打标，
                      先去「数据打包」页封成 .parquet / .zip；如果目录里装的就是分片，忽略这条。
                    </p>
                  )}
                </SettingRow>
              </>
            )}

            {!isVlm && (
              <SettingRow
                label="输入路径"
                description={
                  scan
                    ? `这一层 ${fmtCount(scan.images)} 张图${scan.zips ? `，${scan.zips} 个 .zip 包` : ''}${
                        recursive && includeZips ? '（包内与子目录中的也会算）' : ''
                      }`
                    : undefined
                }
                help="接受目录或单个 .zip 素材包；png / jpg / webp / bmp / gif / tif"
                icon={<FolderOpen size={14} />}
              >
                <div className="flex gap-2">
                  <input
                    className="lx-input"
                    value={inputDir}
                    onChange={(e) => setInputDir(e.target.value)}
                    placeholder="D:\datasets\my-images 或 D:\datasets\pack.zip"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-3"
                    onClick={() => setPicker(true)}
                  >
                    浏览
                  </button>
                </div>
              </SettingRow>
            )}

            <SettingRow
              label="输出目录"
              help={isVlm ? 'parquet/json 分片落在这里；留空默认 work/out' : 'tags/ 逐张产物落在这里（默认 <输入名>-tags）；之后在「数据打包」页封包时指回这里'}
              icon={<HardDrive size={14} />}
            >
              <input
                className="lx-input font-mono text-xs"
                value={outDir}
                onChange={(e) => setOutDir(e.target.value)}
                placeholder={effectiveOut || '跟随输入路径'}
                spellCheck={false}
              />
            </SettingRow>

            {!isVlm && (
              <>
                <SettingRow label="递归扫描">
                  <ToggleRow checked={recursive} onChange={setRecursive} label="包含子目录" />
                </SettingRow>

                <SettingRow label="读取包内图片">
                  <ToggleRow checked={includeZips} onChange={setIncludeZips} label="展开 .zip" />
                </SettingRow>

                <SettingRow label="读取散图">
                  <ToggleRow checked={includeLoose} onChange={setIncludeLoose} label="目录里直接放的图也算" />
                </SettingRow>
              </>
            )}

            <SettingRow label="忽略已有产物" help="默认断点续跑，打开后全部重算">
              <ToggleRow checked={force} onChange={setForce} label="强制重打" />
            </SettingRow>
          </SectionPanel>

          {isVlm && (
            <SectionPanel
              title="NL 模型"
              trailing={
                // API 模式没有"本机服务状态"可言：既没有进程也没有指纹，
                // 如实显示来源，别把本机服务的状态芯片挂在外部 API 上。
                isApiMode ? (
                  <StatusChip tone="neutral">{providerLabel(vlmProvider)}</StatusChip>
                ) : (
                  <StatusChip
                    tone={
                      vllm?.status === 'ready'
                        ? 'success'
                        : vllm?.status === 'failed'
                          ? 'danger'
                          : vllm?.status === 'starting' || vllm?.status === 'stopping'
                            ? 'warning'
                            : 'neutral'
                    }
                  >
                    {vllmStatusLabel(vllm)}
                  </StatusChip>
                )
              }
            >
              {/* 推理来源放在最前面：它决定下面整块长什么样。本机/自建要起服务、
                  有环境指纹；API 模式只填地址 + 密钥 + 模型名，什么都不用启。 */}
              <SettingRow
                label="推理来源"
                icon={<Cloud size={14} />}
                help="本机/自建走本地服务（有环境指纹、能重试提分辨率）；API 模式不加载权重、不用起服务，密钥留空时从环境变量读"
              >
                <select
                  className="lx-input"
                  value={vlmProvider}
                  onChange={(e) => changeProvider(e.target.value as VlmProvider)}
                  aria-label="推理来源"
                >
                  {providerList.length === 0 && (
                    <option value="local">本机 / 自建服务</option>
                  )}
                  {providerList.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </SettingRow>

              {/* 本机服务整块：API 模式下不渲染，而不是灰着。里面一半字段
                  （端口、引擎、分档、放行目录）对 API 毫无意义。 */}
              {!isApiMode && (
                <>
              <SettingRow
                label="本机服务"
                help="第一次用：先点「启动服务」→ 等右上角变成「就绪」→ 再提交任务。首次加载要几分钟"
                icon={<Server size={14} />}
                description={
                  vllm?.status === 'failed'
                    ? vllm.error || '启动失败（日志里通常有原因）'
                    : vllm && !vllm.local_ok
                      ? vllm.local_reason.split('\n')[0]
                      : undefined
                }
              >
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    type="number"
                    className="lx-input w-24 font-mono tabular-nums"
                    min={1024}
                    max={65535}
                    value={vllmPort}
                    disabled={vllm?.status === 'starting' || vllm?.status === 'ready'}
                    onChange={(e) =>
                      setVllmPort(Math.min(65535, Math.max(1024, Number(e.target.value) || 8000)))
                    }
                    aria-label="服务端口"
                  />
                  {vllm?.status === 'starting' || vllm?.status === 'ready' ? (
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex items-center gap-1.5 px-3"
                      disabled={vllmBusy}
                      onClick={() => void stopVllm()}
                    >
                      <Square size={12} />
                      {vllmBusy ? '停止中…' : '停止服务'}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex items-center gap-1.5 px-3"
                      disabled={vllmBusy || capOk === false}
                      title={capOk === false ? capReason : undefined}
                      onClick={() => void startVllm()}
                    >
                      <Play size={12} />
                      {vllmBusy ? '启动中…' : '启动服务'}
                    </button>
                  )}
                  {vllm?.pid != null && (
                    <span className="font-mono text-[11px] text-lx-dim">pid {vllm.pid}</span>
                  )}
                </div>
              </SettingRow>

              {/* 引擎决定"这台机器到底能不能起"。Windows 原生只有 llama.cpp
                  能用（vLLM 官方只有 Linux 版，要 WSL2）；两者产出的指纹不同，
                  换引擎等于换一代数据，所以放在最前面、且起了服务就锁住。 */}
              <SettingRow
                label="引擎"
                help="Windows 原生只能用 llama.cpp；vLLM 需要 Linux / WSL2"
              >
                <select
                  className="lx-select"
                  value={vllmEngine}
                  disabled={vllmLocked}
                  onChange={(e) => setVllmEngine(e.target.value)}
                  aria-label="推理引擎"
                >
                  <option value="vllm">vLLM（Linux / WSL2）</option>
                  <option value="llamacpp">llama.cpp（Windows 原生）</option>
                </select>
              </SettingRow>

              {/* 多卡：一台机器插了多张卡时，一卡一进程各打各的。
                  放在「引擎」下面是因为它复用同一个引擎选择——换引擎等于
                  换一代指纹，不能两块面板各写各的。 */}
              <MultiGpuPanel
                engine={vllmEngine}
                enabled={multiGpu}
                onEnabledChange={setMultiGpu}
                perCardSeqs={perCardSeqs}
                onPerCardSeqsChange={setPerCardSeqs}
              />

              {/* 换权重是最常被问的"在哪儿换模型"。放在这里是因为它只对
                  本机拉起的服务有效——远程服务的模型由那台机器决定。 */}
              <SettingRow
                label="模型"
                icon={<Boxes size={14} />}
                help="留空按显存自动落档；换模型要重启服务"
                description={
                  vllmLocked
                    ? '服务运行中：换模型要先停止服务。'
                    : activePlan && !activePlan.ok
                      ? `这组参数解析不了：${activePlan.error}`
                      : activePlan && activePlan.ok
                        ? `${vllmLocked ? '正在跑' : '将落'} ${activePlan.tier} 档 · ${activePlan.model} · ${activePlan.precision}${
                            // 意图与能力不一致时必须显式说出来：选了 W4A16 但
                            // 没有量化权重，服务实际是按 BF16 加载的。
                            activePlan.precision === 'w4a16' && !activePlan.weight_quantized
                              ? ' → BF16（缺量化权重）'
                              : ''
                          } · 上下文 ${activePlan.max_model_len} · 默认并发 ${activePlan.max_num_seqs}`
                        : undefined
                }
              >
                <select
                  className="lx-select"
                  value={vllmModelKey}
                  disabled={vllmLocked}
                  onChange={(e) => setVllmModelKey(e.target.value)}
                  aria-label="VLM 权重"
                >
                  <option value="">自动（按显存落档）</option>
                  {vllmModels.map((m) => (
                    <option key={m.key} value={m.key}>
                      {m.key}（{basename(m.path)}
                      {m.note ? ` · ${m.note}` : ''}）
                    </option>
                  ))}
                </select>
              </SettingRow>

              {/* 分档与权重是两件事：档位定 max_model_len / 默认并发 / 精度，
                  权重定加载哪个仓库。8B 落在 low-16g 上就会上下文偏短——
                  想让它跑得开就跟着改这里。 */}
              <SettingRow label="分档" help="决定上下文长度、默认并发与精度。留空按显存自动匹配">

                <select
                  className="lx-select"
                  value={vllmTier}
                  disabled={vllmLocked}
                  onChange={(e) => setVllmTier(e.target.value)}
                  aria-label="VLM 分档"
                >
                  <option value="">自动（按显存）</option>
                  {vllmTiers.map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.name} · {t.vram_min}–{t.vram_max === 999 ? '∞' : t.vram_max}G ·{' '}
                      {t.max_model_len} 上下文 · 并发 {t.max_num_seqs}
                    </option>
                  ))}
                </select>
              </SettingRow>

              {/* 精度覆盖分档表。默认 BF16 是唯一"不依赖额外硬件、也不依赖
                  量化权重"的档；换档等于换一代数据（model_id 里带 precision），
                  所以服务运行中锁住，必须先停止。 */}
              <SettingRow
                label="权重精度"
                icon={<Gauge size={14} />}
                help="这是 VLM 权重（出 NL）。默认 BF16；FP8 需 Ada 及以上；W4A16 需先量化，选它会弹窗问一次"
                description={
                  vllmPrecision === 'fp8'
                    ? 'FP8：vLLM 在线量化，显存约省一半；长尾 tag 与数字可能少量改判'
                    : vllmPrecision === 'w4a16'
                      ? 'W4A16：4bit 权重 + 16bit 激活。当前模型没有量化权重时会回退 BF16（见下方警告）'
                      : 'BF16：不传 --quantization，按权重自带 dtype 加载，精度最稳、显存最贵'
                }
              >
                <ChoiceGrid
                  options={VLLM_PRECISIONS}
                  value={vllmPrecision}
                  onChange={choosePrecision}
                  className="grid-cols-3"
                  size="sm"
                  mono
                  disabled={vllmLocked}
                  ariaLabel="VLM 权重精度"
                  renderLabel={(v) => VLLM_PRECISION_LABELS[v]}
                />
              </SettingRow>

              {activePlan && activePlan.ok && activePlan.warnings.length > 0 && (
                <details className="border-b border-lx py-2.5">
                  <summary className="cursor-pointer select-none text-[11px] text-lx-warning hover:text-lx-secondary">
                    模型提示（{activePlan.warnings.length} 条）
                  </summary>
                  <ul className="mt-1.5 space-y-1 text-[11px] leading-relaxed text-lx-muted">
                    {activePlan.warnings.map((w) => (
                      <li key={w} className="flex gap-1.5">
                        <span className="shrink-0 text-lx-warning">·</span>
                        <span>{w}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}

              {/* 批调度：并发调大而单步 token 预算不跟上，吞吐不会涨。
                  收在 details 里是因为默认值就够用，改它的前提是跑过 bench。 */}
              <details className="border-b border-lx py-2.5">
                <summary className="cursor-pointer select-none text-[11px] text-lx-muted hover:text-lx-secondary">
                  批调度（并发调不动时先看这里）
                  {vllm?.status === 'ready' && vllm.overrides.max_num_seqs != null && (
                    <span className="ml-1.5 text-lx-dim">
                      · 当前 seqs {vllm.overrides.max_num_seqs}
                      {vllm.overrides.max_num_batched_tokens != null &&
                        ` / batched ${vllm.overrides.max_num_batched_tokens}`}
                    </span>
                  )}
                </summary>
                <div className="mt-2 space-y-2">
                  <SettingRow
                    label="max_num_seqs"
                    help="服务端同时处理多少条请求。0 = 用分档表的值（16G 卡是 8）"
                  >
                    <input
                      type="number"
                      className="lx-input w-32 font-mono tabular-nums"
                      min={0}
                      max={512}
                      value={vllmSeqs}
                      disabled={vllm?.status === 'starting' || vllm?.status === 'ready'}
                      onChange={(e) =>
                        setVllmSeqs(Math.min(512, Math.max(0, Number(e.target.value) || 0)))
                      }
                      aria-label="服务端并发上限"
                    />
                  </SettingRow>
                  <SettingRow
                    label="max_num_batched_tokens"
                    help="单步 token 预算；0 = vLLM 默认，调大才能同时 prefill 多张图"
                  >
                    <input
                      type="number"
                      className="lx-input w-32 font-mono tabular-nums"
                      min={0}
                      max={262144}
                      step={1024}
                      value={vllmBatched}
                      disabled={vllm?.status === 'starting' || vllm?.status === 'ready'}
                      onChange={(e) =>
                        setVllmBatched(
                          Math.min(262144, Math.max(0, Number(e.target.value) || 0)),
                        )
                      }
                      aria-label="服务端单步 token 预算"
                    />
                  </SettingRow>
                  <SettingRow
                    label="显存占比"
                    help="0 = 用配置值（0.88）；调太高会起不来"
                  >
                    <input
                      type="number"
                      className="lx-input w-32 font-mono tabular-nums"
                      min={0}
                      max={0.98}
                      step={0.01}
                      value={vllmGpuMem}
                      disabled={vllm?.status === 'starting' || vllm?.status === 'ready'}
                      onChange={(e) =>
                        setVllmGpuMem(Math.min(0.98, Math.max(0, Number(e.target.value) || 0)))
                      }
                      aria-label="显存占比"
                    />
                  </SettingRow>
                  <SettingRow
                    label="file:// 放行目录"
                    help={
                      allowedMediaRoot
                        ? `当前服务已放行 ${allowedMediaRoot}。任务侧才能开 file:// 直传`
                        : '留空 = 服务端只收 base64。填了才能用 file:// 传图（省 33% 体积）；必须覆盖任务的产物目录'
                    }
                  >
                    <input
                      className="lx-input font-mono text-xs"
                      value={vllmMediaRoot}
                      disabled={vllmLocked}
                      onChange={(e) => setVllmMediaRoot(e.target.value)}
                      placeholder="留空 = 不收 file://"
                      spellCheck={false}
                    />
                  </SettingRow>
                  <p className="text-[11px] leading-relaxed text-lx-dim">
                    改完要重启服务才生效。每张图约 576 图像 token + 400 指令 token，
                    单步预算给到 seqs × 1024 左右才排得开。
                  </p>
                </div>
              </details>

              {vllmError && (
                <p className="border-b border-lx py-2.5 text-[11px] leading-relaxed text-lx-danger">
                  {vllmError}
                </p>
              )}

              {/* 环境原因常常是好几行（"WSL2 / 换台机器"两条路），
                  一整段塞在左栏会把表单挤乱，收起来看完整版。 */}
              {vllm && !vllm.local_ok && vllm.local_reason.includes('\n') && (
                <details className="border-b border-lx py-2.5">
                  <summary className="cursor-pointer select-none text-[11px] text-lx-muted hover:text-lx-secondary">
                    为什么不能在本机起服务
                  </summary>
                  <pre className="mt-1.5 whitespace-pre-wrap rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10px] leading-relaxed text-lx-muted">
                    {vllm.local_reason}
                  </pre>
                </details>
              )}

              {vllm && (
                <div className="space-y-1.5 border-b border-lx py-3 text-[11px] text-lx-muted">
                  <div className="flex justify-between gap-2">
                    <span>地址</span>
                    <span className="truncate font-mono text-lx-secondary">{vllm.url || '—'}</span>
                  </div>
                  <div className="flex justify-between gap-2">
                    <span>环境指纹</span>
                    <span className="truncate text-lx-secondary">
                      {vllm.fingerprint
                        ? `${String(vllm.fingerprint.model_id ?? '?')} · max_pixels ${
                            vllm.fingerprint.max_pixels ?? '?'
                          }`
                        : '缺失（provenance 会残，重试提分辨率也不会生效）'}
                    </span>
                  </div>
                  {vllm.log_tail.length > 0 && (
                    <details className="pt-1">
                      <summary className="cursor-pointer select-none text-[11px] text-lx-muted hover:text-lx-secondary">
                        服务日志（最近 {vllm.log_tail.length} 行）
                      </summary>
                      <pre className="mt-1.5 max-h-56 overflow-auto rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10px] leading-relaxed text-lx-muted">
                        {vllm.log_tail.join('\n')}
                      </pre>
                    </details>
                  )}
                </div>
              )}

                </>
              )}

              {/* API 模式的两个字段只在真的走外部 API 时出现：本机模式下
                  它们恒为空，摆着只会让人以为哪里填漏了。 */}
              {isApiMode && (
                <>
                  <SettingRow
                    label="API 地址"
                    help="留空按来源取默认域名；自建网关（one-api / LM Studio）填自己的地址"
                  >
                    <div className="flex gap-2">
                      <input
                        className="lx-input font-mono text-xs"
                        value={apiBase}
                        onChange={(e) => {
                          setApiBase(e.target.value)
                          setApiProbe(null)
                        }}
                        placeholder={providerBase(vlmProvider)}
                        spellCheck={false}
                      />
                      <button
                        type="button"
                        className="lx-btn-ghost inline-flex shrink-0 items-center gap-1.5 px-3"
                        disabled={apiProbeBusy || !vlmModel.trim()}
                        onClick={() => void probeApi()}
                        title="发一张 8×8 的小图走一次真实请求"
                      >
                        <RefreshCw size={12} className={apiProbeBusy ? 'animate-spin' : undefined} />
                        {apiProbeBusy ? '测试中…' : '测试连接'}
                      </button>
                    </div>
                    {apiProbe && (
                      <p
                        className={`mt-1.5 text-[11px] leading-relaxed ${
                          apiProbe.ok ? 'text-lx-success' : 'text-lx-danger'
                        }`}
                      >
                        {apiProbe.ok
                          ? `通 · ${apiProbe.latency_ms}ms · 入 ${apiProbe.prompt_tokens} / 出 ${apiProbe.completion_tokens} token${
                              apiProbe.text ? ` · 回「${apiProbe.text.slice(0, 24)}」` : ''
                            }`
                          : apiProbe.error}
                      </p>
                    )}
                  </SettingRow>
                  <SettingRow
                    label="API 密钥"
                    help={
                      savedKey
                        ? `已记住 ${savedKey.key_masked}，留空就用它`
                        : providerEnvKey(vlmProvider)
                          ? `留空则读环境变量 ${providerEnvKey(vlmProvider)}`
                          : '留空表示不带鉴权头（免鉴权的自建网关很常见）'
                    }
                  >
                    <input
                      type="password"
                      className="lx-input font-mono text-xs"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder={savedKey ? `${savedKey.key_masked}（已记住）` : '粘贴密钥'}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    {/* 默认不存：密钥落盘是明文，勾了才写 configs/nl_api.json。 */}
                    <label className="mt-1.5 flex items-center gap-1.5 text-[11px] text-lx-muted">
                      <input
                        type="checkbox"
                        className="lx-check"
                        checked={rememberKey}
                        onChange={(e) => void toggleRemember(e.target.checked)}
                      />
                      记住到本机（明文存 configs/nl_api.json）
                    </label>
                  </SettingRow>
                </>
              )}

              {!isApiMode && (
                <>
              <SettingRow
                label="服务地址"
                help="本机服务就绪后自动填；远程服务直接填 IP"
              >
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-xs"
                    value={vlmUrl}
                    onChange={(e) => {
                      setVlmUrl(e.target.value)
                      setProbe(null)
                    }}
                    placeholder="http://127.0.0.1:8000"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost inline-flex shrink-0 items-center gap-1.5 px-3"
                    disabled={probeBusy || !vlmUrl.trim()}
                    onClick={() => void probeVlm()}
                  >
                    <RefreshCw size={12} className={probeBusy ? 'animate-spin' : undefined} />
                    {probeBusy ? '探测中…' : '探测'}
                  </button>
                </div>
                {probe && (
                  <p
                    className={`mt-1.5 text-[11px] leading-relaxed ${
                      probe.ok ? 'text-lx-success' : 'text-lx-danger'
                    }`}
                  >
                    {probe.ok
                      ? `在线${probe.version ? ` · vllm ${probe.version}` : ''}${
                          probe.models.length ? ` · 模型 ${probe.models.join(', ')}` : ''
                        }`
                      : probe.error}
                  </p>
                )}
                {/* 指本机但服务没起：任务会在第一张图就超时失败，而"连不上"
                    与"模型名写错"在页面上长得一样，先说清是哪一种。 */}
                {localVlmUrl && vllm?.status !== 'ready' && (
                  <p className="mt-1.5 text-[11px] leading-relaxed text-lx-warning">
                    这是本机地址，但本机服务
                    {vllm?.status === 'starting'
                      ? '还在启动（首次加载要几分钟）'
                      : vllm?.status === 'failed'
                        ? '启动失败'
                        : '没在运行'}
                    ：先在上面拉起服务，或把地址改成别的机器。
                  </p>
                )}
              </SettingRow>
                </>
              )}

              <SettingRow
                label="模型名"
                help={
                  isApiMode
                    ? 'API 侧的模型 id（如 gpt-4o / claude-sonnet-4-5），不是本地路由名'
                    : '对应 --served-model-name，只是路由名，与权重无关'
                }
              >
                <input
                  className="lx-input font-mono text-xs"
                  value={vlmModel}
                  onChange={(e) => {
                    setVlmModel(e.target.value)
                    setApiProbe(null)
                  }}
                  placeholder={isApiMode ? '填 API 提供的模型 id' : 'tagger'}
                  list={isApiMode ? 'vlm-api-model-hints' : undefined}
                  spellCheck={false}
                />
                {/* 常见 id 只是提示：写错了的表现是 404 一张张失败，而
                    下拉列表不可能穷举账号里能用的模型。 */}
                {isApiMode && (
                  <datalist id="vlm-api-model-hints">
                    {(providerMeta(vlmProvider)?.models ?? []).map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
                )}
              </SettingRow>

              <SettingRow
                label="并发请求数"
                help={
                  isApiMode
                    ? '0 = 用该来源的建议值；云端按每分钟请求数限流，调太高会成片撞 429'
                    : '0 = 跟服务端 max_num_seqs 走'
                }
                description={
                  concurrency === 0
                    ? `自动：${isApiMode ? `${providerLabel(vlmProvider)} 建议` : '按当前指纹是'} ${effectiveSeqs}`
                    : undefined
                }
              >
                <input
                  type="number"
                  className="lx-input font-mono tabular-nums"
                  min={0}
                  max={32}
                  value={concurrency}
                  onChange={(e) =>
                    setConcurrency(Math.min(32, Math.max(0, Number(e.target.value) || 0)))
                  }
                />
              </SettingRow>

              <details className="pt-1">
                <summary className="cursor-pointer select-none text-[11px] uppercase tracking-wide text-lx-muted hover:text-lx-secondary">
                  生成参数（一般不用动）
                </summary>
                <div className="mt-2 space-y-0">
                  <SettingRow label="温度" help="越低越稳；打标建议 ≤0.3">
                    <div className="flex items-center gap-3">
                      <input
                        type="range"
                        className="lx-slider"
                        min={0}
                        max={1}
                        step={0.05}
                        value={temperature}
                        onChange={(e) => setTemperature(Number(e.target.value))}
                        aria-label="温度"
                      />
                      <span className="w-12 shrink-0 text-right font-mono text-sm tabular-nums text-lx">
                        {temperature.toFixed(2)}
                      </span>
                    </div>
                  </SettingRow>

                  <SettingRow label="top_p">
                    <div className="flex items-center gap-3">
                      <input
                        type="range"
                        className="lx-slider"
                        min={0.1}
                        max={1}
                        step={0.05}
                        value={topP}
                        onChange={(e) => setTopP(Number(e.target.value))}
                        aria-label="top_p"
                      />
                      <span className="w-12 shrink-0 text-right font-mono text-sm tabular-nums text-lx">
                        {topP.toFixed(2)}
                      </span>
                    </div>
                  </SettingRow>

                  <SettingRow label="max_tokens" help="0 = 按规格自动抬高（NL + 全量 tag 放得下）">
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      value={maxTokens}
                      onChange={(e) => setMaxTokens(Math.max(0, Number(e.target.value) || 0))}
                    />
                  </SettingRow>

                  {/* 描述长度：只决定"往多长写"，不动合格线（nl_min/max_tokens）。
                      实测 400-1024 → 250-500：字数 −33%、单张 −36%，
                      六段要素覆盖只掉 2~3pp，所以这是最省机时的一个旋钮。 */}
                  <SettingRow
                    label="描述长度"
                    help="字符数下限-上限。空 = 按 token 区间派生（400-1024）；250-500 实测快 36%、要素覆盖只掉 2~3pp"
                  >
                    <div className="flex items-center gap-1.5">
                      <input
                        type="number"
                        className="lx-input w-20 font-mono tabular-nums"
                        min={100}
                        max={2900}
                        step={50}
                        placeholder="400"
                        value={captionLo || ''}
                        onChange={(e) => setCaptionLo(Number(e.target.value) || 0)}
                        aria-label="描述长度下限"
                      />
                      <span className="text-xs text-lx-dim">-</span>
                      <input
                        type="number"
                        className="lx-input w-20 font-mono tabular-nums"
                        min={200}
                        max={3000}
                        step={50}
                        placeholder="1024"
                        value={captionHi || ''}
                        onChange={(e) => setCaptionHi(Number(e.target.value) || 0)}
                        aria-label="描述长度上限"
                      />
                    </div>
                  </SettingRow>

                  <SettingRow label="请求超时（秒）">
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={10}
                      value={timeoutS}
                      onChange={(e) => setTimeoutS(Math.max(10, Number(e.target.value) || 90))}
                    />
                  </SettingRow>

                  <SettingRow label="像素预算" help="0 = 用配置/指纹的默认预算；超预算的图会先缩">
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      step={50000}
                      value={maxPixels}
                      onChange={(e) => setMaxPixels(Math.max(0, Number(e.target.value) || 0))}
                    />
                  </SettingRow>

                  <SettingRow label="随机种子" help="留空 = 不固定">
                    <input
                      className="lx-input font-mono tabular-nums"
                      value={seed}
                      onChange={(e) => setSeed(e.target.value.replace(/[^\d-]/g, ''))}
                      placeholder="不固定"
                    />
                  </SettingRow>

                  <SettingRow
                    label="质检放行线"
                    help={`1~5 分，≥ 这条直接收（0 = 用配置的 ${qPassDef}）。抬得越高，重打越多、吞吐越低`}
                  >
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      max={5}
                      step={0.1}
                      value={qualityPass}
                      onChange={(e) =>
                        setQualityPass(Math.min(5, Math.max(0, Number(e.target.value) || 0)))
                      }
                    />
                  </SettingRow>

                  <SettingRow
                    label="质检丢弃线"
                    help={`< 这条直接丢，不浪费重打（0 = 用配置的 ${qDropDef}）。必须低于放行线`}
                  >
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      max={5}
                      step={0.1}
                      value={qualityDrop}
                      onChange={(e) =>
                        setQualityDrop(Math.min(5, Math.max(0, Number(e.target.value) || 0)))
                      }
                    />
                  </SettingRow>

                  <SettingRow
                    label="重打次数上限"
                    help={`不合格时最多再打几次（0 = 用配置的 ${qAttemptsDef}）。每多一次就是多一份推理成本`}
                  >
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      max={5}
                      value={qualityAttempts}
                      onChange={(e) =>
                        setQualityAttempts(Math.min(5, Math.max(0, Number(e.target.value) || 0)))
                      }
                    />
                  </SettingRow>

                  <SettingRow
                    label="失败跳过阈值"
                    help="累计失败多少次后不再碰这张图（0 = 不限）"
                  >
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      value={maxFailures}
                      onChange={(e) =>
                        setMaxFailures(Math.max(0, Number(e.target.value) || 0))
                      }
                    />
                  </SettingRow>

                  <div className="px-1 pb-1 pt-2 text-[11px] uppercase tracking-wide text-lx-muted">
                    重试阶梯
                  </div>
                  <SettingRow
                    label="温度上调"
                    help={`每重打一次温度加多少（0 = 用配置的 ${qTempStepDef}）`}
                  >
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      max={1}
                      step={0.05}
                      value={retryTempStep}
                      onChange={(e) =>
                        setRetryTempStep(Math.min(1, Math.max(0, Number(e.target.value) || 0)))
                      }
                    />
                  </SettingRow>
                  <SettingRow
                    label="温度封顶"
                    help={`温度最高抬到哪（0 = 用配置的 ${qTempCapDef}）。太高会把描述带偏`}
                  >
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      max={2}
                      step={0.05}
                      value={retryTempCap}
                      onChange={(e) =>
                        setRetryTempCap(Math.min(2, Math.max(0, Number(e.target.value) || 0)))
                      }
                    />
                  </SettingRow>
                  <SettingRow
                    label="像素放大倍数"
                    help={`重打时像素预算乘几倍（0 = 用配置的 ${qPixelsScaleDef}）。需要服务端 max_pixels 留余量，否则会被钳回去`}
                  >
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      max={4}
                      step={0.1}
                      value={retryPixelsScale}
                      onChange={(e) =>
                        setRetryPixelsScale(Math.min(4, Math.max(0, Number(e.target.value) || 0)))
                      }
                    />
                  </SettingRow>
                  <SettingRow
                    label="像素封顶"
                    help={`重打最高抬到多少像素（0 = 用配置的 ${qPixelsCapDef}）`}
                  >
                    <input
                      type="number"
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      step={50000}
                      value={retryPixelsCap}
                      onChange={(e) =>
                        setRetryPixelsCap(Math.max(0, Number(e.target.value) || 0))
                      }
                    />
                  </SettingRow>

                  <div className="px-1 pb-1 pt-2 text-[11px] uppercase tracking-wide text-lx-muted">
                    语义复核（L2）
                  </div>
                  <SettingRow
                    label="图文 CLIP 打分"
                    help={`规则层抓不到的"句子通顺但完全跑题"靠它。需要 pip install open_clip_torch，吃一份显存，且打分不是线程安全的——开启后并发会被摁到 1，整条链路慢一个数量级`}
                    icon={clipOn ? <Cpu size={14} /> : <CloudOff size={14} />}
                  >
                    <ToggleRow
                      checked={clipOn}
                      onChange={setClipOn}
                      label={clipOn ? `开启（${qDef?.clip_model || 'ViT-L-14'}）` : '关闭'}
                    />
                  </SettingRow>
                  {clipOn && (
                    <>
                      <SettingRow
                        label="相似度下限"
                        help={`低于这条判"跑题"（0 = 用配置的 ${qClipMinDef}）。抬得越高，误杀越多`}
                      >
                        <input
                          type="number"
                          className="lx-input font-mono tabular-nums"
                          min={0}
                          max={1}
                          step={0.01}
                          value={clipMin}
                          onChange={(e) =>
                            setClipMin(Math.min(1, Math.max(0, Number(e.target.value) || 0)))
                        }
                      />
                    </SettingRow>
                    <SettingRow
                      label="CLIP 权重"
                      help={`open_clip 的模型名（留空 = 配置的 ${qDef?.clip_model || 'ViT-L-14'}）。换它等于换打分口径，两批分数不能直接比`}
                    >
                      <input
                        className="lx-input font-mono text-xs"
                        value={clipModel}
                        onChange={(e) => setClipModel(e.target.value)}
                        placeholder={qDef?.clip_model || 'ViT-L-14'}
                        spellCheck={false}
                      />
                    </SettingRow>
                    <SettingRow
                      label="预训练来源"
                      help={`留空 = 配置的 ${qDef?.clip_pretrained || 'openai'}。laion2b 这类开放权重与 openai 的分数分布不同，阈值要重调`}
                    >
                      <input
                        className="lx-input font-mono text-xs"
                        value={clipPretrained}
                        onChange={(e) => setClipPretrained(e.target.value)}
                        placeholder={qDef?.clip_pretrained || 'openai'}
                        spellCheck={false}
                      />
                    </SettingRow>
                    </>
                  )}

                  <div className="px-1 pb-1 pt-2 text-[11px] uppercase tracking-wide text-lx-muted">
                    规则层（L1）
                  </div>
                  <SettingRow
                    label="最短字符数"
                    help={`低于这条判"过短"（留空 = 配置的 ${qDef?.min_chars ?? 140}；填 0 = 不设下限）。注意它和下面那条一起构成区间`}
                  >
                    <input
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      step={10}
                      value={minChars}
                      onChange={(e) => setMinChars(e.target.value.replace(/[^\d]/g, ''))}
                      placeholder={String(qDef?.min_chars ?? 140)}
                    />
                  </SettingRow>
                  <SettingRow
                    label="最长字符数"
                    help={`高于这条判"过长"（留空 = 配置的 ${qDef?.max_chars ?? 1200}）。必须不小于最短字符数`}
                  >
                    <input
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      step={10}
                      value={maxChars}
                      onChange={(e) => setMaxChars(e.target.value.replace(/[^\d]/g, ''))}
                      placeholder={String(qDef?.max_chars ?? 1200)}
                    />
                  </SettingRow>
                  {minChars !== '' && maxChars !== '' && Number(minChars) > Number(maxChars) && (
                    <p className="px-1 text-[11px] leading-relaxed text-lx-danger">
                      最短（{minChars}）大于最长（{maxChars}）：每条都会被判过长，通过率会是 0。
                    </p>
                  )}
                  <SettingRow
                    label="tag 覆盖率下限"
                    help={`高置信 tag 在 caption 里被覆盖的比例低于这条就扣分（留空 = 配置的 ${qDef?.min_tag_coverage ?? 0.6}，填 0 = 不检查）`}
                  >
                    <input
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      max={1}
                      step={0.05}
                      value={minCoverage}
                      onChange={(e) => setMinCoverage(e.target.value)}
                      placeholder={String(qDef?.min_tag_coverage ?? 0.6)}
                    />
                  </SettingRow>
                  <SettingRow
                    label="重复率上限"
                    help={`4-gram 重复率高于这条判复读（留空 = 配置的 ${qDef?.max_repeat_ratio ?? 0.35}，填 0 = 一个都不许重复）`}
                  >
                    <input
                      className="lx-input font-mono tabular-nums"
                      min={0}
                      max={1}
                      step={0.05}
                      value={maxRepeat}
                      onChange={(e) => setMaxRepeat(e.target.value)}
                      placeholder={String(qDef?.max_repeat_ratio ?? 0.35)}
                    />
                  </SettingRow>

                  <div className="px-1 pb-1 pt-2 text-[11px] uppercase tracking-wide text-lx-muted">
                    重试升级
                  </div>
                  <SettingRow
                    label="升级服务地址"
                    help={
                      isApiMode
                        ? '重试用尽仍不合格时改打第二个地址（更大的模型）。API 模式复用同一把密钥，填目标地址即可'
                        : '重试用尽仍不合格时，把这张图打到第二个服务（更大的权重）。一个 vLLM 进程只服务一套权重，所以必须是另一个地址'
                    }
                  >
                    <input
                      className="lx-input font-mono text-xs"
                      value={escalateUrl}
                      onChange={(e) => setEscalateUrl(e.target.value)}
                      placeholder="留空 = 不升级"
                      spellCheck={false}
                    />
                  </SettingRow>
                  {escalateUrl.trim() !== '' && (
                    <SettingRow
                      label="升级模型名"
                      help="那个服务 --served-model-name 的值；两个都填才会生效"
                    >
                      <input
                        className="lx-input font-mono text-xs"
                        value={escalateModel}
                        onChange={(e) => setEscalateModel(e.target.value)}
                        placeholder="tagger-8b"
                        spellCheck={false}
                      />
                    </SettingRow>
                  )}
                  {(escalateUrl.trim() !== '') !== (escalateModel.trim() !== '') && (
                    <p className="px-1 text-[11px] leading-relaxed text-lx-warning">
                      地址和模型名要一起填，只填一个不会生效。
                    </p>
                  )}
                  {escalateUrl.trim() !== '' && escalateModel.trim() !== '' && (
                    <p className="px-1 text-[11px] leading-relaxed text-lx-secondary">
                      升级出来的样本会在 provenance 里标 <code>escalated</code> 与
                      <code> escalated_model</code>，清洗时能整批挑出来——两套权重的产物
                      不能当同一批数据混用。
                    </p>
                  )}

                  {/* file:// 只有本机引擎认：API 模式必走 base64，这个开关
                      露出来只会让人以为能省 33% 体积，实际每张都会被拒。 */}
                  {!isApiMode && (
                    <>
                  <div className="px-1 pb-1 pt-2 text-[11px] uppercase tracking-wide text-lx-muted">
                    传图方式
                  </div>
                  <SettingRow
                    label="file:// 直传"
                    help={
                      allowedMediaRoot
                        ? `服务端已放行 ${allowedMediaRoot}；开启后省掉 base64 的 33% 体积与编码 CPU`
                        : '服务端没开 --allowed-local-media-path，开了每张图都会被 400 拒绝。请在上面「VLM 服务」里填放行目录'
                    }
                  >
                    <ToggleRow
                      checked={useFileUrl && Boolean(allowedMediaRoot)}
                      onChange={setUseFileUrl}
                      disabled={!allowedMediaRoot}
                      label={useFileUrl && allowedMediaRoot ? '用 file://' : 'base64（默认）'}
                    />
                  </SettingRow>
                  {useFileUrl && allowedMediaRoot && effectiveOut && (() => {
                    // 客户端的 spool 落在产物目录下，放行根目录必须覆盖它，
                    // 否则每张图都会被服务端 400 拒绝，而错误信息只说"路径不允许"。
                    const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '')
                    const inside = norm(effectiveOut).toLowerCase().startsWith(
                      norm(allowedMediaRoot).toLowerCase(),
                    )
                    return inside ? null : (
                      <p className="px-1 text-[11px] leading-relaxed text-lx-danger">
                        产物目录 <code>{effectiveOut}</code> 不在放行目录{' '}
                        <code>{allowedMediaRoot}</code> 之下，file:// 会被服务端拒绝。
                        要么把放行目录改成产物目录的上级，要么关掉这个开关。
                      </p>
                    )
                  })()}
                    </>
                  )}
                </div>
              </details>
            </SectionPanel>
          )}

          {isVlm && (
            <SectionPanel title="产物（VLM）">
              <SettingRow
                label="分片格式"
                help="parquet 回传页直接上传；json 逐张一份"
                icon={<Package size={14} />}
              >
                <ChoiceGrid
                  options={ARTIFACT_FORMATS}
                  value={artifactFormat}
                  onChange={setArtifactFormat}
                  className="grid-cols-2"
                  size="sm"
                  mono
                  ariaLabel="分片格式"
                />
              </SettingRow>

              {artifactFormat === 'parquet' && (
                <SettingRow label="每片行数" help="写满就封口；2000 一片对 FTP 回传比较友好">
                  <input
                    type="number"
                    className="lx-input font-mono tabular-nums"
                    min={100}
                    value={parquetRows}
                    onChange={(e) => setParquetRows(Math.max(100, Number(e.target.value) || 2000))}
                  />
                </SettingRow>
              )}

              <SettingRow
                label="结果缓存"
                help="同图同参数直接复用上次产出，不送模型。首轮命中率为 0，重跑 / 换输出目录 / 多机重叠素材才划算"
              >
                <label className="flex cursor-pointer items-center gap-2 text-xs text-lx-secondary">
                  <input
                    type="checkbox"
                    className="accent-lx-accent"
                    checked={cacheOn}
                    onChange={(e) => setCacheOn(e.target.checked)}
                  />
                  开启（库在 run/ 下跨任务共享，默认上限 8 GB）
                </label>
              </SettingRow>

              <SettingRow
                label="画师注入"
                icon={<Tags size={14} />}
                help="打开会改写 .nl / .tag 文本，与之前几批就不再是同一代数据"
                description={
                  vlmArtist
                    ? '已启用：会在 .nl / .tag 文本开头写入 @画师'
                    : '关闭：画师名仍保留在产物 JSON 的 artist 字段，只是不进文本'
                }
              >
                <ToggleRow
                  checked={vlmArtist}
                  onChange={setVlmArtist}
                  label={vlmArtist ? '写进 .nl / .tag 文本' : '仅保留 JSON 字段'}
                />
              </SettingRow>

              {vlmArtist && (
                <SettingRow
                  label="注入方式"
                  help="关掉只留在产物 JSON 的 artist / artist_tag 字段，.nl 文本里不带 @画师"
                >
                  <ToggleRow
                    checked={vlmArtistInject}
                    onChange={setVlmArtistInject}
                    label={vlmArtistInject ? '@画师 放在文本最前面' : '只留在 JSON 字段'}
                  />
                </SettingRow>
              )}

              <p className="rounded-lx-sm bg-lx-accent-subtle px-2.5 py-2 text-[11px] leading-relaxed text-lx-secondary">
                这批打完别急着发分包：先去「去重」页扫一遍重复图（打标跑着时就可以扫下一批），
                分包时带上排除清单，重复图不进包、不占编号。
              </p>
            </SectionPanel>
          )}

          {!isVlm && (
            <SectionPanel title="命名">
              <SettingRow
                label="布局"
                help="flat = 全部拍平；mirror = 保留原目录结构"
                icon={<Tags size={14} />}
              >
                <ChoiceGrid
                  options={LAYOUTS}
                  value={layout}
                  onChange={setLayout}
                  className="grid-cols-2"
                  size="sm"
                  renderLabel={(v) => (v === 'flat' ? '平铺改名' : '保持原结构')}
                  ariaLabel="布局"
                />
              </SettingRow>

              <SettingRow
                label="画师名"
                help="从素材路径的某一层目录取画师名，写进 image_id 与归档 meta；取不到就只留序号"
              >
                <ToggleRow checked={artist} onChange={setArtist} label="从目录名提取画师" />
              </SettingRow>

              <SettingRow
                label="取第几层"
                help={
                  artist
                    ? `0 = 素材根目录下第一层（如 alice/1.jpg → alice）；1 = 再往下一层（如 2024/alice/1.jpg → alice）`
                    : '先打开上面的「从目录名提取画师」'
                }
                description={
                  artist && preview
                    ? `本批识别到 ${preview.artists.count} 个画师，覆盖 ${fmtCount(preview.artists.covered)} 张${
                        preview.artists.missing ? `，${fmtCount(preview.artists.missing)} 张取不到` : ''
                      }`
                    : undefined
                }
              >
                <input
                  type="number"
                  className="lx-input font-mono tabular-nums"
                  min={0}
                  max={8}
                  value={artistDepth}
                  disabled={!artist}
                  onChange={(e) =>
                    setArtistDepth(Math.min(8, Math.max(0, Number(e.target.value) || 0)))
                  }
                />
              </SettingRow>

              <SettingRow
                label="画师注入"
                help={
                  artist
                    ? '开：把 @画师 写到 .tag / .nl 文本开头（训练时文本条件里带画师）；关：只留在 JSON 的 artist 字段'
                    : '需要先打开「从目录名提取画师」'
                }
              >
                <ToggleRow
                  checked={artist && artistInject}
                  onChange={(v) => {
                    setArtistInject(v)
                    if (v && !artist) setArtist(true)
                  }}
                  disabled={!artist}
                  label={artist && artistInject ? '写进 .tag / .nl 文本' : '只保留在 JSON 字段'}
                />
              </SettingRow>

              <SettingRow label="序号宽度" help="6 位支持 999999 张；素材超过这个量级要提前调大">
                <input
                  type="number"
                  className="lx-input font-mono tabular-nums"
                  min={3}
                  max={12}
                  value={seqWidth}
                  onChange={(e) => setSeqWidth(Math.min(12, Math.max(3, Number(e.target.value) || 6)))}
                />
              </SettingRow>

              <div className="pt-1">
                <div className="mb-2 text-[11px] uppercase tracking-wide text-lx-muted">命名预览</div>
                {previewError ? (
                  <p className="text-xs text-lx-danger">{previewError}</p>
                ) : !preview ? (
                  <p className="text-xs text-lx-muted">选好输入后这里会显示实际会用的名字。</p>
                ) : (
                  <div className="space-y-1">
                    {preview.samples.map((s) => (
                      <div key={s.image_id} className="flex items-center gap-2 font-mono text-[11px]">
                        <span className="min-w-0 flex-1 truncate text-lx-muted" title={s.rel}>
                          {s.rel}
                        </span>
                        <span className="shrink-0 text-lx-dim">→</span>
                        <span className="shrink-0 text-lx-secondary">{s.name}</span>
                      </div>
                    ))}
                    {preview.warnings.map((w) => (
                      <p key={w} className="pt-1 text-[11px] text-lx-warning">
                        {w}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            </SectionPanel>
          )}

          {hasTag && (
            <SectionPanel
              title="Tag 模型"
              description={
                isVlm && backend === 'none'
                  ? '当前 tag 交给 VLM 出，本地分类头不会加载。'
                  : undefined
              }
            >
              <SettingRow
                label="后端"
                help={isVlm ? 'none = tag 交给 VLM；选本地后端则 tag 走 GPU/CPU、NL 走 VLM' : undefined}
              >
                <select
                  className="lx-select"
                  value={backend}
                  onChange={(e) => {
                    const next = e.target.value
                    setBackend(next)
                    // 预设模型是按后端分组的：切后端时把不属于新后端的选中项
                    // 退回「配置默认」，否则会把上一个后端的仓库名喂给新后端。
                    const owner = backendOfRepo(modelRepo)
                    if (owner && owner !== next) setModelRepo('')
                  }}
                >
                  {backendOptions.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </SettingRow>

            <SettingRow
              label="分类头精度"
              help="tag 分类头的算力精度（与上面 NL 的权重精度是两件事）。bf16 与 fp32 的 tag 集合会有极小差异（实测 79 张里差 2 张）"
            >
              <ChoiceGrid
                options={PRECISIONS}
                value={precision as (typeof PRECISIONS)[number]}
                onChange={(v) => setPrecision(v)}
                className="grid-cols-3"
                size="sm"
                mono
                ariaLabel="精度"
              />
            </SettingRow>

            <SettingRow
              label="设备"
              help="auto 会在有卡时选 cuda:0，否则退回 cpu；选 GPU 才能跑 pixai"
            >
              <ChoiceGrid
                options={DEVICES}
                value={device as (typeof DEVICES)[number]}
                onChange={(v) => setDevice(v)}
                className="grid-cols-4"
                size="sm"
                mono
                ariaLabel="设备"
              />
            </SettingRow>

            <SettingRow
              label="批大小"
              help="CUDA Graph 只在批内形状固定时才生效，4~8 通常最划算"
            >
              <ChoiceGrid
                options={BATCHES}
                value={String(batch) as (typeof BATCHES)[number]}
                onChange={(v) => setBatch(Number(v))}
                className="grid-cols-5"
                size="sm"
                mono
                ariaLabel="批大小"
                renderLabel={(v) => `${v}`}
              />
            </SettingRow>

            <SettingRow
              label="预处理"
              help={
                '解码都在 CPU，区别只在缩放/归一化放哪；1024² 大 PNG 上卡约 1.5×，' +
                'JPEG 基本不赚。上卡换了重采样实现，线稿类图约一成 tag 会变'
              }
            >
              <ChoiceGrid
                options={PREPROCESS}
                value={preprocess as (typeof PREPROCESS)[number]}
                onChange={(v) => setPreprocess(v)}
                className="grid-cols-2"
                size="sm"
                ariaLabel="预处理"
                renderLabel={(v) => PREPROCESS_LABELS[v]}
              />
            </SettingRow>

            <SettingRow
              label="阈值"
              help="全局默认阈值；按类别覆盖见下方「按类别阈值」"
              icon={<Gauge size={14} />}
            >
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  className="lx-slider"
                  min={0.05}
                  max={0.95}
                  step={0.01}
                  value={threshold}
                  onChange={(e) => setThreshold(Number(e.target.value))}
                  aria-label="阈值"
                />
                <span className="w-12 shrink-0 text-right font-mono text-sm tabular-nums text-lx">
                  {threshold.toFixed(2)}
                </span>
              </div>
            </SettingRow>

            {keepOptions.length > 0 && (
              <SettingRow
                label="保留类别"
                help="不勾 = 用配置默认；勾选后只保留这些类别的 tag"
              >
                <div className="flex flex-wrap gap-1.5">
                  {keepOptions.map((c) => {
                    const active = keepCategories.includes(c)
                    return (
                      <button
                        key={c}
                        type="button"
                        onClick={() => toggleKeep(c)}
                        aria-pressed={active}
                        className={`rounded-lx-sm border px-2 py-1 font-mono text-[11px] transition-colors ${
                          active
                            ? 'border-lx-accent bg-lx-accent-subtle text-lx'
                            : 'border-lx bg-lx-surface-raised text-lx-muted hover:text-lx'
                        }`}
                      >
                        {c}
                      </button>
                    )
                  })}
                </div>
              </SettingRow>
            )}

            {keepOptions.length > 0 && (
              <details className="pt-1">
                <summary className="cursor-pointer select-none text-[11px] uppercase tracking-wide text-lx-muted hover:text-lx-secondary">
                  按类别阈值（空 = 用全局）
                </summary>
                <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
                  {keepOptions.map((c) => (
                    <label key={c} className="flex items-center gap-2 text-[11px]">
                      <span className="w-20 shrink-0 truncate font-mono text-lx-muted" title={c}>
                        {c}
                      </span>
                      <input
                        type="number"
                        className="lx-input px-1.5 py-1 font-mono text-[11px] tabular-nums"
                        min={0}
                        max={1}
                        step={0.01}
                        value={catThresholds[c] ?? ''}
                        placeholder="默认"
                        onChange={(e) =>
                          setCatThresholds((prev) => ({ ...prev, [c]: e.target.value }))
                        }
                      />
                    </label>
                  ))}
                </div>
              </details>
            )}

            <SettingRow
              label="tag 模型（分类头）"
              icon={<Tags size={14} />}
              help="tag 分类头的权重，与出 NL 的大模型无关"
              description={
                isVlm && backend === 'none'
                  ? '当前 tag 由 VLM 自己出，这里不会加载任何本地分类头。'
                  : tagRepoForBackend
                    ? `当前后端的默认仓库：${tagRepoForBackend}`
                    : undefined
              }
            >
              <div className="space-y-2">
                <select
                  className="lx-select"
                  value={tagModelCustom ? '__custom__' : modelRepo}
                  disabled={isVlm && backend === 'none'}
                  onChange={(e) => {
                    const v = e.target.value
                    if (v === '__custom__') {
                      setTagModelCustom(true)
                      return
                    }
                    setTagModelCustom(false)
                    setModelRepo(v)
                    // 选了别的后端的权重就把后端一起切过去，否则会把
                    // pixai 的仓库名喂给 wd-eva02，加载直接失败。
                    const owner = backendOfRepo(v)
                    if (owner) setBackend(owner)
                  }}
                  aria-label="tag 模型"
                >
                  <option value="">
                    配置默认{tagRepoForBackend ? `（${tagRepoForBackend}）` : '（后端内置）'}
                  </option>
                  {tagModelPresets.map(([key, repo]) => (
                    <option key={key} value={repo}>
                      {repo} · {key}
                    </option>
                  ))}
                  <option value="__custom__">自定义仓库名…</option>
                </select>
                {tagModelCustom && (
                  <input
                    className="lx-input font-mono text-xs"
                    value={modelRepo}
                    onChange={(e) => setModelRepo(e.target.value)}
                    placeholder="org/model-name"
                    spellCheck={false}
                    aria-label="自定义 tag 模型仓库"
                  />
                )}
              </div>
            </SettingRow>

            <SettingRow
              label="权重目录"
              description={defaults?.models_dir || undefined}
              help="填了就不走 HF 缓存/仓库名。wd-eva02 / cl-tagger-v2 / onnx 认这个字段"
            >
              <input
                className="lx-input font-mono text-xs"
                value={modelDir}
                onChange={(e) => setModelDir(e.target.value)}
                placeholder="h:\tagger\models\wd-eva02"
                spellCheck={false}
              />
            </SettingRow>
            </SectionPanel>
          )}

          <SectionPanel title="运行保护">
            <SettingRow label="只跑前 N 张" help="0 = 不限">
              <input
                type="number"
                className="lx-input font-mono tabular-nums"
                min={0}
                value={limit}
                onChange={(e) => setLimit(Math.max(0, Number(e.target.value) || 0))}
              />
            </SettingRow>

            <SettingRow
              label="硬件保护"
              help="占空比 / 温度 / 强制休息"
              icon={guard ? <Cpu size={14} /> : <CloudOff size={14} />}
            >
              <ToggleRow checked={guard} onChange={setGuard} label="开启" />
            </SettingRow>

            {/* 只对本地引擎生效：VLM 的产物是 parquet 流式写出，总量要跑到
                中途才知道，没法在开跑前判盘。本地引擎有完整 plan，可以估。
                之前这个开关只在「数据打包」页有，打标这边写满盘就被吞了。 */}
            {!isVlm && (
              <SettingRow
                label="盘位预算"
                help="按「预计产出 + 20% 冗余」判；关掉就回落到配置里的固定磁盘阈值"
                icon={<HardDrive size={14} />}
              >
                <ToggleRow checked={checkSpace} onChange={setCheckSpace} label="开跑前检查磁盘" />
              </SettingRow>
            )}

            {/* 阈值的默认值来自 tiers.yaml 的 limits 段（后端 /api/defaults 转出来），
                这里改只影响本次任务，不写回配置文件——配置文件是"团队的默认口径"，
                一次试跑就把它改掉，下一批人跑出来的东西就不是同一个东西了。 */}
            {guard && (
              <div className="border-b border-lx py-3.5 last:border-b-0">
                <div className="mb-2.5 flex items-center justify-between gap-3">
                  <span className="text-xs font-medium text-lx-secondary">
                    阈值（只影响本次任务）
                  </span>
                  <button
                    type="button"
                    className="lx-btn-ghost px-2 py-1 text-[11px]"
                    disabled={!defaults?.guard}
                    onClick={() => setGuardPolicy({ ...(defaults?.guard ?? {}) })}
                  >
                    恢复配置默认
                  </button>
                </div>

                <label className="mb-3 flex flex-col gap-1.5">
                  <span className="flex items-baseline justify-between gap-2 text-[11px] text-lx-secondary">
                    <span>占空比</span>
                    <span className="font-mono tabular-nums text-lx">
                      {guardPolicy.duty_cycle != null ? guardPolicy.duty_cycle.toFixed(2) : '—'}
                      <span className="ml-1 text-[10px] text-lx-dim">
                        {guardPolicy.duty_cycle != null
                          ? `留 ${Math.round((1 - guardPolicy.duty_cycle) * 100)}% 空闲`
                          : ''}
                      </span>
                    </span>
                  </span>
                  <input
                    type="range"
                    className="lx-slider"
                    min={0.2}
                    max={1}
                    step={0.05}
                    value={guardPolicy.duty_cycle ?? 0.7}
                    onChange={(e) => setKnob('duty_cycle', Number(e.target.value))}
                    aria-label="占空比"
                  />
                  <span className="text-[10px] leading-snug text-lx-dim">
                    1.00 = 全速；0.70 = 每忙 7 份睡 3 份（直接影响耗时）
                  </span>
                </label>

                <div className="grid gap-x-6 gap-y-2.5 sm:grid-cols-2">
                  <GuardKnob
                    label="温度闭环目标"
                    suffix="°C"
                    min={0}
                    max={95}
                    step={1}
                    value={guardPolicy.target_temp_c ?? 0}
                    onChange={(v) => setKnob('target_temp_c', v)}
                    hint="0 = 关，只靠下面的硬线兜底"
                  />
                  <GuardKnob
                    label="警告温度"
                    suffix="°C"
                    min={30}
                    max={100}
                    step={1}
                    value={guardPolicy.temp_warn_c ?? 72}
                    onChange={(v) => setKnob('temp_warn_c', v)}
                    hint="只记警告，不停任务"
                  />
                  <GuardKnob
                    label="熔断温度"
                    suffix="°C"
                    min={40}
                    max={110}
                    step={1}
                    value={guardPolicy.temp_cutoff_c ?? 82}
                    onChange={(v) => setKnob('temp_cutoff_c', v)}
                    hint="到这条线立即停止提交"
                  />
                  <GuardKnob
                    label="恢复温度"
                    suffix="°C"
                    min={30}
                    max={105}
                    step={1}
                    value={guardPolicy.temp_resume_c ?? 70}
                    onChange={(v) => setKnob('temp_resume_c', v)}
                    hint="必须低于熔断温度，否则一熔断就恢复不了"
                  />
                  <GuardKnob
                    label="连续运行"
                    suffix="分钟"
                    min={0}
                    max={1440}
                    step={10}
                    value={guardPolicy.continuous_minutes ?? 0}
                    onChange={(v) => setKnob('continuous_minutes', v)}
                    hint="0 = 不限；到点强制休息一次"
                  />
                  <GuardKnob
                    label="单次休息"
                    suffix="分钟"
                    min={0}
                    max={240}
                    step={1}
                    value={guardPolicy.rest_minutes ?? 5}
                    onChange={(v) => setKnob('rest_minutes', v)}
                    hint="配合上一项，只有连跑上限非 0 时才生效"
                  />
                </div>

                {/* 温度闭环的细调参数。只在闭环目标非 0 时才起作用——
                    目标为 0 表示不闭环，这些系数没有任何输入。 */}
                <details className="mt-3 border-t border-lx pt-3">
                  <summary className="cursor-pointer select-none text-[11px] uppercase tracking-wide text-lx-muted hover:text-lx-secondary">
                    闭环细调（目标非 0 时才生效）
                  </summary>
                  <div className="mt-2.5 grid gap-x-6 gap-y-2.5 sm:grid-cols-2">
                    <GuardKnob
                      label="比例 kp"
                      min={0}
                      max={1}
                      step={0.01}
                      value={guardPolicy.kp ?? 0.04}
                      onChange={(v) => setKnob('kp', v)}
                      hint="压占空比的速度；太大温度会来回震"
                    />
                    <GuardKnob
                      label="积分 ki"
                      min={0}
                      max={0.5}
                      step={0.001}
                      value={guardPolicy.ki ?? 0.004}
                      onChange={(v) => setKnob('ki', v)}
                      hint="消掉稳态误差；太大会超调"
                    />
                    <GuardKnob
                      label="死区"
                      suffix="°C"
                      min={0.5}
                      max={10}
                      step={0.5}
                      value={guardPolicy.temp_deadband_c ?? 2}
                      onChange={(v) => setKnob('temp_deadband_c', v)}
                      hint="目标温度上下这么多度内不调整"
                    />
                    <GuardKnob
                      label="最低占空比"
                      min={0.02}
                      max={1}
                      step={0.02}
                      value={guardPolicy.min_duty ?? 0.2}
                      onChange={(v) => setKnob('min_duty', v)}
                      hint="闭环最多压到这条线，压到 0 等于停跑"
                    />
                    <GuardKnob
                      label="单次让步上限"
                      suffix="秒"
                      min={1}
                      max={120}
                      step={1}
                      value={guardPolicy.max_sleep_s ?? 30}
                      onChange={(v) => setKnob('max_sleep_s', v)}
                      hint="一次最多睡这么久；太大温度会过冲"
                    />
                  </div>
                  <p className="mt-2 text-[10px] leading-snug text-lx-dim">
                    kp / ki 同时为 0 时闭环退化成"只按最低占空比跑"，不再跟随温度。
                  </p>
                </details>
              </div>
            )}
          </SectionPanel>
        </div>

        {/* 右栏跟滚：左栏有十几个面板，走到「运行保护」时页顶的「开始打标」
            早就滚出视野了。sticky 之后，机器/产出/产物/提交一直在手边。 */}
        <aside className="flex min-w-0 flex-col gap-3.5 lg:sticky lg:top-3.5 lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-y-auto">
          <SectionPanel
            title="这台机器"
            trailing={
              <StatusChip tone={gpus.length ? 'success' : 'warning'}>
                {gpus.length ? `${gpus.length} 张卡` : '无显卡'}
              </StatusChip>
            }
          >
            <dl className="space-y-2.5 text-xs">
              <div className="flex items-center justify-between gap-2">
                <dt className="text-lx-muted">系统</dt>
                <dd className="truncate text-right text-lx-secondary">{health?.platform ?? '—'}</dd>
              </div>
              <div className="flex items-center justify-between gap-2">
                <dt className="text-lx-muted">Python</dt>
                <dd className="font-mono text-lx-secondary">{health?.python ?? '—'}</dd>
              </div>
              <div className="flex items-center justify-between gap-2">
                <dt className="flex items-center gap-1 text-lx-muted">
                  <Cpu size={12} /> 显卡
                </dt>
                <dd className="truncate text-right text-[11px] text-lx-secondary">
                  {gpus.length ? (gpus[0].match(/NVIDIA\s+([^,]+?)\s{2,}/)?.[1] ?? `${gpus.length} 张`) : '未检测到'}
                </dd>
              </div>
              {gpus.map((g) => (
                <div
                  key={g}
                  className="lx-sheet-inset px-2.5 py-1.5 font-mono text-[10px] leading-relaxed text-lx-muted"
                >
                  {g}
                </div>
              ))}
            </dl>
          </SectionPanel>

          <SectionPanel title="这次会产出">
            <OverviewFigureRow className="grid-cols-2 border-t-0 pt-0 sm:grid-cols-2">
              <OverviewFigure
                label="待处理"
                value={useHf ? '仓库' : isVlm ? '流式' : preview ? fmtCount(preview.total) : '—'}
                note={useHf ? 'HF 分片' : isVlm ? '边读边打' : '张'}
                first
              />
              <OverviewFigure
                label="预计耗时"
                value={estimateS !== null ? String(estimateS) : '—'}
                note="秒"
              />
            </OverviewFigureRow>
            {!isVlm && preview && (
              <div className="mt-3 space-y-1 text-[11px] text-lx-muted">
                <div className="flex justify-between gap-2">
                  <span>来源</span>
                  <span className="truncate text-lx-secondary">{preview.sources.length} 个</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>素材体积</span>
                  <span className="tabular-nums text-lx-secondary">
                    {(preview.bytes / 1073741824).toFixed(2)} GB
                  </span>
                </div>
                {artist && (
                  <div className="flex justify-between gap-2">
                    <span>画师名</span>
                    <span className="truncate text-lx-secondary">
                      {preview.artists.count} 个 · 覆盖 {fmtCount(preview.artists.covered)} 张
                    </span>
                  </div>
                )}
              </div>
            )}
            <p className="mt-3 flex items-center gap-1.5 text-[11px] leading-relaxed text-lx-muted">
              <Sparkles size={11} className="shrink-0" />
              <span className="min-w-0 truncate">
                {useHf
                  ? `${hfRepo} → ${basename(effectiveOut)}`
                  : inputDir
                    ? `${basename(inputDir)} → ${basename(effectiveOut || '')}`
                    : isVlm
                      ? '先选分片包，或填 HF 团队仓库'
                      : '先选一个目录或 .zip 素材包'}
              </span>
            </p>
          </SectionPanel>

          <SectionPanel title="产物">
            <div className="space-y-1.5 font-mono text-[11px] leading-relaxed text-lx-muted">
              {isVlm ? (
                <>
                  <div className="truncate text-lx-secondary">
                    {artifactFormat === 'parquet' ? `pack-*.parquet（每片 ${parquetRows} 行）` : 'tags/ + json 分片'}
                  </div>
                  <div className="text-lx-dim">
                    {mode === 'both' ? 'NL + tag' : '只出 NL'} · 跑完去「回传」页上传
                  </div>
                </>
              ) : (
                <>
                  <div className="truncate text-lx-secondary">tags/&lt;id&gt;-tag.json</div>
                  <div className="text-lx-dim">逐张 tag / rating / provenance</div>
                  <div className="pt-1.5 text-lx-dim">封包在「数据打包」页</div>
                </>
              )}
            </div>
          </SectionPanel>

          {/* 左下角的提交入口。页顶那个按钮在长表单滚到底以后就看不见了，
              这里再放一个同样的入口，顺带把「还差什么」直接写出来。 */}
          <SectionPanel title="提交">
            <p className="mb-2.5 text-[11px] leading-relaxed text-lx-muted">
              {missingInput
                ? isVlm
                  ? '还差输入：填 HF 团队仓库，或选一个本地分片包。'
                  : '还差输入路径：选一个图片目录或素材 .zip。'
                : '参数都在上面；提交后到「运行中」看进度，跑完去「回传」或「数据打包」。'}
            </p>
            <button
              type="button"
              className="lx-btn-primary lx-launch-glow flex w-full items-center justify-center gap-1.5"
              disabled={missingInput || busy}
              onClick={() => void submit()}
            >
              <Play size={13} />
              {busy ? '创建中…' : '开始打标'}
            </button>
          </SectionPanel>

          {!health?.ui_built && (
            <div className="flex items-start gap-2 rounded-lx-sm border border-lx-warning bg-lx-warning-subtle px-3 py-2 text-[11px] leading-relaxed text-lx-warning">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" />
              <span>前端是开发模式（dist 未构建），部署时记得 npm run build</span>
            </div>
          )}
        </aside>
      </div>

      <FolderPicker
        open={picker}
        start={inputDir || defaults?.models_dir}
        onClose={() => setPicker(false)}
        onPick={(path) => {
          setInputDir(path)
          setPicker(false)
        }}
      />

      <ShardAssets
        open={assetsOpen}
        startDir={inputDir}
        onClose={() => setAssetsOpen(false)}
        onPick={(path) => {
          setInputDir(path)
          setAssetsOpen(false)
        }}
      />

      <QuantizeDialog
        open={quantOpen}
        modelKey={vllmModelKey}
        onCancel={() => {
          setQuantOpen(false)
          // 取消 = 回默认精度。留在 W4A16 会得到一个"看着是 4bit、实际
          // 16bit"的状态，比直接退回去更误导。
          setVllmPrecision('bf16')
        }}
        onUse={() => {
          setQuantOpen(false)
          setVllmPrecision('w4a16')
          setPlanTick((n) => n + 1)
        }}
      />
    </div>
  )
}
