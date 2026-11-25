import { useEffect, useState } from 'react'
import { api, type CenterDispatchPlanState } from '../../lib/api'

export function DispatchPlanNotice({ connected, centerPage, onOpen }: { connected: boolean; centerPage: boolean; onOpen: () => void }) {
  const [state, setState] = useState<CenterDispatchPlanState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let alive = true, timer: ReturnType<typeof setTimeout>
    const check = async () => {
      try {
        const local = await api.centerDispatchPlans()
        if (alive) setState(local)
        if (connected) {
          const latest = await api.centerDispatchPlanCheck()
          if (alive) setState(latest)
        }
      } catch { /* 断网时保留已收到的通知，下一轮重试。 */ }
      if (alive) timer = setTimeout(check, 30000)
    }
    void check(); return () => { alive = false; clearTimeout(timer) }
  }, [connected, centerPage])
  const dismiss = async () => {
    setBusy(true); setError('')
    try { setState(await api.centerDispatchPlanDismiss()) } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  if (!state?.pending || centerPage) return null
  return <div role="status" className="mb-4 flex flex-wrap items-center gap-3 rounded-lx border border-lx-accent/40 bg-lx-accent-subtle p-3">
    <div className="min-w-0 flex-1"><p className="text-sm">收到调度中心的分派，是否应用？</p>
      <p className="mt-1 break-all text-xs text-lx-secondary">{state.pending.workspace.name || state.pending.workspace.ws_id} · {state.pending.tasks.length} 个分项</p>
      {error && <p className="mt-1 text-xs text-lx-danger">{error}</p>}</div>
    <button type="button" className="lx-btn-primary" onClick={onOpen}>查看并选择应用方式</button>
    <button type="button" className="lx-btn-ghost" disabled={busy} onClick={() => void dismiss()}>暂不应用</button>
  </div>
}
