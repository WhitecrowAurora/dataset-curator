import type { ReactNode } from 'react'
import { Cpu, Moon, Sun, Server, Waypoints } from 'lucide-react'
import type { CenterBrief, Health } from '../lib/api'
import type { Theme } from '../lib/theme'
import { cn } from '../lib/cn'
import { StatusChip } from '../components/ui/launcher'
import { PresetMenu } from '../components/PresetMenu'
import { Sidebar, type NavItem } from './Sidebar'

/**
 * 应用壳：左侧栏 + 顶栏 + 滚动主区。
 * 版式取自 lulynx 的 AppShell，但去掉了桌面端的 TitleBar（浏览器自带标题栏），
 * 主区宽度交给页面自己控制（页面根用 mx-auto max-w-6xl）。
 */
export function AppShell({
  items,
  health,
  center,
  onOpenCenter,
  theme,
  onToggleTheme,
  children,
  footer,
  statusBar,
}: {
  items: NavItem[]
  health: Health | null
  /** 调度中心的连接状态；null = 还没问到（按"没连上"显示） */
  center: CenterBrief | null
  onOpenCenter: () => void
  theme: Theme
  onToggleTheme: () => void
  children: ReactNode
  /** 侧栏底部的小字（版本/监听地址之类） */
  footer?: ReactNode
  /** 主区底部的全局状态条：所有页面都能看到"现在在跑什么" */
  statusBar?: ReactNode
}) {
  const gpu = health?.gpus?.[0]
  // 灯看的是 online（此刻真的问得通），不是 linked（以前登录过）。
  const online = center?.online ?? false
  const centerTitle = !center
    ? '调度中心：状态未知'
    : online
      ? `已连接调度中心${center.user_id ? `（${center.user_id}）` : ''}${center.running ? '，正在跑' : ''}`
      : center.linked
        ? `连不上调度中心：${center.reason || '检查网络或总机是否在跑'}`
        : '还没连调度中心：点这里填地址和姓名'
  return (
    <div className="flex h-screen w-full overflow-hidden bg-lx-bg font-sans text-lx">
      <Sidebar items={items} footer={footer} />

      <div className="flex min-w-0 flex-1 flex-col">
        {/* relative z-30：backdrop-blur 会自建一层 stacking context，不抬 z-index 的
            话顶栏里的下拉（配置方案）会被下面的 <main> 盖住——看得见、点不到。 */}
        <header className="relative z-30 flex h-12 shrink-0 items-center justify-between gap-4 border-b border-lx bg-lx-surface/70 px-4 backdrop-blur md:px-5">
          <div className="flex min-w-0 items-center gap-3">
            {/* 调度中心的入口 + 连接灯。放左边是因为它和"这是哪台机器"同属
                连接状态；右边已经挤了显卡、配置方案和任务状态片。 */}
            <button
              type="button"
              onClick={onOpenCenter}
              title={centerTitle}
              className="flex shrink-0 items-center gap-1.5 rounded-lx-sm px-1.5 py-1 text-[11.5px] text-lx-secondary transition-colors hover:bg-[color-mix(in_srgb,var(--lx-text)_5%,transparent)] hover:text-lx"
            >
              <Waypoints size={13} className="shrink-0" />
              <span className="hidden sm:inline">调度中心</span>
              <span
                className={cn(
                  'h-1.5 w-1.5 shrink-0 rounded-full',
                  online ? 'bg-lx-success' : 'bg-lx-warning',
                  online && center?.running && 'animate-pulse',
                )}
                aria-hidden="true"
              />
              <span className="sr-only">{online ? '已连接' : '未连接'}</span>
            </button>

            <span className="h-3.5 w-px shrink-0 bg-lx" aria-hidden="true" />

            <div className="flex min-w-0 items-center gap-2 text-[11.5px] text-lx-muted">
              <Server size={13} className="shrink-0" />
              <span className="truncate font-mono">
                {health ? health.platform : '正在连接本机服务…'}
              </span>
              {health && <span className="hidden shrink-0 text-lx-dim sm:inline">Python {health.python}</span>}
            </div>
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

            {/* 配置方案紧挨着状态片：它是"今天用哪套口径"，与运行状态同一排 */}
            <PresetMenu />

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
          <div className="px-4 py-4 md:px-6 md:py-5">{children}</div>
        </main>

        {/* 底部状态条常驻：空闲时它就是一行 "-- | -- | --"。
            以前是有任务才出现，代价是整条高度来回跳，切换页面时会抖。 */}
        {statusBar ? (
          <footer className="shrink-0 border-t border-lx bg-lx-surface/70 px-4 py-1 md:px-5">
            {statusBar}
          </footer>
        ) : null}
      </div>
    </div>
  )
}
