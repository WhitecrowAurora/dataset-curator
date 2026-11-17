import type { ReactNode } from 'react'
import { FolderOpen } from 'lucide-react'
import { ChoiceGrid, ToggleRow } from '../ui/controls'
import { useFolderPicker } from '../FolderPicker'
import type { SetupData, SetupDraft } from '../../lib/setup'
import { localServiceSettings, setupFieldErrors } from '../../lib/setup'
import { FieldNotice } from '../ui/FieldNotice'

export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: ReactNode }) {
  return <div className="space-y-2"><div className="text-sm font-medium text-lx">{label}</div>
    {children}<FieldNotice message={error} />{hint && <p className="text-xs leading-relaxed text-lx-muted">{hint}</p>}</div>
}

export function SetupFields({ step, data, value: d, onChange, secret, onSecret }: {
  step: number; data: SetupData; value: SetupDraft; onChange: (d: SetupDraft) => void;
  secret: string; onSecret: (v: string) => void;
}) {
  const put = <K extends keyof SetupDraft>(key: K, value: SetupDraft[K]) => onChange({ ...d, [key]: value })
  const folder = useFolderPicker()
  const errors = setupFieldErrors(d, step)
  const input = (key: 'url' | 'servedName' | 'server' | 'userId' | 'proxy', label: string, placeholder?: string) =>
    <input className="lx-input" aria-label={label} aria-invalid={Boolean(errors[key])} value={d[key]} placeholder={placeholder}
      onChange={(e) => put(key, e.target.value)} />
  const directory = (key: 'input' | 'output', label: string, hint?: string) =>
    <Field label={label} hint={hint} error={errors[key]}><div className="flex gap-2">
      <input className="lx-input min-w-0 flex-1" aria-label={label} aria-invalid={Boolean(errors[key])} value={d[key]}
        onChange={(e) => put(key, e.target.value)} />
      <button type="button" className="lx-btn-ghost" aria-label={`选择${label}`}
        onClick={() => folder.pick(d[key] || undefined, (v) => put(key, v))}><FolderOpen size={16} /></button>
    </div></Field>
  return <div className="space-y-6">
    {step === 0 && <>
      <Field label="下载源" hint="受限模型建议使用官方源；留空沿用环境设置。">
        <input className="lx-input" aria-label="下载源" list="setup-endpoints" value={d.endpoint}
          placeholder="https://huggingface.co" onChange={(e) => put('endpoint', e.target.value)} />
        <datalist id="setup-endpoints"><option value="https://huggingface.co" /><option value="https://hf-mirror.com" /></datalist>
      </Field>
      <Field label="下载代理" hint="模型与分片共用；留空直连。">
        {input('proxy', '下载代理', 'http://127.0.0.1:7890')}
      </Field>
    </>}
    {step === 1 && <>
      <Field label="产出模式"><ChoiceGrid options={['both', 'tag', 'nl'] as const} value={d.mode}
        ariaLabel="输出内容" onChange={(mode) => onChange({ ...d, mode,
          backend: mode === 'tag' && d.backend === 'none' ? data.catalog.tag[0]?.backend ?? 'wd-eva02' : d.backend })}
        renderLabel={(v) => ({ both: 'TAG + NL', tag: 'TAG', nl: 'NL' })[v]} />
      </Field>
      {d.mode !== 'tag' && <Field label="NL 引擎" hint="Windows 本机推荐 llama.cpp；vLLM 需要 Linux 或 WSL 环境。">
        <ChoiceGrid options={['llamacpp', 'vllm', 'native', 'existing'] as const} value={d.engine} ariaLabel="自然语言后端"
          optionDisabled={(v) => v === 'vllm' && !data.options.engines.includes('vllm')}
          onChange={(engine) => onChange({ ...d, engine, precision: engine === 'vllm' ? 'bf16' : 'q6',
            ...(engine !== 'existing' ? localServiceSettings(d.url, d.servedName) : {}),
            nlDevice: engine === 'vllm' && d.nlDevice === 'cpu' ? 'auto' : d.nlDevice })}
          renderLabel={(v) => ({ llamacpp: 'llama.cpp', vllm: 'vLLM', native: 'lulynx', existing: '已有服务' })[v]} />
      </Field>}
      {d.mode !== 'tag' && d.engine === 'existing' && <div className="grid gap-4 sm:grid-cols-2">
        <Field label="已有服务地址" error={errors.url}>{input('url', '已有服务地址')}</Field>
        <Field label="已有服务模型" error={errors.servedName}>{input('servedName', '已有服务模型')}</Field>
      </div>}
      {d.mode === 'tag' && <p className="text-sm text-lx-muted">TAG 分类后端在下一步选择，无需自然语言服务或 GGUF 量化。</p>}
    </>}
    {step === 3 && <>
      <Field label="使用方式"><ChoiceGrid options={['center', 'local'] as const} value={d.usage}
        ariaLabel="使用方式" className="grid-cols-2" onChange={(usage) => put('usage', usage)}
        renderLabel={(v) => v === 'center' ? '调度中心' : '本地模式'} /></Field>
      {d.usage === 'center' ? <>
        <Field label="调度中心地址" error={errors.server}>{input('server', '调度中心地址', 'https://调度中心地址')}</Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="用户 ID" error={errors.userId}>{input('userId', '用户 ID')}</Field>
          <Field label="连接密钥" hint="管理员提供的签发密钥；已有连接可留空。">
            <input className="lx-input" aria-label="连接密钥" type="password" autoComplete="off"
              value={secret} onChange={(e) => onSecret(e.target.value)} />
          </Field>
        </div>
        <Field label="调度方式"><ChoiceGrid options={['active', 'passive'] as const} value={d.hosting}
          ariaLabel="调度方式" className="grid-cols-2" onChange={(v) => put('hosting', v)}
          renderLabel={(v) => v === 'active' ? '主动模式' : '被动模式'} /></Field>
      </> : <>{directory('input', '素材目录')}
        <Field label="打标产物格式"><select className="lx-select" aria-label="打标产物格式"
          value={d.artifactFormat} onChange={(e) => put('artifactFormat', e.target.value)}>
          <option value="jsonl">JSONL</option><option value="json">JSON</option><option value="parquet">Parquet</option>
        </select></Field>
        <Field label="封包格式"><ChoiceGrid options={['parquet', 'webdataset', 'zip'] as const}
          value={d.packFormat} ariaLabel="封包格式" onChange={(v) => put('packFormat', v)} /></Field>
      </>}
      {directory('output', '产物目录', '打标结果保存位置；调度模式还用于分片工作目录。')}
      {d.usage === 'center' && <div className="space-y-3 border-t border-lx pt-4">
        <ToggleRow label="自动回传" checked={d.autoUpload} onChange={(v) => put('autoUpload', v)} />
        <ToggleRow label="等待派工" checked={d.wait} onChange={(v) => put('wait', v)} />
        <ToggleRow label="启动时恢复" checked={d.resume} onChange={(v) => put('resume', v)} />
      </div>}
    </>}
    {folder.dialog}
  </div>
}
