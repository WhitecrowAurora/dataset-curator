import { useEffect, useState } from 'react'
import { Download, RefreshCw } from 'lucide-react'
import { json } from '../../lib/api'
import { SectionPanel, StatusChip } from '../ui/launcher'

const BASE = '/api/conflicts/learning/text-encoding'
type State = {
  tokenizer: { ready: boolean; error?: string }
  text_encoding: { tokenizer_path: string; max_length: number }
  source: { repo: string; revision: string; path: string }
}
type Count = { tokens: number; target: number; limit: number; over_target: boolean; over_limit: boolean }

export function TrainingTokenizer() {
  const [state, setState] = useState<State | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [caption, setCaption] = useState('')
  const [tag, setTag] = useState('')
  const [count, setCount] = useState<Count | null>(null)
  const refresh = async () => setState(await json<State>(BASE))
  useEffect(() => { void refresh().catch(e => setMessage(String(e))) }, [])
  async function run(action: () => Promise<unknown>) {
    setBusy(true); setMessage('')
    try { await action(); await refresh() }
    catch (e) { setMessage(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }
  return <SectionPanel title="训练文本 tokenizer" description="Qwen3.5-0.8B · 用于打标正文的 token 预算和冲突比较">
    <div className="space-y-3" data-training-tokenizer>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span>Qwen3.5-0.8B</span>
          <StatusChip tone={state?.tokenizer.ready ? 'success' : 'neutral'}>
            {state?.tokenizer.ready ? '已就绪' : '待准备'}
          </StatusChip>
          <span className="text-xs text-lx-dim">软目标 384 / 硬上限 {state?.text_encoding.max_length ?? 512} token</span>
        </div>
        <div className="flex gap-2">
          <button type="button" className="lx-btn-ghost" disabled={busy} onClick={() => void run(refresh)}>
            <RefreshCw size={13} className="mr-1 inline" />刷新
          </button>
          <button type="button" className="lx-btn-primary" disabled={busy}
            onClick={() => void run(async () => {
              const result = await json<{ path: string }>(`${BASE}/prepare`, { method: 'POST' })
              setMessage(`Tokenizer 已准备：${result.path}`)
            })}>
            <Download size={13} className="mr-1 inline" />{busy ? '处理中…' : state?.tokenizer.ready ? '补齐 tokenizer 文件' : '下载 tokenizer'}
          </button>
        </div>
      </div>
      <p className="text-xs text-lx-dim">仅下载 tokenizer 和模板文件。下载源、代理沿用模型管理设置。</p>
      {state && <div className="space-y-1 text-xs text-lx-dim">
        <p className="break-all">下载目录：{state.source.path}</p>
        <p className="break-all">当前训练目录：{state.text_encoding.tokenizer_path}</p>
        <details><summary className="cursor-pointer">固定来源</summary>
          <p className="mt-1 break-all">{state.source.repo} · {state.source.revision}</p>
        </details>
      </div>}
      <details className="text-xs"><summary className="cursor-pointer text-lx-secondary">检查训练正文长度</summary>
        <div className="mt-3 space-y-2">
          <textarea aria-label="检查描述" placeholder="待检查的描述" className="lx-input text-xs" value={caption}
            onChange={e => { setCaption(e.target.value); setCount(null) }} />
          <textarea aria-label="检查标签" placeholder="标签（训练模板包含 tag 时计入）" className="lx-input text-xs" value={tag}
            onChange={e => { setTag(e.target.value); setCount(null) }} />
          <button type="button" className="lx-btn-ghost" disabled={busy || !state?.tokenizer.ready}
            onClick={() => void run(async () => { setCount(await json<Count>(`${BASE}/check`, {
              method: 'POST', body: JSON.stringify({ caption, tag }),
            })) })}>按已保存的训练设置检查</button>
          {count && <p className={count.over_limit ? 'text-lx-danger' : count.over_target ? 'text-lx-warning' : 'text-lx-success'}>
            {count.tokens} token · 软目标 {count.target} / 硬上限 {count.limit}
            {count.over_limit ? ' · 超出硬上限，需精简' : count.over_target ? ' · 超出软目标，仍在硬上限内' : ' · 在目标内'}
          </p>}
        </div>
      </details>
      <p className="text-xs text-lx-dim">训练模板、自定义 tokenizer 目录等设置仍在「冲突与审查 → 训练文本设置」中保存。</p>
      {(!state?.tokenizer.ready && state?.tokenizer.error || message) &&
        <p role="status" className="whitespace-pre-wrap break-all text-xs text-lx-warning">{message || state?.tokenizer.error}</p>}
    </div>
  </SectionPanel>
}
