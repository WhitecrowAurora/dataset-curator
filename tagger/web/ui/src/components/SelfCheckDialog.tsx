/**
 * 部署自检弹窗。
 *
 * 装完机器点一下，15 个方块逐格亮起：转圈 → 打勾 / 打叉 / 划掉。
 * 为什么做成"格子墙"而不是一行行文字清单：这一屏要在 5 秒里回答一个
 * 是/否问题（这台机器能干活吗），颜色和形状比读 15 行字快。
 *
 * 三种终态刻意区分：
 *   ✓ 通过   绿
 *   ✕ 失败   红（方块会抖一下，并进下面的错误列表）
 *   – 跳过   灰虚线（这台机器没有这个可选组件：没起服务、没装 torch）
 * 跳过不算失败——装机验证不该因为"没开那个功能"整片飘红。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { FolderOpen, Play, RotateCcw, Square, X } from 'lucide-react'

import { api, type SelfCheckState, type SelfCheckStep } from '../lib/api'
import { useFolderPicker } from './FolderPicker'

const C = {
  pending: 'var(--lx-border-hover)',
  running: 'var(--lx-accent)',
  pass: 'var(--lx-success)',
  fail: 'var(--lx-danger)',
  skip: 'var(--lx-text-dim)',
} as const

/** 勾：描边动画。key 变化时重挂载，所以每次进 pass 都会重新画一遍。 */
function Tick({ color }: { color: string }) {
  return (
    <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke={color}
      strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
      <motion.path
        d="M5 12.5 L10 17.5 L19 6.5"
        initial={{ pathLength: 0, opacity: 0.4 }}
        animate={{ pathLength: 1, opacity: 1 }}
        transition={{ duration: 0.34, ease: 'easeOut' }}
      />
    </svg>
  )
}

/** 叉：两条线先后画出来，比一个静态 ✕ 更有"判定"的感觉。 */
function Cross({ color }: { color: string }) {
  return (
    <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke={color}
      strokeWidth="2.6" strokeLinecap="round">
      <motion.path d="M7 7 L17 17" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }}
        transition={{ duration: 0.2, ease: 'easeOut' }} />
      <motion.path d="M17 7 L7 17" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }}
        transition={{ duration: 0.2, ease: 'easeOut', delay: 0.12 }} />
    </svg>
  )
}

/** 转圈：底环 + 一段短弧在转。短弧比整圈更像"在跑"而不是"卡住"。 */
function Spinner({ color }: { color: string }) {
  return (
    <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke={color}>
      <circle cx="12" cy="12" r="9" strokeWidth="2" opacity={0.16} />
      <motion.circle
        cx="12" cy="12" r="9" strokeWidth="2" strokeLinecap="round"
        strokeDasharray="13 44"
        animate={{ rotate: 360 }}
        transition={{ repeat: Infinity, duration: 0.85, ease: 'linear' }}
        style={{ transformOrigin: '12px 12px' }}
      />
    </svg>
  )
}

function Glyph({ step }: { step: SelfCheckStep }) {
  if (step.status === 'running') return <Spinner color={C.running} />
  if (step.status === 'pass') return <Tick color={C.pass} />
  if (step.status === 'fail') return <Cross color={C.fail} />
  if (step.status === 'skip') {
    return (
      <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke={C.skip}
        strokeWidth="2.4" strokeLinecap="round">
        <path d="M7 12 H17" />
      </svg>
    )
  }
  return (
    <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke={C.pending}>
      <circle cx="12" cy="12" r="3.2" strokeWidth="2" opacity={0.5} />
    </svg>
  )
}

function Cell({
  step,
  index,
  active,
  onClick,
}: {
  step: SelfCheckStep
  index: number
  active: boolean
  onClick: () => void
}) {
  const running = step.status === 'running'
  const passed = step.status === 'pass'
  const failed = step.status === 'fail'
  const skipped = step.status === 'skip'
  const tint = C[step.status]

  return (
    <motion.button
      type="button"
      onClick={onClick}
      title={step.detail || step.title}
      className="relative flex aspect-square flex-col items-center justify-center gap-1 rounded-[10px] border px-1"
      style={{
        borderColor: skipped ? 'var(--lx-border)' : tint,
        borderStyle: skipped ? 'dashed' : 'solid',
        // 运行中**不填底色**：accent 本身就是橙红，填上之后和"失败"几乎分不开，
        // 用户会以为这一格已经挂了。靠描边 + 呼吸光 + 转圈表达"正在跑"更准。
        background:
          passed ? 'var(--lx-success-subtle)'
            : failed ? 'var(--lx-danger-subtle)'
              : 'var(--lx-surface-raised)',
        opacity: skipped ? 0.6 : 1,
        cursor: step.detail ? 'pointer' : 'default',
      }}
      initial={{ opacity: 0, scale: 0.86 }}
      animate={{
        opacity: skipped ? 0.6 : 1,
        scale: 1,
        // 失败时抖一下：一眼就能找到是哪一格断了
        x: failed ? [0, -2.5, 2.5, -1.5, 1.5, 0] : 0,
      }}
      transition={{
        delay: failed ? 0 : index * 0.025,
        duration: failed ? 0.32 : 0.22,
        ease: 'easeOut',
      }}
      whileHover={{ scale: 1.04 }}
      whileTap={{ scale: 0.97 }}
    >
      {/* 运行时背后一圈呼吸光：转圈之外再给一个"它在忙"的隐喻 */}
      {running && (
        <motion.span
          className="pointer-events-none absolute inset-0 rounded-[10px]"
          animate={{ opacity: [0.0, 0.5, 0.0] }}
          transition={{ repeat: Infinity, duration: 1.4, ease: 'easeInOut' }}
          style={{ background: 'var(--lx-accent-ring)' }}
        />
      )}
      <motion.span
        key={step.status}
        initial={{ scale: 0.7, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ type: 'spring', stiffness: 520, damping: 24 }}
        className="relative flex items-center justify-center"
        style={{ height: 28 }}
      >
        <Glyph step={step} />
      </motion.span>
      <span
        className="relative text-center text-[10px] leading-tight"
        style={{
          color: arrived(step) ? tint : 'var(--lx-text-muted)',
          fontWeight: active ? 600 : 400,
        }}
      >
        {step.title}
      </span>
      {active && (
        <motion.span
          layoutId="selfcheck-focus"
          className="pointer-events-none absolute -inset-[2px] rounded-[11px]"
          style={{ border: '2px solid var(--lx-accent)' }}
          transition={{ type: 'spring', stiffness: 420, damping: 32 }}
        />
      )}
    </motion.button>
  )
}

function arrived(step: SelfCheckStep): boolean {
  return step.status === 'pass' || step.status === 'fail' || step.status === 'skip'
}

export function SelfCheckDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [state, setState] = useState<SelfCheckState | null>(null)
  const [dir, setDir] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [sample, setSample] = useState(3)
  const [url, setUrl] = useState('')
  const folder = useFolderPicker()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [focus, setFocus] = useState<string | null>(null)
  const alive = useRef(true)

  const msg = (e: unknown) => (e instanceof Error ? e.message : String(e))

  const pull = useCallback(async () => {
    try {
      const s = await api.selfcheck()
      if (alive.current) setState(s)
    } catch {
      /* 后端刚起来时会短暂失败，下一轮自己会好 */
    }
  }, [])

  // 打开就拉一次，之后 350ms 一轮：后端每项至少停 350ms，再慢就会漏掉
  // "运行中"那一帧，用户看到的是一格一格直接变绿，动画也就白做了。
  useEffect(() => {
    if (!open) return
    alive.current = true
    void pull()
    const timer = window.setInterval(() => void pull(), 350)
    return () => {
      alive.current = false
      window.clearInterval(timer)
    }
  }, [open, pull])

  // 打开时把上次的输入恢复回来：自检往往要连点几次，不想每次都重选目录
  useEffect(() => {
    if (!open || !state) return
    if (!dir && state.input_dir) setDir(state.input_dir)
    if (state.url) setUrl((v) => v || state.url)
  }, [open, state, dir])

  const running = state?.status === 'running'
  const steps = state?.steps ?? []
  const finished = steps.filter(arrived).length
  const total = steps.length || 1
  const percent = Math.round((finished / total) * 100)
  const fails = steps.filter((s) => s.status === 'fail')
  const current = steps.find((s) => s.status === 'running')
  const focused = steps.find((s) => s.key === focus)

  async function start() {
    setBusy(true)
    setErr(null)
    setFocus(null)
    try {
      setState(await api.selfcheckStart({
        input_dir: dir,
        recursive,
        sample,
        url: url.trim(),
      }))
    } catch (e) {
      setErr(msg(e))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    setBusy(true)
    try {
      setState(await api.selfcheckStop())
    } catch (e) {
      setErr(msg(e))
    } finally {
      setBusy(false)
    }
  }

  const verdict = (() => {
    if (busy && !running) return '准备中…'
    if (running) return current ? `正在测试：${current.title}` : '正在测试…'
    if (!state || state.status === 'idle') return '还没有跑过'
    if (state.status === 'stopped') return '已停止'
    if (fails.length) return `${fails.length} 项未通过，看下面`
    return `全部通过（${state.summary.pass} 项）`
  })()

  return (
    <>
      <AnimatePresence>
        {open && (
        <motion.div
          className="fixed inset-0 z-[10000] flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget && !running) onClose()
          }}
          role="dialog"
          aria-modal="true"
          aria-label="部署自检"
        >
          <div
            className="absolute inset-0"
            style={{
              backgroundColor: 'color-mix(in srgb, var(--lx-bg) 62%, transparent)',
              backdropFilter: 'blur(3px)',
            }}
          />
          <motion.div
            className="lx-sheet relative flex max-h-[88vh] w-full max-w-[720px] flex-col overflow-hidden"
            initial={{ opacity: 0, scale: 0.97, y: 10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 6 }}
            transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          >
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-lx px-[18px] py-3">
              <div className="flex items-baseline gap-2">
                <h3 className="text-[15px] font-semibold text-lx">部署自检</h3>
                <span className="text-[11px] text-lx-dim">
                  逐项验一遍这台机器能不能打图
                </span>
              </div>
              <button className="lx-btn-ghost-icon" onClick={onClose} aria-label="关闭">
                <X size={16} />
              </button>
            </header>

            <div className="min-h-0 flex-1 overflow-y-auto px-[18px] py-3.5">
              {/* 配置区：跑起来之后收成一行摘要，把屏幕让给格子 */}
              {running ? (
                <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-lx-dim">
                  <span className="font-mono">{state?.input_dir}</span>
                  <span>{state?.recursive ? '含子文件夹' : '仅当前层'}</span>
                  <span>抽样 {state?.sample} 张</span>
                  {state?.url ? <span className="font-mono">{state.url}</span> : null}
                </div>
              ) : (
                <div className="mb-3 space-y-2.5">
                  <div className="flex items-center gap-2">
                    <input
                      className="lx-input min-w-0 flex-1 font-mono text-[11px]"
                      placeholder="选一个包含图片的文件夹，例如 D:\datasets\samples"
                      value={dir}
                      onChange={(e) => setDir(e.target.value)}
                      spellCheck={false}
                    />
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex shrink-0 items-center gap-1.5 px-3"
                      onClick={() => folder.pick(dir, setDir)}
                    >
                      <FolderOpen size={12} />
                      浏览
                    </button>
                  </div>

                  <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-[11px] text-lx-muted">
                    <label className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={recursive}
                        onChange={(e) => setRecursive(e.target.checked)}
                      />
                      包含子文件夹
                    </label>
                    <label className="flex items-center gap-1.5">
                      抽样
                      <input
                        type="number"
                        className="lx-input w-16 font-mono tabular-nums"
                        min={1}
                        max={10}
                        value={sample}
                        onChange={(e) =>
                          setSample(Math.min(10, Math.max(1, Number(e.target.value) || 1)))
                        }
                      />
                      张
                    </label>
                    <label className="flex flex-1 items-center gap-1.5">
                      <span className="shrink-0">服务地址</span>
                      <input
                        className="lx-input min-w-0 flex-1 font-mono text-[11px]"
                        placeholder="留空 = 自动找已就绪的多卡服务"
                        value={url}
                        onChange={(e) => setUrl(e.target.value)}
                        spellCheck={false}
                      />
                    </label>
                  </div>

                  {err && (
                    <div className="text-[11px]" style={{ color: 'var(--lx-danger-text)' }}>
                      {err}
                    </div>
                  )}
                </div>
              )}

              {/* 格子墙：15 项，5 列 */}
              <div className="grid grid-cols-5 gap-2">
                {steps.map((step, i) => (
                  <Cell
                    key={step.key}
                    step={step}
                    index={i}
                    active={focus === step.key}
                    onClick={() => setFocus(focus === step.key ? null : step.key)}
                  />
                ))}
              </div>

              {/* 单格详情：点哪格看哪格 */}
              <AnimatePresence mode="wait">
                {focused && (
                  <motion.div
                    key={focused.key}
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.16 }}
                    className="overflow-hidden"
                  >
                    <div
                      className="mt-2 rounded-[8px] px-2.5 py-2 text-[11px] leading-relaxed"
                      style={{
                        background: 'var(--lx-surface-raised)',
                        color: 'var(--lx-text-secondary)',
                      }}
                    >
                      <span className="font-semibold" style={{ color: C[focused.status] }}>
                        {focused.title}
                      </span>
                      {focused.detail ? ` · ${focused.detail}` : ' · 还没跑'}
                      {focused.ms > 0 && (
                        <span className="ml-1 font-mono text-lx-dim">{focused.ms}ms</span>
                      )}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>

              {/* 失败清单：失败必须能读到原因，不能只靠点开每一格 */}
              <AnimatePresence>
                {fails.length > 0 && (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    className="mt-3 space-y-1"
                  >
                    {fails.map((f) => (
                      <div
                        key={f.key}
                        className="rounded-[8px] px-2.5 py-1.5 text-[11px] leading-relaxed"
                        style={{
                          background: 'var(--lx-danger-subtle)',
                          border: '1px solid var(--lx-danger-border)',
                          color: 'var(--lx-danger-text)',
                        }}
                      >
                        <span className="font-semibold">{f.title}</span>
                        <span className="ml-1.5" style={{ color: 'var(--lx-text-secondary)' }}>
                          {f.detail}
                        </span>
                      </div>
                    ))}
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            <footer className="shrink-0 border-t border-lx px-[18px] py-3">
              {/* 底部：进度条 + 旋转小图标 + 当前在测哪一项 */}
              <div className="mb-2.5 flex items-center gap-2.5">
                <div
                  className="h-[5px] flex-1 overflow-hidden rounded-full"
                  style={{ background: 'var(--lx-bar-track)' }}
                >
                  <motion.div
                    className="h-full rounded-full"
                    style={{
                      background: fails.length
                        ? 'var(--lx-danger)'
                        : state?.status === 'done'
                          ? 'var(--lx-success)'
                          : 'var(--lx-accent)',
                    }}
                    animate={{ width: `${percent}%` }}
                    transition={{ type: 'spring', stiffness: 220, damping: 30 }}
                  />
                </div>
                <span className="w-10 text-right font-mono text-[11px] text-lx-dim">
                  {percent}%
                </span>
                {running && <Spinner color="var(--lx-accent)" />}
              </div>

              <div className="flex items-center justify-between gap-3">
                <span
                  className="min-w-0 flex-1 truncate text-[11px]"
                  style={{
                    color: fails.length && !running
                      ? 'var(--lx-danger-text)'
                      : 'var(--lx-text-muted)',
                  }}
                >
                  {verdict}
                </span>
                <div className="flex shrink-0 items-center gap-2">
                  {running ? (
                    <button
                      type="button"
                      className="lx-btn-ghost inline-flex items-center gap-1.5 px-3"
                      disabled={busy}
                      onClick={() => void stop()}
                    >
                      <Square size={12} />
                      停止
                    </button>
                  ) : (
                    // 只留一个按钮：跑完之后"再跑一次"和"开始"是同一件事，
                    // 摆两个只会让人犹豫该点哪个。
                    <button
                      type="button"
                      className="lx-btn-primary inline-flex items-center gap-1.5"
                      disabled={busy || !dir.trim()}
                      onClick={() => void start()}
                    >
                      {state && state.status !== 'idle' ? (
                        <RotateCcw size={12} />
                      ) : (
                        <Play size={12} />
                      )}
                      {busy
                        ? '准备中…'
                        : state && state.status !== 'idle'
                          ? '重新自检'
                          : '开始自检'}
                    </button>
                  )}
                </div>
              </div>
            </footer>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {folder.dialog}
    </>
  )
}
