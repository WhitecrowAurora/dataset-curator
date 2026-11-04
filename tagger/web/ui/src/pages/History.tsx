import { useCallback, useEffect, useState } from 'react'
import { History as HistoryIcon, ListChecks, PlayCircle, RefreshCw } from 'lucide-react'
import { api, type Snapshot } from '../lib/api'
import { basename, fmtCount, fmtSeconds } from '../lib/format'
import { PageHeader, SectionPanel, SheetHead, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'
import { ProgressBar } from '../components/ui/ProgressBar'

const STATUS: Record<string, { label: string; tone: StatusTone }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '打标中', tone: 'accent' },
  done: { label: '已完成', tone: 'success' },
  stopped: { label: '已停止', tone: 'warning' },
  failed: { label: '失败', tone: 'danger' },
}

function fmtClock(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * 历史任务。数据直接来自 /api/jobs（后端只保留本进程最近的 20 条）——
 * 不做本地缓存：任务簿在任何终端都可能被重启，缓存只会让页面与后端互相打脸。
 */
export function History({
  currentId,
  onOpenRun,
  onOpenResults,
}: {
  currentId: string
  onOpenRun: (id: string) => void
  onOpenResults: (id: string) => void
}) {
  const [jobs, setJobs] = useState<Snapshot[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.listJobs()
      setJobs(data.jobs)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<HistoryIcon size={17} />}
        eyebrow="本进程内最近 20 条"
        title="历史任务"
        description="刷新页面或切走都不影响这里。想接着看某一批，点「进度」回到实时视图，或直接「结果」翻产物。"
        actions={
          <button
            type="button"
            className="lx-btn-ghost inline-flex items-center gap-1.5"
            onClick={() => void load()}
            disabled={loading}
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : undefined} />
            刷新
          </button>
        }
      />

      {error && <ErrorBox error={error} title="读不到任务列表" />}

      {jobs !== null && jobs.length === 0 && (
        <SectionPanel title="还没有任务">
          <p className="flex items-center gap-2 text-xs text-lx-muted">
            <HistoryIcon size={13} /> 去「新建任务」跑第一批图，跑过之后这里会有记录。
          </p>
        </SectionPanel>
      )}

      {jobs !== null && jobs.length > 0 && (
        <section className="lx-sheet overflow-hidden">
          <SheetHead title="任务记录" count={jobs.length} hint="按创建时间倒序" />
          <div className="border-t border-lx">
            {jobs.map((job) => {
              const status = STATUS[job.status] ?? STATUS.pending
              // progress 为 null = 流式任务（HF 分片），总量未知：进度条退化为计数
              const pct = (job.progress ?? (job.counts.total ? (job.counts.settled / job.counts.total) : 0)) * 100
              const active = job.id === currentId
              return (
                <div
                  key={job.id}
                  className="lx-sheet-row flex flex-wrap items-center gap-x-4 gap-y-2 px-[18px] py-3 md:px-7"
                >
                  <div className="flex min-w-0 flex-1 items-center gap-2.5">
                    <StatusChip tone={status.tone}>{status.label}</StatusChip>
                    <div className="min-w-0">
                      <div
                        className="truncate text-[13px] font-medium text-lx"
                        title={job.spec.input_dir}
                      >
                        {basename(job.spec.input_dir) || job.spec.input_dir}
                        {active && <span className="ml-1.5 text-[10px] text-lx-accent">当前</span>}
                      </div>
                      <div className="mt-0.5 truncate font-mono text-[10.5px] text-lx-muted" title={job.out_dir}>
                        {job.spec.backend} · {job.spec.precision} · batch {job.spec.batch} ·{' '}
                        {basename(job.out_dir) || '默认输出'}
                      </div>
                    </div>
                  </div>

                  <div className="w-[132px] shrink-0">
                    <div className="mb-1 flex items-baseline justify-between gap-2 text-[10.5px] text-lx-muted">
                      <span className="tabular-nums">{Math.round(pct)}%</span>
                      <span className="tabular-nums">
                        {fmtCount(job.counts.settled)}/{fmtCount(job.counts.total)}
                      </span>
                    </div>
                    <ProgressBar
                      percent={pct}
                      tone={
                        job.status === 'failed'
                          ? 'danger'
                          : job.status === 'done'
                            ? 'success'
                            : job.status === 'running'
                              ? 'accent'
                              : 'neutral'
                      }
                      heightClass="h-1"
                    />
                  </div>

                  <div className="hidden w-[184px] shrink-0 text-[10.5px] text-lx-muted md:block">
                    <div className="tabular-nums">
                      完成 {fmtCount(job.counts.done)} · 跳过 {fmtCount(job.counts.skipped)} · 失败{' '}
                      <span className={job.counts.failed ? 'text-lx-danger' : undefined}>
                        {fmtCount(job.counts.failed)}
                      </span>
                    </div>
                    <div className="mt-0.5 tabular-nums text-lx-dim">
                      {fmtClock(job.created)} · {fmtSeconds(job.elapsed_s)}
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-1.5">
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
                      onClick={() => onOpenRun(job.id)}
                    >
                      <PlayCircle size={13} /> 进度
                    </button>
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
                      onClick={() => onOpenResults(job.id)}
                    >
                      <ListChecks size={13} /> 结果
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </section>
      )}

      {jobs === null && !error && (
        <SectionPanel title="读取中">
          <p className="text-xs text-lx-muted">正在问后端要任务列表…</p>
        </SectionPanel>
      )}
    </div>
  )
}
