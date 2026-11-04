// 后端契约的唯一定义处。字段名与 tagger/web/app.py 一一对应，
// 改名要两边一起改——所以这里刻意不写"更漂亮"的别名。

/** 下载队列里的一条（一个分片）。status=cancelling 是"叫停了但还在收尾"。 */
export type DownloadItem = {
  id: string
  repo: string
  filename: string
  revision: string
  size: number
  done: number
  status: 'queued' | 'running' | 'cancelling' | 'cancelled' | 'done' | 'error'
  /** 这一片实际用的连接数；小文件会自动退化成 1 */
  conns: number
  message: string
  created: number
  started: number
  finished: number
  /** 字节/秒（滑动窗口），只在 running 时有意义 */
  speed: number
}

export type DownloadsState = {
  items: DownloadItem[]
  /** 分片落盘根目录（run/hf-cache），按仓库再分一层 */
  cache_root: string
  free_bytes: number
  /** 给产物留的余量（字节），空间不够时队列就在 queued 里等 */
  reserve_bytes: number
  running: number
  queued: number
  /** 当前全局代理（与模型下载共用一份）；空 = 直连 */
  proxy: string
}

/** 打标管理：扫描到的一个 json 文件 */
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
  /** pil = CPU 缩放/归一化（现状）；tensor = 缩放/归一化上卡（解码仍在 CPU） */
  preprocess: string
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
  /** 推理来源：local = 本机/自建服务；openai / anthropic = 外部 API */
  vlm_provider: 'local' | 'openai' | 'anthropic'
  /** API 地址（本机模式留空）。留空时后端按来源取默认域名 */
  api_base: string
  /** API 密钥。只在提交时走一趟，快照里会被抹成 *** */
  api_key: string
  url: string
  vlm_model: string
  /** both = NL + tag；nl / tag = 单模式，都由 VLM 出（本地后端会被强制关掉） */
  output_mode: 'nl' | 'tag' | 'both'
  temperature: number
  top_p: number
  max_tokens: number
  /** 描述长度引导（字符区间，只决定"往多长写"）；null = 按 token 区间派生 */
  caption_chars: [number, number] | null
  timeout_s: number
  concurrency: number
  /** 多卡：地址由后端从多卡服务组取，这里只表明"用多卡" */
  multi_gpu?: boolean
  /** 每卡并发（0 = 按分档表推算）。多卡时与 concurrency 同义，优先它 */
  per_card_seqs?: number
  multi_gpus?: number[]
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
  /** auto = 有 parquet 就流式读（不落盘），否则按 tar 分片下载 */
  hf_format: 'auto' | 'parquet' | 'webdataset'
  /** tar 模式预取几片（下载与打标并行）；0 = 严格串行 */
  hf_prefetch: number
  /** 单个分片用几条并发连接（国内单条 TCP 常被限速）；1 = 单连接 */
  hf_conns: number
  /** 给产物留的磁盘余量（GB）；0 = 自动 */
  hf_reserve_gb: number
  /** 结果缓存：同图同参复用上次产出，不送模型 */
  cache: boolean
  cache_max_gb: number
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

export type JobStatus = 'pending' | 'running' | 'paused' | 'done' | 'stopped' | 'failed'
export type JobPhase = '' | 'tag' | 'pack'

/** 上次没跑完的任务。只含摘要——完整 spec 里有 API 密钥，不下发到页面。 */
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
  /**
   * 云端按量计费的账：token 与撞限流次数。
   * cost 为 null = 没配价目（configs/nl_api.json 的 prices 段），只报 token。
   */
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
  preprocess: string
  threshold: number
  keep_categories: string[]
  /** 各 tag 后端在配置里绑定的 HF 仓库（按后端分组） */
  tag_models: Record<string, string>
  /** VLM 可选权重，来自 tiers.yaml 的 models 段 */
  vllm_models: VllmModelOption[]
  /** VLM 分档，来自 tiers.yaml 的 tiers 段 */
  vllm_tiers: VllmTierOption[]
  /** NL 推理来源下拉（local / openai / anthropic）与各自的默认地址 */
  vlm_providers: VlmProviderOption[]
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

/** NL 推理来源。local = 本机/自建服务；其余走外部 API（不加载本地权重）。 */
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

// ---- 多卡（数据并行）：一张卡一个服务进程
//
// 与 VllmState 是两套结构：那边是"一个服务"，这里是"一组服务 + 一张
// 每卡状态表"。页面两块面板各自轮询各自的接口，不互相推断。

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
  // ---- 外部推理 API（NL 打标的 openai / anthropic 来源）
  //
  // 探测发一张 8×8 的小图走真实请求：本机模式有「就绪」红灯可看，API 模式
  // 没有进程，只能靠这一次调用回答"地址/密钥/模型/视觉能力"四件事。
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
  // 存档只回脱敏值：浏览器永远拿不到明文密钥。
  nlApiKeys: () =>
    json<{
      providers: Record<string, { base: string; key_masked: string } | null>
      prices: string[]
    }>('/api/nl-api/keys'),
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
  /** 暂停：在途的几张打完就挂起，产物留在盘上 */
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
  resumeState: () => json<{ ok: boolean; resume: ResumeInfo | null }>('/api/resume'),
  resumeStart: () => json<Snapshot>('/api/resume', { method: 'POST' }),
  /** 不再提示这次续做（只删记录，产物一张不动） */
  resumeDismiss: () =>
    json<{ ok: boolean }>('/api/resume', { method: 'DELETE' }),
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
  // ---- 多卡（数据并行）
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
  diskFree: (path: string) =>
    json<{ path: string; free_bytes: number }>(
      `/api/disk?path=${encodeURIComponent(path)}`,
    ),
  hfFiles: (
    repo: string,
    token: string,
    revision = 'main',
    format: 'auto' | 'parquet' | 'webdataset' = 'auto',
  ) => {
    const qs = new URLSearchParams({ repo, revision, format })
    if (token) qs.set('token', token)
    return json<{
      repo: string
      revision: string
      format: string
      files: string[]
      /** 文件名 → 字节数（只有 tar 分片有；parquet 侧由下载进度自己报总长） */
      sizes: Record<string, number>
    }>(`/api/hf/files?${qs.toString()}`)
  },
  // ---- 下载页（分片预下载，与打标任务解耦）
  downloads: () => json<DownloadsState>('/api/downloads'),
  downloadsStart: (payload: {
    repo: string
    token: string
    revision: string
    files: string[]
    sizes: Record<string, number>
    reserve_gb: number
    /** 单片并发连接数；1 = 单连接，上限 16 */
    conns: number
  }) =>
    json<{ created: DownloadItem[] }>('/api/downloads', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  downloadCancel: (id: string) =>
    json<{ ok: boolean }>(`/api/downloads/${id}/cancel`, { method: 'POST' }),
  downloadsCancelAll: () =>
    json<{ ok: boolean }>('/api/downloads/cancel-all', { method: 'POST' }),
  downloadsClear: () =>
    json<{ removed: number }>('/api/downloads/clear', { method: 'POST' }),
  // ---- 打标管理（多个 json → 一个单体 json）
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
  downloadsCached: (repo: string) =>
    json<{ repo: string; files: { filename: string; bytes: number }[] }>(
      `/api/downloads/cached?repo=${encodeURIComponent(repo)}`,
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
}
