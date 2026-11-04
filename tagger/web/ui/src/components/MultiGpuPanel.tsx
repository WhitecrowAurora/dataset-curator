/**
 * 多卡（数据并行）面板：每张卡一个独立推理进程。
 *
 * 与「本机服务」那块的区别：那边启动的是**一个**服务、地址要自己填端口；
 * 这边启动的是一组服务，地址按卡自动分配（基端口 + 序号），任务表单里的
 * 「服务地址」在多卡模式下不参与——打标侧直接从这组服务取地址分槽。
 *
 * 为什么不做张量并行：数据并行每张卡都是一份完整权重，卡与卡之间除了
 * 分活之外没有耦合，一张卡挂了其余照跑（状态会变 partial）。张量并行
 * 要把权重切开，任何一张卡掉线整组都停，对"30M 张图分批打"这种长任务
 * 是不划算的取舍。
 */
import { useEffect, useState } from 'react'
import { Cpu, Loader2, Play, Square } from 'lucide-react'

import { api, type MultiGpuCapability, type MultiGpuState } from '../lib/api'
import { SettingRow } from './ui/launcher'

type Props = {
  /** 与"本机服务"用同一个引擎选择：换引擎等于换一代指纹，不能两处各写各的 */
  engine: string
  enabled: boolean
  onEnabledChange: (v: boolean) => void
  /** 每卡并发。0 = 按分档表/显存推算，非 0 时同时用于服务和任务 */
  perCardSeqs: number
  onPerCardSeqsChange: (v: number) => void
}

const STATUS_TEXT: Record<string, string> = {
  stopped: '未启动',
  starting: '启动中',
  ready: '就绪',
  failed: '失败',
  partial: '部分就绪',
  stopping: '停止中',
}

const DOT: Record<string, string> = {
  stopped: '#94a3b8',
  starting: '#f59e0b',
  ready: '#10b981',
  failed: '#ef4444',
  partial: '#f59e0b',
  stopping: '#f59e0b',
}

export function MultiGpuPanel({
  engine,
  enabled,
  onEnabledChange,
  perCardSeqs,
  onPerCardSeqsChange,
}: Props) {
  const [state, setState] = useState<MultiGpuState | null>(null)
  const [cap, setCap] = useState<MultiGpuCapability | null>(null)
  const [selected, setSelected] = useState<number[]>([])
  const [basePort, setBasePort] = useState(8010)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [showLog, setShowLog] = useState<number | null>(null)

  // 轮询：启动中是"逐张加载"，1.5 秒一次才看得见进度；
  // 就绪后 4 秒一次足够，nvidia-smi 也不至于被频繁拉起。
  useEffect(() => {
    let alive = true
    let timer: number | undefined
    const tick = async () => {
      try {
        const s = await api.multi()
        if (!alive) return
        setState(s)
        timer = window.setTimeout(tick, s.status === 'starting' ? 1500 : 4000)
      } catch {
        if (alive) timer = window.setTimeout(tick, 6000)
      }
    }
    void tick()
    return () => {
      alive = false
      if (timer) window.clearTimeout(timer)
    }
  }, [])

  // 体检只在展开时问一次；卡列表同时用来做默认全选。
  useEffect(() => {
    if (!enabled) return
    let alive = true
    api
      .multiCapability(engine)
      .then((c) => {
        if (!alive) return
        setCap(c)
        setSelected((prev) => (prev.length ? prev : c.gpus.map((g) => g.index)))
      })
      .catch(() => alive && setCap(null))
    return () => {
      alive = false
    }
  }, [enabled, engine])

  const running =
    state != null &&
    (state.status === 'starting' ||
      state.status === 'ready' ||
      state.status === 'partial' ||
      state.status === 'stopping')
  const workers = state?.workers ?? []
  const readyCount = workers.filter((w) => w.status === 'ready').length

  async function start() {
    setBusy(true)
    setErr(null)
    try {
      const s = await api.multiStart({
        engine,
        gpus: selected,
        base_port: basePort,
        per_card_seqs: perCardSeqs || undefined,
      })
      setState(s)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    setBusy(true)
    setErr(null)
    try {
      setState(await api.multiStop())
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const failLog = workers.find((w) => w.index === showLog)

  return (
    <>
      <SettingRow
        label="多卡并行"
        icon={<Cpu size={14} />}
        help="每张卡各起一个独立服务、各打各的图，结果照旧写进同一批产物。4 张卡约等于 4 倍吞吐，代价是每卡一份权重（8B-Q4 约 6GB）"
        description={
          running
            ? `${readyCount}/${workers.length} 张就绪 · 每卡并发 ${state?.per_card_seqs || '按分档'} · 合计 ${state?.total_seqs || 0} 条在途`
            : '不融合权重，卡与卡互不影响；一张卡挂了其余照跑'
        }
      >
        <label className="flex items-center gap-2 text-[12px]">
          <input
            type="checkbox"
            checked={enabled}
            disabled={running}
            onChange={(e) => onEnabledChange(e.target.checked)}
          />
          启用多卡
        </label>
      </SettingRow>

      {enabled && (
        <div
          className="flex flex-col gap-3 rounded-md p-3"
          style={{ border: '1px solid rgba(128,128,128,0.28)' }}
        >
          {cap && !cap.ok && (
            <div className="text-[12px]" style={{ color: '#f59e0b' }}>
              {cap.reason}
              {cap.gpus.length > 0 && `（已检测到 ${cap.gpus.length} 张卡）`}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[12px] text-lx-dim">参与打标的卡：</span>
            {(cap?.gpus ?? []).map((g) => {
              const on = selected.includes(g.index)
              return (
                <button
                  key={g.index}
                  type="button"
                  disabled={running}
                  className="lx-btn-ghost px-2 py-1 text-[11px]"
                  style={on ? { borderColor: '#10b981', color: '#10b981' } : undefined}
                  onClick={() =>
                    setSelected(
                      on
                        ? selected.filter((x) => x !== g.index)
                        : [...selected, g.index].sort((a, b) => a - b),
                    )
                  }
                >
                  #{g.index} {g.name} · {Math.round(g.vram_gb)}G
                </button>
              )
            })}
            {selected.length > 0 && selected.length < 2 && (
              <span className="text-[11px]" style={{ color: '#f59e0b' }}>
                至少选 2 张；单卡请用上面的「启动服务」
              </span>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-[12px]">
              基端口
              <input
                type="number"
                className="lx-input w-24 font-mono tabular-nums"
                min={1024}
                max={65000}
                value={basePort}
                disabled={running}
                onChange={(e) =>
                  setBasePort(
                    Math.min(65000, Math.max(1024, Number(e.target.value) || 8010)),
                  )
                }
                aria-label="多卡基端口"
              />
            </label>
            <label className="flex items-center gap-2 text-[12px]">
              每卡并发
              <input
                type="number"
                className="lx-input w-20 font-mono tabular-nums"
                min={0}
                max={64}
                value={perCardSeqs}
                onChange={(e) =>
                  onPerCardSeqsChange(
                    Math.min(64, Math.max(0, Number(e.target.value) || 0)),
                  )
                }
                aria-label="每卡并发"
              />
              <span className="text-lx-dim">0 = 按显存推算</span>
            </label>
            {running ? (
              <button
                type="button"
                className="lx-btn-ghost inline-flex items-center gap-1.5 px-3"
                disabled={busy}
                onClick={() => void stop()}
              >
                <Square size={12} />
                停止多卡
              </button>
            ) : (
              <button
                type="button"
                className="lx-btn-ghost inline-flex items-center gap-1.5 px-3"
                disabled={busy || selected.length < 2 || cap?.ok === false}
                title={cap?.ok === false ? cap.reason : undefined}
                onClick={() => void start()}
              >
                {busy ? <Loader2 size={12} /> : <Play size={12} />}
                {busy ? '启动中…' : '启动多卡'}
              </button>
            )}
          </div>

          {err && (
            <div className="text-[12px]" style={{ color: '#ef4444' }}>
              {err}
            </div>
          )}
          {state?.error && (
            <div className="text-[12px]" style={{ color: '#f59e0b' }}>
              {state.error}
            </div>
          )}

          {workers.length > 0 && (
            <div className="flex flex-col gap-1">
              {workers.map((w) => (
                <div
                  key={w.index}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded px-2 py-1 text-[11px]"
                  style={{ border: '1px solid rgba(128,128,128,0.22)' }}
                >
                  <span style={{ color: DOT[w.status] ?? '#94a3b8' }}>●</span>
                  <span className="font-mono">#{w.index}</span>
                  <span className="text-lx-dim">{w.name}</span>
                  <span className="font-mono">:{w.port}</span>
                  <span>
                    {STATUS_TEXT[w.status] ?? w.status}
                    {w.pid ? ` · pid ${w.pid}` : ''}
                  </span>
                  <span className="font-mono">已打 {w.processed}</span>
                  {w.failed > 0 && (
                    <span className="font-mono" style={{ color: '#ef4444' }}>
                      失败 {w.failed}
                    </span>
                  )}
                  {w.gpu?.mem_total_mb ? (
                    <span className="font-mono text-lx-dim">
                      显存 {(w.gpu.mem_used_mb! / 1024).toFixed(1)}/
                      {(w.gpu.mem_total_mb / 1024).toFixed(0)}G
                      {w.gpu.mem_pct != null ? ` · ${w.gpu.mem_pct.toFixed(0)}%` : ''}
                    </span>
                  ) : null}
                  {w.gpu?.util != null && (
                    <span className="font-mono text-lx-dim">
                      util {w.gpu.util.toFixed(0)}%
                    </span>
                  )}
                  {w.status === 'failed' && (
                    <button
                      type="button"
                      className="lx-btn-ghost px-2 py-0.5 text-[11px]"
                      onClick={() => setShowLog(showLog === w.index ? null : w.index)}
                    >
                      {showLog === w.index ? '收起' : '看日志'}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          {failLog && failLog.log_tail.length > 0 && (
            <pre
              className="max-h-48 overflow-auto rounded p-2 font-mono text-[11px] text-lx-dim"
              style={{ background: 'rgba(0,0,0,0.18)' }}
            >
              {failLog.log_tail.join('\n')}
            </pre>
          )}

          {running && (
            <div className="text-[11px] text-lx-dim">
              多卡就绪后，任务里的「服务地址」不再使用——打标会按上表顺序把
              任务轮流分给各卡，产物写同一个输出目录，不需要事后合并。
            </div>
          )}
        </div>
      )}
    </>
  )
}
