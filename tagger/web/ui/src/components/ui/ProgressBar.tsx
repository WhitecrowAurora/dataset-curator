import type { CSSProperties } from 'react'

export type ProgressTone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger'

const FILL: Record<ProgressTone, string> = {
  neutral: 'var(--lx-bar-fill)',
  accent: 'var(--lx-accent)',
  success: 'var(--lx-success)',
  warning: 'var(--lx-warning)',
  danger: 'var(--lx-danger)',
}

/**
 * 共享进度条（搬自 lulynx launcher）。
 * 强调色预算：默认中性轨道 + 中性填充，accent/状态色只在显式传 tone 时出现。
 */
export function ProgressBar({
  percent,
  tone = 'neutral',
  autoTone = false,
  className = '',
  heightClass = 'h-1',
  style,
}: {
  percent: number
  tone?: ProgressTone
  /** 按数值自动升档：≥92 danger，≥80 warning，否则维持 tone */
  autoTone?: boolean
  className?: string
  heightClass?: string
  style?: CSSProperties
}) {
  const clamped = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0))
  const resolved: ProgressTone = autoTone
    ? clamped >= 92
      ? 'danger'
      : clamped >= 80
        ? 'warning'
        : tone
    : tone
  return (
    <div
      className={`overflow-hidden rounded-full ${heightClass} ${className}`}
      style={{ backgroundColor: 'var(--lx-bar-track)', ...style }}
      role="progressbar"
      aria-valuenow={Math.round(clamped)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full transition-[width] duration-500"
        style={{ width: `${clamped}%`, backgroundColor: FILL[resolved] }}
      />
    </div>
  )
}
