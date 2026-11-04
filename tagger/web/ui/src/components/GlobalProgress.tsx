import { useCallback, useEffect, useState } from 'react'
import { AlarmClock } from 'lucide-react'
import { api, ApiError, type Activity, type PlanState, type SysInfo } from '../lib/api'

/**
 * 底部全局状态条：左边是"现在在跑什么"，右边是这台机器累不累。
 *
 * 为什么不让每个页面自己显示：打标在「运行中」页有进度、分包在「数据打包」页
 * 有进度，但用户切到别的页面就完全看不见了——而这几件事往往要跑几分钟到
 * 几小时。底部条是唯一"永远在"的位置。
 *
 * 数据只从 `/api/activities` 与 `/api/sysinfo` 来：后端把打标/分包/拼接/封包/
 * 合并/自检归一化成同一种形态，这里不按业务分支，只负责画。
 */
export function GlobalProgress({
  interval,
  onOpen,
}: {
  /** 轮询间隔（毫秒）；0 = 暂停刷新（底部条不消失，计划入口仍然可达） */
  interval: number
  onOpen: (href: string) => void
}) {
  const [items, setItems] = useState<Activity[]>([])
  const [sys, setSys] = useState<SysInfo | null>(null)
  const paused = interval <= 0

  useEffect(() => {
    if (paused) return
    let alive = true
    let timer = 0
    const tick = async () => {
      // 两个接口一起拉：分开发两个请求会让底栏的数字互相差半秒
      const [acts, info] = await Promise.allSettled([
        api.activities(),
        api.sysinfo(),
      ])
      if (!alive) return
      if (acts.status === 'fulfilled') setItems(acts.value.items)
      // 后端没起来时别在底部刷红字：顶栏已经有"后端未响应"了。
      else setItems([])
      if (info.status === 'fulfilled') setSys(info.value)
      timer = window.setTimeout(() => void tick(), interval)
    }
    void tick()
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [interval, paused])

  return (
    <div className="flex items-center gap-3">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-4 gap-y-1">
        {items.slice(0, 3).map((item) => {
          const view = describe(item)
          return (
            <button
              key={`${item.kind}-${item.id}`}
              type="button"
              onClick={() => onOpen(item.href)}
              title={view.hint}
              className="group flex min-w-[220px] max-w-[420px] flex-1 items-center gap-2 text-left"
            >
              <span className="shrink-0 text-[11px] font-medium text-lx-secondary group-hover:text-lx">
                {item.title}
              </span>
              <span className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-lx-raised">
                <span
                  className="block h-full rounded-full bg-lx-accent transition-[width] duration-300"
                  style={{ width: `${view.pct}%` }}
                />
              </span>
              {/* 数量统计用 | 隔开：一长串没有分隔符的数字读起来要先数格子 */}
              <span className="shrink-0 whitespace-nowrap font-mono text-[10px] tabular-nums text-lx-dim">
                {view.parts.join(' | ')}
              </span>
            </button>
          )
        })}
        {items.length > 3 && (
          <span className="shrink-0 text-[10px] text-lx-dim">+{items.length - 3}</span>
        )}
      </div>

      <div className="flex shrink-0 flex-wrap items-center justify-end gap-x-2.5 gap-y-1 border-l border-lx pl-3">
        <Meter label="CPU" pct={sys?.cpu_pct} />
        <Sep />
        <Meter label="GPU" pct={sys?.gpu_pct} temp={sys?.gpu_temp_c} />
        <Sep />
        <MemMeter
          label="内存"
          pct={sys?.mem_pct}
          used={sys?.mem_used_gb}
          total={sys?.mem_total_gb}
        />
        <Sep />
        <MemMeter
          label="显存"
          pct={sys?.vram_pct}
          used={sys?.vram_used_gb}
          total={sys?.vram_total_gb}
        />
        <PlanMenu />
      </div>
    </div>
  )
}

function Sep() {
  return <span className="text-[10px] text-lx-dim/50">|</span>
}

/** CPU/GPU 一个读数：`CPU 13%`、`GPU 41% 33°`。拿不到的项显示 --。 */
function Meter({
  label,
  pct,
  temp,
}: {
  label: string
  pct?: number | null
  temp?: number | null
}) {
  const hot = pct !== null && pct !== undefined && pct >= 90
  // 温度是可选参数：CPU 没有可靠的用户态温度口，传了才画。
  // 不传时不能渲染成 `--`，否则每台机器上都会多一个没意义的占位符。
  const hasTemp = temp !== undefined
  return (
    <span
      className="flex items-center gap-1 whitespace-nowrap font-mono text-[10px] tabular-nums"
      title={hasTemp ? `${label} 使用率 / 温度` : `${label} 使用率`}
    >
      <span className="text-lx-muted">{label}</span>
      <span className={hot ? 'text-lx-warning' : 'text-lx-secondary'}>
        {pct === null || pct === undefined ? '--' : `${Math.round(pct)}%`}
      </span>
      {hasTemp && (
        <span className="text-lx-dim">
          {temp === null ? '--' : `${Math.round(temp)}°`}
        </span>
      )}
    </span>
  )
}

/** 内存/显存：底栏只放百分比，悬浮给 GB 明细（`39.1 / 95.9 GB`）。 */
function MemMeter({
  label,
  pct,
  used,
  total,
}: {
  label: string
  pct?: number | null
  used?: number | null
  total?: number | null
}) {
  const hot = pct !== null && pct !== undefined && pct >= 90
  const hint =
    used !== null && used !== undefined && total !== null && total !== undefined
      ? `${label} ${used.toFixed(1)} / ${total.toFixed(1)} GB`
      : `${label} 占用读不到`
  return (
    <span
      className="flex items-center gap-1 whitespace-nowrap font-mono text-[10px] tabular-nums"
      title={hint}
    >
      <span className="text-lx-muted">{label}</span>
      <span className={hot ? 'text-lx-warning' : 'text-lx-secondary'}>
        {pct === null || pct === undefined ? '--' : `${Math.round(pct)}%`}
      </span>
    </span>
  )
}

/** 计划任务小菜单：跑到 N 张后停任务 / 关机。 */
function PlanMenu() {
  const [open, setOpen] = useState(false)
  const [state, setState] = useState<PlanState | null>(null)
  const [count, setCount] = useState(1000)
  const [action, setAction] = useState<'stop' | 'shutdown'>('stop')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      setState(await api.planGet())
    } catch {
      // 后端没起来：菜单里显示空白即可，不用报警
    }
  }, [])

  // 只在菜单打开时轮询：收起来还打后端就是白耗电
  useEffect(() => {
    if (!open) return
    void refresh()
    const t = window.setInterval(() => void refresh(), 3000)
    return () => window.clearInterval(t)
  }, [open, refresh])

  async function submit() {
    const n = Math.max(1, Math.round(count))
    // 关机不可逆：二次确认。暂停任务不需要，产物都在盘上。
    if (action === 'shutdown' &&
        !window.confirm(`处理到 ${n} 张后停止任务并关机，确定？\n（会先停任务让产物落盘，60 秒后关机）`)) {
      return
    }
    setBusy(true)
    setError('')
    try {
      setState(await api.planSet({ after_images: n, action, enabled: true }))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    setBusy(true)
    setError('')
    try {
      setState(await api.planCancel())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const active = Boolean(state?.plan.enabled)
  const fired = Boolean(state?.plan.fired)

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={active ? '计划任务已启用' : '设置计划任务'}
        className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] transition-colors ${
          active
            ? 'bg-lx-accent-subtle text-lx-accent'
            : 'text-lx-dim hover:text-lx-muted'
        }`}
      >
        <AlarmClock size={11} />
        <span>{active ? `剩 ${state?.remaining ?? '?'} 张` : '计划'}</span>
      </button>

      {open && (
        <div className="absolute right-0 bottom-full z-50 mb-1.5 w-[268px] rounded-lx border border-lx bg-lx-surface p-2.5 shadow-xl">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[11px] font-medium text-lx">计划任务</span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-[10px] text-lx-dim hover:text-lx-muted"
            >
              收起
            </button>
          </div>

          <div className="flex items-center gap-1.5 text-[10px] text-lx-muted">
            <span className="shrink-0">处理到</span>
            <input
              type="number"
              min={1}
              step={100}
              value={count}
              onChange={(e) => setCount(Math.max(1, Number(e.target.value) || 1))}
              className="lx-input w-[76px] py-0.5 text-center font-mono text-[11px]"
            />
            <span className="shrink-0">张后</span>
          </div>

          <div className="mt-1.5 flex gap-1">
            <button
              type="button"
              onClick={() => setAction('stop')}
              className={`flex-1 rounded border px-2 py-1 text-[10.5px] transition-colors ${
                action === 'stop'
                  ? 'border-lx-accent/50 bg-lx-accent-subtle text-lx-accent'
                  : 'border-lx text-lx-muted hover:text-lx'
              }`}
            >
              暂停任务
            </button>
            <button
              type="button"
              onClick={() => setAction('shutdown')}
              className={`flex-1 rounded border px-2 py-1 text-[10.5px] transition-colors ${
                action === 'shutdown'
                  ? 'border-lx-danger/50 bg-lx-danger/10 text-lx-danger'
                  : 'border-lx text-lx-muted hover:text-lx'
              }`}
            >
              关机
            </button>
          </div>

          <div className="mt-2 flex gap-1">
            <button
              type="button"
              disabled={busy}
              onClick={() => void submit()}
              className="lx-btn-ghost flex-1 px-2 py-1 text-[10.5px]"
            >
              启用
            </button>
            <button
              type="button"
              disabled={busy || !active}
              onClick={() => void cancel()}
              className="lx-btn-ghost flex-1 px-2 py-1 text-[10.5px]"
            >
              取消计划
            </button>
          </div>

          {active && (
            <p className="mt-2 text-[10px] leading-relaxed text-lx-muted">
              {fired
                ? `已触发：${state?.plan.note || '任务已停止'}`
                : `还差 ${state?.remaining ?? '?'} 张 · ${
                    state?.plan.action === 'shutdown' ? '然后关机' : '然后暂停'
                  }${state?.running ? '' : '（当前没有在跑的任务）'}`}
            </p>
          )}
          {error && <p className="mt-2 text-[10px] text-lx-danger">{error}</p>}
        </div>
      )}
    </div>
  )
}

/** 把一条 Activity 翻成"进度条宽度 + 数字串"。 */
function describe(item: Activity): { pct: number; parts: string[]; hint: string } {
  const { done, total, progress, rate, eta_s } = item

  let pct: number
  if (progress !== null && progress !== undefined) {
    pct = Math.max(0, Math.min(1, progress)) * 100
  } else if (total > 0) {
    pct = Math.min(100, (done / total) * 100)
  } else {
    pct = 0
  }

  const parts: string[] = []
  if (total > 0) parts.push(`${fmtCount(done)}/${fmtCount(total)}`)
  else if (done > 0) parts.push(fmtCount(done))
  else parts.push(stageLabel(item))
  if (rate > 0) parts.push(`${rate.toFixed(0)}/s`)
  if (eta_s !== null && eta_s !== undefined && eta_s > 0) parts.push(`剩 ${fmtEta(eta_s)}`)

  const hint = [item.detail, stageLabel(item)].filter(Boolean).join(' · ')
  return { pct, parts, hint }
}

function stageLabel(item: Activity): string {
  if (item.kind === 'check') return item.phase === 'read' ? '读入中' : '核对中'
  if (item.kind === 'merge') return item.phase === 'read' ? '读入中' : '写出中'
  if (item.kind === 'tag') return item.phase === 'pack' ? '封包中' : '打标中'
  return '进行中'
}

/** 12345 → 12.3k，让底栏的小字在百万级时也不至于横着长出去 */
function fmtCount(n: number): string {
  if (n < 10000) return String(n)
  if (n < 1000000) return `${(n / 1000).toFixed(1)}k`
  return `${(n / 1000000).toFixed(1)}M`
}

function fmtEta(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  return `${(s / 3600).toFixed(1)}h`
}
