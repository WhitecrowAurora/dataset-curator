import type { CenterState } from '../../lib/api'
import { SectionPanel, StatusChip } from '../ui/launcher'

export function ExecutionStatus({ state, busy = false, onResume, onForceContinue }: { state: CenterState | null; busy?: boolean; onResume?: () => void; onForceContinue?: () => void }) {
  if (!state) return null
  const p = state.progress
  const runtime = state.runtime
  const gpus = runtime.telemetry?.gpus ?? []
  const probe = runtime.probe
  const mode = p.package && ['tag', 'nl', 'both'].includes(p.phase ?? '')
    ? p.phase : runtime.resolved.mode ?? state.config.mode
  const showNl = mode !== 'tag'
  const showTag = mode !== 'nl' && (runtime.resolved.tag_backend ?? state.config.tag_backend) !== 'none'
  const settings = [
    showNl && runtime.resolved.concurrency !== undefined ? `实际 NL 并发 ${runtime.resolved.concurrency}` : '',
    showTag && runtime.resolved.tag_batch !== undefined ? `TAG 批大小 ${runtime.resolved.tag_batch}` : '',
  ].filter(Boolean)
  const stale = typeof p.updated_ago_s === 'number' && p.updated_ago_s >= 120 && state.running
  return <SectionPanel title="实时执行" description="逐图进度与整包交付分开统计；流式输入读完前不显示百分比">
    <div className="flex flex-wrap gap-3 text-xs text-lx-secondary">
      <StatusChip tone={stale ? 'warning' : state.running ? 'accent' : 'neutral'}>
        {runtime.pending_action ? '完成当前分片后调整／探测' : stale ? '超过 2 分钟没有新进度，请查看日志' : runtime.message || '等待任务'}
      </StatusChip>
      {runtime.managed?.waiting_reason && <span>{runtime.managed.waiting_reason}</span>}
      {runtime.active && runtime.managed?.automation?.desired === 'paused' && onResume &&
        <button type="button" className="lx-btn-primary" disabled={busy || runtime.preparing} onClick={onResume}>恢复自动接单</button>}
      {state.identity.device_id && onForceContinue && !state.running && !runtime.preparing && !state.history?.running &&
        <button type="button" className="lx-btn-ghost" disabled={busy || state.release?.running || Boolean(runtime.pending_action)}
          onClick={onForceContinue}>强制继续</button>}
    </div>
    {runtime.managed?.start_pending && <p className="mt-2 text-xs text-lx-secondary">恢复请求已保留，等待中心确认；断线后会自动重送。</p>}
    {runtime.managed?.automation?.limit && <p className="mt-2 text-xs text-lx-muted">本轮完成 {runtime.managed.automation.limit.completed} 片
      {' · '}上限 {runtime.managed.automation.limit.max_packages || '不限'}</p>}
    {runtime.managed?.automation?.pending_results?.map(r => <p key={`${r.repo}/${r.pkg}`} className="mt-2 text-xs text-lx-warning">
      本地完成、待回传：{r.pkg} · {r.parts.join(' / ').toUpperCase()}</p>)}
    {runtime.managed?.automation?.workspace && <p className="mt-2 text-xs text-lx-muted">
      接单工作区：{runtime.managed.automation.workspace.name || runtime.managed.automation.workspace.ws_id}
    </p>}
    {p.package && <p className="mt-3 truncate font-mono text-xs" title={`${p.repo}/${p.package}`}>当前分片：{p.package} {p.package_index ? `· 本轮第 ${p.package_index} 片` : ''} · {p.phase?.toUpperCase() || '准备／下载'}</p>}
    {typeof p.done === 'number' && <p className="mt-2 text-xs text-lx-muted">
      已处理 {p.done} 张 · {p.total_known ? `总计 ${p.total}` : `已读取 ${p.total ?? 0}，总量待确定`}
      {' · '}成功 {p.successful ?? '—'} · 跳过 {p.skipped ?? '—'} · 失败 {p.failed ?? '—'}
      {' · '}有效速度 {typeof p.rate_per_s === 'number' ? `${(p.rate_per_s * 60).toFixed(1)} 张/分钟` : '待上报'}
      {typeof p.updated_ago_s === 'number' && ` · ${Math.round(p.updated_ago_s)} 秒前更新`}
    </p>}
    {p.image_id && <p className="mt-1 text-xs text-lx-dim">最近结算图片：{p.image_id}</p>}
    {gpus.map(g => <p className="mt-2 text-xs text-lx-muted" key={g.device}>
      {g.device} · {g.name} · GPU {g.util}% · 显存 {(g.mem_used_mb / 1024).toFixed(1)} / {(g.mem_total_mb / 1024).toFixed(1)} GB
    </p>)}
    {settings.length > 0 && <p className="mt-2 text-xs text-lx-dim">{settings.join(' · ')}</p>}
    {Object.entries(runtime.managed?.automation?.parallel ?? {}).map(([key,value]) =>
      <p key={key} className="mt-1 text-xs text-lx-dim">{key==='concurrency'?'NL 并发':'TAG 批大小'}：{value.value || '自动'} · 来源：{value.source}</p>)}
    {probe?.error && <p className="mt-2 text-xs text-lx-warning">探测：{probe.error}</p>}
    {probe?.recommended && <p className="mt-2 text-xs text-lx-secondary">{probe.sample_count} 张小样本探测建议：
      {showNl && probe.recommended.concurrency !== undefined && ` NL 并发 ${probe.recommended.concurrency}`}
      {showTag && probe.recommended.tag_batch !== undefined && ` TAG 批大小 ${probe.recommended.tag_batch}`}
      {' · '}可在模型配置中采用；还需观察正式任务稳定性。
    </p>}
  </SectionPanel>
}
