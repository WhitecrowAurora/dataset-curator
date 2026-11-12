/**
 * 侧栏「重试失败」按钮。
 *
 * 它做什么：把**最近一个任务**的 failures.jsonl 里那些图挑出来重打一遍。
 * 仍失败的样本由后端剔除出分母（excused.jsonl），分片因此可以"有条件完成"，
 * 不会再被几张救不回来的图永久卡住。
 *
 * 为什么要先探测再让点：这个按钮一按就真的去推理、真的改产物目录。一个从
 * 来没失败过的任务上，如果照旧可以点，用户点下去只会看到一句报错——那还不
 * 如一开始就把"现在没有可重试的失败项"写在按钮上，并 `disabled`。
 *
 * 探测口径与后端完全一致（failures 减去 excused）：
 *   - failures.jsonl = 还没处理完的失败，下次还会再试
 *   - excused.jsonl  = 已经放弃、从分母剔除的坏样本，不会再试
 * 两个都读，才能回答"这个按钮现在有没有活可干"。
 */
import { useEffect, useRef, useState } from 'react'
import { Loader2, RotateCcw } from 'lucide-react'

import { api } from '../lib/api'
import { cn } from '../lib/cn'

type Book = { failed: number; excused: number; todo: number; status: string }

export function RetryFailedButton({
  jobId,
  onStarted,
}: {
  /** 要重试的任务；空串 = 还没有任务，按钮不可用 */
  jobId: string
  onStarted: (jobId: string) => void
}) {
  const [book, setBook] = useState<Book | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)
  const generation = useRef(0)

  useEffect(() => {
    const current = ++generation.current
    let timer: number | undefined
    setBook(null)
    setBusy(false)
    const probe = async () => {
      try {
        const next = await api.jobFailureBook(jobId)
        if (generation.current !== current) return
        setBook(next)
        if (['done', 'failed', 'stopped'].includes(next.status)) return
      } catch {
        if (generation.current !== current) return
      }
      timer = window.setTimeout(() => void probe(), 2000)
    }
    if (jobId) void probe()
    return () => {
      generation.current += 1
      window.clearTimeout(timer)
    }
  }, [jobId, refresh])

  const todo = book?.todo ?? 0
  const running = book !== null && ['pending', 'running', 'paused'].includes(book.status)
  const usable = Boolean(jobId) && todo > 0 && !busy && !running

  async function run() {
    if (!usable) return
    setBusy(true)
    setErr(null)
    const current = generation.current
    try {
      const snap = await api.retryFailed(jobId)
      if (generation.current !== current) return
      onStarted(snap.id)
      // 重试任务已建好，这一份的账本马上会被改写，先别用旧数字骗人。
      setBook(null)
    } catch (e) {
      if (generation.current !== current) return
      setErr(e instanceof Error ? e.message : String(e))
      setRefresh((value) => value + 1)
    } finally {
      if (generation.current === current) setBusy(false)
    }
  }

  const title = !jobId
    ? '还没有跑过任务'
    : busy
      ? '正在重建重试任务…'
      : running
        ? '任务仍在运行，结束后可重试失败项'
        : book === null
          ? '正在检查失败项…'
          : todo > 0
            ? `重打 ${todo} 张失败的图。仍失败会从分母剔除，明细进 excused.jsonl`
            : book.excused > 0
              ? `没有待重试的失败项（另有 ${book.excused} 张已剔除的坏样本不会再试）`
              : '当前任务没有失败的图'

  return (
    <div className="flex flex-col gap-1">
      <button
        type="button"
        onClick={() => void run()}
        disabled={!usable}
        title={title}
        className={cn(
          'group flex w-full items-center gap-2 rounded-lx-sm px-2 py-[6px] text-left text-[12.5px] transition-colors',
          usable
            ? 'cursor-pointer text-lx-secondary hover:bg-[color-mix(in_srgb,var(--lx-text)_4%,transparent)] hover:text-lx'
            : 'cursor-not-allowed text-lx-dim opacity-60',
        )}
      >
        <span className="shrink-0 text-lx-muted group-hover:text-lx-secondary">
          {busy ? <Loader2 size={15} className="animate-spin" /> : <RotateCcw size={15} />}
        </span>
        <span className="min-w-0 flex-1 truncate">
          {busy ? '重试中…' : '重试失败'}
        </span>
        {/* 数量和按钮写在同一行：这也是"现在能不能点"的答案，
            比把数字藏进 tooltip 更有用——用户不用悬停就知道有没有活。 */}
        {todo > 0 && (
          <span className="shrink-0 rounded-full bg-lx-accent-subtle px-1.5 text-[10.5px] tabular-nums text-lx-accent-text">
            {todo}
          </span>
        )}
      </button>
      {err && (
        <p className="px-2 text-[10.5px] leading-snug text-lx-danger-text">{err}</p>
      )}
      {!err && book !== null && todo === 0 && book.excused > 0 && (
        <p className="px-2 text-[10.5px] leading-snug text-lx-dim">
          {book.excused} 张已剔除（不再重试）
        </p>
      )}
    </div>
  )
}
