import { useCallback, useEffect, useMemo, useState } from 'react'
import { ChartSpline, RefreshCw } from 'lucide-react'
import { api, type JobStats, type JobStatsBucket, type Snapshot } from '../lib/api'
import { fmtCount, fmtPct } from '../lib/format'
import { PageHeader, SectionPanel, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ChoiceGrid, ErrorBox } from '../components/ui/controls'
import { OverviewFigure, OverviewFigureRow } from '../components/ui/OverviewFigures'
import { TrendChart, type TrendSeries } from '../components/TrendChart'

const BUCKETS = ['500', '1000', '5000'] as const
const LAST_JOB_KEY = 'tagger-stats-job'
const LAST_BUCKET_KEY = 'tagger-stats-bucket'
const LAST_THRESH_KEY = 'tagger-stats-thresholds'

// token 体系只有一个强调色，但折线图要几条线同屏可分辨：
// 这三个是补色相的低饱和常量，明暗两主题下都够清楚（其余线全走 token）
const C_RETRY = '#38a3a5'
const C_LOW = '#b58ad9'
const C_HIGH = '#4e9dd9'

const STATUS: Record<string, { label: string; tone: StatusTone }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '运行中', tone: 'accent' },
  done: { label: '已完成', tone: 'success' },
  stopped: { label: '已停止', tone: 'warning' },
  failed: { label: '失败', tone: 'danger' },
}

function fmtClock(ts: number) {
  const d = new Date(ts * 1000)
  const pad = (v: number) => v.toString().padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v))
}

/** 阈值 → 直方图格数：bin i 的中心分是 1 + i*0.1，"< 阈值"即中心更小的格 */
function lowCutOf(low: number) {
  return clamp(Math.ceil((low - 1) * 10), 0, 41)
}

export function Stats() {
  const [jobs, setJobs] = useState<Snapshot[]>([])
  const [jobId, setJobId] = useState(() => localStorage.getItem(LAST_JOB_KEY) ?? '')
  const [bucket, setBucket] = useState(
    () => localStorage.getItem(LAST_BUCKET_KEY) ?? '1000',
  )
  const [thresh, setThresh] = useState(() => {
    try {
      const raw = localStorage.getItem(LAST_THRESH_KEY)
      if (raw) {
        const v = JSON.parse(raw) as { low: number; high: number }
        if (Number.isFinite(v.low) && Number.isFinite(v.high)) {
          return { low: clamp(v.low, 1, 5), high: clamp(v.high, 1, 5) }
        }
      }
    } catch { /* 坏数据就当没存过 */ }
    return { low: 2.5, high: 4.5 }
  })
  const [stats, setStats] = useState<JobStats | null>(null)
  const [error, setError] = useState('')
  const [loadingJobs, setLoadingJobs] = useState(false)
  const [off, setOff] = useState<Record<string, boolean>>({})

  const loadJobs = useCallback(async () => {
    setLoadingJobs(true)
    try {
      const data = await api.listJobs()
      setJobs(data.jobs)
      // 记住的任务没了（服务重启清了内存）：退回最近一个
      setJobId((cur) =>
        cur && data.jobs.some((j) => j.id === cur) ? cur : data.jobs[0]?.id ?? '',
      )
    } catch { /* 后端没起：任务列表留空，错误由下面的 stats 拉取报 */ }
    setLoadingJobs(false)
  }, [])
  useEffect(() => { void loadJobs() }, [loadJobs])

  useEffect(() => {
    if (jobId) localStorage.setItem(LAST_JOB_KEY, jobId)
  }, [jobId])
  useEffect(() => {
    localStorage.setItem(LAST_BUCKET_KEY, bucket)
  }, [bucket])
  useEffect(() => {
    localStorage.setItem(LAST_THRESH_KEY, JSON.stringify(thresh))
  }, [thresh])

  const refresh = useCallback(async () => {
    if (!jobId) {
      setStats(null)
      return
    }
    try {
      const data = await api.jobStats(jobId, Number(bucket))
      setStats(data)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [jobId, bucket])
  useEffect(() => { void refresh() }, [refresh])

  const live = stats !== null && (stats.status === 'running' || stats.status === 'pending')
  useEffect(() => {
    if (!live) return
    const t = window.setInterval(() => { void refresh() }, 3000)
    return () => window.clearInterval(t)
  }, [live, refresh])

  const series = useMemo<TrendSeries[]>(() => {
    if (!stats || stats.buckets.length === 0) return []
    const bs = stats.buckets
    const pctVals = (pick: (b: JobStatsBucket) => number): (number | null)[] =>
      bs.map((b) => (b.settled > 0 ? pick(b) / b.settled : null))
    const histSum = (b: JobStatsBucket, from: number, to: number) =>
      b.score_hist.slice(from, to).reduce((a, v) => a + v, 0)
    const lowCut = lowCutOf(thresh.low)
    const highCut = clamp(Math.ceil((thresh.high - 1) * 10), 0, 41)
    const list: TrendSeries[] = [
      {
        key: 'first', label: '一次通过', color: 'var(--lx-success)', kind: 'pct',
        values: pctVals((b) => b.done_first), counts: bs.map((b) => b.done_first),
      },
      {
        key: 'retry', label: '重试后通过', color: C_RETRY, kind: 'pct',
        values: pctVals((b) => b.done_retry), counts: bs.map((b) => b.done_retry),
      },
      {
        key: 'skipped', label: '跳过', color: 'var(--lx-bar-fill)', kind: 'pct',
        values: pctVals((b) => b.skipped), counts: bs.map((b) => b.skipped),
      },
      {
        key: 'blacklisted', label: '拉黑', color: 'var(--lx-warning)', kind: 'pct',
        values: pctVals((b) => b.blacklisted), counts: bs.map((b) => b.blacklisted),
      },
      {
        key: 'failed', label: '失败', color: 'var(--lx-danger)', kind: 'pct',
        values: pctVals((b) => b.failed), counts: bs.map((b) => b.failed),
      },
      {
        key: 'low', label: `低分 <${thresh.low.toFixed(1)}`, color: C_LOW, kind: 'pct',
        values: bs.map((b) => (b.score_n > 0 ? histSum(b, 0, lowCut) / b.score_n : null)),
        counts: bs.map((b) => histSum(b, 0, lowCut)),
      },
      {
        key: 'high', label: `高分 ≥${thresh.high.toFixed(1)}`, color: C_HIGH, kind: 'pct',
        values: bs.map((b) => (b.score_n > 0 ? histSum(b, highCut, 41) / b.score_n : null)),
        counts: bs.map((b) => histSum(b, highCut, 41)),
      },
      {
        key: 'avg', label: '平均质检分', color: 'var(--lx-accent)', kind: 'score',
        values: bs.map((b) => (b.score_n > 0 ? b.score_sum / b.score_n : null)),
      },
    ]
    // 全程没数据的线自动退场：纯新增跑批没有"跳过"、本地引擎没有分数——
    // 留着只会是一叠贴地的零线
    return list.filter((s) => s.values.some((v) => v !== null && v !== 0))
  }, [stats, thresh])
  const visible = series.filter((s) => !off[s.key])

  const t = stats?.totals
  const passRate = t && t.settled > 0 ? (t.done_first + t.done_retry) / t.settled : null
  const failRate = t && t.settled > 0 ? t.failed / t.settled : null
  const avgScore = t && t.score_n > 0 ? t.score_sum / t.score_n : null
  const status = STATUS[stats?.status ?? 'pending'] ?? STATUS.pending

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<ChartSpline size={17} />}
        eyebrow="质检趋势"
        title="统计"
        description="按完成顺序看通过率、失败率与质检分走势。质检分只有 NL 任务才有。"
        badges={stats && <StatusChip tone={status.tone}>{status.label}</StatusChip>}
        actions={
          <div className="flex items-center gap-2">
            <select
              className="lx-select max-w-[360px]"
              value={jobId}
              onChange={(e) => setJobId(e.target.value)}
              aria-label="选择任务"
            >
              {jobs.length === 0 && <option value="">（还没有任务）</option>}
              {jobs.map((j) => {
                const st = STATUS[j.status] ?? STATUS.pending
                return (
                  <option key={j.id} value={j.id}>
                    {fmtClock(j.created)} · {st.label} · {j.counts.done}/{j.counts.total} · {j.id}
                  </option>
                )
              })}
            </select>
            <button
              type="button"
              className="lx-btn-ghost-icon shrink-0"
              onClick={() => void loadJobs()}
              disabled={loadingJobs}
              title="刷新任务列表"
            >
              <RefreshCw size={14} className={loadingJobs ? 'animate-spin' : ''} />
            </button>
          </div>
        }
      />

      {error && <ErrorBox error={error} title="统计读取失败" />}

      {stats && t && (
        <OverviewFigureRow>
          <OverviewFigure
            first
            label="已结算"
            value={fmtCount(stats.settled)}
            note={`/ ${fmtCount(stats.total)}`}
          />
          <OverviewFigure
            label="通过率"
            value={passRate === null ? '—' : fmtPct(passRate)}
            tone={passRate !== null && passRate < 0.8 ? 'warning' : undefined}
          />
          <OverviewFigure
            label="失败率"
            value={failRate === null ? '—' : fmtPct(failRate)}
            tone={failRate !== null && failRate > 0.1 ? 'danger' : undefined}
          />
          <OverviewFigure
            label="平均质检分"
            value={avgScore === null ? '—' : avgScore.toFixed(2)}
            note={t.score_n > 0 ? `${fmtCount(t.score_n)} 张有分` : 'VLM 任务才有'}
          />
        </OverviewFigureRow>
      )}

      <SectionPanel
        title="趋势"
        description="每条线是该桶内的占比（平均质检分走右轴 1~5）；X 轴是完成顺序，不是时间。悬停看绝对张数，点图例可以隐藏线。"
        trailing={
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="text-xs text-lx-muted">桶宽</span>
              <ChoiceGrid
                options={BUCKETS}
                value={bucket as (typeof BUCKETS)[number]}
                onChange={setBucket}
                className="grid-cols-3"
                size="sm"
                mono
                ariaLabel="桶宽"
              />
            </div>
            <div className="flex items-center gap-1.5 text-xs text-lx-muted">
              低分线
              <input
                type="number"
                className="lx-input w-16 px-2 py-1 font-mono text-xs tabular-nums"
                step={0.1} min={1} max={5}
                value={thresh.low}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) setThresh((s) => ({ ...s, low: clamp(v, 1, 5) }))
                }}
                aria-label="低分线"
              />
              高分线
              <input
                type="number"
                className="lx-input w-16 px-2 py-1 font-mono text-xs tabular-nums"
                step={0.1} min={1} max={5}
                value={thresh.high}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) setThresh((s) => ({ ...s, high: clamp(v, 1, 5) }))
                }}
                aria-label="高分线"
              />
            </div>
          </div>
        }
      >
        <div className="flex flex-col gap-3">
          {jobs.length === 0 ? (
            <div className="flex h-40 items-center justify-center text-xs text-lx-dim">
              还没有任务：先去「新任务」跑一批，这里才有趋势可看
            </div>
          ) : (
            <>
              <TrendChart buckets={stats?.buckets ?? []} series={visible} />
              {series.length > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                  {series.map((s) => (
                    <button
                      key={s.key}
                      type="button"
                      onClick={() => setOff((o) => ({ ...o, [s.key]: !o[s.key] }))}
                      className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium transition-opacity ${
                        off[s.key]
                          ? 'border-lx bg-transparent text-lx-muted opacity-50'
                          : 'border-lx bg-lx-surface-raised text-lx-secondary hover:text-lx'
                      }`}
                      title="点击显示 / 隐藏这条线"
                    >
                      <span className="h-1.5 w-1.5 rounded-full" style={{ background: s.color }} />
                      {s.label}
                    </button>
                  ))}
                </div>
              )}
              {stats?.fallback && (
                <p className="text-xs text-lx-muted">
                  这个任务是统计功能上线前跑的：只有整体汇总，没有逐桶趋势。
                </p>
              )}
            </>
          )}
        </div>
      </SectionPanel>
    </div>
  )
}
