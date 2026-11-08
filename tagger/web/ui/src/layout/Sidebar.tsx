import type { ReactNode } from 'react'
import { motion } from 'framer-motion'
import { Wand2 } from 'lucide-react'
import { springSnappy } from '../lib/motion'
import { cn } from '../lib/cn'

export type NavItem = {
  key: string
  label: string
  icon: ReactNode
  active: boolean
  disabled?: boolean
  hint?: string
  onClick: () => void
}

/**
 * 侧边导航。选中态用 framer-motion 的 layoutId 做"滑块"，切换页面时
 * 底色与左侧竖条会滑过去——这是 lulynx 侧栏的手感，纯 CSS 做不出来。
 */
export function Sidebar({
  items,
  footer,
}: {
  items: NavItem[]
  footer?: ReactNode
}) {
  return (
    <aside className="flex w-[196px] shrink-0 flex-col border-r border-lx bg-lx-sidebar-bg">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-lx px-3">
        <span className="grid h-6 w-6 place-items-center rounded-lx-sm bg-lx-accent-subtle text-lx-accent">
          <Wand2 size={14} />
        </span>
        <div className="min-w-0">
          <div className="truncate text-[12.5px] font-semibold leading-tight text-lx">Tagger</div>
          {/* 副标题只描述这个应用是什么，不写具体后端：本地 tag 后端随配置变，
              VLM 模式下根本不用它——写死 wd-eva02 会让人以为当前就是那个后端。 */}
          <div className="truncate text-[9.5px] leading-tight text-lx-muted">NL + tag 打标工作台</div>
        </div>
      </div>

      <nav className="flex flex-col gap-px px-2 py-2" aria-label="主导航">
        {items.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={item.onClick}
            disabled={item.disabled}
            aria-current={item.active ? 'page' : undefined}
            title={item.hint}
            className={cn(
              'group relative flex items-center gap-2 rounded-lx-sm px-2 py-[6px] text-left text-[12.5px] transition-colors',
              item.active ? 'text-lx' : 'text-lx-secondary',
              item.disabled
                ? 'cursor-not-allowed opacity-40'
                : 'cursor-pointer hover:text-lx hover:bg-[color-mix(in_srgb,var(--lx-text)_4%,transparent)]',
            )}
          >
            {item.active && (
              <motion.span
                layoutId="nav-active-bg"
                className="absolute inset-0 rounded-lx-sm bg-lx-accent-subtle"
                transition={springSnappy}
              />
            )}
            {item.active && (
              // 强调条要贴满整行高度：原来写的是 h-4 + 垂直居中，行高约 30px，
              // 上下各空出 7px，看起来就是"只画了一半"。留 4px 是因为按钮本身
              // 有圆角，顶满会让竖条的直角戳出圆角外。
              <motion.span
                layoutId="nav-active-bar"
                className="absolute inset-y-1 left-0 w-[2px] rounded-full bg-lx-accent"
                transition={springSnappy}
              />
            )}
            <span
              className={cn(
                'relative z-10 shrink-0',
                item.active ? 'text-lx-accent' : 'text-lx-muted group-hover:text-lx-secondary',
              )}
            >
              {item.icon}
            </span>
            <span className="relative z-10 min-w-0 flex-1 truncate">{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="mt-auto border-t border-lx px-2.5 py-2.5">{footer}</div>
    </aside>
  )
}
