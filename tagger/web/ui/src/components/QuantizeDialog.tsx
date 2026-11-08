/**
 * W4A16 量化对话框。
 *
 * 为什么要有这一步：W4A16 不是运行时开关——它需要一份**预先量化好**的权重。
 * 页面上一选就生效的话，实际只是 plan 里多一句"回退 BF16"，用户还以为省了
 * 显存。所以把"要不要现在量化"摆到台面上。
 *
 * 取消 = 回默认精度（BF16）。这是刻意的：留在 W4A16 会得到一个"看起来选了
 * 4bit、实际跑 16bit"的状态，比直接退回去更误导。量化已经跑起来时才取消，
 * 会顺带把子进程收掉（半成品目录保留，下次覆盖写）。
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { CheckCircle2, Download, Loader2, TriangleAlert, X } from 'lucide-react'
import { api, type DepState, type QuantState, type QuantTaskState } from '../lib/api'
import { StatusChip } from './ui/launcher'
import { ErrorBox } from './ui/ErrorBox'

function msg(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function fmtBytes(n: number): string {
  const gb = n / (1 << 30)
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(n / (1 << 20))} MB`
}

/**
 * 显存档位的经验结论。
 *
 * 口径纠正过两次，这里记下来免得再错：
 * 1. 一开始写"16G 只能 Q4"——错的。16G 上 Q6_K（6.3G）+ mmproj（1.1G）+ KV
 *    完全放得下，实测加载后 10.5/16 G。
 * 2. 后来写"并发降到 2~3"——也是错的。**KV 按槽位数在启动时预留**，把任务
 *    并发调小只是让 slot 空着，显存一点不还。真要腾显存是调 llama.cpp 的
 *    「槽位（-np）」，在新建任务页的服务设置里。
 *
 * 所以这里只回答"这张卡该选哪一档精度"，腾显存的动作指向槽位。
 */
function vramHint(gb: number | null): string {
  if (gb === null) {
    return '16G 档可整卡跑 Q6/FP8；只有 12G 才需要 Q4。要让出显存调「槽位（-np）」。'
  }
  const v = Math.round(gb)
  if (v >= 24) return `本机 ${v}G：F16 / FP8 都能整卡装下，不必量化。`
  if (v >= 13) {
    return `本机 ${v}G：Q6 / FP8 能整卡装下。要让显存给别的程序，去调「槽位（-np）」。`
  }
  return `本机 ${v}G：建议量化到 Q4（W4A16）。`
}

function fmtElapsed(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return m > 0 ? `${m} 分 ${String(s).padStart(2, '0')} 秒` : `${s} 秒`
}

export function QuantizeDialog({
  open,
  modelKey,
  onCancel,
  onUse,
}: {
  open: boolean
  /** '' = 按显存自动落档 */
  modelKey: string
  /** 放弃：调用方把精度退回 BF16 */
  onCancel: () => void
  /** 确认使用 W4A16（已有权重或刚量化完） */
  onUse: () => void
}) {
  const [info, setInfo] = useState<QuantState | null>(null)
  const [task, setTask] = useState<QuantTaskState | null>(null)
  const [outDir, setOutDir] = useState('')
  const [method, setMethod] = useState('gptq')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 已有权重时默认只显示"直接用"，点「重新量化」才展开目录/方法
  const [showForm, setShowForm] = useState(false)
  // 缺依赖时的"一键安装"：状态直接复用依赖检测页那份（同一条 pip 队列、
  // 同一个日志），不另起一套安装器。
  const [deps, setDeps] = useState<DepState | null>(null)
  const [installing, setInstalling] = useState(false)
  // 只为了在选择量化档位时给一句"你这张卡该选哪个"，用完即弃。
  const [vramGb, setVramGb] = useState<number | null>(null)

  useEffect(() => {
    if (!open) return
    let alive = true
    api
      .sysinfo()
      .then((s) => {
        if (alive) setVramGb(s.vram_total_gb ?? null)
      })
      .catch(() => {
        /* 拿不到就按保守文案提示，不影响主流程 */
      })
    return () => {
      alive = false
    }
  }, [open])

  const running = task?.status === 'running'
  const canUse = Boolean(info?.has_quantized) || task?.status === 'done'
  const installLog = deps?.job?.log ?? []

  // 打开时拉一次：能不能量化、有没有现成权重、默认落哪
  useEffect(() => {
    if (!open) return
    let alive = true
    setError(null)
    setShowForm(false)
    setTask(null)
    setInfo(null)
    api
      .quantize(modelKey || undefined)
      .then((s) => {
        if (!alive) return
        setInfo(s)
        setTask(s.task)
        setOutDir(s.suggested_dir)
        setMethod(s.methods.includes('gptq') ? 'gptq' : (s.methods[0] ?? 'gptq'))
        // 没有现成权重时直接展开表单，少点一次
        setShowForm(!s.has_quantized)
      })
      .catch((err) => alive && setError(msg(err)))
    return () => {
      alive = false
    }
  }, [open, modelKey])

  // 量化期间轮询。1.5s 一次：进度是阶段级的，再密也看不出差别。
  useEffect(() => {
    if (!open || !running) return
    let alive = true
    const timer = window.setInterval(() => {
      api
        .quantize(modelKey || undefined)
        .then((s) => {
          if (!alive) return
          setInfo(s)
          if (s.task) setTask(s.task)
        })
        .catch(() => {
          /* 轮询失败不该把弹窗变成报错页，下一次再试 */
        })
    }, 1500)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [open, running, modelKey])

  // ESC 与遮罩点击同义：取消并回到默认精度。量化跑着的时候要先掐掉子进程，
  // 否则弹窗关了它还在偷偷吃显存。
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (running) void api.quantizeCancel().catch(() => {})
      onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, running, onCancel])

  // 安装期间轮询同一条 pip 队列：几百 MB 的下载没有输出是最难熬的，
  // 而且用户需要看到"装的是哪个包、走到哪一步"。
  useEffect(() => {
    if (!installing) return
    let alive = true
    const timer = window.setInterval(() => {
      api
        .deps()
        .then((s) => {
          if (!alive) return
          setDeps(s)
          const st = s.job?.status
          if (st && st !== 'running') setInstalling(false)
        })
        .catch(() => {
          /* 轮询失败不该把弹窗变成报错页 */
        })
    }, 1500)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [installing])

  // 装完立刻重查可用性（refresh=1 清掉后端 60 秒的缓存）。不这么做的话，
  // 用户装完回来看到的仍是"这台机器现在不能量化"，只会认为白装了。
  const installDone = deps?.job != null && deps.job.status !== 'running'
  useEffect(() => {
    if (!installDone) return
    let alive = true
    api
      .quantize(modelKey || undefined, true)
      .then((s) => {
        if (!alive) return
        setInfo(s)
        setShowForm(!s.has_quantized)
      })
      .catch(() => {
        /* 刷新失败就保持原样，用户可以关掉重开 */
      })
    return () => {
      alive = false
    }
  }, [installDone, modelKey])

  async function installDeps() {
    setError(null)
    setInstalling(true)
    try {
      setDeps(await api.depInstallExtra('quantize'))
    } catch (err) {
      setError(msg(err))
      setInstalling(false)
    }
  }

  async function start() {
    setBusy(true)
    setError(null)
    try {
      setTask(
        await api.quantizeStart({
          model: modelKey || undefined,
          out_dir: outDir.trim() || undefined,
          method,
        }),
      )
    } catch (err) {
      setError(msg(err))
    } finally {
      setBusy(false)
    }
  }

  async function close() {
    if (running) {
      try {
        await api.quantizeCancel()
      } catch {
        /* 已经自己结束了 */
      }
    }
    onCancel()
  }

  // 挂到 body 上，而不是留在页面 DOM 里：页面根节点带 .lx-page-enter（入场
  // 动画结束后仍残留 transform），会让 position:fixed 相对那个"整页高"的容器
  // 定位，弹窗于是出现在整页的中间——用户在顶部时要往下滚才看得见。
  // FolderPicker / ShardAssets 踩过同一个坑，处理方式一致。
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
          aria-label="量化为 W4A16"
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
                <h3 className="text-[15px] font-semibold text-lx">量化为 W4A16</h3>
                {running && (
                  <StatusChip tone="warning" icon={<Loader2 size={11} className="animate-spin" />}>
                    {task?.percent.toFixed(0) ?? 0}%
                  </StatusChip>
                )}
                {!running && task?.status === 'done' && (
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
              {!info && !error && (
                <div className="flex items-center gap-2 py-2">
                  <Loader2 size={14} className="animate-spin" /> 读取量化环境…
                </div>
              )}

              {info && !info.available && !info.has_quantized && (
                <div className="rounded-lx-sm border border-lx-warning/40 bg-lx-warning/5 px-3 py-2.5 text-lx-warning">
                  <div className="mb-1 flex items-center gap-1.5 font-medium">
                    <TriangleAlert size={13} /> 这台机器现在不能量化
                  </div>
                  <p className="leading-relaxed text-lx-muted">{info.reason}</p>

                  {/* 缺依赖时给按钮：后端原来只会说"pip install ..."，
                      要求用户离开页面去命令行找解释器，等于这个功能不存在。 */}
                  {(info.missing_deps ?? []).length > 0 && (
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
                        {installing
                          ? '正在安装依赖…'
                          : `一键安装（${(info.missing_deps ?? []).join('、')}）`}
                      </button>
                      {installing && (
                        <pre className="max-h-28 overflow-auto rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10px] leading-relaxed text-lx-muted">
                          {installLog.slice(-6).join('\n') || '正在启动 pip…'}
                        </pre>
                      )}
                      {!installing && deps?.job?.status === 'failed' && (
                        <p className="text-[11px] text-lx-danger">
                          安装失败，上面日志的最后几行就是原因。也可以命令行
                          <code className="mx-1 font-mono">pip install &quot;tagger[quantize]&quot;</code>
                          再试。
                        </p>
                      )}
                    </div>
                  )}
                </div>
              )}

              {info?.available && (
                <p>
                  选中的模型是 <code className="font-mono text-lx">{info.model}</code>。
                  W4A16 需要一份预先量化好的权重，不能由 BF16 在启动时直接转换；
                  量化要跑几十分钟，产物约是原权重的四分之一。
                </p>
              )}

              {info?.has_quantized && !running && !showForm && (
                <div className="rounded-lx-sm border border-lx px-3 py-2.5">
                  <div className="mb-1 flex items-baseline justify-between gap-2">
                    <span className="text-lx">已有量化权重</span>
                    {/* 有了体积才敢决定"要不要留着原权重" */}
                    {info.quantized_bytes ? (
                      <span className="text-[11px] tabular-nums text-lx-muted">
                        {fmtBytes(info.quantized_bytes)}
                      </span>
                    ) : null}
                  </div>
                  <code className="break-all font-mono text-[11px] text-lx-secondary">
                    {info.quantized_path}
                  </code>
                </div>
              )}

              {showForm && info?.available && !running && task?.status !== 'done' && (
                <div className="space-y-2.5">
                  <label className="block">
                    <span className="mb-1 block text-[11px] text-lx-muted">产物目录（必须在 models/ 之内）</span>
                    <input
                      className="lx-input font-mono text-xs"
                      value={outDir}
                      onChange={(e) => setOutDir(e.target.value)}
                      spellCheck={false}
                    />
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-[11px] text-lx-muted">量化算法</span>
                    <select
                      className="lx-select"
                      value={method}
                      onChange={(e) => setMethod(e.target.value)}
                    >
                      {info.methods.map((m) => (
                        <option key={m} value={m}>
                          {m === 'gptq' ? 'GPTQ（推荐，校准稳定）' : 'AWQ（激活感知）'}
                        </option>
                      ))}
                    </select>
                  </label>
                  {/* 显存档位的经验结论。写在选算法的位置，因为这里才是真正
                      要做取舍的地方；写进文档没人会去翻。 */}
                  <p className="text-[11px] leading-relaxed text-lx-muted">
                    {vramHint(vramGb)}
                  </p>
                </div>
              )}

              {task && (
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
                  {task.message && (
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
                  )}
                  {task.log_tail.length > 0 && (
                    <details className="pt-0.5">
                      <summary className="cursor-pointer select-none text-[11px] text-lx-muted hover:text-lx-secondary">
                        量化日志（最近 {task.log_tail.length} 行）
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
              {running ? (
                <button type="button" className="lx-btn-ghost" onClick={() => void close()}>
                  取消量化
                </button>
              ) : (
                <button type="button" className="lx-btn-ghost" onClick={() => void close()}>
                  取消（回到 BF16）
                </button>
              )}

              {/* 已有量化权重时不受 available 限制：权重是现成的，这台机器
                  不能量化也不影响直接用它（比如权重在共享盘、vLLM 跑在别处）。 */}
              {!running && (info?.available || canUse) && (
                canUse && (!showForm || task?.status === 'done') ? (
                  <>
                    {info?.available && (
                      <button
                        type="button"
                        className="lx-btn-ghost"
                        onClick={() => setShowForm(true)}
                      >
                        重新量化
                      </button>
                    )}
                    <button type="button" className="lx-btn-primary" onClick={onUse}>
                      使用 W4A16
                    </button>
                  </>
                ) : info?.available ? (
                  <button
                    type="button"
                    className="lx-btn-primary"
                    disabled={busy || !outDir.trim()}
                    onClick={() => void start()}
                  >
                    {busy ? '启动中…' : '开始量化'}
                  </button>
                ) : null
              )}
            </footer>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
}
