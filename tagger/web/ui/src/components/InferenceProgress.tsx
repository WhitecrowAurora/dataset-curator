import { Gauge } from 'lucide-react'
import type { OutputProgress } from '../lib/api'
import { fmtCount, fmtPct, fmtSeconds } from '../lib/format'
import { ProgressBar } from './ui/ProgressBar'

export function InferenceProgress({ outputs, finished }: {
  outputs: { tag: OutputProgress; nl: OutputProgress }
  finished: boolean
}) {
  return (
    <div className="grid gap-5 border-t border-lx px-[18px] py-5 md:grid-cols-2 md:px-7">
      {(['tag', 'nl'] as const).map((part) => {
        const progress = outputs[part]
        return (
          <section key={part} aria-label={`${part.toUpperCase()} 打标进度`} className="min-w-0">
            <div className="flex items-baseline justify-between gap-3">
              <h3 className="text-xs font-semibold text-lx">{part === 'tag' ? 'TAG 打标' : 'NL 描述'}</h3>
              <span className="font-mono text-sm tabular-nums text-lx">
                {progress.progress === null ? `${fmtCount(progress.settled)} 张` : fmtPct(progress.progress)}
              </span>
            </div>
            {progress.progress !== null && (
              <ProgressBar percent={progress.progress * 100} className="mt-2.5"
                tone={progress.failed > 0 ? 'warning' : progress.progress >= 1 ? 'success' : 'accent'} />
            )}
            <div className="mt-2.5 flex flex-wrap justify-between gap-x-3 gap-y-1 text-[11px] text-lx-muted">
              <span>
                {fmtCount(progress.settled)}{progress.progress === null ? ' 张已处理 · 总量未知' : ` / ${fmtCount(progress.total)} 张已处理`}
              </span>
              <span className="flex items-center gap-1.5 tabular-nums">
                <Gauge size={12} /> {progress.rate_per_s > 0 ? `${progress.rate_per_s.toFixed(2)} 张/s` : '—'}
              </span>
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-lx-muted">
              <span>完成 {fmtCount(progress.done)}</span>
              <span>跳过 {fmtCount(progress.skipped)}</span>
              {progress.failed > 0 && <span className="text-lx-danger">失败 {fmtCount(progress.failed)}</span>}
              {progress.eta_s != null && !finished && progress.progress !== 1 && (
                <span className="ml-auto tabular-nums">约剩 {fmtSeconds(progress.eta_s)}</span>
              )}
            </div>
          </section>
        )
      })}
    </div>
  )
}
