import { useEffect, useState } from 'react'

export function ConcurrencyControl({ value, onChange, disabled, automatic = 2 }:
  { value: number; onChange: (value: number) => void; disabled?: boolean; automatic?: number }) {
  const shown = value || automatic
  const [draft, setDraft] = useState(String(shown))
  useEffect(() => setDraft(String(shown)), [shown])
  const commit = () => {
    const parsed = draft.trim() === '' ? shown : Number(draft)
    const next = Math.max(1, Math.min(256, Math.round(Number.isFinite(parsed) ? parsed : shown)))
    setDraft(String(next))
    if (next !== shown) onChange(next)
  }
  return <div className="space-y-2">
    <div className="flex items-center gap-3">
      <input type="range" aria-label="Torii 并发滑动条" min={1} max={256} step={1}
        className="min-w-0 flex-1 accent-orange-500" value={shown} disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))} />
      <input type="number" aria-label="Torii 并发数量" min={1} max={256} step={1}
        className="lx-input w-20 shrink-0 font-mono text-xs tabular-nums" value={draft} disabled={disabled}
        onChange={(e) => {
          const text = e.target.value
          setDraft(text)
          const next = Number(text)
          if (text && Number.isInteger(next) && next >= 1 && next <= 256) onChange(next)
        }} onBlur={commit} onKeyDown={(e) => { if (e.key === 'Enter') commit() }} />
    </div>
    <label className="flex items-center gap-2 text-xs text-lx-muted">
      <input type="checkbox" checked={value === 0} disabled={disabled}
        onChange={(e) => onChange(e.target.checked ? 0 : shown)} />
      自动（{automatic} 并发）
    </label>
  </div>
}
