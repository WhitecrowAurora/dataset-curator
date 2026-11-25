import { useEffect, useRef, useState } from 'react'
import { Activity, TrendingUp } from 'lucide-react'
import { resultSeries, SERIES_COLORS, type BenchmarkRow, type BenchmarkState } from '../../lib/benchmark'
import { ENGINE_LABELS } from '../../lib/nlEngines'

function axis(max: number, count = 5) {
  const rough = Math.max(max, 1) / count
  const scale = 10 ** Math.floor(Math.log10(rough))
  const step = ([1, 2, 5, 10].find(n => n * scale >= rough) ?? 10) * scale
  return { step, max: Math.ceil(Math.max(max, 1) / step) * step }
}

export function BenchmarkChart({ state }: { state: BenchmarkState | null }) {
  const container = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(900)
  const [hidden, setHidden] = useState<string[]>([])
  const [hover, setHover] = useState<{ index: number; row: BenchmarkRow } | null>(null)
  const series = resultSeries(state)
  const rows = state?.rows ?? []
  useEffect(() => {
    const observer = new ResizeObserver(entries => setWidth(entries[0].contentRect.width))
    if (container.current) observer.observe(container.current)
    return () => observer.disconnect()
  }, [])
  useEffect(() => { setHidden([]); setHover(null) }, [state?.report_path])
  const height = width < 500 ? 290 : 330
  const margin = { left: 52, right: 24, top: 34, bottom: 45 }
  const complete = rows.filter(r => r.status === 'completed' && r.rate_per_s !== undefined && r.actual !== undefined)
  const maxX = Math.max(2, ...(state?.request?.levels ?? [1, 2, 4, 6, 8]), ...complete.map(r => r.actual!))
  const yAxis = axis(Math.max(1, ...complete.map(r => r.rate_per_s! * 60)))
  const xAxis = axis(maxX, width < 500 ? 4 : 8)
  const x = (value: number) => margin.left + value / xAxis.max * Math.max(1, width - margin.left - margin.right)
  const y = (value: number) => height - margin.bottom - value / yAxis.max * (height - margin.top - margin.bottom)
  const hovered = hover ? complete.filter(r => r.actual === hover.row.actual && !hidden.includes(r.series_id ?? 'config-0')) : []
  const legend = (i: number) => {
    const s = series[i]
    return `${s.model} · ${ENGINE_LABELS[s.engine] ?? s.engine} · ${s.precision.toUpperCase()}`
  }
  return <section className="bn-chart" aria-label="速度对比">
    <header className="bn-chart-head"><div><div className="bn-eyebrow">THROUGHPUT / CONCURRENCY</div><h2><TrendingUp size={19} />并发与速度</h2></div>
      <span className="bn-chart-note">同批图片 · 独立预热 · 依次运行</span>
    </header>
    <div className="bn-legend">{series.map((s, i) => <button key={s.id} aria-pressed={!hidden.includes(s.id)} aria-label={`显示曲线 ${String.fromCharCode(65 + i)}`} className={hidden.includes(s.id) ? 'is-hidden' : ''}
      onClick={() => { setHidden(v => v.includes(s.id) ? v.filter(id => id !== s.id) : [...v, s.id]); setHover(null) }}>
      <span className="bn-line-key" style={{ background: SERIES_COLORS[i] }} /><span>{legend(i)}</span>
    </button>)}</div>
    <div ref={container} className="bn-plot">
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} role="group" aria-label="并发速度折线图">
        <text x={margin.left} y={16} className="bn-axis-title">速度 · 张/分钟</text>
        {Array.from({ length: Math.round(yAxis.max / yAxis.step) + 1 }, (_, i) => i * yAxis.step).map(tick => <g key={tick}>
          <line x1={margin.left} x2={width - margin.right} y1={y(tick)} y2={y(tick)} className="bn-grid" />
          <text x={margin.left - 12} y={y(tick) + 4} textAnchor="end" className="bn-tick">{Number(tick.toFixed(2))}</text>
        </g>)}
        {Array.from({ length: Math.round(xAxis.max / xAxis.step) + 1 }, (_, i) => i * xAxis.step).filter(Number.isInteger).map(tick => <g key={tick}>
          <line x1={x(tick)} x2={x(tick)} y1={margin.top} y2={height - margin.bottom} className="bn-grid bn-grid-vertical" />
          <text x={x(tick)} y={height - margin.bottom + 21} textAnchor="middle" className="bn-tick">{tick}</text>
        </g>)}
        <text x={width - margin.right} y={height - 3} textAnchor="end" className="bn-axis-title">实际并发数</text>
        {series.map((s, i) => {
          if (hidden.includes(s.id)) return null
          const points = rows.filter(r => (r.series_id ?? 'config-0') === s.id)
          const segments: BenchmarkRow[][] = []
          let segment: BenchmarkRow[] = []
          for (const row of points) {
            if (row.status === 'completed' && row.actual !== undefined && row.rate_per_s !== undefined) segment.push(row)
            else if (row.status !== 'skipped') { if (segment.length) segments.push(segment); segment = [] }
          }
          if (segment.length) segments.push(segment)
          return <g key={s.id} style={{ color: SERIES_COLORS[i] }}>
            {segments.map((points, n) => <polyline key={n} points={[...points].sort((a, b) => a.actual! - b.actual!).map(r => `${x(r.actual!)},${y(r.rate_per_s! * 60)}`).join(' ')} fill="none" stroke="currentColor" strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />)}
            {segments.flat().map(r => <circle key={r.requested} cx={x(r.actual!)} cy={y(r.rate_per_s! * 60)} r={hover?.row === r ? 6 : 4.5} fill="var(--lx-surface)" stroke="currentColor" strokeWidth={2.5}
              tabIndex={0} role="button" aria-label={`${legend(i)}，实际并发 ${r.actual}，${(r.rate_per_s! * 60).toFixed(1)} 张/分钟`}
              onMouseEnter={() => setHover({ index: i, row: r })} onMouseLeave={() => setHover(null)} onFocus={() => setHover({ index: i, row: r })} onBlur={() => setHover(null)}
              onClick={() => setHover({ index: i, row: r })} />)}
          </g>
        })}
      </svg>
      {!complete.length && <div className="bn-chart-empty"><Activity size={24} /><strong>{state?.running ? '正在准备第一组结果' : '让性能曲线给出答案'}</strong><span>选择配置后开始测试，每完成一档便生成一个实测点。</span></div>}
      {hover && hovered.length > 0 && <div className="bn-tooltip" role="status"><span>实际并发 {hover.row.actual}</span>{hovered.map(row => {
        const i = Math.max(0, series.findIndex(s => s.id === (row.series_id ?? 'config-0')))
        return <div className="bn-tooltip-series" key={row.series_id ?? 'config-0'}><span style={{ color: SERIES_COLORS[i] }}>{legend(i)}</span><strong>{(row.rate_per_s! * 60).toFixed(1)} <small>张/分钟</small></strong><span>请求 {row.requested}{row.peak_vram_mb !== undefined ? ` · 显存 ${(row.peak_vram_mb / 1024).toFixed(2)} GB` : ''}</span></div>
      })}</div>}
    </div>
    <footer className="bn-chart-footer">{state?.sample_count ? `${state.sample_count} 张固定样本` : '固定样本集'}<span>悬停查看数据 · 点击图例切换曲线 · 失败点不连线</span></footer>
  </section>
}
