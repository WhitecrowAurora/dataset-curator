// 搬自 lulynx-trainer/backend/launcher/web/src/components/ui/controls.tsx
// 裁剪：去掉 i18n 依赖（文案直接写中文），逻辑与键盘行为保持不变。
import { useRef } from 'react'
import { AlertTriangle, CheckCircle2, XCircle, type LucideIcon } from 'lucide-react'
import { SheetHead, StatusChip } from './launcher'

/**
 * 选项格：等宽按钮组单选（设备 / 精度 / 批大小等），选中态 accent 底 + 边。
 * 键盘：方向键在选项间移动并直接切换选中，Home/End 跳首尾。
 */
export function ChoiceGrid<T extends string>({
  options,
  value,
  onChange,
  className = 'grid-cols-3',
  ariaLabel,
  mono,
  size = 'md',
  renderLabel,
  glyph,
  disabled,
  optionDisabled,
  onBlocked,
}: {
  options: readonly T[]
  value: T
  onChange: (v: T) => void
  /** 网格列类（可带响应式断点），如 "grid-cols-2 sm:grid-cols-4" */
  className?: string
  ariaLabel?: string
  mono?: boolean
  size?: 'sm' | 'md'
  renderLabel?: (v: T) => React.ReactNode
  /** 选项左侧的小示意图标；不传时外观不变 */
  glyph?: (v: T, active: boolean) => React.ReactNode
  /** 整组置灰 */
  disabled?: boolean
  /**
   * 单个选项不可选（例如这一档 GGUF 还没压出来）。
   *
   * 与 `disabled` 的区别是**还能点**：点了走 `onBlocked` 而不是 `onChange`，
   * 好让页面说明"为什么不能选、去哪儿能把它变成能选"。真把它设成 HTML
   * disabled，用户只会看到一个按不动的灰块，反而更困惑。
   */
  optionDisabled?: (v: T) => boolean
  onBlocked?: (v: T) => void
}) {
  const groupRef = useRef<HTMLDivElement>(null)
  const sizeClass = size === 'sm' ? 'py-2 px-2 text-xs' : 'py-2.5 text-sm'

  const focusOption = (index: number) =>
    groupRef.current?.querySelectorAll<HTMLButtonElement>("[role='radio']")[index]?.focus()

  const moveTo = (index: number) => {
    const opt = options[index]
    if (optionDisabled?.(opt)) {
      // 走到不可选的档只提示、不切换：静默切过去等于让用户以为选上了
      onBlocked?.(opt)
      focusOption(index)
      return
    }
    onChange(opt)
    focusOption(index)
  }

  return (
    <div
      ref={groupRef}
      role="radiogroup"
      aria-label={ariaLabel}
      aria-disabled={disabled || undefined}
      className={`grid gap-2 ${className} ${disabled ? 'opacity-50' : ''}`}
      onKeyDown={(e) => {
        if (disabled) return
        const current = options.indexOf(value)
        if (current < 0) return
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
          e.preventDefault()
          moveTo((current + 1) % options.length)
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
          e.preventDefault()
          moveTo((current - 1 + options.length) % options.length)
        } else if (e.key === 'Home') {
          e.preventDefault()
          moveTo(0)
        } else if (e.key === 'End') {
          e.preventDefault()
          moveTo(options.length - 1)
        }
      }}
    >
      {options.map((opt) => {
        const active = opt === value
        const label = renderLabel ? renderLabel(opt) : opt
        const blocked = !disabled && Boolean(optionDisabled?.(opt))
        return (
          <button
            key={opt}
            type="button"
            role="radio"
            aria-checked={active}
            aria-disabled={blocked || undefined}
            title={blocked ? '这一档还没有权重：先去「模型管理」把它压出来' : undefined}
            tabIndex={active ? 0 : -1}
            disabled={disabled}
            onClick={() => (blocked ? onBlocked?.(opt) : onChange(opt))}
            className={`${sizeClass} rounded-lx-sm font-medium transition-colors border ${mono ? 'font-mono' : ''} ${
              active
                ? 'bg-lx-accent-subtle border-lx-accent text-lx'
                : 'bg-lx-surface-raised border-lx text-lx-muted hover:text-lx'
            } ${blocked ? 'opacity-45 line-through decoration-lx-dim decoration-1' : ''} ${
              disabled || blocked ? 'cursor-not-allowed' : 'cursor-pointer'
            }`}
          >
            {glyph ? (
              <span className="inline-flex max-w-full items-center justify-center gap-2 px-1">
                <span aria-hidden className="inline-flex shrink-0">
                  {glyph(opt, active)}
                </span>
                <span className="truncate">{label}</span>
              </span>
            ) : (
              label
            )}
          </button>
        )
      })}
    </div>
  )
}

/**
 * 多选版的 ChoiceGrid（同样的外观，语义换成"勾选清单"）。
 *
 * 为什么不复用 ChoiceGrid：那套是 role=radio + 方向键在档位间移动，语义是
 * "选一个"。多选下每个按钮是独立的开关（aria-pressed），方向键不该把焦点
 * 移走——否则用户按一下方向键就不知道自己在勾第几个了。
 *
 * `exclusive` 里的值互斥：勾它清空其余（如 auto / cpu 与具体 cuda:N）。
 * 一个都不剩时不允许取消最后一个——全空的清单没有"用哪个设备"的答案，
 * 后端只能退回 auto，而页面上一个都没亮，看起来像坏了。
 */
export function ChoiceMulti<T extends string>({
  options,
  value,
  onChange,
  exclusive = [],
  className = 'grid-cols-4',
  ariaLabel,
  mono,
  size = 'md',
  renderLabel,
  disabled,
}: {
  options: readonly T[]
  value: readonly T[]
  onChange: (v: T[]) => void
  exclusive?: readonly T[]
  className?: string
  ariaLabel?: string
  mono?: boolean
  size?: 'sm' | 'md'
  renderLabel?: (v: T) => React.ReactNode
  disabled?: boolean
}) {
  const sizeClass = size === 'sm' ? 'py-2 px-2 text-xs' : 'py-2.5 text-sm'

  const toggle = (opt: T) => {
    if (disabled) return
    if (value.includes(opt)) {
      if (value.length > 1) onChange(value.filter((v) => v !== opt))
      return
    }
    if (exclusive.includes(opt)) {
      onChange([opt])
      return
    }
    const next = value.filter((v) => !exclusive.includes(v))
    next.push(opt)
    onChange(next)
  }

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      aria-disabled={disabled || undefined}
      className={`grid gap-2 ${className} ${disabled ? 'opacity-50' : ''}`}
    >
      {options.map((opt) => {
        const active = value.includes(opt)
        const label = renderLabel ? renderLabel(opt) : opt
        return (
          <button
            key={opt}
            type="button"
            aria-pressed={active}
            disabled={disabled}
            onClick={() => toggle(opt)}
            className={`${sizeClass} rounded-lx-sm font-medium transition-colors border ${
              mono ? 'font-mono' : ''
            } ${
              active
                ? 'bg-lx-accent-subtle border-lx-accent text-lx'
                : 'bg-lx-surface-raised border-lx text-lx-muted hover:text-lx'
            } ${disabled ? 'cursor-not-allowed' : 'cursor-pointer'}`}
          >
            {label}
          </button>
        )
      })}
    </div>
  )
}

/** 开关行：原生 checkbox + 统一主题 accent，可带一行说明。 */
export function ToggleRow({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label: string
  hint?: string
  disabled?: boolean
}) {
  return (
    <label
      className={`flex items-start gap-3 select-none ${
        disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'
      }`}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 shrink-0 rounded accent-[var(--lx-accent)]"
      />
      <span className="min-w-0">
        <span className="block text-sm text-lx-secondary">{label}</span>
        {hint && <span className="mt-0.5 block text-xs text-lx-dim">{hint}</span>}
      </span>
    </label>
  )
}

type ErrorBoxTone = 'danger' | 'warning'

const ERROR_BOX_PALETTE: Record<
  ErrorBoxTone,
  { border: string; bg: string; text: string; textSoft: string; icon: LucideIcon }
> = {
  danger: {
    border: 'border-lx-danger',
    bg: 'bg-lx-danger-subtle',
    text: 'text-lx-danger',
    textSoft: 'text-lx-danger/90',
    icon: XCircle,
  },
  warning: {
    border: 'border-lx-warning',
    bg: 'bg-lx-warning-subtle',
    text: 'text-lx-warning',
    textSoft: 'text-lx-warning/90',
    icon: AlertTriangle,
  },
}

/** 错误/警告条：单行提示 + 等宽换行详情。tone="warning" 用于非致命提示。 */
export function ErrorBox({
  error,
  tone = 'danger',
  title,
}: {
  error: string
  tone?: ErrorBoxTone
  title?: string
}) {
  if (!error) return null
  const palette = ERROR_BOX_PALETTE[tone]
  const Icon = palette.icon
  const heading = title ?? (tone === 'danger' ? '出错了' : '注意')
  return (
    <div
      role="alert"
      className={`flex items-start gap-3 rounded-lx-sm border-l-2 ${palette.border} ${palette.bg} px-4 py-3`}
    >
      <Icon className={`w-4 h-4 ${palette.text} shrink-0 mt-0.5`} />
      <div className="min-w-0">
        <div className={`text-sm font-semibold ${palette.text}`}>{heading}</div>
        <pre className={`mt-1 text-xs ${palette.textSoft} whitespace-pre-wrap break-all font-mono`}>
          {error}
        </pre>
      </div>
    </div>
  )
}

/**
 * 结果面板：lx-sheet 外壳 + SheetHead（成功 chip、可选操作），
 * 取代各页手写的绿盒/红盒。
 */
export function ToolResultSheet({
  title,
  tone = 'success',
  chip,
  actions,
  children,
}: {
  title?: string
  tone?: 'success' | 'danger'
  chip?: React.ReactNode
  actions?: React.ReactNode
  children: React.ReactNode
}) {
  const failed = tone === 'danger'
  const toneLabel = failed ? '失败' : '完成'
  const heading = title ?? toneLabel
  return (
    <section className={`lx-sheet ${failed ? 'border-l-2 border-l-lx-danger' : ''}`} aria-label={heading}>
      <SheetHead
        title={heading}
        trailing={
          <div className="flex items-center gap-2">
            {chip ?? (
              <StatusChip
                tone={tone}
                icon={failed ? <XCircle size={12} /> : <CheckCircle2 size={12} />}
              >
                {toneLabel}
              </StatusChip>
            )}
            {actions}
          </div>
        }
      />
      <div className="space-y-5 border-t border-lx px-[18px] pb-5 pt-4 md:px-7">{children}</div>
    </section>
  )
}
