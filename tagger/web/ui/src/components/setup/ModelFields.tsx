import { CheckCircle2 } from 'lucide-react'
import { ChoiceGrid } from '../ui/controls'
import { Field } from './SetupFields'
import { PRECISION_LABELS, setupFieldErrors, type SetupData, type SetupDraft } from '../../lib/setup'
import { enginePrecisions } from '../../lib/nlEngines'

export function ModelFields({ data, value: d, onChange }: {
  data: SetupData; value: SetupDraft; onChange: (value: SetupDraft) => void;
}) {
  const errors = setupFieldErrors(d, 2)
  const localNl = d.mode !== 'tag' && d.engine !== 'existing'
  const model = data.catalog.vlm.find((m) => m.key === d.model)
  return <div className="space-y-5">
    {localNl && <Field label="自然语言权重" error={errors.model} hint="选择后点击开始，助手会下载缺少的原始权重并准备所选精度。">
      <div role="radiogroup" aria-label="自然语言权重" aria-invalid={Boolean(errors.model)} className="grid gap-2 sm:grid-cols-2">
        {data.catalog.vlm.map((m) => <button key={m.key} type="button" role="radio" aria-checked={d.model === m.key}
          onClick={() => onChange({ ...d, model: m.key })}
          className={`min-w-0 rounded-lx-sm border p-3 text-left ${d.model === m.key
            ? 'border-lx-accent bg-lx-accent-subtle' : 'border-lx bg-lx-surface-raised hover:border-lx-accent/50'}`}>
          <span className="flex items-center gap-2 text-sm text-lx"><span className="min-w-0 flex-1 break-all">{m.key}</span>
            {d.model === m.key && <CheckCircle2 size={15} className="shrink-0 text-lx-accent" />}</span>
        </button>)}
      </div>
    </Field>}
    {d.mode !== 'nl' && <Field label="TAG 模型" error={errors.backend}>
      <ChoiceGrid options={[...data.catalog.tag.map((m) => m.backend), ...(d.mode === 'both' ? ['none'] : [])]}
        value={d.backend} ariaLabel="TAG 模型" onChange={(backend) => onChange({ ...d, backend })}
        renderLabel={(v) => v === 'none' ? 'NL' : v === 'pixai' ? 'PixAI' : v} />
    </Field>}
    {localNl && <Field label="自然语言权重量化" hint={model?.note || 'Q6_K 为默认档，保留模型能力并减少显存占用。'}>
      <ChoiceGrid options={d.engine === 'native' ? enginePrecisions(d.engine) : d.engine === 'vllm' ? ['bf16', 'fp8', 'w4a16']
        : model?.gguf_ready?.includes('q8') ? ['f16', 'q4', 'q6', 'q8'] : ['f16', 'q4', 'q6']}
        value={d.precision} ariaLabel="自然语言权重量化" onChange={(precision) => onChange({ ...d, precision })}
        className="grid-cols-2" renderLabel={(v) => PRECISION_LABELS[v] ?? v} />
    </Field>}
    {d.mode !== 'nl' && d.backend !== 'none' && <Field label="TAG 计算精度" hint="BF16 直接加载原始模型，无需量化。">
      <ChoiceGrid options={['bf16', 'tf32', 'fp32', 'fp16']} value={d.tagPrecision} ariaLabel="TAG 计算精度"
        onChange={(tagPrecision) => onChange({ ...d, tagPrecision })} className="grid-cols-4" renderLabel={(v) => v.toUpperCase()} />
    </Field>}
    {d.engine === 'existing' && d.mode !== 'tag' && <p className="text-xs text-lx-muted">已有服务负责 NL 权重与量化；开始只准备本机需要的 TAG 模型。</p>}
  </div>
}

export function SetupDevices({ data, value: d, onChange }: {
  data: SetupData; value: SetupDraft; onChange: (value: SetupDraft) => void;
}) {
  const device = (key: 'tagDevice' | 'nlDevice', label: string) => <Field label={label}>
    <select className="lx-select" aria-label={label} value={d[key]} onChange={(e) => onChange({ ...d, [key]: e.target.value })}>
      <option value="auto">自动</option>
      {data.options.devices.map((v) => <option key={v.value} value={v.value}>{v.name} · {v.vram_gb} GB</option>)}
      {(key === 'tagDevice' || d.engine !== 'vllm') && <option value="cpu">CPU</option>}
    </select>
  </Field>
  return <div className="mt-6 space-y-3 border-t border-lx pt-5">
    <h3 className="text-sm font-medium text-lx">计算设备</h3>
    <div className="grid gap-4 sm:grid-cols-2">
      {d.mode !== 'nl' && d.backend !== 'none' && device('tagDevice', 'TAG 计算设备')}
      {d.mode !== 'tag' && d.engine !== 'existing' && device('nlDevice', 'NL 计算设备')}
    </div>
    <p className="text-xs leading-relaxed text-lx-muted">默认优先 GPU。llama.cpp 会先减少并发与显存占用，仍无法启动时才回退 CPU；vLLM 需要 CUDA。</p>
  </div>
}
