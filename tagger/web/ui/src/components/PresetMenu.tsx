import { useEffect, useRef, useState } from 'react'
import { BookMarked, Check, ChevronDown, Save, Trash2 } from 'lucide-react'
import { cn as cx } from '../lib/cn'
import { usePresets } from '../lib/presets'

/**
 * 顶栏的「配置方案」：把当前这套参数存成具名快照，之后一键切回来。
 *
 * 三件事放在一起：切方案、存方案、自动保存。都跟"这一页填了什么"有关，
 * 分散到侧栏或页面里反而找不到。
 */
export function PresetMenu() {
  const state = usePresets((s) => s.state)
  const busy = usePresets((s) => s.busy)
  const note = usePresets((s) => s.note)
  const error = usePresets((s) => s.error)
  const refresh = usePresets((s) => s.refresh)
  const saveAs = usePresets((s) => s.saveAs)
  const switchTo = usePresets((s) => s.switchTo)
  const remove = usePresets((s) => s.remove)
  const setAuto = usePresets((s) => s.setAuto)
  const clearNote = usePresets((s) => s.clearNote)

  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const boxRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 点外面 / Esc 收起。别用 onBlur：点面板里的按钮会先失焦，菜单就关了。
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // 提示只留 2.5s，别让它常驻顶栏
  useEffect(() => {
    if (!note) return
    const t = window.setTimeout(clearNote, 2500)
    return () => window.clearTimeout(t)
  }, [note, clearNote])

  const active = state?.active ?? null
  const items = state?.items ?? []

  const submit = async () => {
    if (!draft.trim()) return
    const ok = await saveAs(draft)
    if (ok) setDraft('')
  }

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        // ★ 必须显式写 inline-flex items-center：`.lx-btn` 只给了 padding 和
        //   圆角，不带 display。少了它，图标 / 文字 / 箭头会按 inline 排——
        //   按钮是固定 h-8，第二行就直接溢出到边框外面（顶栏那个错位）。
        className={cx(
          'lx-btn-ghost inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap px-2 text-[11.5px]',
          open && 'border-lx-accent',
        )}
        title="配置方案：保存 / 切换当前这套参数"
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <BookMarked size={13} className="shrink-0 text-lx-muted" />
        <span className="hidden max-w-[150px] truncate sm:inline">
          {note ?? active ?? '配置方案'}
        </span>
        <ChevronDown size={12} className="shrink-0 text-lx-dim" />
      </button>

      {/* 面板：top-full + mt-2 贴着按钮下沿落，又不钻进 48px 高的顶栏被边框切头 */}
      {open && (
        <div className="lx-card absolute right-0 top-full z-40 mt-2 w-[320px] p-2 shadow-xl">
          <label className="flex cursor-pointer items-center gap-2 rounded-lx px-1.5 py-1.5 hover:bg-[color-mix(in_srgb,var(--lx-text)_5%,transparent)]">
            <input
              type="checkbox"
              className="lx-checkbox"
              checked={Boolean(state?.auto)}
              onChange={(e) => void setAuto(e.target.checked)}
              aria-label="自动保存"
            />
            <span className="text-[12px] text-lx">自动保存</span>
            <span className="ml-auto max-w-[170px] truncate font-mono text-[10.5px] text-lx-dim">
              {state?.auto ? (state.auto_name ?? '') : '开启后按时间戳记一份'}
            </span>
          </label>

          <div className="my-1 border-t border-lx" />

          <div className="max-h-[248px] overflow-y-auto">
            {items.length === 0 && (
              <p className="px-1.5 py-2 text-[11.5px] text-lx-dim">还没有方案，先存一份</p>
            )}
            {items.map((it) => (
              <div
                key={it.name}
                className={cx(
                  'group flex items-center gap-1 rounded-lx px-1.5 py-1',
                  it.name === active
                    ? 'bg-[color-mix(in_srgb,var(--lx-accent)_14%,transparent)]'
                    : 'hover:bg-[color-mix(in_srgb,var(--lx-text)_5%,transparent)]',
                )}
              >
                <button
                  type="button"
                  onClick={() => void switchTo(it.name)}
                  disabled={busy}
                  className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                  title={`${it.name}　${it.ts}`}
                >
                  {it.name === active ? (
                    <Check size={12} className="shrink-0 text-lx-accent" />
                  ) : (
                    <span className="h-3 w-3 shrink-0" />
                  )}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12px] text-lx">{it.name}</span>
                    <span className="block truncate font-mono text-[10px] text-lx-dim">
                      {it.ts}
                      {it.auto ? ' · 自动' : ''}
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => void remove(it.name)}
                  disabled={busy}
                  className="shrink-0 rounded p-1 text-lx-dim opacity-0 transition hover:text-lx-danger group-hover:opacity-100"
                  title="删除这份方案"
                  aria-label={`删除方案 ${it.name}`}
                >
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>

          <div className="my-1 border-t border-lx" />

          <div className="flex items-center gap-1.5 px-1.5 pb-0.5 pt-1">
            <input
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void submit()
              }}
              placeholder="方案名，同名会覆盖"
              className="lx-input h-7 min-w-0 flex-1 text-[12px]"
              aria-label="方案名"
            />
            <button
              type="button"
              onClick={() => void submit()}
              disabled={busy || !draft.trim()}
              className="lx-btn-ghost inline-flex h-7 shrink-0 items-center gap-1 whitespace-nowrap px-2 text-[11.5px]"
            >
              <Save size={12} />
              保存
            </button>
          </div>

          {error && <p className="px-1.5 pb-1 pt-1 text-[11px] text-lx-danger">{error}</p>}
        </div>
      )}
    </div>
  )
}
