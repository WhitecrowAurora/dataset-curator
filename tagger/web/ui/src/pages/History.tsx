import { useCallback, useEffect, useState } from 'react'
import {
  History as HistoryIcon,
  ListChecks,
  Loader2,
  Play,
  PlayCircle,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { api, type Snapshot } from '../lib/api'
import { basename, fmtCount, fmtSeconds } from '../lib/format'
import { PageHeader, SectionPanel, SheetHead, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'
import { ProgressBar } from '../components/ui/ProgressBar'

const STATUS: Record<string, { label: string; tone: StatusTone }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '打标中', tone: 'accent' },
  paused: { label: '已暂停', tone: 'warning' },
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
 * 历史任务。数据直接来自 /api/jobs：内存里的活任务 + 盘上的历史（run/history/），
 * 后端各留最近 20 条。不做本地缓存：缓存只会让页面与后端互相打脸。
 *
 * 来自磁盘的那批带 historical 标记，只有快照没有 reader —— 「进度 / 结果」
 * 打不开（那两页要读产物索引），只能看摘要或者点「继续」按原参数重跑。
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
  // 哪一行正在"继续"。用一个 id 而不是布尔：行与行的按钮互不牵连。
  const [busyId, setBusyId] = useState('')
  const [clearing, setClearing] = useState(false)
  // 清空之后的一行回执。没有它的话，点完只看到列表变短，用户不确定
  // 到底清掉了什么、跑着的任务有没有被误伤。
  const [note, setNote] = useState('')

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

  /** 继续。两种状态走两条路，但对用户是同一件事：
   *  - paused：worker 还挂着，叫醒它即可，同一个 id 接着跑；
   *  - stopped / failed：线程已经退出了，只能按原参数重开一个任务，
   *    已打完的图由产物 + 指纹判定跳过，等于续做（历史里会多一条）。 */
  async function continueJob(job: Snapshot) {
    setBusyId(job.id)
    setError(null)
    try {
      if (job.status === 'paused') {
        await api.resume(job.id)
        onOpenRun(job.id)
      } else {
        const snap = await api.restart(job.id)
        await load()
        onOpenRun(snap.id)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyId('')
    }
  }

  /** 清空历史。二次确认里必须写清"不删产物"——这个按钮看着像删除数据，
   *  而实际只删记录；不写的话没人敢点，或者误以为盘上的产物被清了。 */
  async function clearAll() {
    const n = jobs?.length ?? 0
    const ok = window.confirm(
      `清空 ${n} 条历史记录？\n\n` +
        '只删「历史任务」列表里的记录，不会删任何产物文件，也不会停下正在跑的任务。\n' +
        '清空后这些任务不能再从这里点开「进度 / 结果」，产物目录还在盘上。',
    )
    if (!ok) return
    setClearing(true)
    setError(null)
    try {
      const r = await api.clearHistory()
      await load()
      setNote(
        r.kept > 0
          ? `已清空 ${r.removed} 条；${r.kept} 个任务还在跑，保留着`
          : `已清空 ${r.removed} 条`,
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setClearing(false)
    }
  }

  useEffect(() => {
    void load()
  }, [load])

  // 回执只留 3 秒，别在页头常驻
  useEffect(() => {
    if (!note) return
    const t = window.setTimeout(() => setNote(''), 3000)
    return () => window.clearTimeout(t)
  }, [note])

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<HistoryIcon size={17} />}
        eyebrow="最近 20 条 · 含重启前的记录"
        title="历史任务"
        description="停下或失败的任务可点「继续」"
        actions={
          <>
            {note && (
              <span className="text-[11px] text-lx-muted">{note}</span>
            )}
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1.5 text-lx-danger"
              onClick={() => void clearAll()}
              // 空列表时不给点：点了也只是弹一个"清空 0 条"的确认框
              disabled={clearing || loading || !jobs || jobs.length === 0}
              title="清空历史列表（不删产物，不停任务）"
            >
              {clearing ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <Trash2 size={14} />
              )}
              清空
            </button>
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1.5"
              onClick={() => void load()}
              disabled={loading}
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : undefined} />
              刷新
            </button>
          </>
        }
      />

      {error && <ErrorBox error={error} title="读不到任务列表" />}

      {jobs !== null && jobs.length === 0 && (
        <SectionPanel title="还没有任务">
          <p className="flex items-center gap-2 text-xs text-lx-muted">
            <HistoryIcon size={13} /> 还没有跑过任务
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
              // 盘上的历史记录：只有快照，事件流和产物索引都随进程没了。
              const offline = Boolean(job.historical)
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
                        {job.spec.backend} · {job.spec.precision} · batch {job.spec.batch}
                        {job.spec.preprocess === 'tensor' ? ' · GPU 预处理' : ''} ·{' '}
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
                    {['paused', 'stopped', 'failed'].includes(job.status) && (
                      <button
                        type="button"
                        className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px] text-lx-accent"
                        disabled={busyId === job.id}
                        title={
                          job.status === 'paused'
                            ? '原地继续：模型和连接都还热着，立刻接着跑'
                            : '用同一套参数重开一批，已打完的图会自动跳过'
                        }
                        onClick={() => void continueJob(job)}
                      >
                        {busyId === job.id ? (
                          <Loader2 size={13} className="animate-spin" />
                        ) : (
                          <Play size={13} />
                        )}
                        继续
                      </button>
                    )}
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
                      disabled={offline}
                      title={offline ? '进程重启后这条只剩记录，没有实时进度' : undefined}
                      onClick={() => onOpenRun(job.id)}
                    >
                      <PlayCircle size={13} /> 进度
                    </button>
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
                      disabled={offline}
                      title={offline ? '产物索引随进程退出，历史记录打不开结果页' : undefined}
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
