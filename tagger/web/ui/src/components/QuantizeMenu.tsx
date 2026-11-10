import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Cpu } from 'lucide-react'
import { cn as cx } from '../lib/cn'

/**
 * 「量化」这一个按钮下面挂两条完全不同的路：
 *
 * - llama.cpp → .gguf（Windows 上直接能跑，默认引擎就是它）
 * - vLLM      → W4A16（AWQ/GPTQ 的 safetensors，只在 Linux / WSL2 上跑）
 *
 * 以前这是两个并排的按钮，用户看不出它们其实是"同一个问题的两种答案"；
 * 而且 GGUF 那个只有在下完权重之前的工具栏里出现过，下完就消失。
 * 档位 key 必须和后端对得上：gguf_convert.TARGETS / quantize.QUANT_METHODS。
 * 这里只做快捷入口，真正的档位清单仍从后端状态里取，选完在弹窗里确认。
 */
export type QuantPick =
  | { route: 'gguf'; target: string }
  | { route: 'w4a16'; method: string }

const GROUPS: {
  route: QuantPick['route']
  title: string
  hint: string
  items: { key: string; label: string; note: string }[]
}[] = [
  {
    route: 'gguf',
    title: 'llama.cpp',
    hint: 'Windows 上直接能跑',
    items: [
      { key: 'f16', label: 'F16', note: '不压缩，体积最大' },
      { key: 'q4_k_m', label: 'Q4_K_M', note: '最小最快' },
      { key: 'q6_k', label: 'Q6_K', note: '质量接近 F16' },
    ],
  },
  {
    route: 'w4a16',
    title: 'vLLM',
    hint: '需要 Linux / WSL2',
    items: [
      { key: 'gptq', label: 'GPTQ', note: '校准稳定，推荐' },
      { key: 'awq', label: 'AWQ', note: '激活感知' },
    ],
  },
]

export function QuantizeMenu({
  disabled,
  hint,
  onPick,
}: {
  disabled?: boolean
  /** 按钮被禁用时说明原因，走 title，不占版面 */
  hint?: string
  onPick: (pick: QuantPick) => void
}) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement | null>(null)

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

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        className={cx(
          'lx-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[11px]',
          open && 'border-lx-accent',
        )}
        disabled={disabled}
        title={disabled ? hint : '本地量化：llama.cpp 出 GGUF，vLLM 出 W4A16'}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((v) => !v)}
      >
        <Cpu size={12} />
        量化
        <ChevronDown size={11} className="text-lx-dim" />
      </button>

      {open && (
        <div className="lx-card absolute right-0 top-full z-40 mt-1 w-[236px] p-1.5 shadow-xl" role="menu">
          {GROUPS.map((g, gi) => (
            <div key={g.route} className={cx(gi > 0 && 'mt-1 border-t border-lx pt-1')}>
              <div className="flex items-baseline justify-between gap-2 px-1.5 py-1">
                <span className="text-[11.5px] font-medium text-lx">{g.title}</span>
                <span className="text-[10px] text-lx-dim">{g.hint}</span>
              </div>
              {g.items.map((it) => (
                <button
                  key={it.key}
                  type="button"
                  role="menuitem"
                  className="flex w-full items-baseline justify-between gap-2 rounded-lx px-1.5 py-1 text-left hover:bg-[color-mix(in_srgb,var(--lx-text)_5%,transparent)]"
                  onClick={() => {
                    setOpen(false)
                    onPick(
                      g.route === 'gguf'
                        ? { route: 'gguf', target: it.key }
                        : { route: 'w4a16', method: it.key },
                    )
                  }}
                >
                  <span className="font-mono text-[11.5px] text-lx">{it.label}</span>
                  <span className="text-[10px] text-lx-dim">{it.note}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
