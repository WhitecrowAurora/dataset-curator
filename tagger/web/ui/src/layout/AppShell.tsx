import type { ReactNode } from 'react'
import { Cpu, Moon, Sun, Server } from 'lucide-react'
import type { Health } from '../lib/api'
import type { Theme } from '../lib/theme'
import { StatusChip } from '../components/ui/launcher'
import { Sidebar, type NavItem } from './Sidebar'

/**
 * 应用壳：左侧栏 + 顶栏 + 滚动主区。
 * 版式取自 lulynx 的 AppShell，但去掉了桌面端的 TitleBar（浏览器自带标题栏），
 * 主区宽度交给页面自己控制（页面根用 mx-auto max-w-6xl）。
 */
export function AppShell({
  items,
  health,
  theme,
  onToggleTheme,
  children,
  footer,
  statusBar,
}: {
  items: NavItem[]
  health: Health | null
  theme: Theme
  onToggleTheme: () => void
  children: ReactNode
  /** 侧栏底部的小字（版本/监听地址之类） */
  footer?: ReactNode
  /** 主区底部的全局状态条：所有页面都能看到"现在在跑什么" */
  statusBar?: ReactNode
}) {
  const gpu = health?.gpus?.[0]
  return (
    <div className="flex h-screen w-full overflow-hidden bg-lx-bg font-sans text-lx">
      <Sidebar items={items} footer={footer} />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-lx bg-lx-surface/70 px-5 backdrop-blur md:px-6">
          <div className="flex min-w-0 items-center gap-2 text-[11.5px] text-lx-muted">
            <Server size={13} className="shrink-0" />
            <span className="truncate font-mono">
              {health ? health.platform : '正在连接本机服务…'}
            </span>
            {health && <span className="hidden shrink-0 text-lx-dim sm:inline">Python {health.python}</span>}
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <span
              className="hidden max-w-[240px] items-center gap-1.5 text-[11px] text-lx-muted lg:flex"
              title={health?.gpus?.join('\n') ?? '未检测到 NVIDIA 显卡'}
            >
              <Cpu size={12} className="shrink-0" />
              <span className="truncate font-mono">
                {gpu ? gpu.split(',')[0] : '未检测到显卡'}
              </span>
            </span>

            <StatusChip tone={health ? (health.running_job ? 'accent' : 'success') : 'danger'}>
              {health ? (health.running_job ? '打标中' : '空闲') : '后端未响应'}
            </StatusChip>

            <button
              type="button"
              onClick={onToggleTheme}
              className="lx-btn-ghost-icon"
              title={theme === 'dark' ? '切到浅色' : '切到深色'}
              aria-label={theme === 'dark' ? '切到浅色' : '切到深色'}
            >
              {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
            </button>
          </div>
        </header>

        <main className="min-h-0 flex-1 overflow-y-auto">
          <div className="px-4 py-6 md:px-7 md:py-7">{children}</div>
        </main>

        {/* 没有任务时整条不渲染：不留一条空边框占高度 */}
        {statusBar ? (
          <footer className="shrink-0 border-t border-lx bg-lx-surface/70 px-4 py-1.5 md:px-6">
            {statusBar}
          </footer>
        ) : null}
      </div>
    </div>
  )
}
