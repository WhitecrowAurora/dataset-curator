import { useEffect, useState } from 'react'
import {
  Archive,
  Boxes,
  CheckCircle2,
  FileJson,
  FolderOpen,
  FolderSearch,
  HardDrive,
  Layers,
  ListChecks,
  Loader2,
  Package,
  PackageOpen,
  Palette,
  Play,
  Plus,
  RefreshCw,
  ScanSearch,
  Square,
  Trash2,
  TriangleAlert,
  X,
} from 'lucide-react'
import {
  api,
  ApiError,
  type DataJobKind,
  type DataJobState,
  type Defaults,
  type MergeSummary,
  type PackSummary,
  type PlanPreview,
  type SplitPreview,
  type SplitSummary,
} from '../lib/api'
import { fmtBytes, fmtCount, fmtSeconds } from '../lib/format'
import { PageHeader, SectionPanel, SettingRow, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ChoiceGrid, ErrorBox, ToggleRow } from '../components/ui/controls'
import { OverviewFigure, OverviewFigureRow } from '../components/ui/OverviewFigures'
import { ProgressBar } from '../components/ui/ProgressBar'
import { useFolderPicker } from '../components/FolderPicker'

// 数据打包页：分包 / 拼接 / 封包 三条数据物流链路，与打标分家。
//
// 工作流：素材 →（去重）→ 分包 → 成员打标 → 产物回收 → 拼接对账 → 封包 → 回传。
// 之前"打完就封包"混在新建任务页里，但这三件事是磁盘物流不是 GPU 推理，
// 与打标并行跑才对——所以全部搬到这里，打标页只管打标。
//
// 同一时刻只允许一个数据任务（后端 409）：分包/拼接/封包都是重 IO，
// 互相抢磁盘没有收益。与打标/去重/回传互不影响。

const MODES = ['split', 'merge', 'pack'] as const
const SPLIT_MODES = ['count', 'bytes'] as const
const LAYOUTS = ['flat', 'mirror'] as const
const ID_SCHEMES = ['rel', 'global'] as const
const REQUIRES = ['nl', 'tag', 'both'] as const
const MERGE_FMTS = ['jsonl', 'json'] as const
const REENCODES = ['always', 'non_jpeg', 'never'] as const
const SUBSAMPLING = ['0', '1', '2'] as const
const COMPRESS = ['stored', 'deflate'] as const
const FORMATS = [
  { value: 'parquet', label: 'parquet', note: 'HF 流式读' },
  { value: 'webdataset', label: 'tar', note: 'WebDataset' },
  { value: 'zip', label: 'zip', note: '可直接打开' },
] as const

const SUBSAMPLING_LABEL: Record<string, string> = {
  '0': '4:4:4',
  '1': '4:2:2',
  '2': '4:2:0',
}

const KIND_LABEL: Record<DataJobKind, string> = {
  split: '分包',
  merge: '拼接',
  pack: '封包',
}

const STATUS_LABEL: Record<string, string> = {
  pending: '等待',
  running: '进行中',
  done: '完成',
  stopped: '已停止',
  failed: '失败',
}

const LAST_DATAJOB_KEY = 'tagger-last-datajob'

/** 与后端 _sibling_stem 同口径：<输入名> + 后缀，zip 输入去掉扩展名。 */
function siblingPath(input: string, suffix: string): string {
  const p = input.trim().replace(/[\\/]+$/, '')
  if (!p) return ''
  const parts = p.split(/[\\/]/)
  const name = parts.pop() ?? ''
  const stem = name.replace(/\.zip$/i, '')
  const parent = parts.join('/')
  return parent ? `${parent}/${stem}${suffix}` : `${stem}${suffix}`
}

export function Packaging() {
  const [mode, setMode] = useState<DataJobKind>('split')

  // ---- 分包
  const [splitInput, setSplitInput] = useState('')
  const [splitOut, setSplitOut] = useState('')
  const [splitTable, setSplitTable] = useState('')
  const [previousTable, setPreviousTable] = useState('')
  const [splitSeqWidth, setSplitSeqWidth] = useState(6)
  const [startSeq, setStartSeq] = useState(1)
  const [splitMode, setSplitMode] = useState<(typeof SPLIT_MODES)[number]>('count')
  const [perShard, setPerShard] = useState(20000)
  const [maxBytesGB, setMaxBytesGB] = useState(0)
  const [splitLayout, setSplitLayout] = useState<(typeof LAYOUTS)[number]>('flat')
  const [splitRecursive, setSplitRecursive] = useState(true)
  const [exclude, setExclude] = useState('')
  const [splitVerify, setSplitVerify] = useState(true)
  const [preview, setPreview] = useState<SplitPreview | null>(null)
  const [autoExcluded, setAutoExcluded] = useState(false)

  // ---- 拼接
  const [roots, setRoots] = useState<string[]>([''])
  const [shardDir, setShardDir] = useState('')
  const [mergeTable, setMergeTable] = useState('')
  const [mergeOut, setMergeOut] = useState('')
  const [require, setRequire] = useState<(typeof REQUIRES)[number]>('both')
  const [mergeFmt, setMergeFmt] = useState<(typeof MERGE_FMTS)[number]>('jsonl')
  const [dataset, setDataset] = useState('')
  const [sampleN, setSampleN] = useState(20)
  const [sampleOut, setSampleOut] = useState('')
  const [keepEmpty, setKeepEmpty] = useState(false)

  // ---- 封包
  const [packInput, setPackInput] = useState('')
  const [packOut, setPackOut] = useState('')
  const [formats, setFormats] = useState<string[]>(['parquet'])
  const [quality, setQuality] = useState(95)
  const [subsampling, setSubsampling] = useState('0')
  const [reencode, setReencode] = useState<(typeof REENCODES)[number]>('always')
  const [compress, setCompress] = useState<(typeof COMPRESS)[number]>('stored')
  const [maxRecords, setMaxRecords] = useState(5000)
  const [packMaxBytesGB, setPackMaxBytesGB] = useState(0)
  const [packLayout, setPackLayout] = useState<(typeof LAYOUTS)[number]>('flat')
  const [assignIds, setAssignIds] = useState<(typeof ID_SCHEMES)[number]>('rel')
  const [packSeqWidth, setPackSeqWidth] = useState(6)
  const [packStartSeq, setPackStartSeq] = useState(1)
  const [artist, setArtist] = useState(true)
  const [artistDepth, setArtistDepth] = useState(0)
  const [artistInject, setArtistInject] = useState(true)
  // 画师对照表：空 = 跟 tiers.yaml 走。分包时它影响 image_id 的排序键，
  // 封包时它决定"跟着图一起封进去的画师"是谁——两边必须与打标那次一致。
  const [artistTable, setArtistTable] = useState('')
  const [cfgArtistTable, setCfgArtistTable] = useState('')
  const [checkSpace, setCheckSpace] = useState(true)
  const [packPreview, setPackPreview] = useState<PlanPreview | null>(null)

  const [job, setJob] = useState<DataJobState | null>(null)
  const [busy, setBusy] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const folder = useFolderPicker()
  // 四种目标共用一个入口：字段不同，但"从哪开始选、选完写到哪"完全同构。
  // 有宿主时弹系统对话框，没有才落到自绘窗口（这一页目标多，尤其值得）。
  const openPicker = (
    target: 'split-input' | 'split-out' | 'pack-input' | 'pack-out' | 'shard-dir' | 'merge-root',
    index = 0,
  ) => {
    const start =
      target === 'split-input'
        ? splitInput
        : target === 'split-out'
          ? splitOut
          : target === 'pack-input'
            ? packInput
            : target === 'pack-out'
              ? packOut
              : target === 'shard-dir'
                ? shardDir
                : (roots[index] ?? '')
    folder.pick(start, (path) => {
      if (target === 'split-input') setSplitInput(path)
      else if (target === 'split-out') setSplitOut(path)
      else if (target === 'pack-input') setPackInput(path)
      else if (target === 'pack-out') setPackOut(path)
      else if (target === 'shard-dir') setShardDir(path)
      else setRoots((prev) => prev.map((x, j) => (j === index ? path : x)))
    })
  }

  useEffect(() => {
    api
      .defaults()
      .then((d: Defaults) => {
        setFormats(d.formats.length ? d.formats : ['parquet'])
        setQuality(d.jpeg_quality)
        setSubsampling(String(d.jpeg_subsampling))
        setReencode(d.reencode)
        setCompress(d.compress)
        setMaxRecords(d.max_records)
        setPackLayout(d.layout)
        setArtist(d.artist)
        setArtistDepth(d.artist_depth)
        setArtistInject(d.artist_inject)
        // 配置值只做占位提示，不预填：填进去就分不清"配置的"和"我改的"
        setCfgArtistTable(d.artist_table ?? '')
      })
      .catch(() => undefined)
  }, [])

  // 刷新页面后重新接上最近一次数据任务——拼接的对账看板值得留着看
  useEffect(() => {
    const saved = (() => {
      try {
        return localStorage.getItem(LAST_DATAJOB_KEY)
      } catch {
        return null
      }
    })()
    if (!saved) return
    api
      .dataJob(saved)
      .then((st) => setJob(st))
      .catch(() => {
        try {
          localStorage.removeItem(LAST_DATAJOB_KEY)
        } catch {
          // 忽略
        }
      })
  }, [])

  // 进行中的任务每秒轮询
  useEffect(() => {
    if (!job || (job.status !== 'pending' && job.status !== 'running')) return
    const timer = window.setInterval(() => {
      api
        .dataJob(job.id)
        .then((st) => setJob(st))
        .catch(() => undefined)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [job])

  const running = job !== null && (job.status === 'pending' || job.status === 'running')
  const inputFilled =
    mode === 'split' ? splitInput.trim() !== ''
      : mode === 'pack' ? packInput.trim() !== '' && formats.length > 0
        : roots.some((r) => r.trim() !== '')
  const canStart = !running && inputFilled && !busy

  const summary = job?.summary ?? null
  const splitSummary = mode === 'split' && job?.kind === 'split' ? (summary as SplitSummary | null) : null
  const mergeSummary = mode === 'merge' && job?.kind === 'merge' ? (summary as MergeSummary | null) : null
  const packSummary = mode === 'pack' && job?.kind === 'pack' ? (summary as PackSummary | null) : null

  const splitOutDefault = siblingPath(splitInput, '-dist')
  const splitTableDefault = siblingPath(splitInput, '-shards.json')
  const packOutDefault = siblingPath(packInput, '-tags')
  const excludeDefault = siblingPath(splitInput, '-dedup/dedup-exclude.txt')

  function buildParams(): Record<string, unknown> {
    if (mode === 'split') {
      return {
        input_dir: splitInput.trim(),
        out_dir: splitOut.trim(),
        table: splitTable.trim(),
        previous_table: previousTable.trim(),
        seq_width: previousTable.trim() ? null : splitSeqWidth,
        start_seq: startSeq,
        mode: splitMode,
        per_shard: Math.max(1, perShard),
        max_bytes: Math.round(maxBytesGB * 1073741824),
        layout: splitLayout,
        recursive: splitRecursive,
        exclude: exclude.trim(),
        verify_pack: splitVerify,
        artist_table: artistTable.trim(),
      }
    }
    if (mode === 'merge') {
      return {
        roots: roots.map((r) => r.trim()).filter(Boolean),
        shard_dir: shardDir.trim(),
        tables: mergeTable.split(/\r?\n/).map((p) => p.trim()).filter(Boolean),
        out: mergeOut.trim(),
        fmt: mergeFmt,
        require,
        dataset: dataset.trim(),
        sample_n: Math.max(0, sampleN),
        sample_out: sampleOut.trim(),
        keep_empty: keepEmpty,
      }
    }
    return {
      input_dir: packInput.trim(),
      out_dir: packOut.trim(),
      formats,
      jpeg_quality: quality,
      jpeg_subsampling: Number(subsampling),
      reencode,
      compress,
      max_records: Math.max(1, maxRecords),
      max_bytes: Math.round(packMaxBytesGB * 1073741824),
      layout: packLayout,
      assign_ids: assignIds === 'global',
      seq_width: packSeqWidth,
      start_seq: packStartSeq,
      artist,
      artist_depth: artistDepth,
      artist_inject: artistInject,
      artist_table: artistTable.trim(),
      check_space: checkSpace,
    }
  }

  async function start() {
    setBusy(true)
    setError(null)
    try {
      const st = await api.dataJobStart(mode, buildParams())
      setJob(st)
      setPreview(null)
      setPackPreview(null)
      setAutoExcluded(false)
      try {
        localStorage.setItem(LAST_DATAJOB_KEY, st.id)
      } catch {
        // 存不了就只在内存里记着
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    if (!job) return
    try {
      const res = await api.dataJobStop(job.id)
      if (res.snapshot) setJob(res.snapshot)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  // 分包预览：只扫路径结构不读字节。exclude 留空时顺手用猜的默认路径
  // 试探去重清单在不在——在且真排除了东西，就替用户填上。
  async function runSplitPreview() {
    if (!splitInput.trim()) return
    setPreviewing(true)
    setError(null)
    try {
      const probeExclude = exclude.trim() || excludeDefault
      const p = await api.dataJobPreview(
        splitInput.trim(), probeExclude, splitRecursive, artistTable.trim())
      setPreview(p)
      if (!exclude.trim() && p.excluded > 0) {
        setExclude(probeExclude)
        setAutoExcluded(true)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setPreviewing(false)
    }
  }

  // 封包预览：复用 /api/preview 的磁盘估算（传 formats 才会算产出估计）
  async function runPackPreview() {
    if (!packInput.trim()) return
    setPreviewing(true)
    setError(null)
    try {
      const p = await api.preview({
        path: packInput.trim(),
        layout: packLayout,
        artist,
        artist_depth: artistDepth,
        // 不传表的话，预览按目录名算、实际封包却查表 → 画师名与归档名对不上
        artist_table: artistTable.trim() || undefined,
        pack: assignIds === 'global',
        seq_width: packSeqWidth,
        start_seq: packStartSeq,
        out_dir: packOut.trim() || packOutDefault || undefined,
        formats: formats.join(','),
        jpeg_quality: quality,
        jpeg_subsampling: Number(subsampling),
        reencode,
      })
      setPackPreview(p)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setPreviewing(false)
    }
  }

  function toggleFormat(value: string) {
    setFormats((prev) =>
      prev.includes(value) ? prev.filter((f) => f !== value) : [...prev, value],
    )
  }

  const tone: StatusTone =
    job === null
      ? 'neutral'
      : job.status === 'done'
        ? 'success'
        : job.status === 'failed'
          ? 'danger'
          : job.status === 'stopped'
            ? 'warning'
            : 'accent'

  const rateText =
    job && job.rate > 0
      ? job.kind === 'split'
        ? `${job.rate.toFixed(2)} 包/秒`
        : `${fmtCount(job.rate)} 条/秒`
      : ''

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<Boxes size={17} />}
        eyebrow="数据打包"
        title="分包 · 拼接 · 封包"
        description="三条链路一次只跑一个"
        actions={
          <>
            {running ? (
              <button type="button" className="lx-btn-ghost" onClick={() => void stop()}>
                <Square size={14} className="mr-1.5 inline" />
                停止任务
              </button>
            ) : (
              <>
                {(mode === 'split' || mode === 'pack') && (
                  <button
                    type="button"
                    className="lx-btn-ghost"
                    disabled={!inputFilled || previewing}
                    onClick={() => void (mode === 'split' ? runSplitPreview() : runPackPreview())}
                  >
                    {previewing ? (
                      <Loader2 size={14} className="mr-1.5 inline animate-spin" />
                    ) : (
                      <ScanSearch size={14} className="mr-1.5 inline" />
                    )}
                    预览
                  </button>
                )}
                <button
                  type="button"
                  className="lx-btn-primary lx-launch-glow inline-flex items-center gap-1.5"
                  disabled={!canStart}
                  onClick={() => void start()}
                >
                  <Play size={14} />
                  {busy ? '创建中…' : `开始${KIND_LABEL[mode]}`}
                </button>
              </>
            )}
          </>
        }
      />

      {error && <ErrorBox error={error} title="操作失败" />}

      <SectionPanel title="做什么">
        <ChoiceGrid
          options={MODES}
          value={mode}
          onChange={setMode}
          className="grid-cols-3"
          glyph={(v, active) =>
            v === 'split' ? (
              <Layers size={13} className={active ? 'text-lx-accent' : 'text-lx-dim'} />
            ) : v === 'merge' ? (
              <ListChecks size={13} className={active ? 'text-lx-accent' : 'text-lx-dim'} />
            ) : (
              <Package size={13} className={active ? 'text-lx-accent' : 'text-lx-dim'} />
            )
          }
          renderLabel={(v) =>
            v === 'split' ? '分包（素材 → dist 包）' : v === 'merge' ? '拼接（产物 → 总表）' : '封包（tags → 成品）'}
          ariaLabel="数据打包模式"
          disabled={running}
        />
      </SectionPanel>

      <div className="grid gap-3.5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="flex min-w-0 flex-col gap-3.5">
          {mode === 'split' && (
            <SectionPanel title="分包：素材 → 分发包 + 分包表">
              <SettingRow
                label="素材目录"
                help="装图的文件夹或单个大 zip；image_id 在这里一次性分配"
              >
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-xs"
                    value={splitInput}
                    onChange={(e) => setSplitInput(e.target.value)}
                    placeholder="D:\batch-07 或 D:\batch-07\shard-0001.zip"
                    spellCheck={false}
                    disabled={running}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-3"
                    onClick={() => openPicker('split-input')}
                  >
                    <FolderOpen size={14} className="mr-1 inline" />
                    浏览
                  </button>
                </div>
              </SettingRow>

              <SettingRow
                label="切分方式"
                help="按字节切分的包大小更均匀，便于传输"
              >
                <div className="flex items-center gap-3">
                  <ChoiceGrid
                    options={SPLIT_MODES}
                    value={splitMode}
                    onChange={setSplitMode}
                    className="w-44 grid-cols-2"
                    size="sm"
                    renderLabel={(v) => (v === 'count' ? '按张数' : '按字节')}
                    ariaLabel="切分方式"
                    disabled={running}
                  />
                  {splitMode === 'count' ? (
                    <input
                      type="number"
                      className="lx-input w-28 font-mono tabular-nums"
                      min={1}
                      value={perShard}
                      onChange={(e) => setPerShard(Math.max(1, Number(e.target.value) || 1))}
                      disabled={running}
                      aria-label="每包张数"
                    />
                  ) : (
                    <div className="flex items-center gap-2">
                      <input
                        type="number"
                        className="lx-input w-24 font-mono tabular-nums"
                        min={0}
                        step={10}
                        value={maxBytesGB}
                        onChange={(e) => setMaxBytesGB(Math.max(0, Number(e.target.value) || 0))}
                        disabled={running}
                        aria-label="每包大小"
                      />
                      <span className="shrink-0 text-[11px] text-lx-muted">GB</span>
                    </div>
                  )}
                </div>
              </SettingRow>

              <SettingRow
                label="画师表"
                help="parquet、分片目录，或 hf://datasets/<repo>/<路径>；留空跟配置走"
                description={
                  artistTable.trim()
                    ? '按 post id 查画师，查不到退回目录名'
                    : cfgArtistTable
                      ? `配置：${cfgArtistTable}`
                      : '未配置，只从目录名取画师'
                }
              >
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-xs"
                    value={artistTable}
                    onChange={(e) => setArtistTable(e.target.value)}
                    placeholder={cfgArtistTable || 'K:/anlia/_artist/danbooru_artists.parquet'}
                    disabled={running}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-3"
                    disabled={running}
                    onClick={() =>
                      folder.pick(artistTable || undefined, setArtistTable, ['parquet'])
                    }
                  >
                    浏览
                  </button>
                </div>
              </SettingRow>

              <SettingRow
                label="去重清单"
                help="填了重复图就不进包、不占 image_id"
              >
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-xs"
                    value={exclude}
                    onChange={(e) => {
                      setExclude(e.target.value)
                      setAutoExcluded(false)
                    }}
                    placeholder={`留空 = 不排除（检测：${excludeDefault || '…'}）`}
                    spellCheck={false}
                    disabled={running}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-3"
                    disabled={running}
                    onClick={() => folder.pick(exclude || undefined, setExclude, ['json'])}
                  >
                    浏览
                  </button>
                </div>
              </SettingRow>

              <SettingRow label="上一批分包表" help="首批留空；后续批次填上一批的表，自动衔接编号">
                <div className="flex gap-2">
                  <input className="lx-input font-mono text-xs" value={previousTable}
                    onChange={(e) => setPreviousTable(e.target.value)} disabled={running}
                    placeholder="上一批的 shards.json" spellCheck={false} />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-3"
                    disabled={running}
                    onClick={() =>
                      folder.pick(previousTable || undefined, setPreviousTable, ['json'])
                    }
                  >
                    浏览
                  </button>
                </div>
              </SettingRow>
              {!previousTable.trim() && (
                <>
                  <SettingRow label="序号宽度" help="6 位最多 999999 张；上十亿要选 10 位，从第一批起保持一致">
                    <input type="number" className="lx-input w-28 font-mono" min={1} max={12}
                      value={splitSeqWidth} disabled={running}
                      onChange={(e) => setSplitSeqWidth(Math.min(12, Math.max(1, Math.trunc(Number(e.target.value)) || 6)))} />
                  </SettingRow>
                  <SettingRow label="起始序号" help="新数据集从 1 开始；续批请填上面的上一批分包表">
                    <input type="number" className="lx-input font-mono" min={1} max={999999999999}
                      value={startSeq} disabled={running}
                      onChange={(e) => setStartSeq(Math.min(999999999999, Math.max(1, Math.trunc(Number(e.target.value)) || 1)))} />
                  </SettingRow>
                </>
              )}

              <details className="px-1">
                <summary className="cursor-pointer text-xs text-lx-muted">高级</summary>
                <div className="mt-3 space-y-4">
                  <SettingRow label="输出目录" help="默认为素材旁的 <名字>-dist/">
                    <div className="flex gap-2">
                      <input
                        className="lx-input font-mono text-xs"
                        value={splitOut}
                        onChange={(e) => setSplitOut(e.target.value)}
                        placeholder={splitOutDefault || '<素材名>-dist'}
                        spellCheck={false}
                        disabled={running}
                      />
                      <button
                        type="button"
                        className="lx-btn-ghost shrink-0 px-3"
                        disabled={running}
                        onClick={() => openPicker('split-out')}
                      >
                        <FolderOpen size={14} className="mr-1 inline" />
                        浏览
                      </button>
                    </div>
                  </SettingRow>
                  <SettingRow
                    label="分包表"
                    help="拼接对账的凭证；同名表会被拒"
                  >
                    <input
                      className="lx-input font-mono text-xs"
                      value={splitTable}
                      onChange={(e) => setSplitTable(e.target.value)}
                      placeholder={splitTableDefault || '<素材名>-shards.json'}
                      spellCheck={false}
                      disabled={running}
                    />
                  </SettingRow>
                  <SettingRow label="包内布局" help="flat 全平铺；mirror 保留原目录层级">
                    <ChoiceGrid
                      options={LAYOUTS}
                      value={splitLayout}
                      onChange={setSplitLayout}
                      className="grid-cols-2"
                      size="sm"
                      mono
                      ariaLabel="包内布局"
                      disabled={running}
                    />
                  </SettingRow>
                  <ToggleRow
                    checked={splitRecursive}
                    onChange={setSplitRecursive}
                    label="包含子目录"
                    hint="关掉只扫第一层"
                    disabled={running}
                  />
                  <ToggleRow
                    checked={splitVerify}
                    onChange={setSplitVerify}
                    label="写完回读校验"
                    hint="逐包回读，核对首尾 id 与条数"
                    disabled={running}
                  />
                </div>
              </details>
            </SectionPanel>
          )}

          {mode === 'split' && preview && (
            <SectionPanel
              title="分包预览"
              trailing={
                <StatusChip tone="neutral">
                  {fmtCount(preview.total)} 张 · {fmtBytes(preview.bytes)}
                </StatusChip>
              }
            >
              <OverviewFigureRow className="grid-cols-3">
                <OverviewFigure label="发现" value={fmtCount(preview.total)} note="张" first />
                <OverviewFigure
                  label="画师"
                  value={String(preview.artists.count)}
                  note={`覆盖 ${fmtCount(preview.artists.covered)} 张`}
                  tone={preview.artists.missing > 0 ? 'warning' : 'success'}
                />
                <OverviewFigure
                  label="去重排除"
                  value={fmtCount(preview.excluded)}
                  note="张"
                  tone={preview.excluded > 0 ? 'warning' : undefined}
                />
              </OverviewFigureRow>
              {preview.artists.missing > 0 && (
                <p className="mt-2.5 flex items-center gap-1.5 text-[11px] text-lx-warning">
                  <TriangleAlert size={12} className="shrink-0" />
                  {fmtCount(preview.artists.missing)} 张取不到画师名（通常是没有画师目录层），id 里会用占位名。
                </p>
              )}
              {autoExcluded && (
                <p className="mt-2.5 text-[11px] text-lx-accent">
                  已自动填上去重清单：{exclude}
                </p>
              )}
              {preview.sources.length > 0 && (
                <div className="lx-sheet-inset mt-3 max-h-40 overflow-y-auto p-2.5">
                  {preview.sources.map((s, i) => (
                    <div key={i} className="flex items-center justify-between gap-2 text-[11px]">
                      <span className="min-w-0 truncate font-mono text-lx-muted" title={s.name}>
                        {s.name}
                      </span>
                      <span className="shrink-0 tabular-nums text-lx-dim">{fmtCount(s.count)} 张</span>
                    </div>
                  ))}
                </div>
              )}
            </SectionPanel>
          )}

          {mode === 'merge' && (
            <SectionPanel title="拼接：回收目录 → 对账 → 总表">
              <SettingRow
                label="回收目录"
                help="装 -tag.json / -nl.json 的目录，可多个"
                icon={<FolderSearch size={14} />}
              >
                <div className="space-y-2">
                  {roots.map((r, i) => (
                    <div key={i} className="flex gap-2">
                      <input
                        className="lx-input font-mono text-xs"
                        value={r}
                        onChange={(e) =>
                          setRoots((prev) => prev.map((x, j) => (j === i ? e.target.value : x)))
                        }
                        placeholder={`第 ${i + 1} 个回收目录`}
                        spellCheck={false}
                        disabled={running}
                      />
                      <button
                        type="button"
                        className="lx-btn-ghost shrink-0 px-2.5"
                        onClick={() => openPicker('merge-root', i)}
                        aria-label="浏览目录"
                      >
                        <FolderOpen size={14} />
                      </button>
                      <button
                        type="button"
                        className="lx-btn-ghost shrink-0 px-2.5"
                        disabled={roots.length <= 1 || running}
                        onClick={() => setRoots((prev) => prev.filter((_, j) => j !== i))}
                        aria-label="删除这一行"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className="lx-btn-ghost text-xs"
                    disabled={running}
                    onClick={() => setRoots((prev) => [...prev, ''])}
                  >
                    <Plus size={13} className="mr-1 inline" />
                    添加目录
                  </button>
                </div>
              </SettingRow>

              <SettingRow
                label="产物要求"
                help="both = tag 和 NL 都有才算完整"
              >
                <div className="flex items-center gap-3">
                  <ChoiceGrid
                    options={REQUIRES}
                    value={require}
                    onChange={setRequire}
                    className="w-52 grid-cols-3"
                    size="sm"
                    renderLabel={(v) => (v === 'nl' ? '只要 NL' : v === 'tag' ? '只要 tag' : '两者都要')}
                    ariaLabel="产物要求"
                    disabled={running}
                  />
                  <ChoiceGrid
                    options={MERGE_FMTS}
                    value={mergeFmt}
                    onChange={setMergeFmt}
                    className="w-32 grid-cols-2"
                    size="sm"
                    mono
                    ariaLabel="总表格式"
                    disabled={running}
                  />
                </div>
              </SettingRow>

              <SettingRow
                label="抽检张数"
                help="随机抽 N 条做人工比对；0 = 不抽"
              >
                <input
                  type="number"
                  className="lx-input w-24 font-mono tabular-nums"
                  min={0}
                  value={sampleN}
                  onChange={(e) => setSampleN(Math.max(0, Number(e.target.value) || 0))}
                  disabled={running}
                />
              </SettingRow>

              <details className="px-1">
                <summary className="cursor-pointer text-xs text-lx-muted">高级</summary>
                <div className="mt-3 space-y-4">
                  <SettingRow label="分发包目录" help="dist-*.zip 所在目录；给了才能对账整包缺失">
                    <div className="flex gap-2">
                      <input
                        className="lx-input font-mono text-xs"
                        value={shardDir}
                        onChange={(e) => setShardDir(e.target.value)}
                        placeholder="留空 = 跳过逐包对账"
                        spellCheck={false}
                        disabled={running}
                      />
                      <button
                        type="button"
                        className="lx-btn-ghost shrink-0 px-3"
                        onClick={() => openPicker('shard-dir')}
                      >
                        <FolderOpen size={14} className="mr-1 inline" />
                        浏览
                      </button>
                    </div>
                  </SettingRow>
                  <SettingRow label="分包总表" help="每行一份分包表，多批一起对账">
                    <textarea
                      className="lx-input font-mono text-xs"
                      value={mergeTable}
                      onChange={(e) => setMergeTable(e.target.value)}
                      placeholder="<素材名>-shards.json"
                      rows={3}
                      spellCheck={false}
                      disabled={running}
                    />
                  </SettingRow>
                  <SettingRow label="总表输出" help="默认 work/merged.jsonl">
                    <input
                      className="lx-input font-mono text-xs"
                      value={mergeOut}
                      onChange={(e) => setMergeOut(e.target.value)}
                      placeholder="work/merged.jsonl"
                      spellCheck={false}
                      disabled={running}
                    />
                  </SettingRow>
                  <SettingRow label="抽检清单" help="默认 work/sample.md">
                    <input
                      className="lx-input font-mono text-xs"
                      value={sampleOut}
                      onChange={(e) => setSampleOut(e.target.value)}
                      placeholder="work/sample.md"
                      spellCheck={false}
                      disabled={running}
                    />
                  </SettingRow>
                  <SettingRow label="数据集名" help="总表 image_path 的前缀；留空用配置默认">
                    <input
                      className="lx-input font-mono text-xs"
                      value={dataset}
                      onChange={(e) => setDataset(e.target.value)}
                      placeholder="留空 = 用配置"
                      spellCheck={false}
                      disabled={running}
                    />
                  </SettingRow>
                  <ToggleRow
                    checked={keepEmpty}
                    onChange={setKeepEmpty}
                    label="保留不完整记录"
                    hint="开着会把缺字段的也写进总表"
                    disabled={running}
                  />
                </div>
              </details>
            </SectionPanel>
          )}

          {mode === 'pack' && (
            <SectionPanel title="封包：tags 产物 → 训练分片">
              <SettingRow
                label="图片目录"
                help="打标时的素材目录，封包要重读原图"
                icon={<Package size={14} />}
              >
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-xs"
                    value={packInput}
                    onChange={(e) => setPackInput(e.target.value)}
                    placeholder="D:\batch-07（打标时的输入）"
                    spellCheck={false}
                    disabled={running}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-3"
                    onClick={() => openPicker('pack-input')}
                  >
                    <FolderOpen size={14} className="mr-1 inline" />
                    浏览
                  </button>
                </div>
              </SettingRow>

              <SettingRow
                label="产物目录"
                help="打标输出目录，要含 tags/；成品 pack-* 落在一起"
              >
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-xs"
                    value={packOut}
                    onChange={(e) => setPackOut(e.target.value)}
                    placeholder={packOutDefault || '<素材名>-tags'}
                    spellCheck={false}
                    disabled={running}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-3"
                    disabled={running}
                    onClick={() => openPicker('pack-out')}
                  >
                    <FolderOpen size={14} className="mr-1 inline" />
                    浏览
                  </button>
                </div>
              </SettingRow>

              <SettingRow label="格式" help="可多选">
                <div className="grid grid-cols-3 gap-2">
                  {FORMATS.map((f) => {
                    const active = formats.includes(f.value)
                    return (
                      <button
                        key={f.value}
                        type="button"
                        disabled={running}
                        onClick={() => toggleFormat(f.value)}
                        aria-pressed={active}
                        className={`rounded-lx-sm border px-2 py-2 text-center transition-colors ${
                          active
                            ? 'border-lx-accent bg-lx-accent-subtle text-lx'
                            : 'border-lx bg-lx-surface-raised text-lx-muted hover:text-lx'
                        } ${running ? 'cursor-not-allowed opacity-50' : ''}`}
                      >
                        <div className="font-mono text-xs">{f.label}</div>
                        <div className="mt-0.5 text-[10px] text-lx-dim">{f.note}</div>
                      </button>
                    )
                  })}
                </div>
              </SettingRow>

              <SettingRow
                label="重编码"
                help="仅非 JPEG = 原图 JPEG 直通不二压"
              >
                <ChoiceGrid
                  options={REENCODES}
                  value={reencode}
                  onChange={setReencode}
                  className="grid-cols-3"
                  size="sm"
                  disabled={running}
                  renderLabel={(v) =>
                    v === 'always' ? '总是转码' : v === 'non_jpeg' ? '仅非 JPEG' : '保留原字节'}
                  ariaLabel="重编码"
                />
              </SettingRow>

              <SettingRow
                label="JPEG 质量"
                description={
                  reencode === 'never'
                    ? '保留原字节，此项不生效'
                    : quality < 92
                      ? '低于 92 有可见块效应'
                      : quality >= 98
                        ? '体积最大，收益有限'
                        : undefined
                }
                help="所有图按这个质量重压；90~100"
                icon={<Palette size={14} />}
              >
                <div className="flex items-center gap-3">
                  <input
                    type="range"
                    className="lx-slider"
                    min={90}
                    max={100}
                    step={1}
                    value={quality}
                    disabled={running || reencode === 'never'}
                    onChange={(e) => setQuality(Number(e.target.value))}
                    aria-label="JPEG 质量"
                  />
                  <span
                    className={`w-10 shrink-0 text-right font-mono text-sm tabular-nums ${
                      quality < 92 ? 'text-lx-warning' : 'text-lx'
                    }`}
                  >
                    {quality}
                  </span>
                </div>
              </SettingRow>

              <SettingRow
                label="色度采样"
                description={
                  reencode === 'never'
                    ? '保留原字节，此项不生效'
                    : subsampling === '0'
                      ? '体积约为 4:2:0 的两倍'
                      : subsampling === '1'
                        ? '横向砍半，细线有色晕'
                        : '体积最小，饱和色/细线有色晕'
                }
                help="4:4:4 最保真，4:2:0 体积最小"
              >
                <ChoiceGrid
                  options={SUBSAMPLING}
                  value={subsampling as (typeof SUBSAMPLING)[number]}
                  onChange={setSubsampling}
                  className="grid-cols-3"
                  size="sm"
                  mono
                  disabled={running || reencode === 'never'}
                  renderLabel={(v) => SUBSAMPLING_LABEL[v]}
                  ariaLabel="色度采样"
                />
              </SettingRow>

              <SettingRow
                label="编号方式"
                description="必须与打标那次一致，否则找不到产物"
                help="跟随目录 = 本机打的标；全局编号 = 成员打回来的"
              >
                <ChoiceGrid
                  options={ID_SCHEMES}
                  value={assignIds}
                  onChange={setAssignIds}
                  className="grid-cols-2"
                  size="sm"
                  disabled={running}
                  renderLabel={(v) => (v === 'rel' ? '跟随目录（本机打标）' : '全局编号（分包表产物）')}
                  ariaLabel="编号方式"
                />
              </SettingRow>

              {assignIds === 'global' && (
                <SettingRow label="本批编号" help="宽度与起点要和分包表的 seq_width、start_seq 一致">
                  <div className="flex gap-2">
                    <input type="number" className="lx-input w-24 font-mono" min={1} max={12}
                      aria-label="封包序号宽度" value={packSeqWidth} disabled={running}
                      onChange={(e) => setPackSeqWidth(Math.min(12, Math.max(1, Math.trunc(Number(e.target.value)) || 6)))} />
                    <input type="number" className="lx-input font-mono" min={1} max={999999999999}
                      aria-label="封包起始序号" value={packStartSeq} disabled={running}
                      onChange={(e) => setPackStartSeq(Math.min(999999999999, Math.max(1, Math.trunc(Number(e.target.value)) || 1)))} />
                  </div>
                </SettingRow>
              )}

              <SettingRow
                label="画师三件套"
                description="要与打标时一致，否则指纹会拦住重封"
                help="画师名写进归档名；注入写进 caption 正文"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <ToggleRow checked={artist} onChange={setArtist} label="启用画师名" />
                  <label className="flex items-center gap-1.5 text-[11px] text-lx-muted">
                    深度
                    <input
                      type="number"
                      className="lx-input w-16 font-mono tabular-nums"
                      min={0}
                      value={artistDepth}
                      onChange={(e) => setArtistDepth(Math.max(0, Number(e.target.value) || 0))}
                      disabled={running || !artist}
                    />
                  </label>
                  <ToggleRow
                    checked={artistInject}
                    onChange={setArtistInject}
                    label="写进 caption"
                    disabled={!artist}
                  />
                </div>
              </SettingRow>

              <SettingRow
                label="画师表"
                help="parquet、分片目录，或 hf://datasets/<repo>/<路径>；留空跟配置走"
                description={
                  artistTable.trim()
                    ? '按 post id 查画师，查不到退回目录名'
                    : cfgArtistTable
                      ? `配置：${cfgArtistTable}`
                      : '未配置，只从目录名取画师'
                }
              >
                <div className="flex gap-2">
                  <input
                    className="lx-input font-mono text-xs"
                    value={artistTable}
                    onChange={(e) => setArtistTable(e.target.value)}
                    placeholder={cfgArtistTable || 'K:/anlia/_artist/shards'}
                    disabled={running || !artist}
                  />
                  <button
                    type="button"
                    className="lx-btn-ghost shrink-0 px-3"
                    disabled={running || !artist}
                    onClick={() =>
                      folder.pick(artistTable || undefined, setArtistTable, ['parquet'])
                    }
                  >
                    浏览
                  </button>
                </div>
              </SettingRow>

              <SettingRow label="每包条数" help="写满就封口">
                <input
                  type="number"
                  className="lx-input font-mono tabular-nums"
                  min={1}
                  value={maxRecords}
                  disabled={running}
                  onChange={(e) => setMaxRecords(Math.max(1, Number(e.target.value) || 1))}
                />
              </SettingRow>

              <details className="px-1">
                <summary className="cursor-pointer text-xs text-lx-muted">高级</summary>
                <div className="mt-3 space-y-4">
                  <SettingRow label="每包大小上限" help="0 = 不限；适合图片体积差异大的库">
                    <div className="flex items-center gap-2">
                      <input
                        type="number"
                        className="lx-input font-mono tabular-nums"
                        min={0}
                        step={0.5}
                        value={packMaxBytesGB}
                        disabled={running}
                        onChange={(e) => setPackMaxBytesGB(Math.max(0, Number(e.target.value) || 0))}
                      />
                      <span className="shrink-0 text-[11px] text-lx-muted">GB</span>
                    </div>
                  </SettingRow>
                  <SettingRow label="zip 压缩" help="仅对 zip 分片生效；deflate 通常省不到 5%" icon={<Archive size={14} />}>
                    <ChoiceGrid
                      options={COMPRESS}
                      value={compress}
                      onChange={setCompress}
                      className="grid-cols-2"
                      size="sm"
                      mono
                      disabled={running}
                      ariaLabel="压缩方式"
                    />
                  </SettingRow>
                  <SettingRow label="包内布局" help="flat 全平铺；mirror 保留原目录层级">
                    <ChoiceGrid
                      options={LAYOUTS}
                      value={packLayout}
                      onChange={setPackLayout}
                      className="grid-cols-2"
                      size="sm"
                      mono
                      disabled={running}
                      ariaLabel="包内布局"
                    />
                  </SettingRow>
                  <ToggleRow
                    checked={checkSpace}
                    onChange={setCheckSpace}
                    label="盘位预算"
                    hint="按预计产出 + 20% 冗余判断，写不下直接拒绝"
                    disabled={running}
                  />
                </div>
              </details>

              {packPreview?.estimate && (
                <div
                  className={`mt-4 rounded-lx-sm border px-2.5 py-2 text-[11px] leading-relaxed ${
                    packPreview.estimate.ok
                      ? 'border-lx bg-lx-surface-raised text-lx-secondary'
                      : 'border-lx-danger bg-lx-danger-subtle text-lx-danger'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5">
                      <HardDrive size={11} className="shrink-0" />
                      预计写入（含 20% 冗余）
                    </span>
                    <span className="font-mono tabular-nums">
                      {fmtBytes(packPreview.estimate.bytes)}
                    </span>
                  </div>
                  <div className="mt-0.5 flex items-center justify-between gap-2 text-lx-muted">
                    <span>目标盘剩余</span>
                    <span className="font-mono tabular-nums">
                      {fmtBytes(packPreview.estimate.free)}
                    </span>
                  </div>
                  {!packPreview.estimate.ok && (
                    <p className="mt-1">
                      {checkSpace
                        ? '剩余空间不够：清盘，或降质量 / 换 4:2:0 / 少选格式。'
                        : '剩余空间不够，但盘位预算已关闭：照写，满了才报错。'}
                    </p>
                  )}
                </div>
              )}
            </SectionPanel>
          )}

          {mode === 'pack' && packPreview && (
            <SectionPanel
              title="封包预览"
              trailing={
                <StatusChip tone="neutral">
                  {fmtCount(packPreview.total)} 张 · {fmtBytes(packPreview.bytes)}
                </StatusChip>
              }
            >
              <OverviewFigureRow className="grid-cols-3">
                <OverviewFigure label="发现" value={fmtCount(packPreview.total)} note="张" first />
                <OverviewFigure
                  label="画师"
                  value={String(packPreview.artists.count)}
                  note={`覆盖 ${fmtCount(packPreview.artists.covered)} 张`}
                  tone={packPreview.artists.missing > 0 ? 'warning' : 'success'}
                />
                <OverviewFigure label="来源" value={String(packPreview.source_count ?? packPreview.sources.length)} note="个" />
              </OverviewFigureRow>
            </SectionPanel>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-3.5">
          {job && (
            <SectionPanel
              title={`${KIND_LABEL[job.kind]}进度`}
              trailing={<StatusChip tone={tone}>{STATUS_LABEL[job.status] ?? job.status}</StatusChip>}
            >
              {job.status === 'running' || job.status === 'pending' ? (
                <>
                  {job.total > 0 ? (
                    <>
                      <div className="mb-1.5 flex items-baseline justify-between gap-2 text-[10.5px] text-lx-muted">
                        <span className="tabular-nums">{Math.round((job.done / job.total) * 100)}%</span>
                        <span className="tabular-nums">
                          {fmtCount(job.done)}/{fmtCount(job.total)}
                        </span>
                      </div>
                      <ProgressBar
                        percent={(job.done / job.total) * 100}
                        tone="accent"
                      />
                    </>
                  ) : (
                    <div className="mb-1.5 flex items-baseline justify-between gap-2 text-[10.5px] text-lx-muted">
                      <span className="tabular-nums">已扫 {fmtCount(job.done)} 条</span>
                      <span>总量未知</span>
                    </div>
                  )}
                  <div className="mt-2 space-y-0.5 text-[11px] text-lx-muted">
                    {rateText && (
                      <p className="tabular-nums">
                        {rateText}
                        {job.eta_s > 1 &&
                          ` · 预计还要 ${fmtSeconds(job.eta_s)}`}
                      </p>
                    )}
                    {job.current && (
                      <p className="truncate font-mono text-lx-dim" title={job.current}>
                        {job.current}
                      </p>
                    )}
                    <p className="flex items-center gap-1.5">
                      <RefreshCw size={11} className="animate-spin" />
                      打标任务可以照常跑，数据打包不占 GPU
                    </p>
                  </div>
                </>
              ) : (
                <p className="text-[11px] text-lx-muted">
                  {job.status === 'failed' ? job.error || '任务失败' : '没有正在进行的任务。'}
                </p>
              )}

              {job.status === 'stopped' && (
                <p className="mt-2 rounded-lx-sm bg-lx-warning-subtle px-2.5 py-2 text-[11px] leading-relaxed text-lx-warning">
                  已停止：已写盘的部分保留，重跑从头开始
                </p>
              )}

              {job.logs.length > 0 && (
                <div className="lx-sheet-inset mt-3 max-h-40 overflow-y-auto p-2">
                  {job.logs.map((line, i) => (
                    <p key={i} className="font-mono text-[10.5px] leading-relaxed text-lx-muted">
                      {line}
                    </p>
                  ))}
                </div>
              )}
            </SectionPanel>
          )}

          {splitSummary && <SplitResult s={splitSummary} />}
          {mergeSummary && <MergeResult s={mergeSummary} />}
          {packSummary && <PackResult s={packSummary} />}

          {!job && (
            <SectionPanel title="怎么用">
              {mode === 'split' && (
                <ol className="list-decimal space-y-2 pl-5 text-[11.5px] leading-relaxed text-lx-secondary">
                  <li>先去「去重」页扫一遍拿排除清单。</li>
                  <li>填素材目录，预览确认张数。</li>
                  <li>开始分包，产出 dist-*.zip 与分包表；分包表要留好。</li>
                  <li>把包发给成员打标，产物收回来去「拼接」。</li>
                </ol>
              )}
              {mode === 'merge' && (
                <ol className="list-decimal space-y-2 pl-5 text-[11.5px] leading-relaxed text-lx-secondary">
                  <li>把成员交回的产物目录（含 tags/ 与 nl/）逐个一行填进回收目录。</li>
                  <li>分发包目录与总表选填，给了才能发现整包没交。</li>
                  <li>开始拼接，对账看板会列出缺失与冲突。</li>
                  <li>总表落盘后回到「封包」出训练分片。</li>
                </ol>
              )}
              {mode === 'pack' && (
                <ol className="list-decimal space-y-2 pl-5 text-[11.5px] leading-relaxed text-lx-secondary">
                  <li>图片目录填打标时的素材目录，产物目录填打标输出（要含 tags/）。</li>
                  <li>编号方式、格式、质量要与打标那次一致，否则指纹会拦住重封。</li>
                  <li>预览可看盘位预算。</li>
                  <li>成品 pack-* 落在产物目录，去「回传」页上传。</li>
                </ol>
              )}
            </SectionPanel>
          )}
        </div>
      </div>

      {folder.dialog}
    </div>
  )
}

// ------------------------------------------------------------------ 结果面板

function SplitResult({ s }: { s: SplitSummary }) {
  return (
    <SectionPanel
      title="分包结果"
      trailing={
        <StatusChip tone={s.verify && s.verify.bad > 0 ? 'danger' : 'success'}>
          {s.verify ? (s.verify.bad > 0 ? `校验 ${s.verify.bad} 包异常` : '校验全部一致') : '未校验'}
        </StatusChip>
      }
    >
      <OverviewFigureRow className="grid-cols-3">
        <OverviewFigure label="分包" value={fmtCount(s.package_count)} note="个" first />
        <OverviewFigure label="进包" value={fmtCount(s.total)} note="张" tone="success" />
        <OverviewFigure
          label="排除"
          value={fmtCount(s.excluded)}
          note={s.no_artist > 0 ? `· ${s.no_artist} 张无画师名` : '张'}
          tone={s.excluded > 0 ? 'warning' : undefined}
        />
      </OverviewFigureRow>

      {s.packages.length > 0 && (
        <div className="lx-sheet-inset mt-3 max-h-56 overflow-y-auto p-2.5">
          {s.packages.map((p) => (
            <div key={p.name} className="flex items-baseline justify-between gap-2 py-0.5 text-[11px]">
              <span className="shrink-0 font-mono text-lx-secondary">{p.name}</span>
              <span className="min-w-0 flex-1 truncate px-2 text-right font-mono text-[10px] text-lx-dim">
                {p.first_id} ~ {p.last_id}
              </span>
              <span className="shrink-0 tabular-nums text-lx-muted">
                {fmtCount(p.count)} 张 · {fmtBytes(p.bytes)}
              </span>
            </div>
          ))}
          {s.package_count > s.packages.length && (
            <p className="pt-1 text-[10.5px] text-lx-dim">…共 {fmtCount(s.package_count)} 个包（详见分包表）</p>
          )}
        </div>
      )}

      <div className="mt-2.5 space-y-1.5 border-t border-lx pt-2.5">
        {s.next_seq !== undefined && (
          <p className="text-[11px] text-lx-muted">
            序号宽度 {s.seq_width} 位 · 下一批起始序号 {s.next_seq}；续批时填写下方分包表路径。
          </p>
        )}
        <p className="flex items-start gap-1.5 text-[11px] text-lx-secondary">
          <FileJson size={12} className="mt-0.5 shrink-0 text-lx-accent" />
          <span className="min-w-0 break-all font-mono text-lx-muted" title={s.table_path}>
            分包表 {s.table_path}
          </span>
        </p>
        <p className="rounded-lx-sm bg-lx-accent-subtle px-2.5 py-2 text-[11px] leading-relaxed text-lx-secondary">
          包在 <code className="font-mono text-lx-accent">{s.out_dir}</code>。产物收回来后到「拼接」对账。
        </p>
      </div>
    </SectionPanel>
  )
}

function MergeResult({ s }: { s: MergeSummary }) {
  const ratePct =
    s.expected > 0 ? Math.round((s.complete / s.expected) * 100) : null
  const problems: { tone: 'danger' | 'warning'; title: string; lines: string[] }[] = []
  if (s.whole_missing.length > 0) {
    problems.push({
      tone: 'danger',
      title: `整包没交 · ${s.whole_missing.length} 个包`,
      lines: s.whole_missing.map(
        (c) => `${c.name}（${c.first_id} ~ ${c.last_id}，应 ${c.expected} 条）`,
      ),
    })
  }
  if (s.short.length > 0) {
    problems.push({
      tone: 'warning',
      title: `分包不足 · ${s.short.length} 个包`,
      lines: s.short.map((c) => `${c.name}：有 ${c.got} / 应 ${c.expected}`),
    })
  }
  if (s.empty_roots.length > 0) {
    problems.push({ tone: 'danger', title: `整份没交 · ${s.empty_roots.length} 个目录`, lines: s.empty_roots })
  }
  if (s.duplicates_n > 0) {
    problems.push({
      tone: 'danger',
      title: `内容冲突 · ${s.duplicates_n} 个 id`,
      lines: Object.entries(s.duplicates).map(
        ([id, files]) => `${id}：${files.join(' | ')}`,
      ),
    })
  }
  if (s.unreadable_n > 0) {
    problems.push({ tone: 'warning', title: `读不动 · ${s.unreadable_n} 个文件`, lines: s.unreadable })
  }
  if (s.extra_n > 0 || s.unassigned_n > 0) {
    problems.push({
      tone: 'warning',
      title: '账外记录',
      lines: [
        s.extra_n > 0 ? `${fmtCount(s.extra_n)} 个 id 不在分包表里（多出的产出，不算错）` : '',
        s.unassigned_n > 0 ? `${fmtCount(s.unassigned_n)} 个 id 落在所有分包区间之外（表与实际对不上）` : '',
      ].filter(Boolean),
    })
  }
  if (s.prov_warnings.length > 0) {
    problems.push({ tone: 'warning', title: '溯源不一致', lines: s.prov_warnings })
  }

  return (
    <SectionPanel
      title="对账看板"
      trailing={
        <StatusChip tone={s.failed ? 'danger' : 'success'}>
          {s.failed ? '有缺口，先裁决再训练' : '账目齐全'}
        </StatusChip>
      }
    >
      <OverviewFigureRow className="grid-cols-2 sm:grid-cols-4">
        <OverviewFigure
          label="完整率"
          value={ratePct !== null ? `${ratePct}%` : '—'}
          note={s.expected > 0 ? `应 ${fmtCount(s.expected)}` : '无分包表'}
          tone={ratePct !== null && ratePct < 100 ? 'warning' : 'success'}
          first
        />
        <OverviewFigure label="已拼接" value={fmtCount(s.written)} note="条进总表" tone="success" />
        <OverviewFigure
          label="整条缺失"
          value={fmtCount(s.missing_n)}
          tone={s.missing_n > 0 ? 'danger' : undefined}
        />
        <OverviewFigure
          label="内容冲突"
          value={fmtCount(s.duplicates_n)}
          tone={s.duplicates_n > 0 ? 'danger' : undefined}
        />
      </OverviewFigureRow>

      {(s.missing_nl_n > 0 || s.missing_tag_n > 0 || s.empty_content_n > 0 || s.idempotent_n > 0) && (
        <p className="mt-2.5 text-[11px] tabular-nums text-lx-muted">
          {s.missing_nl_n > 0 && `缺 NL ${fmtCount(s.missing_nl_n)} · `}
          {s.missing_tag_n > 0 && `缺 tag ${fmtCount(s.missing_tag_n)} · `}
          {s.empty_content_n > 0 && `内容为空 ${fmtCount(s.empty_content_n)} · `}
          {s.idempotent_n > 0 && `幂等重跑 ${fmtCount(s.idempotent_n)}（内容一致，已取一份）`}
        </p>
      )}

      {problems.map((p, i) => (
        <div
          key={i}
          className={`mt-2.5 rounded-lx-sm border px-2.5 py-2 ${
            p.tone === 'danger'
              ? 'border-lx-danger bg-lx-danger-subtle'
              : 'border-lx-warning bg-lx-warning-subtle'
          }`}
        >
          <p
            className={`flex items-center gap-1.5 text-[11px] font-medium ${
              p.tone === 'danger' ? 'text-lx-danger' : 'text-lx-warning'
            }`}
          >
            {p.tone === 'danger' ? <X size={12} /> : <TriangleAlert size={12} />}
            {p.title}
          </p>
          <div className="mt-1 space-y-0.5">
            {p.lines.slice(0, 8).map((line, j) => (
              <p key={j} className="break-all font-mono text-[10.5px] text-lx-secondary">
                {line}
              </p>
            ))}
            {p.lines.length > 8 && (
              <p className="text-[10.5px] text-lx-dim">…共 {p.lines.length} 条</p>
            )}
          </div>
        </div>
      ))}

      {s.summary_lines.length > 0 && (
        <div className="lx-sheet-inset mt-3 max-h-44 overflow-y-auto p-2">
          {s.summary_lines.map((line, i) => (
            <p key={i} className="font-mono text-[10.5px] leading-relaxed text-lx-muted">
              {line}
            </p>
          ))}
        </div>
      )}

      <div className="mt-2.5 space-y-1.5 border-t border-lx pt-2.5">
        <p className="flex items-start gap-1.5 text-[11px] text-lx-secondary">
          <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-lx-success" />
          <span className="min-w-0 break-all font-mono text-lx-muted" title={s.merged_path}>
            总表 {s.merged_path}
          </span>
        </p>
        {s.sample_path && (
          <p className="flex items-start gap-1.5 text-[11px] text-lx-secondary">
            <FileJson size={12} className="mt-0.5 shrink-0 text-lx-accent" />
            <span className="min-w-0 break-all font-mono text-lx-muted" title={s.sample_path}>
              抽检清单 {s.sample_path}（{s.sample_count} 条）
            </span>
          </p>
        )}
        {!s.failed && (
          <p className="rounded-lx-sm bg-lx-accent-subtle px-2.5 py-2 text-[11px] leading-relaxed text-lx-secondary">
            账目齐全，可到「封包」出训练分片。
          </p>
        )}
      </div>
    </SectionPanel>
  )
}

function PackResult({ s }: { s: PackSummary }) {
  const hasOutput = s.packed + s.skipped_done > 0
  const incomplete = s.failed > 0 || s.skipped_missing > 0
  return (
    <SectionPanel
      title="封包结果"
      trailing={
        <StatusChip tone={s.failed > 0 || !hasOutput ? 'danger' : incomplete ? 'warning' : 'success'}>
          {s.failed > 0 ? `${s.failed} 条失败` : !hasOutput ? '未生成成品' : incomplete ? '待补齐' : '完成'}
        </StatusChip>
      }
    >
      <OverviewFigureRow className="grid-cols-2 sm:grid-cols-4">
        <OverviewFigure label="封包" value={fmtCount(s.packed)} note="条" tone="success" first />
        <OverviewFigure label="跳过" value={fmtCount(s.skipped_done)} note="已封过" />
        <OverviewFigure
          label="缺产物"
          value={fmtCount(s.skipped_missing)}
          tone={s.skipped_missing > 0 ? 'warning' : undefined}
        />
        <OverviewFigure
          label="失败"
          value={fmtCount(s.failed)}
          tone={s.failed > 0 ? 'danger' : undefined}
        />
      </OverviewFigureRow>

      {s.sealed.length > 0 && (
        <div className="lx-sheet-inset mt-3 max-h-56 overflow-y-auto p-2.5">
          {s.sealed.map((p) => (
            <div key={p.name} className="flex items-baseline justify-between gap-2 py-0.5 text-[11px]">
              <span className="min-w-0 truncate font-mono text-lx-secondary">{p.name}</span>
              <span className="shrink-0 tabular-nums text-lx-muted">
                {fmtCount(p.count)} 条 · {fmtBytes(p.bytes)}
              </span>
            </div>
          ))}
          {s.sealed_count > s.sealed.length && (
            <p className="pt-1 text-[10.5px] text-lx-dim">…共 {fmtCount(s.sealed_count)} 个分片</p>
          )}
        </div>
      )}

      <div className="mt-2.5 space-y-1.5 border-t border-lx pt-2.5">
        <p className="flex items-start gap-1.5 text-[11px] text-lx-secondary">
          <PackageOpen size={12} className="mt-0.5 shrink-0 text-lx-accent" />
          <span className="min-w-0 break-all font-mono text-lx-muted">
            {s.formats.join(' / ')} · 共 {fmtBytes(s.bytes)} → {s.out_dir}
          </span>
        </p>
        <p className="rounded-lx-sm bg-lx-accent-subtle px-2.5 py-2 text-[11px] leading-relaxed text-lx-secondary">
          {incomplete || !hasOutput
            ? `封包尚未补齐：缺产物 ${fmtCount(s.skipped_missing)} 张，失败 ${fmtCount(s.failed)} 张。补齐打标结果或修复原图后重跑，已封成品会跳过。`
            : `成品就绪。去「回传」页把 ${s.out_dir} 里的分片传给总机；重跑会跳过已封成品。`}
        </p>
      </div>
    </SectionPanel>
  )
}
