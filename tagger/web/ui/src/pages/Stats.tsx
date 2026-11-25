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
// 跳过按原因拆开后线变多，再补两个中间调；三色循环会在同屏时撞色
const C_SKIP_BLANK = '#8a8f98'
const C_SKIP_ARTIST = '#c08a4e'

const STATUS: Record<string, { label: string; tone: StatusTone }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '运行中', tone: 'accent' },
  paused: { label: '已暂停', tone: 'warning' },
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

function inferenceCount(b: JobStatsBucket) {
  return b.done_first + b.done_retry + b.failed
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

  const live = stats !== null && ['running', 'pending', 'paused'].includes(stats.status)
  useEffect(() => {
    if (!live) return
    const t = window.setInterval(() => { void refresh() }, 3000)
    return () => window.clearInterval(t)
  }, [live, refresh])

  const series = useMemo<TrendSeries[]>(() => {
    if (!stats || stats.buckets.length === 0) return []
    const bs = stats.buckets
    const pctVals = (
      pick: (b: JobStatsBucket) => number,
      denominator: (b: JobStatsBucket) => number = (b) => b.settled,
    ): (number | null)[] => bs.map((b) => {
      const n = denominator(b)
      return n > 0 ? pick(b) / n : null
    })
    const histSum = (b: JobStatsBucket, from: number, to: number) =>
      b.score_hist.slice(from, to).reduce((a, v) => a + v, 0)
    const lowCut = lowCutOf(thresh.low)
    const highCut = clamp(Math.ceil((thresh.high - 1) * 10), 0, 41)
    const list: TrendSeries[] = [
      {
        key: 'first', label: '一次通过率', color: 'var(--lx-success)', kind: 'pct',
        values: pctVals((b) => b.done_first, inferenceCount), counts: bs.map((b) => b.done_first),
      },
      {
        key: 'retry', label: '重试后通过率', color: C_RETRY, kind: 'pct',
        values: pctVals((b) => b.done_retry, inferenceCount), counts: bs.map((b) => b.done_retry),
      },
      {
        key: 'skipped', label: '已有产物占比', color: 'var(--lx-bar-fill)', kind: 'pct',
        values: pctVals((b) => b.skipped), counts: bs.map((b) => b.skipped),
      },
      // 跳过按原因分线：产物已有是正常续跑，图太小 / 没画师是"永远做不成"。
      // 合成一条的话后者会被前者压平——194 张已有 + 5 张图太小，曲线只报
      // "跳过 99.5%"，看着一切正常，漏的那 5 张永远没人发现。
      {
        key: 'blank_skipped', label: '图太小占比', color: C_SKIP_BLANK, kind: 'pct',
        values: pctVals((b) => b.blank_skipped), counts: bs.map((b) => b.blank_skipped),
      },
      {
        key: 'artist_skipped', label: '无画师占比', color: C_SKIP_ARTIST, kind: 'pct',
        values: pctVals((b) => b.artist_skipped), counts: bs.map((b) => b.artist_skipped),
      },
      {
        key: 'excused', label: '重试剔除占比', color: C_HIGH, kind: 'pct',
        values: pctVals((b) => b.excused), counts: bs.map((b) => b.excused),
      },
      {
        key: 'blacklisted', label: '达到失败上限占比', color: 'var(--lx-warning)', kind: 'pct',
        values: pctVals((b) => b.blacklisted), counts: bs.map((b) => b.blacklisted),
      },
      {
        key: 'failed', label: '失败率', color: 'var(--lx-danger)', kind: 'pct',
        values: pctVals((b) => b.failed, inferenceCount), counts: bs.map((b) => b.failed),
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
  const inferred = t ? inferenceCount(t) : 0
  const passRate = t && inferred > 0 ? (t.done_first + t.done_retry) / inferred : null
  const failRate = t && inferred > 0 ? t.failed / inferred : null
  const avgScore = t && t.score_n > 0 ? t.score_sum / t.score_n : null
  const status = STATUS[stats?.status ?? 'pending'] ?? STATUS.pending

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<ChartSpline size={17} />}
        eyebrow="质检趋势"
        title="统计"
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
            label="本次通过率"
            value={passRate === null ? '—' : fmtPct(passRate)}
            note="成功 ÷（成功＋失败）"
            tone={passRate !== null && passRate < 0.8 ? 'warning' : undefined}
          />
          <OverviewFigure
            label="本次失败率"
            value={failRate === null ? '—' : fmtPct(failRate)}
            note="跳过与剔除不计入"
            tone={failRate !== null && failRate > 0.1 ? 'danger' : undefined}
          />
          <OverviewFigure
            label="平均质检分"
            value={avgScore === null ? '—' : avgScore.toFixed(2)}
            note={t.score_n > 0 ? `${fmtCount(t.score_n)} 张有分` : '仅 NL 任务'}
          />
          {(t.skipped > 0) && (
            <OverviewFigure label="跳过（已有）" value={fmtCount(t.skipped)} />
          )}
          {/* 跳过按原因分列：只给一个总数，看不出是"续跑正常"还是"素材/配置有问题" */}
          {(t.blank_skipped > 0 || t.artist_skipped > 0 || t.excused > 0
            || t.blacklisted > 0) && (
            <OverviewFigure
              label="跳过（非已有）"
              value={fmtCount(t.blank_skipped + t.artist_skipped + t.excused + t.blacklisted)}
              note={[
                t.blank_skipped ? `图太小 ${fmtCount(t.blank_skipped)}` : '',
                t.artist_skipped ? `无画师 ${fmtCount(t.artist_skipped)}` : '',
                t.excused ? `重试剔除 ${fmtCount(t.excused)}` : '',
                t.blacklisted ? `重试后依然失败 ${fmtCount(t.blacklisted)}` : '',
              ].filter(Boolean).join(' · ')}
              tone={t.blank_skipped + t.artist_skipped + t.excused + t.blacklisted > 0
                ? 'warning' : undefined}
            />
          )}
        </OverviewFigureRow>
      )}

      <SectionPanel
        title="趋势"
        description={
          jobs.length === 0
            ? undefined
            : '通过与失败按成功＋失败计算；跳过按已结算计算；高低分按有分图片计算。质检分走右轴，X 轴是完成顺序。'
        }
        trailing={
          // 没有任务时这些筛选控件没有可作用的对象，空态里留着只会让人以为图没画出来。
          jobs.length === 0 ? undefined : (
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
          )
        }
      >
        <div className="flex flex-col gap-3">
          {jobs.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
              <ChartSpline size={28} className="text-lx-dim" />
              <p className="text-sm text-lx-secondary">未运行未产生日志</p>
              <p className="max-w-md text-[11px] leading-relaxed text-lx-muted">
                任务记录只存在服务运行期间，重启后为空；跑过的批次在「历史任务」里
              </p>
              <div className="mt-1 flex items-center gap-2">
                <button
                  type="button"
                  className="lx-btn-primary px-3 py-1.5 text-[11px]"
                  onClick={() => {
                    location.hash = '/'
                  }}
                >
                  去新建任务
                </button>
                <button
                  type="button"
                  className="lx-btn-ghost px-3 py-1.5 text-[11px]"
                  onClick={() => {
                    location.hash = '/history'
                  }}
                >
                  看历史任务
                </button>
              </div>
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
                  该任务没有逐桶趋势数据
                </p>
              )}
            </>
          )}
        </div>
      </SectionPanel>
    </div>
  )
}
