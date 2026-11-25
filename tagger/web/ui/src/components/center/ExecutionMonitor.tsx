import { Activity, Cpu, Gauge, Images, Package } from 'lucide-react'
import type { CenterState } from '../../lib/api'
import { executionMode } from './executionMode'
import '../../styles/center-monitor.css'

const number = (value?: number) => typeof value === 'number' && Number.isFinite(value)
  ? value.toLocaleString('zh-CN') : '—'
const percent = (value: number) => Math.min(100, Math.max(0, value))
type Gpu = NonNullable<NonNullable<CenterState['runtime']['telemetry']>['gpus']>[number]

function GpuReading({ gpu, stale }: { gpu: Gpu; stale: boolean }) {
  const memoryKnown = gpu.mem_total_mb > 0 && gpu.mem_used_mb >= 0
  const utilization = Number.isFinite(gpu.util) && gpu.util >= 0 ? percent(gpu.util) : undefined
  return <div className="center-monitor-gpu">
    <div className="center-monitor-device" title={`${gpu.device} · ${gpu.name}`}>
      <span>{gpu.name}</span><small>{gpu.device}</small>
    </div>
    <div className="center-monitor-reading">
      <strong>{stale ? '—' : number(utilization)}</strong><span>% 使用率</span>
    </div>
    <div className="center-monitor-memory">
      <span>显存</span><span>{!stale && memoryKnown
        ? `${(gpu.mem_used_mb / 1024).toFixed(1)} / ${(gpu.mem_total_mb / 1024).toFixed(1)} GB` : '待上报'}</span>
    </div>
    <div className="center-monitor-meter" aria-hidden="true">
      <span style={{ width: `${!stale && memoryKnown ? percent(gpu.mem_used_mb / gpu.mem_total_mb * 100) : 0}%` }} />
    </div>
  </div>
}

export function ExecutionMonitor({ state, unavailable = false }: { state: CenterState | null; unavailable?: boolean }) {
  const runtime = state?.runtime
  const p = state?.running ? state.progress : {}
  const stale = unavailable || (typeof p.updated_ago_s === 'number' && p.updated_ago_s >= 120)
  const speed = !stale && typeof p.rate_per_s === 'number' && Number.isFinite(p.rate_per_s)
    ? Math.max(0, p.rate_per_s * 60).toFixed(1) : '—'
  const total = p.total_known && typeof p.total === 'number' && p.total > 0 ? p.total : undefined
  const completion = total !== undefined && typeof p.done === 'number' ? percent(p.done / total * 100) : undefined
  const telemetry = runtime?.telemetry
  const gpuStale = unavailable || (typeof telemetry?.sampled_at === 'number' && Date.now() / 1000 - telemetry.sampled_at > 30)
  const gpus = telemetry?.gpus ?? []
  const { showNl, showTag } = executionMode(state)
  const workspace = runtime?.managed?.automation?.workspace
  const connected = runtime?.managed?.status === 'online' || state?.board?.ok
  const status = unavailable ? '连接中断' : !state ? '正在读取' : stale ? '进度未更新' : state.running
    ? '正在执行' : runtime?.preparing ? '准备中' : runtime?.managed?.automation?.desired === 'paused'
      ? '接单已暂停' : runtime?.active ? '等待任务' : '未运行'
  return <section className="center-monitor" aria-label="运行监控" data-active={Boolean(state?.running && !stale)}>
    <header className="center-monitor-heading">
      <div><Activity size={15} aria-hidden="true" /><h2>运行监控</h2><span className="center-monitor-local">本机</span></div>
      <span className={`center-monitor-status${stale ? ' is-stale' : ''}`}>
        <i aria-hidden="true" />{status}
        {!stale && typeof p.updated_ago_s === 'number' && <small>{Math.max(0, Math.round(p.updated_ago_s))} 秒前更新</small>}
      </span>
    </header>
    <div className="center-monitor-grid">
      <div className="center-monitor-cell center-monitor-speed" role="group" aria-label="打标速度">
        <div className="center-monitor-label"><Gauge size={14} aria-hidden="true" />有效速度</div>
        <div className="center-monitor-reading"><strong>{speed}</strong></div>
        <div className="center-monitor-unit">张 / 分钟</div>
        <p className="center-monitor-hint">{stale ? '等待新的进度上报' : state?.running ? '当前分片处理速度' : '开始打标后显示速度'}</p>
      </div>
      <div className="center-monitor-cell" role="group" aria-label="图片进度">
        <div className="center-monitor-label"><Images size={14} aria-hidden="true" />图片进度</div>
        <div className="center-monitor-reading"><strong>{number(p.done)}</strong><span>张已处理</span></div>
        <div className="center-monitor-progress-caption">
          <span>{p.total_known ? `共 ${number(p.total)} 张` : typeof p.total === 'number' ? `已读取 ${number(p.total)} 张 · 总量待确定` : '等待分片进度'}</span>
          {completion !== undefined && <b>{completion.toFixed(1)}%</b>}
        </div>
        {completion !== undefined
          ? <div className="center-monitor-meter is-progress" role="progressbar" aria-label="图片处理进度" aria-valuenow={Number(completion.toFixed(1))} aria-valuemin={0} aria-valuemax={100}>
            <span style={{ width: `${completion}%` }} />
          </div>
          : <div className="center-monitor-pending-line" aria-hidden="true" />}
        <div className="center-monitor-outcomes">
          <span>成功 <b>{number(p.successful)}</b></span>
          <span>跳过 <b>{number(p.skipped)}</b></span>
          <span className={p.failed ? 'has-failures' : ''}>失败 <b>{number(p.failed)}</b></span>
        </div>
      </div>
      <div className="center-monitor-cell" role="group" aria-label="GPU 与显存">
        <div className="center-monitor-label"><Cpu size={14} aria-hidden="true" />GPU 与显存{gpuStale && <span>采样待更新</span>}</div>
        {gpus.length ? gpus.map(gpu => <GpuReading key={gpu.device} gpu={gpu} stale={gpuStale} />)
          : <div className="center-monitor-empty"><Cpu size={24} strokeWidth={1.2} aria-hidden="true" /><span>暂无 GPU 数据</span><small>设备上报后显示负载与显存</small></div>}
      </div>
    </div>
    <footer className="center-monitor-footer">
      <div className="center-monitor-task"><Package size={15} aria-hidden="true" />
        <div><strong title={p.package ? `${p.repo ?? ''}/${p.package}` : undefined}>{p.package || '暂无执行分片'}</strong>
          <span>{workspace?.name || workspace?.ws_id || (connected ? '已连接调度中心' : '尚未连接调度中心')}
            {p.package_index ? ` · 本轮第 ${p.package_index} 片` : ''}{p.phase ? ` · ${p.phase.toUpperCase()}` : ''}</span>
        </div>
      </div>
      <div className="center-monitor-parallel">
        {showNl && runtime?.resolved.concurrency !== undefined && <span>实际 NL 并发 <b>{runtime.resolved.concurrency}</b></span>}
        {showTag && runtime?.resolved.tag_batch !== undefined && <span>TAG 批大小 <b>{runtime.resolved.tag_batch}</b></span>}
      </div>
    </footer>
  </section>
}
