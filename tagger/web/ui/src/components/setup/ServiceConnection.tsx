import { useEffect, useState } from 'react'
import { Loader2, Play, RefreshCw } from 'lucide-react'
import { api, type VllmState } from '../../lib/api'
import type { SetupDraft } from '../../lib/setup'
import { failureAdvice } from '../../lib/guidance'
import { ErrorBox } from '../ui/controls'

export function ServiceConnection({ draft: d }: { draft: SetupDraft }) {
  const [state, setState] = useState<VllmState | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const starting = state?.status === 'starting'
  useEffect(() => {
    if (d.mode === 'tag' || d.engine === 'existing') return
    let alive = true, timer = 0
    const tick = async () => {
      try { const value = await api.vllm(); if (alive) setState(value) } catch { /* 显式操作显示错误 */ }
      if (alive && starting) timer = window.setTimeout(tick, 2000)
    }
    void tick()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [d.mode, d.engine, starting])
  if (d.mode === 'tag') return null
  const running = state?.status === 'ready'
  const connect = async () => {
    setBusy(true); setMessage(''); setError('')
    try {
      if (d.engine === 'existing') {
        const result = await api.vllmProbe(d.url)
        if (result.ok) setMessage(`连接成功 · ${result.models.join(' / ') || '服务已响应'}`)
        else setError(result.error || '服务没有响应')
      } else {
        setState(await api.vllmStart(Number(new URL(d.url).port || 8000), 900, {
          engine: d.engine, model: d.model, precision: d.precision, served_name: d.servedName,
          device: d.nlDevice,
        }))
      }
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }
  return <div className="mt-4 space-y-2 border-t border-lx pt-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="text-xs text-lx-muted">{d.engine === 'existing' ? '已有推理服务'
        : starting ? '推理服务启动中，正在加载模型…' : running ? `推理服务已启动 · ${state.url}` : '权重准备好后可直接启动推理服务'}</p>
      <button type="button" className="lx-btn-ghost inline-flex items-center gap-2"
        disabled={busy || starting || (d.engine !== 'existing' && running)} onClick={() => void connect()}>
        {busy || starting ? <Loader2 size={14} className="animate-spin" /> : d.engine === 'existing' ? <RefreshCw size={14} /> : <Play size={14} />}
        {d.engine === 'existing' ? '测试连接' : running ? '服务已启动' : '启动推理服务'}</button>
    </div>
    {message && <p className="break-all text-xs text-lx-muted">{message}</p>}
    {error && <ErrorBox error={error} nextStep={failureAdvice(error, 'service')} />}
    {d.engine !== 'existing' && state?.status === 'failed' && <ErrorBox title="推理服务启动失败"
      error={state.error || state.log_tail.slice(-2).join('\n') || '启动失败，请查看任务页的服务日志'}
      nextStep={failureAdvice(state.error || state.log_tail.slice(-2).join('\n'), 'service')} />}
  </div>
}
