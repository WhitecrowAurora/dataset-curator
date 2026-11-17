import { useCallback, useEffect, useState } from 'react'
import { api, type CenterWorkspacePage } from '../../lib/api'
import { ErrorBox } from '../ui/controls'
import { SectionPanel } from '../ui/launcher'

export function WorkspacePackages({ connected, onChanged }: { connected: boolean; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<CenterWorkspacePage | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const load = useCallback(async (offset = 0) => {
    setBusy(true); setError('')
    try {
      const page = await api.centerWorkspace(offset)
      setData((old) => offset && old ? { ...page, items: [...old.items, ...page.items] } : page)
      if (!offset) setSelected(new Set())
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }, [])
  useEffect(() => { if (connected && open) void load() }, [connected, open, load])
  return <SectionPanel title="工作区分包" trailing={<button type="button" className="lx-btn-ghost" disabled={!connected}
    onClick={() => setOpen(!open)}>{open ? '收起列表' : '查看分包列表'}</button>}>
    {!connected && <p className="text-xs text-lx-muted">连接中心后查看工作区和待处理分包。</p>}
    {open && <>
      {error && <ErrorBox error={error} />}
      {message && <p className="text-xs text-lx-muted">{message}</p>}
      {data && <p className="mb-2 text-xs text-lx-muted">{data.workspace?.name || data.workspace?.ws_id || '公共工作区'} · 待处理 {data.total} 包
        {data.allow_manual_claim ? ' · 允许手动领取' : ' · 由管理员分配'}</p>}
      {data && !data.items.length && <div className="flex min-h-32 items-center justify-center text-sm text-lx-muted">{data.empty_reason || '暂无待处理分包'}</div>}
      <div className="max-h-80 overflow-y-auto">
        {data?.items.map((p) => {
          const key = `${p.repo}/${p.id}`
          return <label key={key} className="flex items-center gap-3 border-b border-lx py-2 text-xs">
            <input type="checkbox" checked={selected.has(key)} disabled={busy || !data.allow_manual_claim || !p.claimable}
              onChange={(e) => setSelected((old) => { const next = new Set(old); if (e.target.checked) next.add(key); else next.delete(key); return next })} />
            <span className="min-w-0 flex-1 break-all">{key}<span className="ml-2 text-lx-muted">缺 {p.missing_parts.join(' / ').toUpperCase()} · {(p.size / 1024 ** 3).toFixed(2)} GB</span></span>
            <span className="text-lx-muted">{p.working_device ? '处理中' : p.assigned_device ? '已分配，待处理' : '可领取'}</span>
          </label>
        })}
      </div>
      <div className="mt-3 flex gap-2">
        <button type="button" className="lx-btn-ghost" disabled={busy} onClick={() => void load()}>刷新</button>
        {data?.next_offset != null && <button type="button" className="lx-btn-ghost" disabled={busy}
          onClick={() => void load(data.next_offset!)}>加载更多</button>}
        {data?.allow_manual_claim && <button type="button" className="lx-btn-primary" disabled={busy || !selected.size}
          onClick={async () => {
            setBusy(true); setError('')
            try {
              const items = data.items.filter((p) => selected.has(`${p.repo}/${p.id}`)).map(({ repo, id }) => ({ repo, id }))
              await api.centerReserve(items)
              setMessage(`已领取 ${items.length} 包，进入本机待处理队列；点击开始后执行。`)
              await load(); onChanged()
            } catch (e) { setError(String(e)) } finally { setBusy(false) }
          }}>领取选中分包（{selected.size}）</button>}
        {data?.allow_manual_claim && data.queue_only && <button type="button" className="lx-btn-ghost" disabled={busy}
          onClick={async () => { setBusy(true); try { await api.centerQueue(false); await load(); setMessage('队列处理完后继续自动领取工作区分包。') } catch (e) { setError(String(e)) } finally { setBusy(false) } }}>恢复自动领包</button>}
        {data?.queue_only && <button type="button" className="lx-btn-ghost" disabled={busy}
          onClick={async () => { setBusy(true); try { await api.centerQueue(true, true); await load(); setMessage('待处理队列已归还，正在执行的包保留。') } catch (e) { setError(String(e)) } finally { setBusy(false) } }}>归还待处理队列</button>}
      </div>
    </>}
  </SectionPanel>
}
