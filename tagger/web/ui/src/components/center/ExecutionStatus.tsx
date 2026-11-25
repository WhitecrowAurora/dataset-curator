import type { CenterState } from '../../lib/api'
import { SectionPanel, StatusChip } from '../ui/launcher'
import { executionMode } from './executionMode'

export function ExecutionStatus({ state, busy = false, onResume, onForceContinue }: { state: CenterState | null; busy?: boolean; onResume?: () => void; onForceContinue?: () => void }) {
  if (!state) return null
  const p = state.running ? state.progress : {}
  const delivery = state.delivery
  const review = delivery?.report?.artist_reinject
  const retryIn = state.watchdog?.next_in
  const retrying = typeof retryIn === 'number'
  const runtime = state.runtime
  const dispatchPaused = runtime.managed?.automation?.dispatch?.running === false
  const devicePaused = runtime.managed?.automation?.desired === 'paused'
  const limit = runtime.managed?.automation?.limit
  const probe = runtime.probe
  const { showNl, showTag } = executionMode(state)
  const parallel = Object.entries(runtime.managed?.automation?.parallel ?? {})
    .filter(([key]) => key === 'concurrency' ? showNl : showTag)
  const stale = typeof p.updated_ago_s === 'number' && p.updated_ago_s >= 120 && state.running
  return <SectionPanel title="任务与交付" trailing={typeof limit?.completed === 'number'
    ? <span className="text-[11px] text-lx-muted">本轮完成 <b className="font-mono font-medium text-lx">{limit.completed}</b> 片 · 上限 {limit.max_packages || '不限'}</span> : undefined}>
    {dispatchPaused && <p className="mb-3 text-xs text-lx-warning">{devicePaused
      ? '自动派单和本设备接单均已暂停；恢复设备后，仍需管理员启动自动派单。'
      : '管理员已暂停自动派单：当前分片可继续，下一片等待管理员启动。'}</p>}
    <div className="flex flex-wrap gap-3 text-xs text-lx-secondary">
      <StatusChip tone={stale ? 'warning' : state.running ? 'accent' : 'neutral'}>
        {runtime.pending_action ? '完成当前分片后调整／探测' : retrying ? `执行已中断，${retryIn} 秒后自动恢复` : stale ? '超过 2 分钟没有新进度，请查看日志' : runtime.message || '等待任务'}
      </StatusChip>
      {runtime.managed?.waiting_reason && <span>{runtime.managed.waiting_reason}</span>}
      {runtime.active && runtime.managed?.automation?.desired === 'paused' && !state.running && onResume &&
        <button type="button" className="lx-btn-primary" disabled={busy || runtime.preparing} onClick={onResume}>恢复自动接单</button>}
      {state.identity.device_id && onForceContinue && !state.running && !runtime.preparing && !state.history?.running &&
        <button type="button" className="lx-btn-ghost" disabled={busy || state.release?.running || Boolean(runtime.pending_action)}
          onClick={onForceContinue}>强制继续</button>}
    </div>
    {delivery && <div className="mt-3 space-y-1 text-xs text-lx-muted">
      <p>{delivery.package} · {({ downloading: '正在下载原始分片', download_failed: '分片下载失败，尚未开始打标或回传',
        processing: '分片已就绪，正在准备或执行打标', uploading: '文件上传中', verifying: '文件已上传，等待中心验收',
        checked: '中心已接收，等待登记', completed: '中心验收及登记完成', pending: '已回传，等待中心核验',
        incomplete: '已保存成功结果，仍有失败、漏项或冲突，尚未整片完成',
        rejected: '中心验收未通过，已停止自动重试', retrying: '回传暂时失败，等待自动补传' } as Record<string, string>)[delivery.status] ?? delivery.status}</p>
      {delivery.error && <p className="text-lx-warning">{delivery.error}</p>}
      {review && <>
        <p>有 {review.review_count} 条画师前缀需要核对。请退出托管，在「本地已做结果」核对后使用「强制重新注入并上报」，无需重跑模型。</p>
        <details><summary className="cursor-pointer">查看画师验收明细</summary>
          {review.review_samples.map(r => <p key={`${r.image_id}/${r.part}`} className="mt-1 break-all text-lx-warning">
            图片 {r.image_id} · 原前缀：{r.existing_prefix} · 表中画师：{r.artists.join('、')}
          </p>)}
        </details>
      </>}
    </div>}
    {runtime.managed?.start_pending && <p className="mt-2 text-xs text-lx-secondary">恢复请求已保留，等待中心确认；断线后会自动重送。</p>}
    {runtime.managed?.automation?.pending_results?.map(r => <p key={`${r.repo}/${r.pkg}`} className="mt-2 text-xs text-lx-warning">
      本地完成、待回传：{r.pkg} · {r.parts.join(' / ').toUpperCase()}</p>)}
    {!state.running && <p className="mt-3 text-xs text-lx-muted">当前没有执行分片；已保留的租约可在下方查看。</p>}
    {parallel.filter(([, value]) => value.warning).map(([key, value]) =>
      <p key={key} className="mt-2 text-xs text-lx-warning">{key === 'concurrency' ? 'NL 并发' : 'TAG 批大小'}：{value.warning}</p>)}
    {(p.image_id || parallel.length > 0) && <details className="mt-3 text-[11px] text-lx-dim">
      <summary className="cursor-pointer">执行详情</summary>
      {p.image_id && <p className="mt-2 break-all">最近结算图片：{p.image_id}</p>}
      {parallel.map(([key, value]) => <p key={key} className="mt-1">
        {key === 'concurrency' ? 'NL 并发' : 'TAG 批大小'}：{value.value || '自动'} · 来源：{value.source}
      </p>)}
    </details>}
    {probe?.error && <p className="mt-2 text-xs text-lx-warning">探测：{probe.error}</p>}
    {probe?.recommended && <p className="mt-2 text-xs text-lx-secondary">{probe.sample_count} 张小样本探测建议：
      {showNl && probe.recommended.concurrency !== undefined && ` NL 并发 ${probe.recommended.concurrency}`}
      {showTag && probe.recommended.tag_batch !== undefined && ` TAG 批大小 ${probe.recommended.tag_batch}`}
      {' · '}可在模型配置中采用；还需观察正式任务稳定性。
    </p>}
  </SectionPanel>
}
