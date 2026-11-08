/**
 * 本地量化成 GGUF：原始权重（safetensors）→ GGUF F16 → Q4_K_M / Q6_K。
 *
 * 为什么需要它：默认引擎 llama.cpp 只吃 GGUF。官方/社区没发 GGUF 的模型
 * （自训、微调、某些破限版）在本地就完全用不了，而"下载完整权重"之后
 * 原来没有任何下一步。
 *
 * 这里的取舍要说清（页面上也写了）：这条路**比直接下 GGUF 慢得多**——
 * 要先拖十几个 G 的原始权重，再转、再压，磁盘临时峰值约两倍模型大小。
 * 只有在"没有现成 GGUF"或者"不想依赖第三方压好的文件"时才值得走。
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { CheckCircle2, Download, Loader2, TriangleAlert, X } from 'lucide-react'
import { api, type DepState, type GgufConvertState, type GgufConvertTask } from '../lib/api'
import { StatusChip } from './ui/launcher'
import { ErrorBox } from './ui/ErrorBox'

function msg(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function fmtBytes(n: number): string {
  const gb = n / (1 << 30)
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(n / (1 << 20))} MB`
}

function fmtElapsed(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return m > 0 ? `${m} 分 ${String(s).padStart(2, '0')} 秒` : `${s} 秒`
}

export function GgufConvertDialog({
  open,
  modelKey,
  onClose,
}: {
  open: boolean
  modelKey: string
  onClose: () => void
}) {
  const [state, setState] = useState<GgufConvertState | null>(null)
  const [task, setTask] = useState<GgufConvertTask | null>(null)
  const [target, setTarget] = useState('q6_k')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [deps, setDeps] = useState<DepState | null>(null)
  const [installing, setInstalling] = useState(false)

  const running = task?.status === 'running'
  const done = task?.status === 'done'

  useEffect(() => {
    if (!open) return
    let alive = true
    setError(null)
    setTask(null)
    api
      .ggufState(modelKey)
      .then((s) => {
        if (!alive) return
        setState(s)
        setTask(s.task)
        // 已有任务在跑时跟随它的目标，别让下拉框显示一个不相干的档位
        if (s.task && s.task.status === 'running') setTarget(s.task.target)
      })
      .catch((err) => alive && setError(msg(err)))
    return () => {
      alive = false
    }
  }, [open, modelKey])

  // 转换要十几分钟，轮询不能太密：进度是按输出文件大小估的，1.5s 足够。
  useEffect(() => {
    if (!open) return
    const active = running || installing
    if (!active) return
    let alive = true
    const timer = window.setInterval(() => {
      const load = running
        ? api.ggufState(modelKey).then((s) => {
            if (!alive) return
            setState(s)
            if (s.task) setTask(s.task)
          })
        : api.deps().then((s) => {
            if (!alive) return
            setDeps(s)
            const st = s.job?.status
            if (st && st !== 'running') setInstalling(false)
          })
      load.catch(() => {
        /* 轮询失败不该把弹窗变成报错页，下一次再试 */
      })
    }, 1500)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [open, running, installing, modelKey])

  async function installDeps() {
    setError(null)
    setInstalling(true)
    try {
      setDeps(await api.depInstallExtra('convert'))
    } catch (err) {
      setError(msg(err))
      setInstalling(false)
    }
  }

  async function start() {
    setBusy(true)
    setError(null)
    try {
      setTask(await api.ggufStart({ model: modelKey, target }))
    } catch (err) {
      setError(msg(err))
    } finally {
      setBusy(false)
    }
  }

  async function close() {
    if (running) {
      try {
        await api.ggufCancel()
      } catch {
        /* 已经自己结束了 */
      }
    }
    onClose()
  }

  // 与 QuantizeDialog 同样的理由：页面根节点带 .lx-page-enter 的 transform，
  // 留在页面 DOM 里会让 fixed 相对整页容器定位，弹窗跑到页面中间。
  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[10000] flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) void close()
          }}
          role="dialog"
          aria-modal="true"
          aria-label="本地量化成 GGUF"
        >
          <div
            className="absolute inset-0"
            style={{
              backgroundColor: 'color-mix(in srgb, var(--lx-bg) 62%, transparent)',
              backdropFilter: 'blur(3px)',
            }}
          />
          <motion.div
            className="lx-sheet relative flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden"
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 4 }}
            transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          >
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-lx px-[18px] py-3">
              <div className="flex items-center gap-2">
                <h3 className="text-[15px] font-semibold text-lx">本地量化成 GGUF</h3>
                {running && (
                  <StatusChip tone="warning" icon={<Loader2 size={11} className="animate-spin" />}>
                    {task?.percent.toFixed(0) ?? 0}%
                  </StatusChip>
                )}
                {!running && done && (
                  <StatusChip tone="success" icon={<CheckCircle2 size={11} />}>
                    已完成
                  </StatusChip>
                )}
              </div>
              <button className="lx-btn-ghost-icon" onClick={() => void close()} aria-label="关闭">
                <X size={16} />
              </button>
            </header>

            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-[18px] py-3.5 text-[12px] leading-relaxed text-lx-muted">
              {!state && !error && (
                <div className="flex items-center gap-2 py-2">
                  <Loader2 size={14} className="animate-spin" /> 读取转换环境…
                </div>
              )}

              {state && !state.available && (
                <div className="rounded-lx-sm border border-lx-warning/40 bg-lx-warning/5 px-3 py-2.5 text-lx-warning">
                  <div className="mb-1 flex items-center gap-1.5 font-medium">
                    <TriangleAlert size={13} /> 这台机器现在不能转换
                  </div>
                  <p className="leading-relaxed text-lx-muted">{state.reason}</p>
                  <div className="mt-2 space-y-1.5">
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex items-center gap-1.5 text-[11px]"
                      disabled={busy || installing}
                      onClick={() => void installDeps()}
                    >
                      {installing ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        <Download size={12} />
                      )}
                      {installing ? '正在安装依赖…' : '一键安装（convert 依赖组）'}
                    </button>
                    {installing && (
                      <pre className="max-h-28 overflow-auto rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10px] leading-relaxed text-lx-muted">
                        {(deps?.job?.log ?? []).slice(-6).join('\n') || '正在启动 pip…'}
                      </pre>
                    )}
                  </div>
                </div>
              )}

              {state?.available && !state.source.ready && (
                <div className="rounded-lx-sm border border-lx px-3 py-2.5">
                  <div className="mb-1 text-lx">还缺原始权重</div>
                  <p className="text-[11px] leading-relaxed">
                    本地量化要用 transformers 格式的原始权重（safetensors）。
                    回到这一行的下载档位，选「原始权重（用于本地量化）」下完再来。
                  </p>
                  {state.source.hint && (
                    <p className="mt-1 break-all font-mono text-[10px] text-lx-dim">
                      {state.source.hint}
                    </p>
                  )}
                </div>
              )}

              {state?.available && state.source.ready && !running && !done && (
                <div className="space-y-2.5">
                  <p>
                    源权重 <code className="font-mono text-lx">{fmtBytes(state.source.bytes)}</code>
                    ，先转成 F16 GGUF，再压成目标档位。转换期间要占约
                    <span className="text-lx"> 两份模型大小 </span>
                    的磁盘，跑完自动删掉中间的 F16。
                  </p>
                  <label className="block">
                    <span className="mb-1 block text-[11px] text-lx-muted">量化档位</span>
                    <select
                      className="lx-select"
                      value={target}
                      onChange={(e) => setTarget(e.target.value)}
                    >
                      {state.targets.map((t) => (
                        <option key={t.key} value={t.key}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p className="text-[11px] leading-relaxed text-lx-muted">
                    {state.targets.find((t) => t.key === target)?.note ?? ''}
                  </p>
                  <p className="text-[11px] leading-relaxed text-lx-dim">
                    这条路比直接下现成的 GGUF 慢很多（十几分钟起）。只有这个模型
                    没有现成 GGUF、或者你不想依赖别人压好的文件时才值得走。
                  </p>
                </div>
              )}

              {running && task && (
                <div className="space-y-1.5 rounded-lx-sm border border-lx px-3 py-2.5">
                  <div className="flex items-center justify-between gap-2 text-[11px]">
                    <span className="text-lx">{task.stage}</span>
                    <span className="font-mono tabular-nums text-lx-muted">
                      {task.percent.toFixed(0)}% · {fmtElapsed(task.elapsed)}
                    </span>
                  </div>
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-lx-surface-raised">
                    <div
                      className="h-full rounded-full bg-lx-accent transition-[width] duration-500"
                      style={{ width: `${Math.max(2, task.percent)}%` }}
                    />
                  </div>
                  <p className="break-all font-mono text-[10px] text-lx-dim">{task.source}</p>
                </div>
              )}

              {!running && task && (
                <div className="space-y-1.5 rounded-lx-sm border border-lx px-3 py-2.5">
                  <p
                    className={`text-[11px] ${
                      task.status === 'error'
                        ? 'text-lx-danger'
                        : task.status === 'done'
                          ? 'text-lx-success'
                          : 'text-lx-muted'
                    }`}
                  >
                    {task.message}
                  </p>
                  {done && (
                    <p className="text-[11px] text-lx-muted">
                      去新建任务页，把权重精度选成{' '}
                      <code className="font-mono text-lx">{task.target.replace('_k_m', '').toUpperCase()}</code>
                      （{task.target === 'q6_k' ? 'Q6' : 'Q4'}），就会用这份。
                    </p>
                  )}
                  {task.log_tail.length > 0 && (
                    <details className="pt-0.5" open={task.status === 'error'}>
                      <summary className="cursor-pointer select-none text-[11px] text-lx-muted hover:text-lx-secondary">
                        转换日志（最近 {task.log_tail.length} 行）
                      </summary>
                      <pre className="mt-1.5 max-h-40 overflow-auto rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10px] leading-relaxed text-lx-muted">
                        {task.log_tail.join('\n')}
                      </pre>
                    </details>
                  )}
                </div>
              )}

              {error && <ErrorBox text={error} />}
            </div>

            <footer className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-lx px-[18px] py-3">
              <button type="button" className="lx-btn-ghost" onClick={() => void close()}>
                {running ? '取消转换' : '关闭'}
              </button>
              {!running && !done && state?.available && state.source.ready && (
                <button
                  type="button"
                  className="lx-btn-primary"
                  disabled={busy}
                  onClick={() => void start()}
                >
                  {busy ? '启动中…' : '开始转换并量化'}
                </button>
              )}
            </footer>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
}
