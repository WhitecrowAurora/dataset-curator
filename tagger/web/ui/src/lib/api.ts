// 后端契约的唯一定义处。字段名与 tagger/web/app.py 一一对应，
// 改名要两边一起改——所以这里刻意不写"更漂亮"的别名。

export type JobSpec = {
  input_dir: string
  /** 只打这些本地分片（绝对路径）；空 = 按 input_dir 整个目录走 */
  input_shards?: string[]
  out_dir: string
  recursive: boolean
  include_zips: boolean
  include_loose: boolean
  limit: number
  force: boolean
  /** 同图同参复用上次结果 */
  cache?: boolean
  /** 复用缓存的体积上限（GB）；后端缺省 8 */
  cache_max_gb?: number
  // 引擎。local = 本地 tag 后端；vlm = vLLM 出 NL（tag 可由本地后端并行出）
  engine: 'local' | 'vlm'
  // 打标
  backend: string
  model_dir: string
  /** 预处理方式（resize / none） */
  preprocess?: string
  device: string
  precision: string
  batch: number
  threshold: number | null
  keep_categories: string[]
  thresholds: Record<string, number>
  model_repo: string
  guard: boolean
  /** 覆盖分档表的护栏阈值；空 = 全用分档表的值 */
  guard_policy?: Record<string, number>
  /** 按"预计产出 + 20% 冗余"判盘够不够（取代 guard 的固定磁盘阈值） */
  check_space: boolean
  // VLM 服务（engine="vlm"）
  url: string
  vlm_model: string
  /** 出 NL 的供应商；local = 上面这个 url，其余走云端 API（后端会归一化） */
  vlm_provider: string
  /** 云端服务地址；空 = 按来源取默认域名 */
  api_base?: string
  /** 云端密钥。只在本次任务的内存里走一趟，快照里会被抹成 *** */
  api_key?: string
  /** both = NL + tag；nl / tag = 单模式，都由 VLM 出（本地后端会被强制关掉） */
  output_mode: 'nl' | 'tag' | 'both'
  temperature: number
  top_p: number
  max_tokens: number
  timeout_s: number
  concurrency: number
  /** 多卡数据并行：每张卡各起一个服务，产物写同一目录，不用事后合并 */
  multi_gpu?: boolean
  /** 参与打标的物理卡号；空 = 用全部卡 */
  multi_gpus?: number[]
  /** 每卡几个 slot；0 = 跟分档表推算 */
  per_card_seqs?: number
  max_pixels: number
  /** 描述长度引导（字符区间）；null = 按 token 区间派生 */
  caption_chars?: number[] | null
  /** 中文 NL 比例（0~1）。按 (seed, image_id) 抽签，tag 段仍为英文；0 = 全英文 */
  caption_zh_ratio?: number
  /** 用哪份 NL 提示词（prompt/<名字>.json 的名字）；空 = 内置那份 */
  prompt_id?: string
  seed: number | null
  /** 同一张图跨运行累计失败多少次后跳过；0 = 不限制 */
  max_failures: number
  /** 质检判定线；0 = 用配置（pass 3.4 / drop 1.8） */
  quality_pass_score: number
  quality_drop_score: number
  /** 重打次数上限；0 = 用配置（2 次） */
  quality_max_attempts: number
  /** 重试阶梯：温度每轮上调多少 / 封顶，像素预算乘几倍 / 封顶；0 = 用配置 */
  quality_retry_temp_step?: number
  quality_retry_temp_cap?: number
  quality_retry_pixels_scale?: number
  quality_retry_pixels_cap?: number
  /** L2 语义打分（图文 CLIP）。开启后并发会被摁到 1 */
  quality_enable_clip?: boolean
  quality_clip_min_score?: number
  /** CLIP 权重与预训练来源；空 = 跟配置（ViT-L-14 / openai） */
  quality_clip_model?: string
  quality_clip_pretrained?: string
  /** L1 规则层阈值；null = 跟配置（这里 0 是合法值，不能当"没填"） */
  quality_min_chars?: number | null
  quality_max_chars?: number | null
  quality_min_tag_coverage?: number | null
  quality_max_repeat_ratio?: number | null
  /** 重试用尽仍不合格时打到第二个服务；留空 = 不升级 */
  escalate_url?: string
  escalate_model?: string
  /** file:// 传图；要求服务端 --allowed-local-media-path 放行 */
  use_file_url?: boolean
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
  artifact_format: 'parquet' | 'jsonl' | 'json'
  parquet_rows: number
  // 命名
  artist: boolean
  artist_depth: number
  /** 画师名写进 NL 正文（caption）开头；false = 只留在 JSON 字段 */
  artist_inject: boolean
  /** 画师名写进 TAG 串开头；与上一条独立（NL / TAG 是两条训练输入） */
  artist_inject_tags: boolean
  /** 画师对照表：单 parquet / 分片目录 / hf://datasets/<repo>/<文件或目录>；空 = 跟配置 */
  artist_table?: string
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

export type JobStatus = 'pending' | 'running' | 'paused' | 'done' | 'stopped' | 'failed'
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
  source_count?: number
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
    busy?: number
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
  /** 云端按量计费用量；本机模式全 0，cost 为 null = 没配价目 */
  usage: {
    prompt_tokens: number
    completion_tokens: number
    rate_limited: number
    cost: number | null
  }
  by_reason: Record<string, number>
  pack: PackProgress
  plan: PlanPreview | null
  current: string
  seq: number
  created: number
  started: number
  ended: number
  /**
   * 来自磁盘的历史记录（进程重启前的任务）。只有快照，没有事件流，
   * 也没有 Job.reader —— 所以「进度 / 结果」打不开，只能看摘要或继续。
   */
  historical?: boolean
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
  /** 只有 VLM 引擎有：NL 文案 */
  caption?: string
  /** 只有 VLM 引擎有：质检分 */
  score?: number | null
  /** false = 这个任务拿不到原图（VLM 流式读分片），结果页不出缩略图 */
  thumb?: boolean
  /** 失败行（来自 failures.jsonl）才有：失败原因 */
  error?: string
  ts?: number
}

export type ResultsPage = {
  total: number
  offset: number
  limit: number
  items: ResultItem[]
  /** 失败图数量（来自 failures.jsonl，去重）。产物里没有失败行，靠它提示。 */
  failed_total?: number
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
  /** 预处理：resize / none 之类，后端按字符串解析 */
  preprocess?: string
  threshold: number
  keep_categories: string[]
  /** 各 tag 后端在配置里绑定的 HF 仓库（按后端分组） */
  tag_models: Record<string, string>
  /** VLM 可选权重，来自 tiers.yaml 的 models 段 */
  vllm_models: VllmModelOption[]
  /** VLM 分档，来自 tiers.yaml 的 tiers 段 */
  vllm_tiers: VllmTierOption[]
  /** NL 服务来源（local / openai / anthropic…），各带默认地址 */
  vlm_providers: VlmProviderOption[]
  /** 硬件保护阈值（tiers.yaml 的 limits 段），用户未改时的参考值 */
  guard: Record<string, number>
  /** 质检判定的默认分（页面上的"0 = 用配置的 3.4"就是它） */
  quality: QualityDefaults
  /** 允许 vLLM 直接读取的本地媒体根目录 */
  local_media_root?: string
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
  artist_inject_tags: boolean
  artist_prefix: string
  /** 配置里配过的画师表路径；空 = 没配。表单用它做预填与"跟配置走"提示 */
  artist_table: string
  seq_width: number
}

export type VllmModelOption = {
  key: string
  path: string
  precision: string
  family: string
  /** 分档表里默认用它的档位名（仅提示，实际落档由服务端解析） */
  tier: string
  /** 模型管理页的备注 */
  note?: string
  /** 下载目录里有没有东西 */
  exists?: boolean
  /** 能不能直接跑：llama.cpp 要的 gguf / mmproj 都在盘上 */
  ready?: boolean
  /** 缺哪几个文件（相对路径的文件名），空 = 不缺 */
  missing_files?: string[]
  /** 哪几档 GGUF 真有文件（f16/q4/q6/q8）。空 = 一档都没压出来 */
  gguf_ready?: string[]
  /** 文件在、但明显不完整（量化中断留下的半截）的档 */
  gguf_broken?: string[]
  /** 有没有 transformers 原始权重（没有就压不出新档） */
  raw_ready?: boolean
  /** 有没有 vLLM 的预量化权重（W4A16 要有它才能用） */
  quantized?: boolean
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
  /** 所有盘符（只有 Windows 有；别的系统为空数组） */
  drives: { path: string; label: string }[]
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
    /** vllm / lmdeploy / llama.cpp；null = 跟后端默认 */
    engine: string | null
    /** 选中的权重 key；null = 按显存落档 */
    model: string | null
    /** 强制分档名；null = 按显存匹配 */
    tier: string | null
    /** 精度档（bf16 / fp8 / int4 / w4a16）；null = 按分档表 */
    precision: string | null
    /** 接口路由名（--served-model-name） */
    served_name: string | null
    /** 服务端并发上限。只对 vLLM 生效；llama.cpp 用下面的 slots */
    max_num_seqs: number | null
    max_num_batched_tokens: number | null
    gpu_mem: number | null
    /** 放行 file:// 的媒体根目录；只有本机自起的服务才可能有 */
    local_media_root: string | null
    /** llama.cpp 的 -np。null = 按显存自动；vLLM 下恒为 null */
    slots: number | null
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
      /** 实际传给 vLLM 的 --quantization；null = 按权重 dtype 加载 */
      quantization?: string | null
      /** 权重本身是否被压成 4bit（false = W4A16，权重还是 BF16） */
      weight_quantized?: boolean
      max_pixels: number
      max_model_len: number
      max_num_seqs: number
      max_num_batched_tokens: number
      gpu_memory_utilization: number
      vram_gb: number
      device: string
      warnings: string[]
      summary: string
      /** 槽位方案。只有 llama.cpp 有：vLLM 的并发由 max_num_seqs 动态分配 */
      slots?: VllmSlotPlan | { error: string }
      /**
       * 这组参数对应的权重在不在盘上（只有 llama.cpp 会算）。
       * ok=false 时 error 里带着"缺哪一档、现有哪些档"的人话原因。
       */
      weights?: { ok: boolean; gguf?: string; mmproj?: string; error?: string }
    }
  | { ok: false; error: string }

/**
 * llama.cpp 的槽位（-np）方案。
 *
 * 为什么需要它在页面上露出来：KV 缓存是**按槽位数在启动时预留**的，
 * 任务并发调小只是让 slot 空着，显存一点不省。想让出显存给游戏/别的
 * 程序，唯一有效的动作是把这个数调小。
 */
export type VllmSlotPlan = {
  slots: number
  /** true = 用户手填；false = 按显存自动推算 */
  slots_manual?: boolean
  /** 留空时按显存取到的值，用来把"自动会给你 12"写出来 */
  slots_default?: number
  ctx_per_slot?: number
  ctx_total?: number
  kv_type?: string
  /** 每槽 KV（GiB） */
  kv_per_slot_gib?: number
  /** 所有槽位合计 KV（GiB） */
  kv_total_gib?: number
  error?: string
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

/** 离线包里的一个分片，以及它在产物目录里已经有多少行。 */
export type PackShard = {
  name: string
  path: string
  bytes: number
  kind: 'parquet' | 'webdataset'
  /** 只有 parquet 读得到（footer）；tar/zip 不扫，null = 未知 */
  total_rows: number | null
  done_rows: number
  /** done = 行数凑满；partial = 有产物但不足/总数未知；none = 没有产物 */
  state: 'done' | 'partial' | 'none'
}

export type PackShardScan = {
  dir: string
  out: string
  out_exists: boolean
  /** 产物是 jsonl 才带包名，parquet 产物按行数切、归属不到分片 */
  attributable: boolean
  shards: PackShard[]
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
  next_seq?: number
  seq_width?: number
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
  /** 画师表；不传 = 跟配置。预览与真实跑必须用同一张表，否则名字对不上 */
  artist_table?: string
  recursive?: boolean
  include_zips?: boolean
  include_loose?: boolean
  limit?: number
  seq_width?: number
  start_seq?: number
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
  kind: 'tag' | 'data' | 'merge' | 'check' | 'quantize' | 'deps' | 'convert'
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

/**
 * 刚收尾的活动：只用来弹一次"跑完了 / 失败了"，不参与底栏渲染。
 * 打标任务的完成情况走 /api/jobs/{id}，这里主要是量化与依赖安装这类
 * 没有独立任务页的活。
 */
export type FinishedActivity = Activity & {
  status: string
  ok: boolean
  elapsed_s: number
  /** 结束时刻（epoch 秒）：同一个活动跑两次时用来区分 */
  finished_at?: number
  error: string
  log_tail: string[]
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
  /**
   * 原始权重已经下好、但 llama.cpp 要的那几个文件（gguf / mmproj）还没压出来。
   * VLM 下的是 safetensors，GGUF 一律由「本地量化」产出，所以这里缺的是
   * "还没做那一步"，不是"下载没下全"。
   */
  missing_files?: string[]
  /** 哪些 GGUF 档真的有文件（`q4` / `q6`…）；只登记了路径但文件不在的不算 */
  gguf_ready?: string[]
  /**
   * 有没有 transformers 原始权重（config.json + *.safetensors）。
   * 只有 NL 行有这个字段；目录里只剩量化产物（.gguf）时为 false。
   */
  raw_ready?: boolean
  /** 真的能直接跑：原始权重在盘上，且该有的 GGUF / mmproj 也在 */
  ready?: boolean
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
  /** llama.cpp 要的默认 GGUF 与视觉投影文件（相对仓库根） */
  gguf?: string
  mmproj?: string
  /** 同一份权重的各档 GGUF。只会是本地产物（原始权重压出来的）。 */
  gguf_quants?: Record<string, string>
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
  /** 下载源（镜像站），空 = 跟随 env_endpoint / 官方 */
  endpoint: string
  /** 环境变量 HF_ENDPOINT 的值，空 = 没设 */
  env_endpoint: string
  /** 最终生效的下载源（页面设置 > 环境变量 > 官方） */
  effective_endpoint: string
  /** 大文件下载连接数（页面设置 > 环境变量 > 默认 4） */
  conns: number
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
  /** 这个包同时属于另一个分组（同一份 site-packages，卸载会两边一起少） */
  shared?: boolean
  /** false = 当前平台用不了（例如 Windows 上的 vllm），安装/重装要置灰 */
  supported?: boolean
  /** 只支持哪些平台，展示用：["linux"] */
  platforms?: string[]
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
  /** core / tag / optional / extra：extra 组来自 pyproject 的附加依赖 */
  kind?: string
  /** kind === 'extra' 时对应的 extra 名（install-extra 的参数） */
  extra?: string
  title: string
  hint: string
  items: DepItem[]
  ok: number
  missing?: number
  /** 缺但本机装不了（Windows 上的 vllm），算进度要扣掉 */
  blocked?: number
  total: number
}

export type DepState = {
  python: string
  torch_index: string
  /** torch 走 CPU 版时的原因（驱动太老 / 读不出驱动）；空 = 正常走 CUDA */
  torch_note: string
  summary: {
    total: number
    ok: number
    missing: number
    mismatch: number
    /** 元数据在但 import 失败：包坏了、DLL 缺了 */
    broken: number
    /** missing 里本机装不了的那部分：按钮上的"要装几个"不能算它 */
    blocked: number
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
  /** 还没装上的量化依赖（导入名）。非空时页面出「一键安装」按钮 */
  missing_deps: string[]
  model: string
  source: string
  quantized_path: string | null
  /** 已经有可用的 4bit 权重：切 W4A16 不需要再量化 */
  has_quantized: boolean
  /** 产物体积（字节），0 = 没有或读不到 */
  quantized_bytes?: number
  /** 默认产物目录（相对仓库根） */
  suggested_dir: string
  methods: string[]
  task: QuantTaskState | null
}

/**
 * 本地量化成 GGUF 的任务状态。
 *
 * 与 QuantTaskState 分开：那条产出 W4A16（给 vLLM），这条产出 .gguf（给
 * 默认引擎）。字段名相近但生命周期、产物、失败原因都不同，共用一个类型
 * 会让"到底在跑哪条路"变得靠猜。
 */
export type GgufConvertTask = {
  model: string
  /** q4_k_m / q6_k */
  target: string
  source: string
  out_file: string
  status: 'running' | 'done' | 'error' | 'canceled'
  percent: number
  stage: string
  message: string
  elapsed: number
  finished_at: number
  log_tail: string[]
}

export type GgufConvertState = {
  /** 依赖齐不齐（脚本缺失不算：首次转换会自动去取） */
  available: boolean
  reason: string
  /** llama.cpp 转换脚本是否已就位 */
  script_ready: boolean
  tag: string
  targets: { key: string; label: string; note: string }[]
  /** 原始权重的落点；ready=false 表示还没下过 */
  source: { dir: string; bytes: number; ready: boolean; hint?: string }
  /**
   * 清单里登记过的 GGUF 档与落盘情况（`q4` / `q6` / `f16`…）。
   * `bytes === 0` = 配置指着的那份文件已经不在了。
   */
  quants: Record<string, { file: string; bytes: number }>
  task: GgufConvertTask | null
}

// ---- 调度中心（把 fox-label v3 worker 托管成一个可启停的子进程）

/** 服务端 `/api/v1/status` 里的一条租约。字段是 worker 那边定的，原样转。 */
export type CenterLease = {
  lease_id: string
  /** active = 正在干；prelock = 干完这片后预占的下一片 */
  kind: string
  repo: string
  package: string
  /** 剩多少秒到期 */
  expires_in: number
  progress?: Record<string, unknown>
}

/** 只读看板。ok=false 时 reason 说人话，其余字段可能没有。 */
export type CenterBoard = {
  ok: boolean
  reason?: string
  status?: string
  tier?: string
  leases?: CenterLease[]
}

export type CenterConfig = {
  server: string
  user_id: string
  tag_url: string
  mode: string
  out_dir: string
  interval: number
  extra_args: string
  /** 长驻等待：池子空了不退出，挂着等人派活 */
  wait: boolean
  /** 配 wait：连续这么多秒没活就自己收工（0 = 一直等） */
  idle_timeout: number
  /** 本次最多交付多少片（0 = 不限） */
  max_packages: number
}

export type CenterIdentity = {
  user_id?: string
  device_id?: string
  server?: string
  server_id?: string
  beat_interval?: number
}

export type CenterState = {
  running: boolean
  /** 上一轮进程的退出码；null = 还没跑过 */
  last_rc: number | null
  uptime_s: number
  config: CenterConfig
  /** 后端自动拼好的 worker --cmd，页面只读展示 */
  cmd: string
  identity: CenterIdentity
  board: CenterBoard
  log: string[]
}

/**
 * 顶栏那盏灯的输入。
 *
 * `linked` 只看本地身份文件：存过就一直为真，总机关了也一样——所以不能用它
 * 点灯。灯看 `online`，那是"此刻真的连得上"，由后端发一次短超时的只读请求判定。
 */
export type CenterBrief = {
  /** 配过身份（登录过）。不代表现在连得上。 */
  linked: boolean
  /** 此刻连得上调度中心。未登录恒为 false */
  online: boolean
  running: boolean
  user_id: string
  /** online 为 false 且 linked 为 true 时的原因，给悬浮提示用 */
  reason: string
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// ---- 冲突与决策：同一个 image_id 两份不同内容（换模型 / 换 prompt / 新旧版本）
export type ConflictCandidate = {
  value: string
  source: string
  ts: number
  model: string
  chars: number
  /** 内容哈希。决策按它存——重扫后候选顺序变了也不会套错 */
  digest: string
}

export type ConflictItem = {
  image_id: string
  kind: 'tag' | 'nl'
  key: string
  /** 已批准那份的 digest；空串 = 还没定 */
  decided: string
  candidates: ConflictCandidate[]
}

export type ConflictsState = {
  /** false = 还没扫过，页面提示先扫 */
  index: boolean
  scanned: number
  idempotent: number
  unreadable: number
  roots: string[]
  elapsed_s: number
  total: number
  offset: number
  limit: number
  decisions: number
  items: ConflictItem[]
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

/** 顶栏「配置方案」的一条（轻量，不带表单内容） */
export type PresetMeta = { name: string; ts: string; auto: boolean }

export type PresetState = {
  items: PresetMeta[]
  active: string | null
  auto: boolean
  auto_name: string | null
}

export type PresetPayload = PresetMeta & {
  form: Record<string, unknown>
  spec: Partial<JobSpec>
}

// ---- NL 提示词文件（仓库根 prompt/*.json）
// 名字就是文件名，所以 id 与 name 恒等；分开写是为了界面别再去切路径。

export type PromptItem = {
  id: string
  name: string
  /** 相对仓库根显示，拿来在界面上说"改的就是这个文件" */
  path: string
  bytes: number
  mtime: number
  chars?: number
  has_zh?: boolean
  has_system?: boolean
  note?: string
  /** 文件读不了 / JSON 坏了的原因。有它就说明这份选不了 */
  error?: string
}

export type PromptDoc = {
  id: string
  name: string
  path: string
  prompt: string
  prompt_zh: string
  system: string
  note: string
}

/** 可提交的内容字段（新建/保存都只认这几个） */
export type PromptBody = Pick<PromptDoc, 'prompt' | 'prompt_zh' | 'system' | 'note'>

/** 新建时的初始内容 = 内置提示词在当前配置下的样子 */
export type PromptTemplate = Pick<PromptDoc, 'prompt' | 'prompt_zh' | 'system'>

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
    if (params.artist_table) qs.set('artist_table', params.artist_table)
    if (params.recursive !== undefined) qs.set('recursive', String(params.recursive))
    if (params.include_zips !== undefined) qs.set('include_zips', String(params.include_zips))
    if (params.include_loose !== undefined) qs.set('include_loose', String(params.include_loose))
    if (params.limit) qs.set('limit', String(params.limit))
    if (params.seq_width) qs.set('seq_width', String(params.seq_width))
    if (params.start_seq !== undefined) qs.set('start_seq', String(params.start_seq))
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
  // ---- NL 提示词文件（仓库根 prompt/*.json）。名字就是文件名，走 query 传。
  prompts: () => json<{ dir: string; items: PromptItem[] }>('/api/prompts'),
  /** 新建时的初始内容 = 内置提示词在当前配置下的样子 */
  promptTemplate: () => json<PromptTemplate>('/api/prompts/template'),
  prompt: (id: string) =>
    json<PromptDoc>(`/api/prompts/one?id=${encodeURIComponent(id)}`),
  savePrompt: (
    id: string,
    body: { prompt: string; prompt_zh?: string; system?: string; note?: string },
  ) =>
    json<PromptDoc>(`/api/prompts/one?id=${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  createPrompt: (body: {
    name: string
    prompt: string
    prompt_zh?: string
    system?: string
    note?: string
  }) => json<PromptDoc>('/api/prompts', { method: 'POST', body: JSON.stringify(body) }),
  revealPrompts: () => json<{ path: string }>('/api/prompts/reveal', { method: 'POST' }),
  /**
   * 新建任务页记住的东西，两份：
   * `form` 页面 state 快照（填一项存一项，中途关掉也不丢）；
   * `spec` 提交成功时的 JobSpec（字段语义稳定，旧记录靠它兜底）。
   */
  jobForm: () =>
    json<{ form: Record<string, unknown>; spec: Partial<JobSpec> }>('/api/job-form'),
  // ---- 配置方案：具名快照，存/切/删 + 自动保存。名字走 query，中文与空格都不怕。
  presets: () => json<PresetState>('/api/presets'),
  preset: (name: string) =>
    json<PresetPayload>(`/api/presets/one?name=${encodeURIComponent(name)}`),
  savePreset: (payload: {
    name: string
    form: Record<string, unknown>
    spec?: Partial<JobSpec>
  }) =>
    json<PresetState>('/api/presets', { method: 'POST', body: JSON.stringify(payload) }),
  applyPreset: (name: string) =>
    json<PresetPayload & { state: PresetState }>('/api/presets/apply', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  deletePreset: (name: string) =>
    json<PresetState>(`/api/presets?name=${encodeURIComponent(name)}`, { method: 'DELETE' }),
  autoPreset: (payload: {
    enabled?: boolean
    form?: Record<string, unknown>
    spec?: Partial<JobSpec>
  }) =>
    json<PresetState>('/api/presets/auto', { method: 'POST', body: JSON.stringify(payload) }),
  saveJobForm: (payload: {
    form?: Record<string, unknown>
    spec?: Partial<JobSpec>
  }) =>
    json<{ form: Record<string, unknown>; spec: Partial<JobSpec> }>('/api/job-form', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  // ---- 冲突与决策
  conflicts: (offset = 0, limit = 100) =>
    json<ConflictsState>(`/api/conflicts?offset=${offset}&limit=${limit}`),
  conflictsScan: (roots: string[], limit = 100) =>
    json<ConflictsState>('/api/conflicts/scan', {
      method: 'POST',
      body: JSON.stringify({ roots, limit }),
    }),
  conflictDecide: (image_id: string, kind: string, pick: string) =>
    json<{ ok: boolean; decisions: number }>('/api/conflicts/decide', {
      method: 'POST',
      body: JSON.stringify({ image_id, kind, pick }),
    }),
  conflictsAuto: (strategy: 'manual' | 'newest' | 'longest', overwrite = false) =>
    json<{ applied: number; skipped: number; reason?: string; decisions: number }>(
      '/api/conflicts/auto',
      { method: 'POST', body: JSON.stringify({ strategy, overwrite }) },
    ),
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
      engine?: string
      model?: string
      tier?: string
      precision?: string
      served_name?: string
      max_model_len?: number
      max_num_seqs?: number
      max_num_batched_tokens?: number
      gpu_mem?: number
      local_media_root?: string
      /** llama.cpp 的 -np。留空 = 按显存自动；这是唯一能让出显存的旋钮 */
      slots?: number
    } = {},
  ) =>
    json<VllmState>('/api/vllm/start', {
      method: 'POST',
      body: JSON.stringify({ port, timeout_s, ...opts }),
    }),
  vllmPlan: (
    opts: {
      /** vllm / llamacpp；用来判定精度档与引擎是否搭得上 */
      engine?: string
      model?: string
      tier?: string
      /** bf16 / fp8 / w4a16（vLLM）或 q4 / q6 / q8（llama.cpp）；不传 = 按分档表默认 */
      precision?: string
      /** 自定义上下文长度；不传 = 按分档表 */
      max_model_len?: number
      max_num_seqs?: number
      max_num_batched_tokens?: number
      gpu_mem?: number
      /** llama.cpp 的 -np；不传 = 按显存自动 */
      slots?: number
    } = {},
  ) => {
    const qs = new URLSearchParams()
    if (opts.engine) qs.set('engine', opts.engine)
    if (opts.model) qs.set('model', opts.model)
    if (opts.tier) qs.set('tier', opts.tier)
    // 精度必须带上：不带的话服务端按 None 解析，页面选 int4 却算出 bf16 的
    // 分档与显存，等于给人看一份不属于这次启动的预估。
    if (opts.precision) qs.set('precision', opts.precision)
    if (opts.max_model_len) qs.set('max_model_len', String(opts.max_model_len))
    if (opts.max_num_seqs) qs.set('max_num_seqs', String(opts.max_num_seqs))
    if (opts.max_num_batched_tokens)
      qs.set('max_num_batched_tokens', String(opts.max_num_batched_tokens))
    if (opts.gpu_mem) qs.set('gpu_mem', String(opts.gpu_mem))
    // 0 不传 = 按显存自动；传 0 会让后端说"取 1~64"。
    if (opts.slots) qs.set('slots', String(opts.slots))
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
  dataJobPreview: (input_dir: string, exclude = '', recursive = true,
                   artist_table = '') =>
    json<SplitPreview>('/api/datajob/preview', {
      method: 'POST',
      body: JSON.stringify({ kind: 'split', input_dir, exclude, recursive, artist_table }),
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
  /** 离线包里的分片清单 + 各片已产出行数（分片管理面板用） */
  packShards: (path: string, out: string) =>
    json<PackShardScan>(
      `/api/packs/shards?path=${encodeURIComponent(path)}&out=${encodeURIComponent(out)}`,
    ),
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
    json<{ proxy: string; catalog: ModelCatalog }>('/api/models/proxy', {
      method: 'PUT',
      body: JSON.stringify({ url }),
    }),
  testProxy: (url: string) =>
    json<ProxyTest>('/api/models/proxy/test', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
  /** 下载源（镜像站）。空串 = 跟随 HF_ENDPOINT / 官方。 */
  saveEndpoint: (url: string) =>
    json<{ endpoint: string; effective: string; catalog: ModelCatalog }>(
      '/api/models/endpoint',
      { method: 'PUT', body: JSON.stringify({ url }) },
    ),
  /** 大文件下载连接数。0 = 用默认 4。 */
  saveConns: (conns: number) =>
    json<{ conns: number; catalog: ModelCatalog }>('/api/models/conns', {
      method: 'PUT',
      body: JSON.stringify({ conns }),
    }),
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
  probeNlApi: (payload: {
    provider: string
    base: string
    model: string
    key: string
    timeout_s?: number
  }) =>
    json<NlApiProbe>('/api/nl-api/probe', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  // ---- llama.cpp 运行时
  //
  // 默认引擎是 llama.cpp，而 llama-server 不是 pip 包：没有它连服务都起不来。
  // 这三条让页面自己把官方 zip 下下来、解开、就地生效。
  saveNlApiKey: (payload: {
    provider: string
    base: string
    key: string
    remember: boolean
  }) =>
    json<{
      provider: string
      saved: { base: string; key_masked: string } | null
    }>('/api/nl-api/keys', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  // ---- 模型管理
  pause: (id: string) =>
    json<{ ok: boolean; snapshot: Snapshot | null }>(`/api/jobs/${id}/pause`, {
      method: 'POST',
    }),
  /** 原地继续：进程还在，worker 只是挂着 */
  resume: (id: string) =>
    json<{ ok: boolean; snapshot: Snapshot | null }>(`/api/jobs/${id}/resume`, {
      method: 'POST',
    }),
  /** 续跑一个已停下的任务：同参数重开，已打完的图自动跳过（返回新任务） */
  restart: (id: string) =>
    json<Snapshot>(`/api/jobs/${id}/restart`, { method: 'POST' }),
  // ---- 跨重启的续做（暂停/停止后关过程序）
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

  /**
   * 下载模型。VLM 一律下原始权重（path 指的那个仓库，整包）；量化档不在
   * 网上拿，由「本地量化」现场压。precision 只为兼容旧调用保留，不影响下载。
   */
  startDownload: (kind: string, key: string, precision?: string) =>
    json<DownloadState>('/api/models/download', {
      method: 'POST',
      body: JSON.stringify({ kind, key, precision: precision || null }),
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

  /** refresh=true 清掉后端 60 秒的可用性缓存：装完依赖要立刻看到"能了" */
  quantize: (model?: string, refresh = false) => {
    const qs = new URLSearchParams()
    if (model) qs.set('model', model)
    if (refresh) qs.set('refresh', '1')
    const tail = qs.toString()
    return json<QuantState>(`/api/vllm/quantize${tail ? `?${tail}` : ''}`)
  },

  quantizeStart: (opts: { model?: string; out_dir?: string; method?: string }) =>
    json<QuantTaskState>('/api/vllm/quantize/start', {
      method: 'POST',
      body: JSON.stringify(opts),
    }),

  quantizeCancel: () =>
    json<QuantTaskState | Record<string, never>>('/api/vllm/quantize/cancel', {
      method: 'POST',
    }),

  // ---- 本地量化成 GGUF（原始 safetensors → F16 → Q4_K_M/Q6_K）
  ggufState: (model?: string) =>
    json<GgufConvertState>(`/api/gguf${model ? `?model=${encodeURIComponent(model)}` : ''}`),
  ggufStart: (opts: { model: string; target: string }) =>
    json<GgufConvertTask>('/api/gguf/convert', {
      method: 'POST',
      body: JSON.stringify(opts),
    }),
  ggufCancel: () =>
    json<GgufConvertTask | Record<string, never>>('/api/gguf/cancel', {
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
  /** 装 pyproject 里某个 extra 的缺失部分（如 quantize 的 llmcompressor） */
  depInstallExtra: (extra: string) =>
    json<DepState>(`/api/deps/install-extra?extra=${encodeURIComponent(extra)}`, {
      method: 'POST',
    }),
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

  activities: () =>
    json<{ items: Activity[]; finished?: FinishedActivity[] }>('/api/activities'),
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

  // ---- 调度中心
  center: () => json<CenterState>('/api/center'),
  /** 顶栏的灯：只读后端本地身份，不打远端 */
  centerBrief: () => json<CenterBrief>('/api/center/status'),
  /**
   * 连接（换身份）。后端在跑的时候会拒：换 server 或换名字都要先停止，
   * 否则老进程还在按旧身份续约，服务端会看见两套身份。
   */
  centerLogin: (payload: { server: string; user_id: string; secret?: string }) =>
    json<CenterState>('/api/center/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  centerStart: (payload: {
    tag_url?: string
    mode?: string
    out_dir?: string
    interval?: number
    extra_args?: string
    cmd_template?: string
    wait?: boolean
    idle_timeout?: number
    max_packages?: number
  }) => json<CenterState>('/api/center/start', { method: 'POST', body: JSON.stringify(payload) }),
  centerStop: () => json<CenterState>('/api/center/stop', { method: 'POST' }),
  /** 断开：忘掉本机设备身份（地址和姓名留着，重连不用重填） */
  centerLogout: () => json<CenterState>('/api/center/logout', { method: 'POST' }),
}
