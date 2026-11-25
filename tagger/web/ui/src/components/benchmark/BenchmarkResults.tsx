import { resultSeries, SERIES_COLORS, type BenchmarkState } from '../../lib/benchmark'
import { ENGINE_LABELS } from '../../lib/nlEngines'
import { StatusChip } from '../ui/launcher'
import { BenchmarkChart } from './BenchmarkChart'

const status: Record<string, string> = {
  preparing: '准备模型', warming: '预热', measuring: '测速', completed: '完成',
  failed: '失败', canceled: '已停止', skipped: '未测量',
}
const seconds = (value?: number) => value === undefined ? '—' : `${value.toFixed(1)} s`

export function BenchmarkResults({ state }: { state: BenchmarkState | null }) {
  const rows = state?.rows ?? []
  const series = resultSeries(state)
  return <>
    <BenchmarkChart state={state} />
    {series.length > 0 && <div className="bn-summaries">{series.map((s, i) => {
      const done = rows.filter(r => (r.series_id ?? 'config-0') === s.id && r.status === 'completed')
      const peak = Math.max(0, ...done.map(r => r.rate_per_s ?? 0))
      return <section key={s.id} className="bn-summary">
        <div className="bn-summary-title"><span className="bn-letter" style={{ color: SERIES_COLORS[i] }}>{String.fromCharCode(65 + i)}</span><div><strong>{s.model}</strong><small>{ENGINE_LABELS[s.engine] ?? s.engine} · {s.precision.toUpperCase()}</small></div></div>
        <div className="bn-summary-metrics"><div><span>最高速度</span><strong style={{ color: SERIES_COLORS[i] }}>{done.length ? (peak * 60).toFixed(1) : '—'}<small>张/分钟</small></strong></div><div><span>建议并发</span><strong>{s.recommended ?? '—'}</strong></div><div><span>成功档位</span><strong>{done.length}<small>/ {state?.request?.levels.length ?? 0}</small></strong></div></div>
      </section>
    })}</div>}
    {rows.length > 0 && <details className="bn-details"><summary>测试明细<span>{rows.length} 条记录</span></summary>
      <div className="bn-table-wrap"><table className="bn-table"><thead><tr><th>配置</th><th>请求 / 实际</th><th>速度 · 张/分钟</th><th>采样峰值显存</th><th>测速 / 预热 / 准备</th><th>成功 / 失败</th><th>状态与说明</th></tr></thead><tbody>
        {rows.map(row => { const i = Math.max(0, series.findIndex(s => s.id === (row.series_id ?? 'config-0'))); return <tr key={`${row.series_id}-${row.requested}`}>
          <td><span style={{ color: SERIES_COLORS[i] }}>{String.fromCharCode(65 + i)}</span></td><td>{row.requested} / {row.actual ?? '—'}</td><td>{row.rate_per_s === undefined ? '—' : (row.rate_per_s * 60).toFixed(1)}</td>
          <td>{row.peak_vram_mb === undefined ? '—' : `${(row.peak_vram_mb / 1024).toFixed(2)} GB`}</td><td>{seconds(row.elapsed_s)} / {seconds(row.warmup_s)} / {seconds(row.prepare_s)}</td><td>{row.successful} / {row.failed}</td>
          <td><StatusChip tone={row.status === 'completed' ? 'success' : row.status === 'failed' ? 'danger' : 'neutral'}>{status[row.status] ?? row.status}</StatusChip>{(row.error || row.warning) && <p className="bn-row-note">{row.error || row.warning}</p>}</td>
        </tr> })}
      </tbody></table></div>
      <p className="bn-footnote">建议为各配置达到自身最高速度 90% 的最小成功并发。显存为采样到的整张显卡占用峰值；不同模型或精度的速度对比不代表输出质量相同。</p>
    </details>}
    {state?.report_path && <p className="bn-footnote break-all">报告：{state.report_path}</p>}
  </>
}
