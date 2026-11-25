import { Plus, X } from 'lucide-react'
import { nextConfig, readyConfig, SERIES_COLORS, type BenchmarkConfig, type BenchmarkOptions } from '../../lib/benchmark'
import { ENGINE_LABELS } from '../../lib/nlEngines'

export function BenchmarkConfigs({ options, values, locked, onChange }: {
  options: BenchmarkOptions | null; values: BenchmarkConfig[]; locked: boolean; onChange: (v: BenchmarkConfig[]) => void
}) {
  const change = (index: number, patch: Partial<BenchmarkConfig>) => onChange(values.map((v, i) => i === index ? readyConfig(options, { ...v, ...patch }) : v))
  return <div className="bn-configs">
    {values.map((value, i) => {
      const model = options?.models.find(m => m.key === value.model)
      const variant = model?.variants.find(v => v.engine === value.engine)
      const letter = String.fromCharCode(65 + i)
      return <section key={i} className="bn-config" style={{ borderTopColor: SERIES_COLORS[i] }} aria-label={`配置 ${letter}`}>
        <div className="bn-config-title"><span className="bn-letter" style={{ color: SERIES_COLORS[i] }}>{letter}</span><strong>对比配置</strong>
          {values.length > 1 && <button className="lx-btn-ghost-icon ml-auto" aria-label={`移除配置 ${letter}`} disabled={locked} onClick={() => onChange(values.filter((_, n) => n !== i))}><X size={14} /></button>}
        </div>
        <label className="bn-field"><span>本地模型</span><select className="lx-select" aria-label={`模型 ${letter}`} disabled={locked} value={value.model} onChange={e => change(i, { model: e.target.value })}>
          {!options?.models.length && <option value="">暂无可测试模型</option>}
          {options?.models.map(m => <option key={m.key} value={m.key}>{m.title}</option>)}
        </select></label>
        <div className="bn-pair">
          <label className="bn-field"><span>推理引擎</span><select className="lx-select" aria-label={`引擎 ${letter}`} disabled={locked} value={value.engine} onChange={e => change(i, { engine: e.target.value })}>
            {model?.variants.map(v => <option key={v.engine} value={v.engine}>{ENGINE_LABELS[v.engine] ?? v.engine}</option>)}
          </select></label>
          <label className="bn-field"><span>已有精度</span><select className="lx-select" aria-label={`精度 ${letter}`} disabled={locked} value={value.precision} onChange={e => change(i, { precision: e.target.value })}>
            {variant?.precisions.map(p => <option key={p} value={p}>{p.toUpperCase()}</option>)}
          </select></label>
        </div>
      </section>
    })}
    {values.length < 4 && <button className="bn-add" disabled={locked || !options?.models.length} onClick={() => onChange([...values, nextConfig(options, values)])}><Plus size={19} /><span>添加对比</span><small>模型 / 引擎 / 精度</small></button>}
  </div>
}
