import { useId, useState, type KeyboardEvent, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { LayoutGroup, motion } from 'framer-motion'
import { controlTransition } from '../../lib/motion'

type FormSection = {
  id: string
  title: string
  description?: string
  content: ReactNode
}

export function FormSections({ label, sections, value, onChange }: {
  label: string
  sections: FormSection[]
  value?: string
  onChange?: (id: string) => void
}) {
  const prefix = useId()
  const [selected, setSelected] = useState(sections[0].id)
  const active = value ?? selected
  const select = (id: string) => {
    setSelected(id)
    onChange?.(id)
  }
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const next = event.key === 'Home' ? 0
      : event.key === 'End' ? sections.length - 1
        : event.key === 'ArrowRight' ? (index + 1) % sections.length
          : event.key === 'ArrowLeft' ? (index + sections.length - 1) % sections.length
            : -1
    if (next < 0) return
    event.preventDefault()
    select(sections[next].id)
    document.getElementById(`${prefix}-tab-${sections[next].id}`)?.focus()
  }

  return <div className="min-w-0 space-y-3.5">
    <LayoutGroup id={prefix}>
    <div role="tablist" aria-label={label} className="lx-tabs">
      {sections.map((section, index) => <button
        key={section.id}
        id={`${prefix}-tab-${section.id}`}
        type="button"
        role="tab"
        aria-selected={active === section.id}
        aria-controls={`${prefix}-panel-${section.id}`}
        tabIndex={active === section.id ? 0 : -1}
        onClick={() => select(section.id)}
        onKeyDown={(event) => onKeyDown(event, index)}
        className="lx-tab"
      >
        {active === section.id && <motion.span
          layoutId="form-tab-selection"
          className="lx-tab-selection"
          transition={controlTransition}
          aria-hidden="true"
        />}
        <span className="relative">{section.title}</span>
      </button>)}
    </div>
    </LayoutGroup>
    {/* 保留所有分组的组件实例，切换时不重置输入、文件选择和折叠状态。 */}
    {sections.map((section) => <div
      key={section.id}
      id={`${prefix}-panel-${section.id}`}
      role="tabpanel"
      aria-labelledby={`${prefix}-tab-${section.id}`}
      hidden={active !== section.id}
      tabIndex={0}
      className="lx-form-panel space-y-3.5"
    >
      {section.description && (
        <p className="px-1 text-xs leading-relaxed text-lx-muted">{section.description}</p>
      )}
      {section.content}
    </div>)}
  </div>
}

export function AdvancedSettings({ title, children }: { title: string; children: ReactNode }) {
  return <details className="lx-advanced group/advanced">
    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium text-lx-secondary [&::-webkit-details-marker]:hidden">
      {title}
      <ChevronDown size={14} aria-hidden="true" className="shrink-0 transition-transform group-open/advanced:rotate-180" />
    </summary>
    <div className="border-t border-lx px-3 pb-1">{children}</div>
  </details>
}
