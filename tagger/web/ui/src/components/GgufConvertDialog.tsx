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
import { CheckCircle2, Download, ExternalLink, Loader2, TriangleAlert, X } from 'lucide-react'
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
  initialTarget,
  onClose,
}: {
  open: boolean
  modelKey: string
  /** 从「量化」菜单点进来的档位；没跑任务时以它为准 */
  initialTarget?: string
  onClose: () => void
}) {
  const [state, setState] = useState<GgufConvertState | null>(null)
  const [task, setTask] = useState<GgufConvertTask | null>(null)
  const [target, setTarget] = useState(initialTarget ?? 'q6_k')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [deps, setDeps] = useState<DepState | null>(null)
  const [installing, setInstalling] = useState(false)

  const running = task?.status === 'running'
  const done = task?.status === 'done'

  // 清单里登记的 GGUF 档里，文件真的在盘上的那些。
  // ★ 用"文件在不在"而不是"配置里有没有"，否则删过文件的档也会被说成"已有"。
  const TIER_LABEL: Record<string, string> = {
    f16: 'F16', q8: 'Q8_0', q6: 'Q6_K', q5: 'Q5_K_M', q4: 'Q4_K_M',
  }
  // 弹窗里的档位（q4_k_m / q6_k / f16）→ 清单里的档键（q4 / q6 / f16）。
  // 必须列出来对齐，不能靠削后缀：q6_k 削不出 q6。
  const TIER_OF_TARGET: Record<string, string> = {
    f16: 'f16', q4_k_m: 'q4', q6_k: 'q6',
  }
  const tierOf = (targetKey: string) => TIER_OF_TARGET[targetKey] ?? targetKey
  const localTiers = Object.entries(state?.quants ?? {})
    .filter(([, q]) => q.bytes > 0)
    .map(([name, q]) => ({ name, label: TIER_LABEL[name] ?? name.toUpperCase(), bytes: q.bytes }))
  const pickedTier = localTiers.find((t) => t.name === tierOf(target))

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
        // 已有任务在跑时跟随它的目标，别让下拉框显示一个不相干的档位；
        // 没有在跑的任务就用菜单点进来的那一档。
        if (s.task && s.task.status === 'running') setTarget(s.task.target)
        else if (initialTarget) setTarget(initialTarget)
      })
      .catch((err) => alive && setError(msg(err)))
    return () => {
      alive = false
    }
  }, [open, modelKey, initialTarget])

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

  /** 转到依赖检测页：那边能看这一组到底装了什么、也能顺手把别的组配上。
   *
   * 切路由就够了——依赖检测是独立页面，「模型管理」连同这个弹窗一起卸载，
   * 回来时是干净的。 */
  function goDeps() {
    location.hash = '/deps'
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
                    {/* 两条路都给：赶时间就地点（只装缺的，不动已装的），想
                        看清这一组装了什么、或者顺手把别的组也配上，就去依赖检测。 */}
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
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
                      <button
                        type="button"
                        className="inline-flex items-center gap-1 text-[11px] text-lx-muted underline-offset-2 hover:text-lx hover:underline"
                        onClick={goDeps}
                      >
                        <ExternalLink size={11} /> 去「依赖检测」里装
                      </button>
                    </div>
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
                  <div className="mb-1 text-lx">
                    {localTiers.length > 0 ? '本地已经有压好的 GGUF' : '还缺原始权重'}
                  </div>
                  {/* 目录里躺着几份 .gguf 时，只说"缺原始权重"会让人以为程序
                      没看见文件——先把"已经有哪几档"摆出来。 */}
                  {localTiers.length > 0 && (
                    <p className="text-[11px] leading-relaxed">
                      已经有{' '}
                      {localTiers
                        .map((t) => `${t.label}（${fmtBytes(t.bytes)}）`)
                        .join('、')}
                      。直接就能跑：回「模型管理」把这条的精度切成对应档。
                    </p>
                  )}
                  <p className="mt-1 text-[11px] leading-relaxed">
                    {localTiers.length > 0 ? '重新压一份才需要原始权重' : '本地量化需要 transformers 格式的原始权重'}
                    ，点这一行的「下载原始权重」
                  </p>
                  {state.source.hint && (
                    <p className="mt-1 whitespace-pre-line break-all font-mono text-[10px] text-lx-dim">
                      {state.source.hint}
                    </p>
                  )}
                </div>
              )}

              {state?.available && state.source.ready && !running && !done && (
                <div className="space-y-2.5">
                  <p>
                    源权重 <code className="font-mono text-lx">{fmtBytes(state.source.bytes)}</code>
                    ，先转 F16 GGUF 再压成目标档位。转换期间要占约
                    <span className="text-lx"> 两份模型大小 </span>
                    的磁盘。
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
                          {localTiers.some((x) => x.name === tierOf(t.key)) ? ' · 本地已有' : ''}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p className="text-[11px] leading-relaxed text-lx-muted">
                    {state.targets.find((t) => t.key === target)?.note ?? ''}
                  </p>
                  {pickedTier && (
                    <p className="text-[11px] leading-relaxed text-lx-warning">
                      这一档本地已经有了（{fmtBytes(pickedTier.bytes)}），再压会重新生成一份，同名会覆盖。
                    </p>
                  )}
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
                  {/* 跑的时候也要能看见日志：0% 停住的时候，日志里往往写着
                      "下载源码 8/37 MB" / "Loading checkpoint"——不给看就只能干等。 */}
                  {task.log_tail.length > 0 && (
                    <pre className="mt-0.5 max-h-24 overflow-auto rounded-lx-sm bg-lx-surface-raised px-2 py-1.5 font-mono text-[10px] leading-relaxed text-lx-muted">
                      {task.log_tail.slice(-4).join('\n')}
                    </pre>
                  )}
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
              {/* 跑挂了才发现缺包（例如 tokenizer 那步要 sentencepiece）：
                  后端已经把包装进报错里了，这里再给一个能直接点过去的入口，
                  不用照着报错去命令行敲。 */}
              {error && /依赖|pip install|没装|ModuleNotFoundError/.test(error) && (
                <button
                  type="button"
                  className="inline-flex items-center gap-1 text-[11px] text-lx-muted underline-offset-2 hover:text-lx hover:underline"
                  onClick={goDeps}
                >
                  <ExternalLink size={11} /> 去「依赖检测」里补装
                </button>
              )}
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
