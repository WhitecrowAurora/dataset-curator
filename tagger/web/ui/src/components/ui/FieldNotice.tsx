import type { ReactNode } from 'react'
import { CircleAlert } from 'lucide-react'

export function FieldNotice({ message, id, children }: {
  message?: string; id?: string; children?: ReactNode;
}) {
  if (!message) return null
  return <div id={id} role="status" className="mt-2 flex items-start gap-2 rounded-lx-sm border border-lx-warning/30 bg-lx-warning-subtle px-3 py-2 text-xs leading-relaxed text-lx-warning">
    <CircleAlert size={14} className="mt-0.5 shrink-0" />
    <div className="min-w-0 flex-1 break-words"><p>{message}</p>{children && <div className="mt-2 flex flex-wrap gap-2">{children}</div>}</div>
  </div>
}
