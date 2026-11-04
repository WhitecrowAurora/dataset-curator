import { useEffect, useState } from 'react'
import { Copy, FileText, FolderOpen, ListFilter, Loader2, RefreshCw, ScanSearch, Square } from 'lucide-react'
import { api, ApiError, type DedupState } from '../lib/api'
import { fmtBytes, fmtCount } from '../lib/format'
import { PageHeader, SectionPanel, SettingRow, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ChoiceGrid, ErrorBox, ToggleRow } from '../components/ui/controls'
import { ProgressBar } from '../components/ui/ProgressBar'
import { FolderPicker } from '../components/FolderPicker'

// 去重页：发分包前的独立步骤。左侧选素材目录 + 参数；右边看扫描进度、
// 重复组预览和产出路径。与打标并行跑（去重是 IO/CPU 活，不占 GPU），
// 但同一时刻只允许一个去重——两个扫描抢同一批磁盘没有收益。
//
// 素材文件全程只读：产出是一份排除清单（发分包时 split --exclude 用它
// 把重复图挡在包外），觉得扫错了删掉清单重来就是。

const MODES = ['similar', 'exact'] as const
const KEEPS = ['first', 'largest'] as const
const LAST_DEDUP_KEY = 'tagger-last-dedup'

const STATUS_LABEL: Record<string, string> = {
  pending: '等待',
  running: '扫描中',
  done: '完成',
  stopped: '已停止',
  failed: '失败',
}

export function Dedup() {
  const [inputDir, setInputDir] = useState('')
  const [picker, setPicker] = useState(false)
  const [outDir, setOutDir] = useState('')
  const [mode, setMode] = useState<(typeof MODES)[number]>('similar')
  const [threshold, setThreshold] = useState(8)
  const [keep, setKeep] = useState<(typeof KEEPS)[number]>('first')
  const [recursive, setRecursive] = useState(true)
  const [workers, setWorkers] = useState(8)

  const [job, setJob] = useState<DedupState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 刷新页面后重新接上还在跑的扫描
  useEffect(() => {
    const saved = (() => {
      try {
        return localStorage.getItem(LAST_DEDUP_KEY)
      } catch {
        return null
      }
    })()
    if (!saved) return
    api
      .dedup(saved)
      .then((st) => {
        if (st.status === 'pending' || st.status === 'running') setJob(st)
        else localStorage.removeItem(LAST_DEDUP_KEY)
      })
      .catch(() => localStorage.removeItem(LAST_DEDUP_KEY))
  }, [])

  // 进行中的扫描每秒轮询
  useEffect(() => {
    if (!job || (job.status !== 'pending' && job.status !== 'running')) return
    const timer = window.setInterval(() => {
      api
        .dedup(job.id)
        .then((st) => {
          setJob(st)
          if (st.status !== 'pending' && st.status !== 'running') {
            try {
              localStorage.removeItem(LAST_DEDUP_KEY)
            } catch {
              // 忽略
            }
          }
        })
        .catch(() => undefined)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [job])

  const running = job !== null && (job.status === 'pending' || job.status === 'running')
  const canStart = !running && inputDir.trim() !== '' && !busy
  const pct = job && job.total > 0 ? Math.min(100, (job.scanned / job.total) * 100) : 0

  async function start() {
    setBusy(true)
    setError(null)
    try {
      const st = await api.dedupStart({
        input_dir: inputDir.trim(),
        out_dir: outDir.trim(),
        mode,
        threshold,
        keep,
        recursive,
        workers,
      })
      setJob(st)
      try {
        localStorage.setItem(LAST_DEDUP_KEY, st.id)
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
      const res = await api.dedupStop(job.id)
      if (res.snapshot) setJob(res.snapshot)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
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

  const summary = job?.summary ?? null

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<Copy size={17} />}
        eyebrow="去重"
        title="发分包前查重复图"
        description="按字节哈希与感知哈希找重复图；素材不动，只产一份排除清单。"
        actions={
          running ? (
            <button type="button" className="lx-btn-ghost" onClick={() => void stop()}>
              <Square size={14} className="mr-1.5 inline" />
              停止扫描
            </button>
          ) : (
            <button
              type="button"
              className="lx-btn-primary lx-launch-glow inline-flex items-center gap-1.5"
              disabled={!canStart}
              onClick={() => void start()}
            >
              <ScanSearch size={14} />
              {busy ? '创建中…' : '开始扫描'}
            </button>
          )
        }
      />

      {error && <ErrorBox error={error} title="操作失败" />}

      <div className="grid gap-3.5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="flex min-w-0 flex-col gap-3.5">
          <SectionPanel title="素材目录">
            <SettingRow
              label="输入目录"
              help="直接指装图的文件夹或单个大 zip 包都行；与打标任务互不影响"
            >
              <div className="flex gap-2">
                <input
                  className="lx-input font-mono text-xs"
                  value={inputDir}
                  onChange={(e) => setInputDir(e.target.value)}
                  placeholder="D:\batch-07 或 D:\batch-07\shard-0001.zip"
                  spellCheck={false}
                  disabled={running}
                />
                <button
                  type="button"
                  className="lx-btn-ghost shrink-0 px-3"
                  onClick={() => setPicker(true)}
                >
                  <FolderOpen size={14} className="mr-1 inline" />
                  浏览
                </button>
              </div>
            </SettingRow>

            <SettingRow label="重复判定" help="精确零误杀；相似额外抓重压缩/重保存的图">
              <ChoiceGrid
                options={MODES}
                value={mode}
                onChange={setMode}
                className="grid-cols-2"
                size="sm"
                renderLabel={(v) => (v === 'similar' ? '精确 + 相似' : '只查精确')}
                ariaLabel="重复判定"
                disabled={running}
              />
            </SettingRow>

            {mode === 'similar' && (
              <SettingRow
                label={`相似阈值：${threshold}`}
                help="感知哈希的汉明距离，≤阈值算同一张。8 允许轻度重压缩；调小更严、误杀少"
              >
                <input
                  type="range"
                  min={2}
                  max={16}
                  value={threshold}
                  onChange={(e) => setThreshold(Number(e.target.value))}
                  className="w-full accent-[var(--lx-accent)]"
                  disabled={running}
                />
              </SettingRow>
            )}

            <SettingRow label="组内保留" help="最先 = 与分包编号同序的那张；最大 = 文件最大的（重压缩场景质量高）">
              <ChoiceGrid
                options={KEEPS}
                value={keep}
                onChange={setKeep}
                className="grid-cols-2"
                size="sm"
                renderLabel={(v) => (v === 'first' ? '最先' : '最大')}
                ariaLabel="保留策略"
                disabled={running}
              />
            </SettingRow>

            <details className="px-1">
              <summary className="cursor-pointer text-xs text-lx-muted">高级</summary>
              <div className="mt-3 space-y-4">
                <SettingRow label="输出目录" help="默认 <输入名>-dedup，哈希缓存 hashes.db 也放这里">
                  <input
                    className="lx-input font-mono text-xs"
                    value={outDir}
                    onChange={(e) => setOutDir(e.target.value)}
                    placeholder="留空 = 自动"
                    spellCheck={false}
                    disabled={running}
                  />
                </SettingRow>
                <SettingRow label="并行线程" help="扫盘是 IO 密集，8 对机械盘通常已经够饱">
                  <input
                    type="number"
                    className="lx-input w-24 font-mono tabular-nums"
                    min={1}
                    max={32}
                    value={workers}
                    onChange={(e) => setWorkers(Math.min(32, Math.max(1, Number(e.target.value) || 8)))}
                    disabled={running}
                  />
                </SettingRow>
                <ToggleRow
                  checked={recursive}
                  onChange={setRecursive}
                  label="包含子目录"
                  hint="关掉只扫第一层"
                  disabled={running}
                />
              </div>
            </details>
          </SectionPanel>

          {summary && summary.preview.length > 0 && (
            <SectionPanel
              title="重复组预览"
              trailing={
                <StatusChip tone="neutral">
                  前 {summary.preview.length} 组 · 共 {summary.groups_exact + summary.groups_near} 组
                </StatusChip>
              }
            >
              <div className="lx-sheet-inset max-h-[24rem] space-y-2 overflow-y-auto p-2.5">
                {summary.preview.map((g, i) => (
                  <div key={i} className="rounded-lx-sm border border-lx px-2.5 py-2">
                    <div className="flex items-center gap-2 text-[11px]">
                      <StatusChip tone={g.kind === 'exact' ? 'danger' : 'warning'}>
                        {g.kind === 'exact' ? '精确' : '相似'}
                      </StatusChip>
                      <span className="min-w-0 flex-1 truncate font-mono text-lx-secondary" title={g.keep.rel_path}>
                        保留 {g.keep.rel_path}
                      </span>
                      <span className="shrink-0 text-lx-dim">{g.keep.artist || '（无画师名）'}</span>
                    </div>
                    <div className="mt-1.5 space-y-1 pl-3">
                      {g.duplicates.map((d, j) => (
                        <div key={j} className="flex items-center gap-2 text-[11px]">
                          <ListFilter size={11} className="shrink-0 text-lx-dim" />
                          <span className="min-w-0 flex-1 truncate font-mono text-lx-muted" title={d.rel_path}>
                            {d.rel_path}
                          </span>
                          <span className="shrink-0 tabular-nums text-lx-dim">
                            {g.kind === 'near' ? `距离 ${d.distance}` : '字节相同'}
                            {d.artist ? ` · ${d.artist}` : ''}
                          </span>
                        </div>
                      ))}
                      {g.more > 0 && (
                        <p className="pl-3 text-[10.5px] text-lx-dim">…还有 {g.more} 张（见报告）</p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </SectionPanel>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-3.5">
          {job && (
            <SectionPanel
              title="扫描进度"
              trailing={<StatusChip tone={tone}>{STATUS_LABEL[job.status] ?? job.status}</StatusChip>}
            >
              {job.status === 'running' || job.status === 'pending' ? (
                <>
                  <div className="mb-1.5 flex items-baseline justify-between gap-2 text-[10.5px] text-lx-muted">
                    <span className="tabular-nums">{Math.round(pct)}%</span>
                    <span className="tabular-nums">
                      {fmtCount(job.scanned)}/{job.total ? fmtCount(job.total) : '…'}
                    </span>
                  </div>
                  <ProgressBar percent={pct} tone="accent" />
                  <div className="mt-2 space-y-0.5 text-[11px] text-lx-muted">
                    {job.rate > 0 && (
                      <p className="tabular-nums">
                        {fmtCount(job.rate)} 张/秒
                        {job.eta_s > 1 &&
                          ` · 预计还要 ${job.eta_s > 3600 ? `${Math.round(job.eta_s / 3600)} 小时` : job.eta_s > 60 ? `${Math.round(job.eta_s / 60)} 分钟` : `${Math.round(job.eta_s)} 秒`}`}
                      </p>
                    )}
                    {job.current && (
                      <p className="truncate font-mono text-lx-dim" title={job.current}>
                        {job.current}
                      </p>
                    )}
                    <p className="flex items-center gap-1.5">
                      <RefreshCw size={11} className="animate-spin" />
                      打标任务可以照常跑，去重不占 GPU
                    </p>
                  </div>
                </>
              ) : (
                <p className="text-[11px] text-lx-muted">
                  {job.status === 'failed' ? job.error || '扫描失败' : '没有正在进行的扫描。'}
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

          {summary && (
            <SectionPanel title="结果">
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded-lx-sm bg-lx-danger-subtle px-3 py-2">
                  <p className="text-lg font-semibold tabular-nums text-lx-danger">{summary.groups_exact}</p>
                  <p className="text-[10.5px] text-lx-muted">精确重复组</p>
                </div>
                <div className="rounded-lx-sm bg-lx-warning-subtle px-3 py-2">
                  <p className="text-lg font-semibold tabular-nums text-lx-warning">{summary.groups_near}</p>
                  <p className="text-[10.5px] text-lx-muted">相似组</p>
                </div>
              </div>
              <div className="mt-2.5 space-y-1 text-[11px] text-lx-secondary">
                <p className="tabular-nums">
                  排除 <span className="font-semibold text-lx">{fmtCount(summary.duplicates)}</span> 张 ·
                  省 {fmtBytes(summary.wasted_bytes)}
                </p>
                <p className="tabular-nums text-lx-muted">
                  扫描 {fmtCount(summary.discovered)} 张 · 新算 {fmtCount(summary.hashed)} · 缓存复用{' '}
                  {fmtCount(summary.cached)}
                  {summary.errors > 0 && ` · 错误 ${summary.errors}`}
                  {' · '}
                  {summary.elapsed_s > 90
                    ? `${Math.round(summary.elapsed_s / 60)} 分钟`
                    : `${summary.elapsed_s} 秒`}
                </p>
              </div>
              <div className="mt-2.5 space-y-1.5 border-t border-lx pt-2.5">
                <p className="flex items-start gap-1.5 text-[11px] text-lx-secondary">
                  <ListFilter size={12} className="mt-0.5 shrink-0 text-lx-accent" />
                  <span className="min-w-0 break-all font-mono text-lx-muted" title={summary.exclude_file}>
                    排除清单 {summary.exclude_file}
                  </span>
                </p>
                <p className="flex items-start gap-1.5 text-[11px] text-lx-secondary">
                  <FileText size={12} className="mt-0.5 shrink-0 text-lx-accent" />
                  <span className="min-w-0 break-all font-mono text-lx-muted" title={summary.report_file}>
                    逐组报告 {summary.report_file}
                  </span>
                </p>
                <p className="rounded-lx-sm bg-lx-accent-subtle px-2.5 py-2 text-[11px] leading-relaxed text-lx-secondary">
                  发分包时加参数即可排除这 {fmtCount(summary.duplicates)} 张：
                  <code className="mt-1 block break-all font-mono text-[10.5px] text-lx-accent">
                    tagger split --input-dir … --exclude {summary.exclude_file}
                  </code>
                </p>
              </div>
            </SectionPanel>
          )}

          {!job && (
            <SectionPanel title="怎么用">
              <ol className="list-decimal space-y-2 pl-5 text-[11.5px] leading-relaxed text-lx-secondary">
                <li>打标完一批素材后、点"发分包"之前，来这里扫一遍。</li>
                <li>
                  扫描按小时计（百万级素材），中断了也没关系——哈希缓存在输出目录的
                  hashes.db，重跑会接着来，不用从头算。
                </li>
                <li>结果是一份排除清单和逐组报告；对分组有异议就调阈值重扫（已扫的不会重算）。</li>
                <li>素材本身不动。确认没问题后再发分包，分包时带上排除清单。</li>
              </ol>
              {running && (
                <p className="mt-3 flex items-center gap-1.5 text-[11px] text-lx-muted">
                  <Loader2 size={11} className="animate-spin" />
                  正在连上去重任务…
                </p>
              )}
            </SectionPanel>
          )}
        </div>
      </div>

      <FolderPicker
        open={picker}
        start={inputDir}
        onClose={() => setPicker(false)}
        onPick={(path) => {
          setInputDir(path)
          setPicker(false)
        }}
      />
    </div>
  )
}
