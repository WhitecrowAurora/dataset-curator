import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type CenterDispatchPlanState } from '../../lib/api'
import { useFolderPicker } from '../FolderPicker'
import { ErrorBox } from '../ui/controls'
import { SectionPanel, SettingRow } from '../ui/launcher'

export function DispatchPlans({ connected, locked, onApplied }: { connected: boolean; locked: boolean; onApplied: () => void }) {
  const [path, setPath] = useState('')
  const [state, setState] = useState<CenterDispatchPlanState | null>(null)
  const [user, setUser] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const picker = useFolderPicker()
  const pendingId = useRef<string | undefined>(undefined)
  const receive = useCallback((value: CenterDispatchPlanState) => {
    if (pendingId.current !== value.pending?.id) setUser(value.pending?.selected_user || '')
    pendingId.current = value.pending?.id; setState(value)
  }, [])
  useEffect(() => {
    let alive = true, timer: ReturnType<typeof setTimeout>
    const check = async () => {
      try { const value = await api.centerDispatchPlans(); if (alive) receive(value) }
      catch { /* 自动通知的网络波动不打断当前打标；手动检查显示具体错误。 */ }
      if (alive) timer = setTimeout(check, 2000)
    }
    void check(); return () => { alive = false; clearTimeout(timer) }
  }, [receive])
  const act = async (action: () => Promise<CenterDispatchPlanState>, applied = false) => {
    setBusy(true); setError('')
    try { receive(await action()); if (applied) onApplied() } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  const pending = state?.pending
  const tasks = pending?.tasks.filter((t) => !user || t.user_id === user) || state?.applied?.tasks || []
  return <SectionPanel title="中心分派通知与导入">
    {error && <ErrorBox error={error} />}
    <SettingRow label="分派清单">
      <div className="flex flex-wrap gap-2"><input aria-label="分派清单路径" className="lx-input flex-1 text-xs" value={path} disabled={busy} onChange={(e) => setPath(e.target.value)} />
        <button type="button" className="lx-btn-ghost" disabled={busy} onClick={() => picker.pick(path, setPath, ['json', 'zip'])}>浏览</button>
        <button type="button" className="lx-btn-primary" disabled={busy || !path.trim()} onClick={() => void act(() => api.centerDispatchPlanImport(path))}>导入分派</button>
        <button type="button" className="lx-btn-ghost" disabled={busy || !connected} onClick={() => void act(api.centerDispatchPlanCheck)}>检查中心分派</button></div>
    </SettingRow>
    {pending && <div role="status" className="my-3 rounded-lx border border-lx-accent/40 bg-lx-accent-subtle p-3">
      <p className="text-sm">收到调度中心的分派，是否应用？</p>
      <p className="mt-1 break-all text-xs text-lx-secondary">{pending.workspace.name || pending.workspace.ws_id} · {pending.id} · {pending.server}</p>
      <SettingRow label="分派用户"><select aria-label="分派用户" className="lx-input text-xs" disabled={busy} value={user} onChange={(e) => setUser(e.target.value)}>
        <option value="">请选择用户</option>{pending.recipients.map((id) => <option key={id} value={id}>{id}</option>)}
      </select></SettingRow>
      <details className="my-2 text-xs"><summary className="cursor-pointer">仓库设置</summary>{pending.repositories.map((repo) => <div key={repo.repo} className="mt-2">
        <p>{repo.repo}</p><pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify({ 画师: repo.artist, 图片总表: repo.source }, null, 2)}</pre></div>)}</details>
      <p className="text-xs text-lx-muted">联网应用后由中心下发此清单范围内的任务；退出此分派后恢复自动接单。此表的离线应用须先联网确认占用；正式离线工单可直接在离线面板导入。</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className="lx-btn-primary" disabled={busy || locked || !connected || !user} onClick={() => void act(() => api.centerDispatchPlanApply('online', user), true)}>应用为联网任务</button>
        <button type="button" className="lx-btn-ghost" disabled={busy || locked || !connected || !user} onClick={() => void act(() => api.centerDispatchPlanApply('offline', user), true)}>应用为离线任务</button>
        <button type="button" className="lx-btn-ghost" disabled={busy} onClick={() => void act(api.centerDispatchPlanDismiss)}>暂不应用</button>
      </div>
    </div>}
    {state?.applied && <div className="my-2 flex flex-wrap items-center gap-2 text-xs">
      <span>已应用 {state.applied.id} · {state.applied.mode === 'online' ? '联网任务，点击上方开始' : '离线任务，使用下方离线面板开始'} · 不自动开跑</span>
      <button type="button" className="lx-btn-ghost" disabled={busy || locked} onClick={() => void act(api.centerDispatchPlanClear, true)}>退出此分派</button>
    </div>}
    {!!tasks.length && <div className="max-h-72 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th className="p-2">仓库／分包</th><th className="p-2">用户／设备</th><th className="p-2">分项与配置</th></tr></thead><tbody>
      {tasks.map((task) => <tr key={`${task.repo}/${task.package}/${task.part}`} className="border-t border-lx">
        <td className="break-all p-2">{task.repo}<br />{task.package}</td><td className="break-all p-2">{task.user_id}<br />{task.device_id || '该用户可用设备'}</td>
        <td className="p-2">{task.part.toUpperCase()} · {task.part==='tag'
          ? `${task.config.tag_backend} · TAG 批大小 ${task.config.tag_batch || '自动'}`
          : `${task.config.nl_model || '自动模型'} · ${task.config.nl_engine} ${task.config.nl_precision} · NL 并发 ${task.config.concurrency || '自动'}`}<br />画师：{JSON.stringify(task.annotation)}</td>
      </tr>)}
    </tbody></table></div>}
    {picker.dialog}
  </SectionPanel>
}
