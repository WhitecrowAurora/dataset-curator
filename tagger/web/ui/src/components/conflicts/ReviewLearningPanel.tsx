import { useEffect, useState } from 'react'
import { json } from '../../lib/api'
import { SectionPanel, SettingRow } from '../ui/launcher'

const BASE = '/api/conflicts/learning'
type TextEncoding = { tokenizer_path: string; revision: string; max_length: number; template: string; add_special_tokens: boolean; chat_template: boolean; system_prompt: string }
type Model = { images: number; pairs: number; validation_images: number; validation_pairs: number; validation_accuracy: number | null }
type State = { events: number; training: boolean; error?: string; models: Record<string, Model>; tokenizer: { ready: boolean; error?: string }; text_encoding: TextEncoding }

export function ReviewLearningPanel() {
  const [state, setState] = useState<State | null>(null)
  const [spec, setSpec] = useState<TextEncoding | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [caption, setCaption] = useState('')
  const [tag, setTag] = useState('')
  const [count, setCount] = useState<{ tokens: number; target: number; limit: number; over_target: boolean; over_limit: boolean } | null>(null)
  async function load() {
    const next = await json<State>(BASE)
    setState(next)
    setSpec((current) => current ?? next.text_encoding)
  }
  useEffect(() => {
    const refresh = () => void load().catch((e) => setError(String(e)))
    refresh()
    const timer = window.setInterval(refresh, 10000)
    return () => window.clearInterval(timer)
  }, [])
  async function run(action: () => Promise<unknown>) {
    setBusy(true); setError('')
    try { await action(); await load() }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }
  return <SectionPanel title="审查偏好与训练文本" description="人工操作自动积累反馈；偏好分辅助选择，不代表标注正确率">
    <div className="flex flex-wrap items-center gap-3 text-xs text-lx-secondary">
      <span>{state?.events ?? 0} 次反馈{state?.training ? ' · 更新评价器中' : ''}</span>
      <button className="lx-btn-ghost text-xs" disabled={busy} onClick={() => void run(() => json(`${BASE}/train`, { method: 'POST' }))}>更新偏好评价器</button>
      <button className="lx-btn-ghost text-xs" disabled={busy} onClick={() => void run(load)}>刷新</button>
      <a className="text-lx-accent" href={`${BASE}/export`} download>导出反馈样本</a>
    </div>
    <p className="mt-2 text-xs text-lx-muted">文本偏好学习描述风格；发色偏好学习你对视觉模型结果的取舍。撤销与自动策略不会作为人工优选样本。</p>
    {Object.entries(state?.models ?? {}).map(([kind, model]) => <p key={kind} className="mt-1 text-xs text-lx-secondary">
      {kind === 'hair' ? '发色' : kind === 'nl' ? '描述' : '标签'}：{model.images} 张图 / {model.pairs} 对偏好；
      {model.validation_accuracy === null ? '尚无独立图片评估' : `留出 ${model.validation_images} 张图、${model.validation_pairs} 对，一致率 ${(model.validation_accuracy * 100).toFixed(1)}%`}
    </p>)}
    <details className="mt-3 text-xs text-lx-secondary"><summary className="cursor-pointer">训练文本 tokenizer 设置与长度检查</summary>
      {spec && <div className="mt-3 flex flex-col gap-2">
        <SettingRow label="Tokenizer 目录"><input className="lx-input text-xs" value={spec.tokenizer_path} onChange={(e) => setSpec({ ...spec, tokenizer_path: e.target.value })} /></SettingRow>
        <SettingRow label="固定版本"><input className="lx-input text-xs" value={spec.revision} onChange={(e) => setSpec({ ...spec, revision: e.target.value })} /></SettingRow>
        <SettingRow label="训练 token 硬上限" help="默认 512，最多 512；尽量不超过 384，超过软目标但未到硬上限时保留必要描述"><input type="number" min={1} max={512} className="lx-input text-xs" value={spec.max_length} onChange={(e) => setSpec({ ...spec, max_length: Number(e.target.value) })} /></SettingRow>
        <SettingRow label="训练文本模板" help="支持 {caption} 和 {tag}；填写实际送入文本编码器的格式"><textarea className="lx-input text-xs" value={spec.template} onChange={(e) => setSpec({ ...spec, template: e.target.value })} /></SettingRow>
        <label><input type="checkbox" checked={spec.add_special_tokens} onChange={(e) => setSpec({ ...spec, add_special_tokens: e.target.checked })} /> 添加特殊 token</label>
        <label><input type="checkbox" checked={spec.chat_template} onChange={(e) => setSpec({ ...spec, chat_template: e.target.checked })} /> 训练端使用 chat template</label>
        {spec.chat_template && <SettingRow label="System 文本"><textarea className="lx-input text-xs" value={spec.system_prompt} onChange={(e) => setSpec({ ...spec, system_prompt: e.target.value })} /></SettingRow>}
        <div className="flex flex-wrap gap-2">
          <button disabled={busy} className="lx-btn-ghost text-xs" onClick={() => void run(() => json(`${BASE}/text-encoding/prepare`, { method: 'POST' }))}>准备 Qwen3.5-0.8B tokenizer</button>
          <button disabled={busy} className="lx-btn-primary text-xs" onClick={() => void run(() => json(`${BASE}/text-encoding`, { method: 'PUT', body: JSON.stringify(spec) }))}>保存训练文本设置</button>
          <span>{state?.tokenizer.ready ? 'Tokenizer 已就绪' : 'Tokenizer 未就绪'}</span>
        </div>
        <textarea aria-label="检查描述" placeholder="待检查的描述" className="lx-input text-xs" value={caption} onChange={(e) => { setCaption(e.target.value); setCount(null) }} />
        <textarea aria-label="检查标签" placeholder="待拼接的标签（模板包含 tag 时计入）" className="lx-input text-xs" value={tag} onChange={(e) => { setTag(e.target.value); setCount(null) }} />
        <button disabled={busy} className="lx-btn-ghost w-fit text-xs" onClick={() => void run(async () => { setCount(await json(`${BASE}/text-encoding/check`, { method: 'POST', body: JSON.stringify({ caption, tag }) })) })}>按已保存设置检查长度</button>
        {count && <p className={count.over_limit ? 'text-lx-danger' : count.over_target ? 'text-lx-warning' : 'text-lx-success'}>{count.tokens} token · 软目标 {count.target} / 硬上限 {count.limit}{count.over_limit ? ' · 超出硬上限，需裁短' : count.over_target ? ' · 超出软目标，仍可接受' : ' · 在目标内'}</p>}
        {!state?.tokenizer.ready && <p className="text-lx-warning">{state?.tokenizer.error}</p>}
      </div>}
    </details>
    {(error || state?.error) && <p className="mt-2 text-xs text-lx-danger">{error || state?.error}</p>}
  </SectionPanel>
}
