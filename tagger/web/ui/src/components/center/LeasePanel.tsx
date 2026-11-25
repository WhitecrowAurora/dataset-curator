import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import type { CenterLease, CenterState } from '../../lib/api'
import { SectionPanel, StatusChip } from '../ui/launcher'

function duration(value: number) {
  const seconds = Math.max(0, Math.ceil(value))
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60)
  return hours ? `${hours}小时 ${minutes}分 ${seconds % 60}秒` : minutes ? `${minutes}分 ${seconds % 60}秒` : `${seconds}秒`
}

function releaseKey(lease: CenterLease) {
  return JSON.stringify([lease.lease_id, lease.kind, lease.repo, lease.package])
}

export function LeasePanel({ state, busy, onRelease }: {
  state: CenterState | null; busy: boolean; onRelease: (id: string, kind: string) => void
}) {
  const [elapsed, setElapsed] = useState(0)
  const [confirmation, setConfirmation] = useState<string | null>(null)
  useEffect(() => {
    const start = performance.now()
    setElapsed(0)
    const timer = window.setInterval(() => setElapsed((performance.now() - start) / 1000), 1000)
    return () => window.clearInterval(timer)
  }, [state])
  const board = state?.board, release = state?.release
  const locked = busy || Boolean(release?.running) || Boolean(state?.runtime.preparing)
  useEffect(() => {
    if (confirmation && (locked || !board?.ok || !board.leases?.some(lease => releaseKey(lease) === confirmation))) {
      setConfirmation(null)
    }
  }, [board, locked, confirmation])
  return <SectionPanel title="持有分片与释放" description="租约续期 48 小时；释放保留本地产物">
    {!board?.ok && <p className="text-xs text-lx-muted">{board?.reason || '连接调度中心后查看租约'}</p>}
    {board?.ok && !board.leases?.length && <p className="text-xs text-lx-muted">当前没有租约</p>}
    <div className="divide-y divide-lx">
      {board?.leases?.map(lease => {
        const heartbeat = state?.heartbeat?.leases[lease.lease_id]
        const left = typeof lease.expires_in === 'number' ? Math.max(0, lease.expires_in - elapsed) : null
        const age = typeof lease.last_beat_ago === 'number' ? lease.last_beat_ago + elapsed : null
        const next = typeof heartbeat?.next_in === 'number' ? Math.max(0, heartbeat.next_in - elapsed) : null
        const active = lease.kind === 'active'
        const confirming = !locked && board.ok && confirmation === releaseKey(lease)
        return <div key={lease.lease_id} className="py-3">
          <div className="flex flex-wrap items-center gap-2">
            <StatusChip tone={active ? 'accent' : 'neutral'}>{active ? '当前包' : lease.kind === 'prelock' ? '预取包' : '离线包'}</StatusChip>
            <span className="min-w-0 flex-1 truncate font-mono text-xs" title={`${lease.repo}/${lease.package}`}>{lease.package}</span>
            {['active', 'prelock'].includes(lease.kind) && <button type="button" className="lx-btn-danger" disabled={locked || !board.ok || confirming}
              aria-expanded={confirming} onClick={() => setConfirmation(releaseKey(lease))}>
              {release?.running && release.lease_id === lease.lease_id && <Loader2 size={12} className="mr-1 inline animate-spin" />}
              {active ? '停止并释放' : '释放预取包'}
            </button>}
          </div>
          {confirming && <div role="group" aria-label={`确认释放 ${lease.package}`} className="mt-2 rounded-lg border border-lx-danger/30 bg-lx-danger-subtle p-3">
            <p className="break-all text-xs text-lx-secondary">确认释放 {lease.repo}/{lease.package}？</p>
            <p className="mt-1 text-xs text-lx-muted">{active
              ? '将停止当前打标、暂停接单，并归还当前包及预取包。服务器可重新派发这些分片，本地产物会保留。'
              : '将归还此预取包，服务器可重新派发；当前打标继续，本地产物会保留。'}</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button type="button" className="lx-btn-danger" onClick={() => {
                setConfirmation(null)
                onRelease(lease.lease_id, lease.kind)
              }}>{active ? '确认停止并释放' : '确认释放预取包'}</button>
              <button type="button" className="lx-btn-ghost" onClick={() => setConfirmation(null)}>取消</button>
            </div>
          </div>}
          <p className={`mt-2 text-xs tabular-nums ${left !== null && left < 3600 ? 'text-lx-warning' : 'text-lx-secondary'}`}>
            {left === null ? '此租约没有在线过期倒计时' : left > 0 ? `距离释放剩余 ${duration(left)}` : '已到期，等待中心回收确认'}
          </p>
          <p className="mt-1 text-xs text-lx-muted">
            {age === null ? '尚未取得上次成功回报时间' : `距离上次成功回报 ${Math.floor(age)}秒`}
            {' · '}{heartbeat?.in_flight && heartbeat.running ? '正在上报，成功后重置释放时间' : next !== null
              ? next > 0 ? `将在 ${Math.ceil(next)}秒后上报，成功后重置释放时间` : '下一次上报待发送，成功后重置释放时间'
              : state?.running ? '下一轮心跳待安排' : '执行已停止，当前不会自动上报'}
          </p>
          {heartbeat?.error && heartbeat.running && <p className="mt-1 text-xs text-lx-warning">{heartbeat.error}</p>}
        </div>
      })}
    </div>
    {release?.message && <p role="status" className="mt-2 text-xs text-lx-muted">{release.message}</p>}
    {release?.error && <p role="alert" className="mt-1 text-xs text-lx-warning">{release.error}</p>}
    {!!board?.leases?.length && <p className="mt-2 text-xs text-lx-dim">停止并释放会暂停接单，同时归还预取包；仅释放预取包时，当前打标继续。</p>}
  </SectionPanel>
}
