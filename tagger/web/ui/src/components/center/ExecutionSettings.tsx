import { useEffect, useState } from 'react'
import { api, type CenterExecution, type CenterOptions } from '../../lib/api'
import { ChoiceGrid, ToggleRow } from '../ui/controls'
import { SettingRow } from '../ui/launcher'
import { ENGINE_LABELS, enginePrecisions } from '../../lib/nlEngines'
import { PRECISION_LABELS } from '../../lib/setup'

export const DEFAULT_EXECUTION: CenterExecution = {
  hosting_mode: 'active', inference_source: 'local', tag_backend: 'wd-eva02',
  tag_device: 'auto', tag_precision: 'tf32', tag_batch: 0, nl_engine: 'llamacpp',
  nl_model: '', nl_device: 'auto', nl_precision: 'f16', served_name: 'tagger',
  concurrency: 0, gpu_mem: 0, auto_download: true, auto_quantize: false,
  stop_service: true, resume_on_startup: false,
}

const LABELS: Record<string, string> = {
  active: '主动模式', passive: '被动模式', local: '本机服务', existing: '已有服务',
  ...PRECISION_LABELS, none: 'NL',
}

export function ExecutionSettings({ value, onChange, mode, disabled, token, onToken,
  remember, onRemember, tokenSet, onClear, offline = false }: {
  value: CenterExecution; onChange: (value: CenterExecution) => void; mode: string;
  disabled: boolean; token: string; onToken: (value: string) => void;
  remember: boolean; onRemember: (value: boolean) => void; tokenSet: boolean; onClear: () => void;
  offline?: boolean;
}) {
  const [catalog, setCatalog] = useState<CenterOptions | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    api.centerOptions().then(setCatalog).catch((e) => setError(String(e)))
  }, [])
  useEffect(() => {
    if (!disabled && value.inference_source === 'local' && value.nl_engine === 'native'
      && !enginePrecisions(value.nl_engine).includes(value.nl_precision)) {
      onChange({ ...value, nl_precision: 'q6' })
    }
  }, [value, onChange, disabled])
  const put = <K extends keyof CenterExecution>(key: K, v: CenterExecution[K]) =>
    onChange({ ...value, [key]: v })
  const device = (key: 'tag_device' | 'nl_device') => (
    <select className="lx-select text-xs" value={value[key]} disabled={disabled}
      aria-label={key === 'tag_device' ? 'TAG 设备' : 'NL 设备'} onChange={(e) => put(key, e.target.value)}>
      <option value="auto">自动</option>
      {(key === 'tag_device' || value.nl_engine !== 'vllm') && <option value="cpu">CPU</option>}
      {catalog?.devices.map((d) => <option key={d.value} value={d.value}>
        {d.value} · {d.name} · {d.vram_gb} GB
      </option>)}
    </select>
  )
  const number = (key: 'tag_batch' | 'concurrency' | 'gpu_mem', max: number, step = 1) => (
    <input className="lx-input text-xs" type="number" aria-label={key}
      min={0} max={max} step={step} disabled={disabled} value={value[key]}
      onChange={(e) => put(key, Math.max(0, Math.min(max, Number(e.target.value) || 0)))} />
  )
  return <>
    {error && <p className="text-xs text-lx-warning">模型清单读取失败：{error}</p>}
    {!offline && <SettingRow label="托管模式">
      <ChoiceGrid options={['active', 'passive'] as const} value={value.hosting_mode}
        onChange={(v) => put('hosting_mode', v)} disabled={disabled} size="sm"
        className="grid-cols-2" ariaLabel="托管模式" renderLabel={(v) => LABELS[v]} />
    </SettingRow>}
    {mode !== 'tag' && <SettingRow label="NL 推理方式">
      <ChoiceGrid options={['local', 'existing'] as const} value={value.inference_source}
        onChange={(v) => put('inference_source', v)} disabled={disabled} size="sm"
        className="grid-cols-2" ariaLabel="NL 推理方式" renderLabel={(v) => LABELS[v]} />
    </SettingRow>}
    {mode !== 'nl' && <>
      <SettingRow label="TAG 模型" notice={mode === 'tag' && value.tag_backend === 'none'
        ? '只出 TAG 时需要选择一个分类模型。' : catalog && value.tag_backend !== 'none'
          && !catalog.tag_models.some((m) => m.backend === value.tag_backend) ? '当前模型已不在清单中，请重新选择。' : ''}>
        <select className="lx-select text-xs" value={value.tag_backend} disabled={disabled}
          aria-label="TAG 模型" onChange={(e) => put('tag_backend', e.target.value)}>
          {(catalog?.tag_models ?? [{ backend: 'wd-eva02', ready: false }]).map((m) =>
            <option key={m.backend} value={m.backend}>{m.backend}</option>)}
          {mode === 'both' && <option value="none">{LABELS.none}</option>}
        </select>
      </SettingRow>
      {value.tag_backend !== 'none' && <>
        <SettingRow label="TAG 设备" help={mode === 'tag' ? '自动模式优先第一张可见显卡，无显卡时使用 CPU'
          : '自动模式优先与 NL 分卡；单卡优先 GPU，加载失败时回退 CPU'}>{device('tag_device')}</SettingRow>
        <SettingRow label="TAG 计算精度">
          <ChoiceGrid options={['tf32', 'fp32', 'bf16', 'fp16']} value={value.tag_precision}
            onChange={(v) => put('tag_precision', v)} disabled={disabled} size="sm"
            className="grid-cols-4" ariaLabel="TAG 计算精度" renderLabel={(v) => v.toUpperCase()} />
        </SettingRow>
        <SettingRow label="TAG 批大小" help="0 = 自动">{number('tag_batch', 128)}</SettingRow>
      </>}
    </>}
    {mode !== 'tag' && <>
      {value.inference_source === 'local' && <>
        <SettingRow label="NL 引擎" notice={catalog?.engine_status?.[value.nl_engine]?.available === false
          ? catalog.engine_status[value.nl_engine].reason : ''}>
          <ChoiceGrid options={['llamacpp', 'vllm', 'native'] as const} value={value.nl_engine}
            onChange={(v) => onChange({ ...value, nl_engine: v, nl_precision: v === 'native' ? 'q6' : v === 'llamacpp' ? 'f16' : 'bf16',
              nl_device: v === 'vllm' && value.nl_device === 'cpu' ? 'auto' : value.nl_device })}
            disabled={disabled} size="sm" className="grid-cols-2" ariaLabel="NL 引擎"
            renderLabel={(v) => ENGINE_LABELS[v]} />
        </SettingRow>
        <SettingRow label="NL 模型" notice={catalog && value.nl_model && !catalog.nl_models.some((m) => m.key === value.nl_model)
          ? '当前模型已不在清单中，请重新选择，或选择按显存自动选择。' : ''}>
          <select className="lx-select text-xs" value={value.nl_model} disabled={disabled}
            aria-label="NL 模型" onChange={(e) => put('nl_model', e.target.value)}>
            <option value="">自动</option>
            {catalog?.nl_models.map((m) => <option key={m.key} value={m.key}>
              {m.key}
            </option>)}
          </select>
        </SettingRow>
        <SettingRow label="NL 设备">{device('nl_device')}</SettingRow>
        <SettingRow label="NL 权重量化精度" help="所选档位需要对应权重文件；自动量化单独开启">
          <ChoiceGrid options={enginePrecisions(value.nl_engine)}
            value={value.nl_precision} onChange={(v) => put('nl_precision', v)} disabled={disabled}
            size="sm" ariaLabel="NL 权重量化精度" renderLabel={(v) => LABELS[v] ?? v} />
        </SettingRow>
        {value.nl_engine !== 'native' && <SettingRow label="NL 显存比例" help="0 = 自动；vLLM 按比例预留，llama.cpp 自动并发时按比例计算槽位"
          notice={value.gpu_mem > 0 && value.gpu_mem < 0.1 ? '显存比例请设为 0（自动），或 0.1～0.98。' : ''}>
          {number('gpu_mem', 0.98, 0.05)}
        </SettingRow>}
      </>}
      <SettingRow label="服务模型名" help="与已有服务的模型名一致；本机启动会自动使用此名称"
        notice={!value.served_name.trim() ? '请填写服务模型名；本机启动可使用 tagger。' : ''}>
        <input className="lx-input text-xs" value={value.served_name} disabled={disabled}
          aria-invalid={!value.served_name.trim()}
          aria-label="服务模型名" onChange={(e) => put('served_name', e.target.value)} />
      </SettingRow>
      <SettingRow label="推理并发" help="0 = 自动；本机服务的槽位与客户端请求并发同步设置">
        {number('concurrency', 64)}
      </SettingRow>
    </>}
    <SettingRow label="Hugging Face Token" help="私有分片或受限模型需要；Token 仅保留在本机">
      <div className="flex gap-2">
        <input className="lx-input font-mono text-xs" type="password" autoComplete="off"
          aria-label="Hugging Face Token" value={token} disabled={disabled}
          placeholder={tokenSet ? '已配置，留空沿用' : '可选，hf_…'} onChange={(e) => onToken(e.target.value)} />
        {tokenSet && <button type="button" className="lx-btn-ghost" disabled={disabled} onClick={onClear}>清除</button>}
      </div>
      <ToggleRow checked={remember} onChange={onRemember} disabled={disabled} label="记住 Token" />
    </SettingRow>
    <div className="mt-3 border-t border-lx pt-3">
      <ToggleRow checked={value.auto_download} onChange={(v) => put('auto_download', v)} disabled={disabled}
        label="自动下载" />
      {mode !== 'tag' && value.inference_source === 'local' && <>
        <ToggleRow checked={value.auto_quantize} onChange={(v) => put('auto_quantize', v)} disabled={disabled}
          label="自动量化" />
        <ToggleRow checked={value.stop_service} onChange={(v) => put('stop_service', v)} disabled={disabled}
          label="停止时关闭服务" />
      </>}
      {!offline && <ToggleRow checked={value.resume_on_startup} onChange={(v) => put('resume_on_startup', v)} disabled={disabled}
        label="启动时恢复" />}
    </div>
  </>
}
