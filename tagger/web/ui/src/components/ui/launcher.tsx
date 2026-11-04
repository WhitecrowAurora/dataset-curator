// 搬自 lulynx-trainer/backend/launcher/web/src/components/ui/launcher.tsx
// 裁剪：去掉 ConnectionTestPanel（依赖镜像检测 API 与 i18n），其余原样。
import { ChevronRight, CircleHelp } from 'lucide-react'
import type { ReactNode } from 'react'
import { motion } from 'framer-motion'
import { ProgressBar } from './ProgressBar'
import { cardHover, cardTap } from '../../lib/motion'
import { cn as cx } from '../../lib/cn'

export type StatusTone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger'

/* tinted 无边框 pill：border 工具类保留（换皮时靠边框重新着色），classic 下透明 */
const chipToneClass: Record<StatusTone, string> = {
  neutral: 'border-transparent bg-lx-surface-raised text-lx-secondary',
  accent: 'border-transparent bg-lx-accent-subtle text-lx-accent',
  success: 'border-transparent bg-lx-success-subtle text-lx-success',
  warning: 'border-transparent bg-lx-warning-subtle text-lx-warning',
  danger: 'border-transparent bg-lx-danger-subtle text-lx-danger',
}

const iconToneClass: Record<StatusTone, string> = {
  neutral: 'text-lx-muted',
  accent: 'text-lx-accent',
  success: 'text-lx-success',
  warning: 'text-lx-warning',
  danger: 'text-lx-danger',
}

/** tone → CSS 变量色，给 SVG stroke / 水印染色用。 */
const toneCssVar: Record<StatusTone, string> = {
  neutral: 'var(--lx-text-muted)',
  accent: 'var(--lx-accent)',
  success: 'var(--lx-success)',
  warning: 'var(--lx-warning)',
  danger: 'var(--lx-danger)',
}

/**
 * 静态同心圆环装饰：圆心偏右外，overflow 裁半弧。
 */
function DecorRings({ color = 'var(--lx-accent)' }: { color?: string }) {
  return (
    <svg
      className="pointer-events-none absolute top-1/2 -right-16 h-40 w-40 -translate-y-1/2"
      viewBox="0 0 160 160"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="80" cy="80" r="34" stroke={color} strokeWidth="1" opacity="0.14" />
      <circle cx="80" cy="80" r="54" stroke={color} strokeWidth="1" opacity="0.09" />
      <circle cx="80" cy="80" r="74" stroke={color} strokeWidth="1" opacity="0.05" />
    </svg>
  )
}

/**
 * 可点 KPI 卡的右侧导航示意：默认右下角大水印，hover 换成三环 + 箭头。
 */
function MetricNavAffordance({ icon, tone = 'accent' }: { icon: ReactNode; tone?: StatusTone }) {
  const color = toneCssVar[tone] ?? toneCssVar.accent
  return (
    <div className="pointer-events-none absolute inset-0 z-0 overflow-hidden" aria-hidden="true">
      <div
        className={cx(
          'absolute right-4 top-1/2 -translate-y-1/2',
          'opacity-[0.06] transition-opacity duration-200 ease-out',
          'group-hover:opacity-0 group-focus-within:opacity-0',
          iconToneClass[tone],
        )}
      >
        <div className="[&>svg]:h-[5rem] [&>svg]:w-[5rem] [&>svg]:stroke-[1.2]">{icon}</div>
      </div>

      <svg
        className={cx(
          'absolute top-1/2 -right-16 h-40 w-40 -translate-y-1/2',
          'opacity-0 transition-opacity duration-300 ease-out',
          'group-hover:opacity-100 group-focus-within:opacity-100',
        )}
        viewBox="0 0 160 160"
        fill="none"
        aria-hidden="true"
      >
        <circle cx="80" cy="80" r="34" stroke={color} strokeWidth="1.15" opacity="0.42" />
        <circle cx="80" cy="80" r="54" stroke={color} strokeWidth="1.05" opacity="0.28" />
        <circle cx="80" cy="80" r="74" stroke={color} strokeWidth="1" opacity="0.16" />
      </svg>

      <div
        className={cx(
          'absolute top-1/2 right-4 flex h-8 w-8 translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full text-white',
          'opacity-0 scale-90 transition-[opacity,transform] duration-300 ease-out',
          'group-hover:opacity-100 group-hover:scale-100',
          'group-focus-within:opacity-100 group-focus-within:scale-100',
        )}
      >
        <span className="absolute inset-0 rounded-full" style={{ backgroundColor: color }} />
        <ChevronRight size={16} strokeWidth={2.25} className="relative z-[1]" />
      </div>
    </div>
  )
}

export function PageHeader({
  eyebrow,
  title,
  description,
  badges,
  actions,
  icon,
}: {
  eyebrow?: string
  title: string
  description?: string
  badges?: ReactNode
  actions?: ReactNode
  icon?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-3 px-1">
      <div className="flex min-w-0 items-center gap-3.5">
        {icon && (
          <div
            className="grid h-10 w-10 shrink-0 place-items-center rounded-lx border border-lx bg-[color-mix(in_srgb,var(--lx-text)_5%,transparent)] text-lx-secondary"
            aria-hidden="true"
          >
            {icon}
          </div>
        )}
        <div className="min-w-0">
          {eyebrow && <div className="mb-0.5 text-[11px] text-lx-muted">{eyebrow}</div>}
          <h1 className="text-2xl font-semibold leading-tight tracking-tight text-lx">{title}</h1>
          {description && <p className="mt-1 max-w-[62ch] text-[13px] text-lx-secondary">{description}</p>}
        </div>
      </div>
      {(badges || actions) && (
        <div className="flex flex-wrap items-center gap-2">
          {badges}
          {actions}
        </div>
      )}
    </div>
  )
}

/** Sheet 标题行：标题 + 可选计数，右侧说明或操作。 */
export function SheetHead({
  id,
  title,
  count,
  hint,
  trailing,
}: {
  id?: string
  title: string
  count?: number | string
  hint?: ReactNode
  trailing?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-[18px] pb-2.5 pt-[18px] md:px-7">
      <div className="min-w-0">
        <h2 id={id} className="m-0 flex items-baseline gap-2 text-[15px] font-semibold text-lx">
          {title}
          {count !== undefined && (
            <span className="text-[13px] font-medium tabular-nums text-lx-muted">{count}</span>
          )}
        </h2>
        {hint && trailing && <p className="m-0 mt-0.5 text-xs text-lx-muted">{hint}</p>}
      </div>
      {trailing ? (
        <div className="shrink-0 self-center">{trailing}</div>
      ) : (
        hint && <p className="m-0 text-xs text-lx-muted">{hint}</p>
      )}
    </div>
  )
}

export function SectionPanel({
  title,
  description,
  trailing,
  children,
  className,
}: {
  title?: string
  description?: string
  trailing?: ReactNode
  children: ReactNode
  className?: string
}) {
  const hasHead = Boolean(title || description || trailing)
  return (
    <section className={cx('lx-sheet', className)}>
      {hasHead && <SheetHead title={title ?? ''} hint={description} trailing={trailing} />}
      <div className={cx('px-[18px] pb-5 md:px-7', hasHead ? 'border-t border-lx pt-4' : 'pt-5')}>
        {children}
      </div>
    </section>
  )
}

export function SettingRow({
  label,
  description,
  help,
  icon,
  children,
}: {
  label: string
  /** 常驻说明。**只在真的会误操作时才用**，其余走 help。 */
  description?: string
  /** 解释性文字：收进标签旁的 ? 图标，悬浮才显示，不占版面 */
  help?: string
  icon?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="grid gap-3 border-b border-lx py-3.5 last:border-b-0 md:grid-cols-[minmax(0,1fr)_minmax(260px,360px)] md:items-center">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-sm font-medium text-lx">
          {icon && <span className="text-lx-muted">{icon}</span>}
          <span>{label}</span>
          {help && (
            /* 用图标而不是"圈里一个 ?"的字符：字符的垂直位置由字体度量决定，
               15px 的圈里会跑到底部；SVG 是替换元素，居中由几何决定。 */
            <span className="inline-flex shrink-0 cursor-help items-center" title={help}>
              <CircleHelp
                size={14}
                strokeWidth={1.6}
                role="img"
                aria-label={help}
                className="text-lx-dim transition-colors hover:text-lx-muted"
              />
            </span>
          )}
        </div>
        {description && (
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-lx-muted">{description}</p>
        )}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

export function StatusChip({
  tone = 'neutral',
  icon,
  children,
  title,
  onClick,
}: {
  tone?: StatusTone
  icon?: ReactNode
  children: ReactNode
  title?: string
  onClick?: () => void
}) {
  const className = cx(
    'inline-flex max-w-full items-center gap-1.5 rounded-lx-sm border px-2.5 py-1 text-[11px] font-semibold',
    chipToneClass[tone],
    onClick &&
      'cursor-pointer transition hover:border-lx-accent hover:bg-lx-surface hover:text-lx-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lx-accent/35',
  )

  if (onClick) {
    return (
      <button type="button" title={title} onClick={onClick} className={className}>
        {icon}
        <span className="truncate">{children}</span>
      </button>
    )
  }

  return (
    <span title={title} className={className}>
      {icon}
      <span className="truncate">{children}</span>
    </span>
  )
}

export function MetricCard({
  icon,
  label,
  value,
  detail,
  tone = 'neutral',
  progress,
}: {
  icon: ReactNode
  label: string
  value: string
  detail?: string
  tone?: StatusTone
  /** 0-100 容量条；null/undefined 不渲染 */
  progress?: number | null
}) {
  return (
    <div className="lx-sheet min-w-0 px-[18px] py-4 md:px-6">
      <div className="flex items-center gap-2 text-[11.5px] text-lx-muted">
        <span className={iconToneClass[tone]}>{icon}</span>
        <span className="truncate">{label}</span>
      </div>
      <div
        className="mt-2 truncate font-display text-2xl font-semibold tabular-nums leading-none text-lx"
        title={value}
      >
        {value}
      </div>
      {detail && (
        <div className="mt-1.5 truncate text-xs text-lx-secondary" title={detail}>
          {detail}
        </div>
      )}
      {typeof progress === 'number' && Number.isFinite(progress) && (
        <ProgressBar percent={progress} tone={tone} autoTone className="mt-3 w-full" />
      )}
    </div>
  )
}

export function MiniMetricCard({
  icon,
  label,
  value,
  detail,
  tone = 'neutral',
  layout = 'stack',
  onClick,
}: {
  icon: ReactNode
  label: string
  value: string
  detail?: string
  tone?: StatusTone
  /** stack: 竖排（窄网格用，默认）；row: 横排（宽单列用，标签左·读数右） */
  layout?: 'stack' | 'row'
  /** 有 onClick 时 row 卡变为可点：右下角水印 + 悬停三环箭头 */
  onClick?: () => void
}) {
  if (layout === 'row') {
    const interactive = typeof onClick === 'function'
    const body = (
      <>
        {interactive ? <MetricNavAffordance icon={icon} tone={tone} /> : <DecorRings />}
        <div className="relative z-10 flex min-w-0 items-center gap-2 pr-[30%]">
          <span className={cx('shrink-0', iconToneClass[tone])}>{icon}</span>
          <span className="min-w-0 truncate text-[12.5px] font-medium text-lx-secondary">{label}</span>
        </div>
        <div className="relative z-10 mt-2.5 flex min-w-0 items-baseline gap-2 pr-[30%]">
          <span
            className="shrink-0 font-display text-[28px] font-semibold tabular-nums tracking-tight leading-none text-lx"
            title={value}
          >
            {value}
          </span>
          {detail && (
            <span className="min-w-0 truncate text-xs text-lx-muted" title={detail}>
              {detail}
            </span>
          )}
        </div>
      </>
    )

    const shellClass = cx(
      'lx-sheet group relative min-w-0 overflow-hidden px-[18px] py-4 text-left transition-colors duration-200 hover:border-lx-hover md:px-6',
      interactive && 'w-full cursor-pointer',
    )

    if (interactive) {
      return (
        <motion.button
          type="button"
          onClick={onClick}
          className={shellClass}
          whileHover={cardHover}
          whileTap={cardTap}
          transition={{ type: 'spring', stiffness: 400, damping: 25 }}
        >
          {body}
        </motion.button>
      )
    }

    return (
      <motion.div
        className={shellClass}
        whileHover={cardHover}
        whileTap={cardTap}
        transition={{ type: 'spring', stiffness: 400, damping: 25 }}
      >
        {body}
      </motion.div>
    )
  }

  return (
    <div className="min-w-0 rounded-lx-sm border border-lx bg-lx-surface px-3 py-2">
      <div className="flex items-center gap-2">
        <span className={cx('shrink-0', iconToneClass[tone])}>{icon}</span>
        <span className="min-w-0 truncate text-[11px] text-lx-muted">{label}</span>
      </div>
      <div className="mt-1 truncate font-display text-base font-semibold tabular-nums text-lx" title={value}>
        {value}
      </div>
      {detail && (
        <div className="mt-0.5 truncate text-[11px] text-lx-muted" title={detail}>
          {detail}
        </div>
      )}
    </div>
  )
}

export function ActionTile({
  icon,
  label,
  detail,
  onClick,
  disabled = false,
}: {
  icon: ReactNode
  label: string
  detail: string
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <motion.button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="group rounded-lx-sm border border-lx bg-lx-surface px-4 py-3.5 text-left transition-colors hover:border-lx-accent disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-lx"
      whileTap={cardTap}
    >
      <div className="flex items-center gap-2 text-[13px] font-semibold text-lx">
        <span className="shrink-0 text-lx-muted transition-colors group-hover:text-lx-accent">{icon}</span>
        <span className="min-w-0 truncate">{label}</span>
      </div>
      <div className="mt-1.5 text-xs leading-5 text-lx-muted">{detail}</div>
    </motion.button>
  )
}
