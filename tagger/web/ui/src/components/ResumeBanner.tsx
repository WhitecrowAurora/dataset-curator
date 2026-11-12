/**
 * "上次那个任务还没跑完"的横幅。
 *
 * 触发场景只有一个：用户按了暂停（或停止）之后把程序关了。进程没了，
 * 内存里的任务也没了，只有 `run/resume.json` 还记着参数，产物还躺在盘上。
 * 所以这里做的是**用原参数重建任务**——已经打过的图由产物 + 指纹判定
 * 跳过，效果等同于"从断点继续"，但不需要任何额外的进度文件。
 *
 * 为什么放在页面最上层而不是某个页里：用户下次打开程序的目的往往就是
 * "接着昨天那个活儿干"，让他先去某个页面里找一圈是把简单事做复杂。
 */
import { useEffect, useState } from 'react'
import { ArrowRight, Loader2, Play, X } from 'lucide-react'

import { api, type ResumeInfo } from '../lib/api'

function fmtWhen(ts: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function ResumeBanner({ onOpen }: { onOpen: (jobId: string) => void }) {
  const [info, setInfo] = useState<ResumeInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api
      .resumeState()
      .then((r) => alive && setInfo(r.ok ? r.resume : null))
      .catch(() => {
        /* 后端刚起来时可能还没就绪，静默即可——这只是一条提示 */
      })
    return () => {
      alive = false
    }
  }, [])

  if (!info) return null

  const done = (info.counts.done ?? 0) + (info.counts.skipped ?? 0)
  const total = info.counts.total ?? 0

  async function go() {
    setBusy(true)
    setErr(null)
    try {
      const snap = await api.resumeStart()
      setInfo(null)
      onOpen(snap.id)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function dismiss() {
    try {
      await api.resumeDismiss()
    } catch {
      /* 删不掉也只是下次再提示一次 */
    }
    setInfo(null)
  }

  return (
    <div
      className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lx-sm border-l-2 px-4 py-2.5"
      style={{
        borderColor: 'var(--lx-accent)',
        background: 'var(--lx-accent-subtle)',
      }}
    >
      <div className="min-w-0 flex-1 text-xs" style={{ color: 'var(--lx-text-secondary)' }}>
        <span className="font-semibold" style={{ color: 'var(--lx-accent-text)' }}>
          上次的任务没跑完
        </span>
        <span className="ml-2">
          {info.why === 'paused' ? '已暂停' : info.why === 'failed' ? '失败待补齐' : '已停止'}
          {info.saved_at ? ` · ${fmtWhen(info.saved_at)}` : ''}
        </span>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-lx-dim">
          <span className="truncate font-mono">{info.input_dir}</span>
          <ArrowRight size={11} className="shrink-0" />
          <span className="truncate font-mono">{info.out_dir}</span>
          {total > 0 && (
            <span className="tabular-nums">
              · 已完成 {done} / {total}
            </span>
          )}
        </div>
        {err && (
          <div className="mt-1" style={{ color: 'var(--lx-danger-text)' }}>
            {err}
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          className="lx-btn-ghost inline-flex items-center gap-1.5 px-2.5"
          onClick={() => void dismiss()}
          title="不再提示（产物不会删）"
        >
          <X size={12} /> 忽略
        </button>
        <button
          type="button"
          className="lx-btn-primary inline-flex items-center gap-1.5"
          disabled={busy}
          onClick={() => void go()}
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
          {busy ? '重建中…' : '继续打标'}
        </button>
      </div>
    </div>
  )
}
