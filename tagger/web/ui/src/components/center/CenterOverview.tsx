import { ChevronDown, Layers3, PlugZap } from 'lucide-react'
import type { CenterState } from '../../lib/api'

export function CenterOverview({ state }: { state: CenterState | null }) {
  if (!state) return null
  const { board, identity, runtime } = state
  const leases = board?.leases ?? []
  const pool = board?.ok ? board.parts : undefined
  const hasPool = pool && ['remaining', 'total', 'done', 'missing_nl', 'missing_tag'].every(key =>
    typeof pool[key as keyof typeof pool] === 'number')
  const managed = runtime.managed
  const results = managed?.results.slice(-3) ?? []
  return <div className="rounded-lx border border-lx bg-lx-surface text-xs">
    <details className="group">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 text-lx-muted [&::-webkit-details-marker]:hidden">
        <span className="flex items-center gap-2"><PlugZap size={13} aria-hidden="true" />
          <span className={board?.ok ? 'text-lx-success' : 'text-lx-muted'}>{board?.ok ? '已连接中心' : identity.server ? '中心暂未连通' : '未连接中心'}</span>
        </span>
        <span>持有租约 <b className="ml-1 font-mono font-medium text-lx">{leases.length}</b></span>
        {hasPool && <span className="flex items-center gap-2"><Layers3 size={13} aria-hidden="true" />
          全池剩余 <b className="font-mono font-medium text-lx">{pool.remaining}</b><span className="text-lx-dim">/ {pool.total} 片</span>
        </span>}
        <span className="ml-auto flex items-center gap-2 text-[10px]">连接与全池详情<ChevronDown size={12} className="transition-transform group-open:rotate-180" aria-hidden="true" /></span>
      </summary>
      <div className="grid gap-3 border-t border-lx px-4 py-3 text-[11px] leading-relaxed text-lx-muted sm:grid-cols-2">
        <div className="space-y-1">
          <p className="break-all">中心地址：{identity.server || '尚未配置'}</p>
          {board?.reason && <p>{board.reason}</p>}
          <p>租约：执行 {leases.filter(l => l.kind === 'active').length} · 预取 {leases.filter(l => l.kind === 'prelock').length}</p>
          {hasPool && <p>全池已完成 {pool.done} · 缺 NL {pool.missing_nl} · 缺 TAG {pool.missing_tag}</p>}
          <p>上次退出码：{state.last_rc ?? '—'}{state.last_rc === 0 ? ' · 正常结束' : ''}</p>
        </div>
        <div className="space-y-1">
          {managed && <p>托管连接：{({ online: '已连接', connecting: '连接中', unsupported: '等待服务端升级',
            offline: '连接中断，自动重连', released: '已退出托管' } as Record<string, string>)[managed.status] ?? managed.status}</p>}
          {results.map(r => <p key={r.id}>指令 {r.seq}：{({ accepted: '准备执行', applied: '已执行', failed: '失败', canceled: '已取消' } as Record<string, string>)[r.status] ?? r.status}</p>)}
        </div>
      </div>
    </details>
    {(managed?.error || results.some(r => r.error)) && <div className="space-y-1 border-t border-lx px-4 py-3 text-xs text-lx-warning">
      {managed?.error && <p className="break-words">{managed.error}</p>}
      {results.filter(r => r.error).map(r => <p className="break-words" key={r.id}>指令 {r.seq}：{r.error}</p>)}
    </div>}
  </div>
}
