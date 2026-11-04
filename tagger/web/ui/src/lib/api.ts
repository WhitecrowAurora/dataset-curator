// 后端契约的唯一定义处。字段名与 tagger/web/app.py 一一对应，
// 改名要两边一起改——所以这里刻意不写"更漂亮"的别名。

export type JobSpec = {
  input_dir: string
  out_dir: string
  recursive: boolean
  include_zips: boolean
  include_loose: boolean
  limit: number
  force: boolean
  // 引擎。local = 本地 tag 后端；vlm = vLLM 出 NL（tag 可由本地后端并行出）
  engine: 'local' | 'vlm'
  // 打标
  backend: string
  model_dir: string
  device: string
  precision: string
  batch: number
  threshold: number | null
  keep_categories: string[]
  thresholds: Record<string, number>
  model_repo: string
  guard: boolean
  /** 阈值覆盖：键名与后端的 GuardPolicy 字段同名（duty_cycle / temp_cutoff_c 等）。
   *  空 = 用 configs/tiers.yaml 的 limits 段 */
  guard_policy: Record<string, number>
  /** 按"预计产出 + 20% 冗余"判盘够不够（取代 guard 的固定磁盘阈值） */
  check_space: boolean
  // VLM 服务（engine="vlm"）
  url: string
  vlm_model: string
  /** both = NL + tag；nl / tag = 单模式，都由 VLM 出（本地后端会被强制关掉） */
  output_mode: 'nl' | 'tag' | 'both'
  temperature: number
  top_p: number
  max_tokens: number
  timeout_s: number
  concurrency: number
  max_pixels: number
  seed: number | null
  /** 同一张图跨运行累计失败多少次后跳过；0 = 不限制 */
  max_failures: number
  /** 质检判定线；0 = 用配置（pass 3.4 / drop 1.8） */
  quality_pass_score: number
  quality_drop_score: number
  /** 重打次数上限；0 = 用配置（2 次） */
  quality_max_attempts: number
  /** 重试阶梯：温度每轮上调多少 / 封顶；0 = 用配置 */
  quality_retry_temp_step: number
  quality_retry_temp_cap: number
  /** 重试阶梯：像素预算乘几倍 / 封顶；0 = 用配置 */
  quality_retry_pixels_scale: number
  quality_retry_pixels_cap: number
  /** L2 语义打分（图文 CLIP）：抓"句子通顺但跑题"，代价是并发被摁到 1 */
  quality_enable_clip: boolean
  /** CLIP 相似度下限；0 = 用配置（0.22） */
  quality_clip_min_score: number
  /** CLIP 权重名（空 = 跟配置 ViT-L-14）；换它等于换打分口径 */
  quality_clip_model: string
  /** CLIP 预训练来源（空 = 跟配置 openai） */
  quality_clip_pretrained: string
  /** L1 规则层阈值；null = 跟配置。0 是合法值，不能当"没填" */
  quality_min_chars: number | null
  quality_max_chars: number | null
  quality_min_tag_coverage: number | null
  quality_max_repeat_ratio: number | null
  /** 重试升级：重试用尽后打到第二个服务（另一套更大的权重） */
  escalate_url: string
  escalate_model: string
  /** 用 file:// 传图：服务端须 --allowed-local-media-path 放行产物目录 */
  use_file_url: boolean
  // HF 输入（engine="vlm"）
  hf_repo: string
  hf_token: string
  hf_revision: string
  hf_files: string[]
  // 产物形态（vlm 引擎默认 parquet：回传页传的就是它）
  artifact_format: 'parquet' | 'json'
  parquet_rows: number
  // 命名
  artist: boolean
  artist_depth: number
  /** 画师名写进 .tag/.nl 文本开头；false = 只留在 JSON 字段 */
  artist_inject: boolean
  layout: 'flat' | 'mirror'
  seq_width: number
  // 封包
  pack: boolean
  formats: string[]
  jpeg_quality: number
  jpeg_subsampling: number
  reencode: 'always' | 'non_jpeg' | 'never'
  compress: 'stored' | 'deflate'
  max_records: number
  max_bytes: number
}

export type JobStatus = 'pending' | 'running' | 'done' | 'stopped' | 'failed'
export type JobPhase = '' | 'tag' | 'pack'

export type PlanSample = {
  rel: string
  name: string
  image_id: string
  artist: string
}

export type PlanPreview = {
  path: string
  layout: 'flat' | 'mirror'
  total: number
  bytes: number
  sources: { source: string; name: string; kind: string; count: number }[]
  artists: { count: number; covered: number; missing: number }
  samples: PlanSample[]
  id_prefix: string
  warnings: string[]
  /** 预计要写的字节 + 目标盘剩余。没传 formats 时没有这一项。 */
  estimate?: {
    bytes: number
    free: number
    ok: boolean
    headroom: number
  }
}

export type PackRow = {
  name: string
  count: number
  bytes: number
  sha256: string
}

export type PackProgress = {
  enabled: boolean
  total: number
  packed: number
  skipped: number
  missing: number
  failed: number
  settled: number
  bytes: number
  rate_per_s: number
  formats: string[]
  layout: string
  quality: number
  subsampling: number
  packs: PackRow[]
}

export type Snapshot = {
  id: string
  status: JobStatus
  phase: JobPhase
  error: string
  spec: JobSpec
  out_dir: string
  counts: {
    total: number
    done: number
    skipped: number
    failed: number
    blacklisted: number
    settled: number
  }
  /** null = 总量未知的流式任务（HF parquet），前端按计数显示不造百分比 */
  progress: number | null
  elapsed_s: number
  rate_per_s: number
  eta_s: number | null
  timings_ms: { decode: number; forward: number }
  by_reason: Record<string, number>
  pack: PackProgress
  plan: PlanPreview | null
  current: string
  seq: number
  created: number
  started: number
  ended: number
}

export type JobEvent = {
  seq: number
  ts: number
  kind:
    | 'job_started'
    | 'item'
    | 'pack_item'
    | 'pack_sealed'
    | 'phase'
    | 'log'
    | 'guard'
    | 'job_finished'
    | string
  image_id?: string
  ok?: boolean
  skipped?: boolean
  missing?: boolean
  tags?: string[]
  tag_count?: number
  /** 质检分（1~5，仅 VLM 引擎且产出 NL 的 done 事件带） */
  score?: number
  error?: string
  message?: string
  status?: JobStatus
  phase?: JobPhase
  total?: number
  name?: string
  count?: number
  bytes?: number
  level?: string
}

export type ResultItem = {
  image_id: string
  rel: string
  name: string
  artist: string
  in_zip: boolean
  ok: boolean
  tag: string
  tags: [string, number][]
  rating: Record<string, number>
  tag_backend: string
  ts?: number
}

export type ResultsPage = {
  total: number
  offset: number
  limit: number
  items: ResultItem[]
}

// ---- 统计页（完成顺序分桶的质检趋势）

export type JobStatsBucket = {
  start: number
  /** 按真实结算数收口（尾桶常常不满） */
  end: number
  settled: number
  done_first: number
  done_retry: number
  skipped: number
  blacklisted: number
  failed: number
  /** 质检分累计（已除回原值），配合 score_n 求平均分 */
  score_sum: number
  /** 1.0~5.0 每 0.1 一格，尾部全零格已被后端截掉 */
  score_hist: number[]
  score_n: number
}

export type JobStats = {
  id: string
  status: JobStatus
  total: number
  settled: number
  bucket: number
  buckets: JobStatsBucket[]
  totals: JobStatsBucket
  /** true = 统计功能上线前的旧任务：合成单桶，没有真正的分桶趋势 */
  fallback: boolean
}

export type Defaults = {
  backend: string
  model_dir: string
  models_dir: string
  device: string
  precision: string
  batch: number
  threshold: number
  keep_categories: string[]
  /** 各 tag 后端在配置里绑定的 HF 仓库（按后端分组） */
  tag_models: Record<string, string>
  /** VLM 可选权重，来自 tiers.yaml 的 models 段 */
  vllm_models: VllmModelOption[]
  /** VLM 分档，来自 tiers.yaml 的 tiers 段 */
  vllm_tiers: VllmTierOption[]
  /** 硬件保护的当前阈值（来自 tiers.yaml 的 limits 段），滑条初值 */
  guard: Record<string, number>
  /** 质检的配置默认值。页面上的"0 = 用配置的 3.4"里那个数从这里来 */
  quality: QualityDefaults
  pack: boolean
  formats: string[]
  layout: 'flat' | 'mirror'
  jpeg_quality: number
  jpeg_subsampling: number
  reencode: 'always' | 'non_jpeg' | 'never'
  compress: 'stored' | 'deflate'
  max_records: number
  max_bytes: number
  artist: boolean
  artist_depth: number
  artist_inject: boolean
  artist_prefix: string
  seq_width: number
}

/** 质检的配置默认值（来自 tiers.yaml 的 quality 段）。 */
export type QualityDefaults = {
  pass_score: number
  drop_score: number
  max_attempts: number
  retry_temp_step: number
  retry_temp_cap: number
  retry_pixels_scale: number
  retry_pixels_cap: number
  enable_clip: boolean
  clip_min_score: number
  clip_model: string
  clip_pretrained: string
  min_chars: number
  max_chars: number
  min_tag_coverage: number
  max_repeat_ratio: number
}

export type VllmModelOption = {
  key: string
  path: string
  precision: string
  family: string
  /** 分档表里默认用它的档位名（仅提示，实际落档由服务端解析） */
  tier: string
  /** 模型管理页填的备注 */
  note: string
}

// ---- 模型管理（左栏独立页）

/** 权重下载任务的状态（后端后台线程，前端轮询 /api/models 拿） */
export type DownloadState = {
  kind: string
  key: string
  repo: string
  dest: string
  /** queued / running / done / error / canceled */
  status: string
  message: string
  /** 正在下的文件名 */
  current: string
  files_total: number
  files_done: number
  bytes_total: number
  bytes_done: number
  percent: number
  /** 字节/秒，0.5s 窗口估的 */
  speed: number
  elapsed: number
  eta: number | null
}

type LocalFiles = {
  /** 下载目录（相对仓库根显示，绝对路径在 local_abs） */
  local_dir: string
  local_abs: string
  exists: boolean
  /** 有文件但没下完（取消 / 报错过）；此时点下载会按文件续传 */
  partial: boolean
  local_bytes: number
  /** 能下载的仓库名；空 = 这条没有可下载来源（path 已经是本地目录） */
  download_repo: string
  download: DownloadState | null
}

export type VlmModelEntry = {
  key: string
  path: string
  precision: string
  family: string
  revision: string | null
  quantized_path: string | null
  note: string
  /** config = 来自 tiers.yaml；managed = 在这个页面加/改的 */
  source: 'config' | 'managed'
  /** 哪些分档在用；非空就删不掉 */
  tiers: string[]
} & LocalFiles

export type TagModelEntry = {
  backend: string
  repo: string
  note: string
  source: 'config' | 'managed'
} & LocalFiles

export type ModelCatalog = {
  /** 当前生效的清单（配置 + 覆盖） */
  vlm: VlmModelEntry[]
  tag: TagModelEntry[]
  /** 配置侧的原始条目：用来显示"配置里有、现在被隐藏了" */
  base_vlm: VlmModelEntry[]
  base_tag: TagModelEntry[]
  /** 覆盖清单落在哪个文件 */
  overlay: string
  overlay_exists: boolean
  /** 下载代理，空 = 直连 */
  proxy: string
  /** 装了 socksio 才支持 socks5:// */
  socks_supported: boolean
}

export type ProxyTest = {
  ok: boolean
  via: string
  endpoint?: string
  latency_ms: number
  error?: string
}

export type VllmTierOption = {
  name: string
  model: string
  vram_min: number
  vram_max: number
  max_model_len: number
  max_num_seqs: number
  note: string
}

export type Health = {
  ok: boolean
  version: string
  python: string
  platform: string
  gpus: string[]
  ui_built: boolean
  running_job: string | null
}

export type FsListing = {
  path: string
  parent: string
  images: number
  zips: { name: string; path: string; bytes: number }[]
  dirs: { name: string; path: string; images: number; zips: number }[]
}

// ---- vLLM 服务（NL 模式的前置）

export type VllmState = {
  /** stopped | starting | ready | stopping | failed */
  status: 'stopped' | 'starting' | 'ready' | 'stopping' | 'failed'
  error: string
  port: number
  url: string
  pid: number | null
  started: number
  ready_at: number
  /** 分档摘要（tier/device/model…），启动后才有 */
  profile: string
  log_path: string
  log_tail: string[]
  /** 环境指纹；null = 盘上没有 run/fingerprint.json，provenance 会残 */
  fingerprint: Record<string, unknown> | null
  fingerprint_path: string
  /** 本机能否拉起 vLLM（Windows 原生 / 无卡 / 缺 vllm 都会 false） */
  local_ok: boolean
  local_reason: string
  /** 启动时传的批调度参数；null = 用的分档表默认值 */
  overrides: {
    /** 选中的权重 key；null = 按显存落档 */
    model: string | null
    /** 强制分档名；null = 按显存匹配 */
    tier: string | null
    /** 权重精度 bf16 / fp8 / w4a16；null = 用分档表的值 */
    precision: string | null
    /** 接口路由名（--served-model-name） */
    served_name: string | null
    max_num_seqs: number | null
    max_num_batched_tokens: number | null
    gpu_mem: number | null
    /** --allowed-local-media-path 的放行根目录；null = 不接受 file:// */
    local_media_root: string | null
  }
  /** 当前这组参数解析出的分档 / 权重 / 显存；ok=false 时 error 给原因 */
  plan: VllmPlan
}

export type VllmPlan =
  | {
      ok: true
      tier: string
      model: string
      model_path: string
      precision: string
      /** 实际传给 vLLM 的 --quantization；null = 按权重 dtype 加载（可能是回退） */
      quantization: string | null
      /** 权重本身是否真被压成 4bit（false = W4A16 回退 BF16） */
      weight_quantized: boolean
      max_pixels: number
      max_model_len: number
      max_num_seqs: number
      max_num_batched_tokens: number
      gpu_memory_utilization: number
      vram_gb: number
      device: string
      warnings: string[]
      summary: string
    }
  | { ok: false; error: string }

// ---- 页内量化（W4A16）：把 bf16 权重压成 4bit，产物落在 models/ 下

export type QuantTaskState = {
  model: string
  source: string
  out_dir: string
  /** gptq / awq；产物格式都是 compressed-tensors */
  method: string
  status: 'running' | 'done' | 'error' | 'canceled'
  percent: number
  stage: string
  message: string
  elapsed: number
  log_tail: string[]
}

export type QuantState = {
  /** 本机能否量化：依赖是否齐、是否 Linux/WSL2 */
  available: boolean
  /** 不能量化时的人话原因 */
  reason: string
  model: string
  source: string
  quantized_path: string | null
  /** 已经有可用的 4bit 权重：切 W4A16 不需要再量化 */
  has_quantized: boolean
  /** 默认产物目录（相对仓库根） */
  suggested_dir: string
  methods: string[]
  task: QuantTaskState | null
}

export type VllmProbe = {
  url: string
  ok: boolean
  version: string
  models: string[]
  error: string
}

// ---- 回传页（FTP 上传）

export type ArtifactInfo = {
  name: string
  bytes: number
  mtime: number
  /** null = 读不动 footer（损坏/pyarrow 缺失），页面显示 "-" */
  rows: number | null
}

export type FtpConfig = {
  host: string
  port: number
  user: string
  password: string
  remote_dir: string
  passive: boolean
}

export type UploadFileState = {
  name: string
  bytes: number
  rows: number | null
  status: 'pending' | 'uploading' | 'done' | 'failed' | 'stopped'
  sent: number
  resumed: boolean
  attempts: number
  error: string
}

export type UploadState = {
  id: string
  dir: string
  status: 'pending' | 'running' | 'done' | 'stopped' | 'failed'
  error: string
  created: number
  started: number
  ended: number
  /** 服务端刻意不给 user/password：轮询方只需要知道连的是哪台机器 */
  ftp: { host: string; port: number; remote_dir: string; passive: boolean }
  files: UploadFileState[]
  total: number
  done: number
  failed: number
  sent_bytes: number
}

export type DedupGroupPreview = {
  kind: 'exact' | 'near'
  keep: { rel_path: string; artist: string }
  duplicates: { rel_path: string; artist: string; distance: number }[]
  more: number
}

export type DedupSummary = {
  input_dir: string
  out_dir: string
  params: { mode: string; threshold: number; keep: string; recursive: boolean }
  discovered: number
  hashed: number
  cached: number
  errors: number
  dropped_stale: number
  groups_exact: number
  groups_near: number
  duplicates: number
  wasted_bytes: number
  exclude_file: string
  report_file: string
  preview: DedupGroupPreview[]
  stopped: boolean
  elapsed_s: number
}

export type DedupState = {
  id: string
  input_dir: string
  out_dir: string
  params: { mode: string; threshold: number; keep: string; recursive: boolean; workers: number }
  status: 'pending' | 'running' | 'done' | 'stopped' | 'failed'
  error: string
  created: number
  started: number
  ended: number
  scanned: number
  total: number
  current: string
  /** 张/秒，按最近约 12 秒的进度估；0 = 还没动或不适用 */
  rate: number
  eta_s: number
  logs: string[]
  summary: DedupSummary | null
}

// ---- 数据打包页（分包 / 拼接 / 封包）

export type DataJobKind = 'split' | 'merge' | 'pack'

export type SplitSummary = {
  discovered: number
  excluded: number
  no_artist: number
  packages: { name: string; count: number; bytes: number; first_id: string; last_id: string }[]
  package_count: number
  total: number
  bytes: number
  out_dir: string
  table_path: string
  verify: { checked: number; bad: number } | null
}

export type MergeSummary = {
  expected: number
  found: number
  complete: number
  missing_n: number
  missing_nl_n: number
  missing_tag_n: number
  empty_content_n: number
  duplicates_n: number
  duplicates: Record<string, string[]>
  idempotent_n: number
  unreadable_n: number
  unreadable: string[]
  extra_n: number
  unassigned_n: number
  empty_roots: string[]
  whole_missing: { name: string; first_id: string; last_id: string; expected: number }[]
  short: { name: string; got: number; expected: number; short: number }[]
  prov_warnings: string[]
  failed: boolean
  summary_lines: string[]
  merged_path: string
  written: number
  sample_path: string
  sample_count: number
}

export type PackSummary = {
  packed: number
  skipped_done: number
  skipped_missing: number
  failed: number
  sealed: { name: string; count: number; bytes: number }[]
  sealed_count: number
  bytes: number
  out_dir: string
  formats: string[]
  total_discovered: number
}

export type DataJobState = {
  id: string
  kind: DataJobKind
  params: Record<string, unknown>
  out_dir: string
  status: 'pending' | 'running' | 'done' | 'stopped' | 'failed'
  error: string
  created: number
  started: number
  ended: number
  done: number
  /** 0 = 总量未知的计数式进度（拼接按已扫条数走） */
  total: number
  current: string
  /** 条/秒（split 是包/秒），按最近约 12 秒的进度估 */
  rate: number
  eta_s: number
  logs: string[]
  summary: SplitSummary | MergeSummary | PackSummary | null
}

export type SplitPreview = {
  total: number
  bytes: number
  excluded: number
  artists: { count: number; covered: number; missing: number }
  sources: { name: string; count: number }[]
}

export type PreviewParams = {
  path: string
  layout?: string
  artist?: boolean
  artist_depth?: number
  recursive?: boolean
  include_zips?: boolean
  include_loose?: boolean
  limit?: number
  seq_width?: number
  /** false = 只打标：id 沿用相对路径，与封包任务的全局序号不同 */
  pack?: boolean
  /** 产物目录：用来算目标盘剩余空间 */
  out_dir?: string
  /** 逗号分隔的格式（parquet,webdataset,zip）；不传就只估算打标产物 */
  formats?: string
  jpeg_quality?: number
  jpeg_subsampling?: number
  reencode?: string
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!resp.ok) {
    let message = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      // FastAPI 的 detail 可能是字符串，也可能是 pydantic 的数组
      message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch {
      // 非 JSON 错误体，就用状态码
    }
    throw new ApiError(resp.status, message)
  }
  return (await resp.json()) as T
}

export const api = {
  health: () => json<Health>('/api/health'),
  defaults: () => json<Defaults>('/api/defaults'),
  // ---- 模型管理
  models: () => json<ModelCatalog>('/api/models'),
  saveModels: (payload: { vlm: VlmModelEntry[]; tag: TagModelEntry[] }) =>
    json<ModelCatalog>('/api/models', { method: 'PUT', body: JSON.stringify(payload) }),
  saveProxy: (url: string) =>
    json<{ proxy: string; catalog: ModelCatalog }>('/api/models/proxy', {
      method: 'PUT',
      body: JSON.stringify({ url }),
    }),
  testProxy: (url: string) =>
    json<ProxyTest>('/api/models/proxy/test', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
  startDownload: (kind: string, key: string) =>
    json<DownloadState>('/api/models/download', {
      method: 'POST',
      body: JSON.stringify({ kind, key }),
    }),
  cancelDownload: (kind: string, key: string) =>
    json<DownloadState>('/api/models/download/cancel', {
      method: 'POST',
      body: JSON.stringify({ kind, key }),
    }),
  deleteLocalFiles: (kind: string, key: string) =>
    json<ModelCatalog>('/api/models/localfiles/delete', {
      method: 'POST',
      body: JSON.stringify({ kind, key }),
    }),
  revealLocalDir: (kind: string, key: string) =>
    json<{ path: string }>('/api/models/reveal', {
      method: 'POST',
      body: JSON.stringify({ kind, key }),
    }),
  fs: (path: string) => json<FsListing>(`/api/fs?path=${encodeURIComponent(path)}`),
  preview: (params: PreviewParams) => {
    const qs = new URLSearchParams()
    qs.set('path', params.path)
    if (params.layout) qs.set('layout', params.layout)
    if (params.artist !== undefined) qs.set('artist', String(params.artist))
    if (params.artist_depth !== undefined) qs.set('artist_depth', String(params.artist_depth))
    if (params.recursive !== undefined) qs.set('recursive', String(params.recursive))
    if (params.include_zips !== undefined) qs.set('include_zips', String(params.include_zips))
    if (params.include_loose !== undefined) qs.set('include_loose', String(params.include_loose))
    if (params.limit) qs.set('limit', String(params.limit))
    if (params.seq_width) qs.set('seq_width', String(params.seq_width))
    if (params.pack !== undefined) qs.set('pack', String(params.pack))
    if (params.out_dir) qs.set('out_dir', params.out_dir)
    if (params.formats) qs.set('formats', params.formats)
    if (params.jpeg_quality) qs.set('jpeg_quality', String(params.jpeg_quality))
    if (params.jpeg_subsampling !== undefined) {
      qs.set('jpeg_subsampling', String(params.jpeg_subsampling))
    }
    if (params.reencode) qs.set('reencode', params.reencode)
    return json<PlanPreview>(`/api/preview?${qs.toString()}`)
  },
  createJob: (spec: Partial<JobSpec>) =>
    json<Snapshot>('/api/jobs', { method: 'POST', body: JSON.stringify(spec) }),
  listJobs: () => json<{ jobs: Snapshot[] }>('/api/jobs'),
  job: (id: string) => json<Snapshot>(`/api/jobs/${id}`),
  events: (id: string, after: number) =>
    json<{ events: JobEvent[]; snapshot: Snapshot }>(`/api/jobs/${id}/events?after=${after}`),
  stop: (id: string) => json<{ ok: boolean }>(`/api/jobs/${id}/stop`, { method: 'POST' }),
  jobStats: (id: string, bucket: number) =>
    json<JobStats>(`/api/jobs/${id}/stats?bucket=${bucket}`),
  // ---- vLLM 服务（NL 模式的前置）
  vllm: () => json<VllmState>('/api/vllm'),
  vllmStart: (
    port: number,
    timeout_s = 900,
    opts: {
      /** vllm | llamacpp。Windows 原生只有 llamacpp 能上 GPU。 */
      engine?: string
      model?: string
      tier?: string
      precision?: string
      served_name?: string
      max_num_seqs?: number
      max_num_batched_tokens?: number
      gpu_mem?: number
      local_media_root?: string
    } = {},
  ) =>
    json<VllmState>('/api/vllm/start', {
      method: 'POST',
      body: JSON.stringify({ port, timeout_s, ...opts }),
    }),
  /** 某个引擎在这台机器上能不能起。切换引擎时问一次即可。 */
  vllmCapability: (engine: string) =>
    json<{ engine: string; ok: boolean; reason: string }>(
      `/api/vllm/capability?engine=${encodeURIComponent(engine)}`,
    ),
  vllmPlan: (
    opts: {
      model?: string
      tier?: string
      precision?: string
      max_num_seqs?: number
      max_num_batched_tokens?: number
      gpu_mem?: number
    } = {},
  ) => {
    const qs = new URLSearchParams()
    if (opts.model) qs.set('model', opts.model)
    if (opts.tier) qs.set('tier', opts.tier)
    if (opts.precision) qs.set('precision', opts.precision)
    if (opts.max_num_seqs) qs.set('max_num_seqs', String(opts.max_num_seqs))
    if (opts.max_num_batched_tokens)
      qs.set('max_num_batched_tokens', String(opts.max_num_batched_tokens))
    if (opts.gpu_mem) qs.set('gpu_mem', String(opts.gpu_mem))
    return json<VllmPlan>(`/api/vllm/plan?${qs.toString()}`)
  },
  vllmStop: () => json<VllmState>('/api/vllm/stop', { method: 'POST' }),
  // ---- 页内量化（W4A16）
  quantize: (model?: string) =>
    json<QuantState>(
      `/api/vllm/quantize${model ? `?model=${encodeURIComponent(model)}` : ''}`,
    ),
  quantizeStart: (opts: { model?: string; out_dir?: string; method?: string }) =>
    json<QuantTaskState>('/api/vllm/quantize/start', {
      method: 'POST',
      body: JSON.stringify(opts),
    }),
  quantizeCancel: () =>
    json<QuantTaskState | Record<string, never>>('/api/vllm/quantize/cancel', {
      method: 'POST',
    }),
  vllmProbe: (url: string) => json<VllmProbe>(`/api/vllm/probe?url=${encodeURIComponent(url)}`),
  results: (id: string, params: { offset?: number; limit?: number; q?: string; only_failed?: boolean }) => {
    const qs = new URLSearchParams()
    if (params.offset) qs.set('offset', String(params.offset))
    if (params.limit) qs.set('limit', String(params.limit))
    if (params.q) qs.set('q', params.q)
    if (params.only_failed) qs.set('only_failed', 'true')
    return json<ResultsPage>(`/api/jobs/${id}/results?${qs.toString()}`)
  },
  thumbUrl: (id: string, imageId: string, w = 420) =>
    `/api/jobs/${id}/thumb/${encodeURIComponent(imageId).replace(/%2F/gi, '/')}?w=${w}`,
  // ---- 回传页
  artifacts: (path: string) =>
    json<{ path: string; files: ArtifactInfo[] }>(
      `/api/artifacts?path=${encodeURIComponent(path)}`,
    ),
  hfFiles: (repo: string, token: string, revision = 'main') => {
    const qs = new URLSearchParams({ repo, revision })
    if (token) qs.set('token', token)
    return json<{ repo: string; revision: string; files: string[] }>(
      `/api/hf/files?${qs.toString()}`,
    )
  },
  uploadStart: (dir: string, files: string[], ftp: FtpConfig) =>
    json<UploadState>('/api/upload/start', {
      method: 'POST',
      body: JSON.stringify({ dir, files, ftp }),
    }),
  upload: (id: string) => json<UploadState>(`/api/upload/${id}`),
  uploadStop: (id: string) =>
    json<{ ok: boolean; snapshot: UploadState | null }>(`/api/upload/${id}/stop`, {
      method: 'POST',
    }),
  // ---- 去重页
  dedupStart: (spec: {    input_dir: string
    out_dir?: string
    mode?: string
    threshold?: number
    keep?: string
    recursive?: boolean
    workers?: number
  }) =>
    json<DedupState>('/api/dedup/start', {
      method: 'POST',
      body: JSON.stringify(spec),
    }),
  dedup: (id: string) => json<DedupState>(`/api/dedup/${id}`),
  dedupStop: (id: string) =>
    json<{ ok: boolean; snapshot: DedupState | null }>(`/api/dedup/${id}/stop`, {
      method: 'POST',
    }),
  // ---- 数据打包页
  dataJobStart: (kind: DataJobKind, params: Record<string, unknown>) =>
    json<DataJobState>('/api/datajob/start', {
      method: 'POST',
      body: JSON.stringify({ kind, params }),
    }),
  dataJobs: () => json<{ jobs: DataJobState[] }>('/api/datajob'),
  dataJob: (id: string) => json<DataJobState>(`/api/datajob/${id}`),
  dataJobStop: (id: string) =>
    json<{ ok: boolean; snapshot: DataJobState | null }>(`/api/datajob/${id}/stop`, {
      method: 'POST',
    }),
  dataJobPreview: (input_dir: string, exclude = '', recursive = true) =>
    json<SplitPreview>('/api/datajob/preview', {
      method: 'POST',
      body: JSON.stringify({ kind: 'split', input_dir, exclude, recursive }),
    }),
}
