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
  // HF 输入（engine="vlm"）
  hf_repo: string
  hf_token: string
  hf_revision: string
  hf_files: string[]
  /** auto = 有 parquet 就流式读，否则按 tar 分片落盘 */
  hf_format: 'auto' | 'parquet' | 'webdataset'
  /** tar 模式预取几片（0 = 严格串行） */
  hf_prefetch: number
  /** 单片并发连接数（服务端不支持 Range 时自动退回单连接） */
  hf_conns: number
  /** 空 = 用全局代理 */
  hf_proxy: string
  /** 给产物留的磁盘余量（GB），0 = 自动 */
  hf_reserve_gb: number
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

export type VllmModelOption = {
  key: string
  path: string
  precision: string
  family: string
  /** 分档表里默认用它的档位名（仅提示，实际落档由服务端解析） */
  tier: string
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
    /** 接口路由名（--served-model-name） */
    served_name: string | null
    max_num_seqs: number | null
    max_num_batched_tokens: number | null
    gpu_mem: number | null
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

/** 仓库分片清单。sizes 是按文件名索引的字节数（tree 接口给的）。 */
export interface HfFilesResult {
  repo: string
  revision: string
  /** 后端探测/确认的形态：parquet = 逐行流式，webdataset = tar 分片落盘 */
  format: 'parquet' | 'webdataset' | string
  files: string[]
  sizes: Record<string, number>
  total_bytes: number
}

export type DownloadStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'cancelled'
  | 'done'
  | 'error'

export interface DownloadItem {
  id: string
  repo: string
  filename: string
  revision: string
  conns: number
  proxy: string
  size: number
  done: number
  status: DownloadStatus
  message: string
  created: number
  started: number
  finished: number
  speed: number
}

export interface DownloadsState {
  items: DownloadItem[]
  cache_root: string
  free_bytes: number
  reserve_bytes: number
  running: number
  queued: number
  /** 全局代理（与打标任务的 HF 拉取共用一份） */
  proxy: string
}

export type TagFileRow = {
  path: string
  name: string
  dir: string
  size: number
  /** 空文件多半是下载/写入被截断，页面上要标出来 */
  empty: boolean
  /** tag / nl 是本项目的逐图产物；other 是任意 json（数组/JSONL/单对象） */
  /** parquet = 产物分片（artifacts-*.parquet），不是逐图 json */
  kind: 'tag' | 'nl' | 'parquet' | 'other'
}

export type TagScanResult = {
  root: string
  files: TagFileRow[]
  counts: {
    tag: number
    nl: number
    parquet: number
    other: number
    empty: number
    bytes: number
  }
}

export type TagMergeReport = {
  inputs: number
  bad_files: string[]
  records_read: number
  records_out: number
  duplicates: number
  conflicts: number
  skipped: number
  fields: string[]
  out_path: string
  /** 本次用的图片路径前缀（空 = 保留产物原值） */
  image_root: string
  /** 走了分桶模式：输出按桶序，不再全局按 image_id 排序 */
  batched: boolean
  buckets: number
  bytes: number
  elapsed_s: number
}

export type TagCheckResult = {
  total: number
  ok: number
  missing: number
  /** hf:// / http:// 之类：本来就不在这台机器上，不算缺图 */
  remote: number
  /** 没有 image_path 字段 */
  empty: number
  samples: { image_id: string; image_path: string }[]
}

export type TagMergeTask = {
  id: string
  /** merge = 合并；check = 路径自检（共用同一套轮询） */
  kind: 'merge' | 'check'
  status: 'running' | 'done' | 'error'
  /** read = 读入分批，write = 写出单体文件；自检是 read / check */
  stage: 'read' | 'write' | 'check'
  done: number
  total: number
  error: string
  /** 合并给 TagMergeReport；自检给 TagCheckResult（同一个 report 字段） */
  report: TagMergeReport | TagCheckResult | null
  started: number
  finished: number
  elapsed_s: number
}

export type TagPreview = {
  rows: Record<string, unknown>[]
  fields: string[]
}

/**
 * 底部进度条的一项。后端把所有"正在跑"的活儿归一化成这一种形态：
 * 打标 / 分包 / 拼接 / 封包 / 合并 / 自检。
 */

export type Activity = {
  kind: 'tag' | 'data' | 'merge' | 'check'
  id: string
  title: string
  /** 副标题：打标是输入目录，数据任务是当前阶段，合并/自检是 stage */
  detail: string
  phase: string
  done: number
  total: number
  /** 后端算好的比例 0~1；null = 总量未知，按计数显示 */
  progress: number | null
  rate: number
  eta_s: number | null
  /** 点这条进度去哪个页面 */
  href: string
}

/** 底栏右侧的硬件数字。null = 这台机器拿不到该项。 */

export type SysInfo = {
  cpu_pct: number | null
  /** 物理内存：底栏放百分比，悬浮提示里给 GB 明细 */
  mem_pct: number | null
  mem_used_gb: number | null
  mem_total_gb: number | null
  gpu_pct: number | null
  gpu_temp_c: number | null
  /** 显存（多卡时只报第一张） */
  vram_pct: number | null
  vram_used_gb: number | null
  vram_total_gb: number | null
}

export type PlanState = {
  plan: {
    enabled: boolean
    after_images: number
    action: 'stop' | 'shutdown'
    fired: boolean
    fired_at: number
    note: string
  }
  running: boolean
  done: number
  total: number
  /** 还差多少张触发；未启用时是 0 */
  remaining: number
}

export type ResumeInfo = {
  job_id: string
  /** paused = 按了暂停；stopped = 按了停止，两者都可以接着做 */
  why: 'paused' | 'stopped' | string
  saved_at: number
  out_dir: string
  counts: { total?: number; done?: number; skipped?: number; failed?: number }
  engine: string
  input_dir: string
  vlm_model: string
  output_mode: string
}

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

export type VlmProviderOption = {
  /** 'local' | 'openai' | 'anthropic' */
  value: string
  label: string
  /** 该来源的默认地址；本机为空 */
  base: string
  /** 密钥没填时去哪个环境变量找（仅提示） */
  env_key: string
  requires_key: boolean
  /** 建议的在途请求数（云端按 RPM 限流，与本机显存无关） */
  concurrency: number
  /** 建议的输入像素预算（云端看不到更多细节时，多给的像素只是多付钱） */
  max_pixels: number
  /** 模型名建议，只是个 datalist */
  models: string[]
  /** 记住过的密钥（脱敏）。null = 没有存档，明文永远不下发 */
  saved: { base: string; key_masked: string } | null
}

/** 一次 API 探测的结果。失败也带 error，不抛异常。 */

export type NlApiProbe = {
  ok: boolean
  provider: string
  base: string
  model: string
  latency_ms: number
  prompt_tokens: number
  completion_tokens: number
  text: string
  error: string
  status?: number
}

/**
 * llama.cpp 运行时状态。默认引擎是 llama.cpp，而 llama-server 不是 pip 包，
 * 所以页面要能自己把那个 zip 下下来。
 */

export type LlamaRuntime = {
  installed: boolean
  binary: string
  version: string
  install_root: string
  busy: boolean
  /** probing | downloading | extracting | verifying | done | error | cancelled */
  stage: string
  message: string
  done: number
  total: number
  error: string
  source: string
  /** 当前平台有没有官方预编译包（macOS 没有，只能自己编译） */
  supported: boolean
  platform: string
}

export type MultiGpuWorker = {
  index: number
  name: string
  vram_gb: number
  port: number
  url: string
  status: 'stopped' | 'starting' | 'ready' | 'failed'
  error: string
  pid: number | null
  started: number
  ready_at: number
  log_path: string
  /** 本轮任务里这张卡处理了多少张（打标侧回调累加，不是进程自报） */
  processed: number
  failed: number
  last_ts: number
  /** nvidia-smi 实时读数；没有 nvidia-smi 时是空对象 */
  gpu: {
    util?: number
    mem_used_mb?: number
    mem_total_mb?: number
    mem_pct?: number
  }
  /** 只有 failed 时才有内容：启动失败的那张卡卡在哪 */
  log_tail: string[]
}

export type MultiGpuState = {
  status: 'stopped' | 'starting' | 'ready' | 'partial' | 'failed' | 'stopping'
  error: string
  engine: string
  base_port: number
  started: number
  ready_at: number
  overrides: {
    engine?: string
    gpus?: number[]
    base_port?: number
    model?: string | null
    tier?: string | null
    precision?: string | null
    served_name?: string
    per_card_seqs?: number | null
  }
  workers: MultiGpuWorker[]
  notes: string[]
  /** 每卡并发（0 = 按分档表/显存推算） */
  per_card_seqs: number
  /** 就绪的卡数 × 每卡并发 */
  total_seqs: number
  /** 就绪的地址，任务侧按这个分槽 */
  urls: string[]
}

export type MultiGpuCapability = {
  engine: string
  ok: boolean
  reason: string
  gpus: { index: number; name: string; vram_gb: number }[]
}

// ---- 依赖检测
//
// 清单来自两份 requirements 文件，后端现读现算——页面不存版本表，
// 否则 pip 装完之后两边会对不上。

export type DepStatus = 'ok' | 'missing' | 'mismatch'

/** 「检查完整性」的结果：真的 import 一次，不是只看 dist-info 在不在 */

export type DepVerify = {
  ok: boolean
  detail: string
  ms: number
}

export type DepItem = {
  name: string
  spec: string
  /** 只要版本那一段：==2.5.4 / >=3.10,<4 / 空 */
  requirement: string
  module: string
  group: string
  note: string
  torch_index: boolean
  installed: string | null
  status: DepStatus
  detail: string
  /** 上次检查的结果；没检查过就是 null */
  verified: DepVerify | null
}

/** 批量动作里的一项。单包动作也有一条，前端统一按列表画 */

export type DepBatchItem = {
  name: string
  status: 'pending' | 'running' | 'ok' | 'failed' | 'skipped'
  detail: string
}

export type DepJob = {
  status: 'running' | 'done' | 'failed' | 'cancelled'
  action: string
  target: string
  started: number
  finished: number
  elapsed: number
  exit_code: number | null
  log: string[]
  batch: DepBatchItem[]
}

export type DepGroup = {
  key: string
  title: string
  hint: string
  items: DepItem[]
  ok: number
  total: number
}

export type DepState = {
  python: string
  torch_index: string
  summary: {
    total: number
    ok: number
    missing: number
    mismatch: number
    /** 元数据在但 import 失败：包坏了、DLL 缺了 */
    broken: number
  }
  /** 上次完整性检查的时间（0 = 还没查过） */
  verify_at: number
  groups: DepGroup[]
  job: DepJob | null
}

// ---- 部署自检
//
// 与 health 的分工：health 说"服务活着"，自检说"它现在能不能真打出一张图"。

export type SelfCheckStepStatus = 'pending' | 'running' | 'pass' | 'fail' | 'skip'

export type SelfCheckStep = {
  key: string
  title: string
  status: SelfCheckStepStatus
  /** 通过时也有一句话——只说"通过"不说明看到了什么，用户没法判断可信度 */
  detail: string
  ms: number
}

export type SelfCheckState = {
  status: 'idle' | 'running' | 'done' | 'stopped'
  steps: SelfCheckStep[]
  started: number
  finished: number
  elapsed: number
  input_dir: string
  recursive: boolean
  sample: number
  url: string
  summary: { pass: number; fail: number; skip: number; total: number }
}

// ---- vLLM 服务（NL 模式的前置）

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
      model?: string
      tier?: string
      served_name?: string
      max_num_seqs?: number
      max_num_batched_tokens?: number
      gpu_mem?: number
    } = {},
  ) =>
    json<VllmState>('/api/vllm/start', {
      method: 'POST',
      body: JSON.stringify({ port, timeout_s, ...opts }),
    }),
  vllmPlan: (
    opts: {
      model?: string
      tier?: string
      max_num_seqs?: number
      max_num_batched_tokens?: number
      gpu_mem?: number
    } = {},
  ) => {
    const qs = new URLSearchParams()
    if (opts.model) qs.set('model', opts.model)
    if (opts.tier) qs.set('tier', opts.tier)
    if (opts.max_num_seqs) qs.set('max_num_seqs', String(opts.max_num_seqs))
    if (opts.max_num_batched_tokens)
      qs.set('max_num_batched_tokens', String(opts.max_num_batched_tokens))
    if (opts.gpu_mem) qs.set('gpu_mem', String(opts.gpu_mem))
    return json<VllmPlan>(`/api/vllm/plan?${qs.toString()}`)
  },
  vllmStop: () => json<VllmState>('/api/vllm/stop', { method: 'POST' }),
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
  // ---- HF 分片下载页
  hfFiles: (
    repo: string,
    token = '',
    revision = 'main',
    format = 'auto',
    proxy = '',
  ) =>
    json<HfFilesResult>('/api/hf/files', {
      method: 'POST',
      body: JSON.stringify({ repo, token, revision, format, proxy }),
    }),
  downloads: () => json<DownloadsState>('/api/downloads'),
  downloadsCached: (repo: string) =>
    json<{ repo: string; files: { filename: string; bytes: number }[] }>(
      `/api/downloads/cached?repo=${encodeURIComponent(repo)}`,
    ),
  /** 本地已下好的离线包（每个仓库一个目录），打标页直接选它就不走网络 */
  downloadsPacks: () =>
    json<{
      cache_root: string
      free_bytes: number
      packs: {
        repo: string
        dir: string
        count: number
        bytes: number
        files: { filename: string; bytes: number }[]
      }[]
    }>('/api/downloads/packs'),
  downloadsStart: (spec: {
    repo: string
    files: string[]
    sizes?: Record<string, number>
    token?: string
    revision?: string
    conns?: number
    proxy?: string
    reserve_gb?: number
  }) =>
    json<{ created: DownloadItem[]; snapshot: DownloadsState }>('/api/downloads/start', {
      method: 'POST',
      body: JSON.stringify(spec),
    }),
  downloadCancel: (id: string) =>
    json<{ ok: boolean; snapshot: DownloadsState }>(
      `/api/downloads/${id}/cancel`,
      { method: 'POST' },
    ),
  downloadsCancelAll: () =>
    json<{ ok: boolean; snapshot: DownloadsState }>('/api/downloads/cancel_all', {
      method: 'POST',
    }),
  downloadsClear: () =>
    json<{ removed: number; snapshot: DownloadsState }>('/api/downloads/clear', {
      method: 'POST',
    }),
  saveProxy: (url: string) =>
    json<{ proxy: string }>('/api/proxy', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
  testProxy: (url: string) =>
    json<{ ok: boolean; via: string; latency_ms: number; error?: string }>(
      '/api/proxy/test',
      { method: 'POST', body: JSON.stringify({ url }) },
    ),
  // ---- 本机能力 / 磁盘
  /**
   * 指定引擎现在能不能跑（决定「启动服务」按钮的可用性）。
   *
   * 这个调用是**页面一进来就发**的：它一旦抛 TypeError，整棵 React 树会
   * 直接崩掉，只留一个黑屏——所以它必须存在且一定返回结果。
   */
  vllmCapability: (engine: string) =>
    json<{ engine: string; ok: boolean; reason: string }>(
      `/api/vllm/capability?engine=${encodeURIComponent(engine)}`,
    ),
  diskFree: (path: string) =>
    json<{ path: string; free_bytes: number; total_bytes: number }>(
      `/api/disk?path=${encodeURIComponent(path)}`,
    ),
  /**
   * 一次性探完"地址通不通 / 密钥对不对 / 模型名认不认 / 看不看得见图"。
   *
   * 后端还没这条路由（见 api.ts 末尾的 TODO 说明）：调用会得到 404 并抛
   * ApiError，界面会把它显示在探针结果里，不会影响页面其他部分。
   */
  probeNlApi: (spec: {
    provider: string
    base: string
    model: string
    key: string
  }) =>
    json<{
      ok: boolean
      steps: { name: string; ok: boolean; detail: string }[]
    }>('/api/nlapi/probe', { method: 'POST', body: JSON.stringify(spec) }),
  /** 记住/忘掉密钥。后端同样未实现，失败不影响提交任务本身。 */
  saveNlApiKey: (spec: {
    provider: string
    base: string
    key: string
    remember: boolean
  }) =>
    json<{ saved: boolean }>('/api/nlapi/key', {
      method: 'POST',
      body: JSON.stringify(spec),
    }),
  // ---- 任务控制
  //
  // 后端没有 pause / resume / restart 这三条路由（见 app.py 的路由表），
  // 界面上却已经有按钮。这里给的是**明确的拒绝**，而不是 undefined——
  // 后者会在点击时抛 "api.pause is not a function"，看起来像整个应用坏了。
  pause: async (_id: string): Promise<{ snapshot: Snapshot | null }> => {
    throw new Error(
      '这个版本还不支持暂停任务。可以等它跑完，或直接停止——已完成的图都已落盘，重跑会跳过。',
    )
  },
  resume: async (_id: string): Promise<{ snapshot: Snapshot | null }> => {
    throw new Error('这个版本还不支持从断点继续任务，重新提交同样的配置即可续做。')
  },
  restart: async (_id: string): Promise<Snapshot> => {
    throw new Error('这个版本还不支持重启任务，请到「新建任务」用同样的配置再提交一次。')
  },

  // ==== 以下从 f55dc72 恢复：407ea15 提交时被整段删掉，
  // 界面一直在调，导致 tsc 报 174 个错、并在运行时抛
  // "xxx is not a function" 把整棵树打崩。
  llamaStatus: () => json<LlamaRuntime>('/api/llama'),

  installLlama: (payload: { url?: string; force?: boolean }) =>
    json<{ started: boolean; reason: string } & LlamaRuntime>('/api/llama/install', {
      method: 'POST',
      body: JSON.stringify({ url: payload.url ?? '', force: payload.force ?? false }),
    }),

  cancelLlamaInstall: () =>
    json<LlamaRuntime>('/api/llama/install/cancel', { method: 'POST' }),

  // 存档只回脱敏值：浏览器永远拿不到明文密钥。

  nlApiKeys: () =>
    json<{
      providers: Record<string, { base: string; key_masked: string } | null>
      prices: string[]
    }>('/api/nl-api/keys'),

  models: () => json<ModelCatalog>('/api/models'),

  saveModels: (payload: { vlm: VlmModelEntry[]; tag: TagModelEntry[] }) =>
    json<ModelCatalog>('/api/models', { method: 'PUT', body: JSON.stringify(payload) }),

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

  resumeState: () => json<{ ok: boolean; resume: ResumeInfo | null }>('/api/resume'),

  resumeStart: () => json<Snapshot>('/api/resume', { method: 'POST' }),
  /** 不再提示这次续做（只删记录，产物一张不动） */

  resumeDismiss: () =>
    json<{ ok: boolean }>('/api/resume', { method: 'DELETE' }),

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

  multi: () => json<MultiGpuState>('/api/multi'),

  multiCapability: (engine: string) =>
    json<MultiGpuCapability>(`/api/multi/capability?engine=${encodeURIComponent(engine)}`),

  multiStart: (opts: {
    engine?: string
    gpus?: number[]
    base_port?: number
    model?: string
    tier?: string
    precision?: string
    served_name?: string
    per_card_seqs?: number
    timeout_s?: number
  }) =>
    json<MultiGpuState>('/api/multi/start', {
      method: 'POST',
      body: JSON.stringify(opts),
    }),

  multiStop: () => json<MultiGpuState>('/api/multi/stop', { method: 'POST' }),
  // ---- 部署自检

  selfcheck: () => json<SelfCheckState>('/api/selfcheck'),

  selfcheckStart: (opts: {
    input_dir: string
    recursive?: boolean
    sample?: number
    url?: string
  }) =>
    json<SelfCheckState>('/api/selfcheck/start', {
      method: 'POST',
      body: JSON.stringify(opts),
    }),

  selfcheckStop: () => json<SelfCheckState>('/api/selfcheck/stop', { method: 'POST' }),
  // ---- 依赖检测

  deps: () => json<DepState>('/api/deps'),

  depAction: (name: string, action: 'install' | 'uninstall' | 'reinstall') =>
    json<DepState>(`/api/deps/${encodeURIComponent(name)}/${action}`, {
      method: 'POST',
    }),
  /** 装上所有缺失的包（不动版本不符的，那等于降级） */

  depInstallMissing: () => json<DepState>('/api/deps/install-missing', { method: 'POST' }),
  /** 卸掉所有已装的包（只限清单内的） */

  depUninstallAll: () => json<DepState>('/api/deps/uninstall-all', { method: 'POST' }),
  /** 逐个真 import 一遍，验证包是真能用的 */

  depVerify: () => json<DepState>('/api/deps/verify', { method: 'POST' }),

  depCancel: () => json<DepState>('/api/deps/cancel', { method: 'POST' }),

  tagScan: (dir: string, recursive = true) =>
    json<TagScanResult>(
      `/api/tag-manage/scan?dir=${encodeURIComponent(dir)}&recursive=${recursive}`,
    ),

  tagPreview: (payload: {
    files: string[]
    limit?: number
    key?: string
    image_root?: string
    image_ext?: string
  }) =>
    json<TagPreview>('/api/tag-manage/preview', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  /** 提交合并，立刻返回 task_id；用 tagMergeStatus 轮询进度 */

  tagMerge: (payload: {
    files: string[]
    out_path: string
    format: 'full' | 'train'
    as_jsonl: boolean
    key: string
    on_conflict: 'first' | 'last'
    require: 'any' | 'both' | 'tag' | 'nl'
    indent: number
    image_root: string
    image_ext: string
    batch_size: number
  }) =>
    json<{ task_id: string }>('/api/tag-manage/merge', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  tagMergeStatus: (taskId: string) =>
    json<TagMergeTask>(`/api/tag-manage/merge/${taskId}`),
  /** 合并前自检：提交后立刻返回 task_id，用 tagCheckStatus 轮询 */

  tagCheck: (payload: {
    files: string[]
    key: string
    image_root: string
    image_ext: string
    local_root: string
  }) =>
    json<{ task_id: string }>('/api/tag-manage/check', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  tagCheckStatus: (taskId: string) =>
    json<TagMergeTask>(`/api/tag-manage/check/${taskId}`),
  /** 底部进度条：所有进行中的任务，一次拉齐 */

  activities: () => json<{ items: Activity[] }>('/api/activities'),
  /** 底栏右侧的硬件读数；拿不到的项是 null（显示 --） */

  sysinfo: () => json<SysInfo>('/api/sysinfo'),

  planGet: () => json<PlanState>('/api/plan'),

  planSet: (payload: {
    after_images: number
    action: 'stop' | 'shutdown'
    enabled?: boolean
  }) =>
    json<PlanState>('/api/plan', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  planCancel: () => json<PlanState>('/api/plan', { method: 'DELETE' }),
}
