import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'

const FIGURE_TONE = {
  danger: 'text-lx-danger',
  warning: 'text-lx-warning',
  accent: 'text-lx-accent',
  success: 'text-lx-success',
} as const

export type OverviewFigureTone = keyof typeof FIGURE_TONE

/** 页头下方概览数字条中的单项：小标签 + 大数字 + 可选附注。 */
export function OverviewFigure({
  label,
  value,
  note,
  tone,
  first,
}: {
  label: string
  value: ReactNode
  note?: string
  tone?: OverviewFigureTone
  first?: boolean
}) {
  return (
    <div
      className={cn(
        'flex min-w-0 flex-col gap-1 border-l border-lx py-0.5 pl-6',
        first && 'border-l-0 pl-0 xl:border-l xl:pl-6',
      )}
    >
      <div className="whitespace-nowrap text-[11.5px] text-lx-muted">{label}</div>
      <div
        className={cn(
          'whitespace-nowrap font-display text-2xl font-semibold leading-tight tabular-nums text-lx',
          tone && FIGURE_TONE[tone],
        )}
      >
        {value}
        {note && <small className="ml-1.5 font-sans text-xs font-normal text-lx-secondary">{note}</small>}
      </div>
    </div>
  )
}

/** 纯数字条：首项在所有断点都去掉左分隔线。 */
export function OverviewFigureRow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'grid grid-cols-2 gap-y-4 border-t border-lx pt-5 sm:grid-cols-4 [&>*:first-child]:border-l-0 [&>*:first-child]:pl-0 max-sm:[&>*:nth-child(odd)]:border-l-0 max-sm:[&>*:nth-child(odd)]:pl-0',
        className,
      )}
    >
      {children}
    </div>
  )
}
