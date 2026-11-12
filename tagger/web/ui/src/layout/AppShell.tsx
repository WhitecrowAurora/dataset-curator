import type { ReactNode } from 'react'
import { Cpu, Moon, Sun, Server, Waypoints } from 'lucide-react'
import type { CenterBrief, Health, TorchHint } from '../lib/api'
import type { Theme } from '../lib/theme'
import { cn } from '../lib/cn'
import { StatusChip } from '../components/ui/launcher'
import { PresetMenu } from '../components/PresetMenu'
import { Sidebar, type NavItem } from './Sidebar'

/**
 * 从 `describe()` 那一行长文本里只取卡名。
 *
 * 后端给的是 `[0] NVIDIA GeForce RTX 4070 Ti SUPER  16.0 GiB  CC 8.9  [fp8...]`，
 * 段与段之间是**两个空格**。以前按 `split(',')[0]` 取，那个逗号只出现在最后的
 * 特性列表里（`[fp8, no-fp4, int4]`），于是整串 `16.0 GiB  CC 8.9` 都被留在
 * 顶栏里，把卡名挤成 "RTX 4070 Ti S…"。显存和 CC 在依赖页看得到，顶栏只需要
 * 认出是哪张卡。
 */
function gpuName(line: string): string {
  return line.split(/\s{2,}/)[0].replace(/^\[\d+\]\s*/, '')
}

/**
 * 顶栏的 torch 档位提示。三种状态各有不同的"该不该让人操心"：
 *
 *   ok     —— 绿点 + 档位名。装对了就不该占注意力，所以压到最小。
 *   switch —— 黄色，可点。这是唯一需要用户动手的状态：可能是装错了档
 *             （50 系装了 cu126，一跑就报 no kernel image），也可能只是版本
 *             号与清单不符。
 *   cpu    —— 黄色，可点。没有 CUDA 版可用：没卡、驱动太老、或探测失败。
 *             不说一句的话，用户只会觉得"这软件就这速度"。
 *
 * 为什么不在这里判断"20/30/40 用 cu126、50 用 cu130"：那条规则在
 * `torch_plan()` 里，重写一份的结果是顶栏和依赖页各说一套。这里只渲染。
 */
function TorchHintChip({
  hint,
  onOpen,
}: {
  hint?: TorchHint
  onOpen: () => void
}) {
  // 老后端没有这个字段：静默不渲染，别在顶栏留一块空白
  if (!hint) return null
  const { status, arch, target, current, reason } = hint
  const title = [
    `显卡架构 ${arch}`,
    target ? `本机建议 ${target}` : '',
    current ? `当前装着 ${current}` : '当前没读到 torch 版本',
    reason,
    status !== 'ok' ? '点这里去依赖页处理' : '',
  ]
    .filter(Boolean)
    .join('\n')

  const tone =
    status === 'ok'
      ? 'text-lx-dim'
      : status === 'switch'
        ? 'text-lx-warning'
        : 'text-lx-warning'

  const label = status === 'ok' ? current || target : `建议 ${target || 'CPU'}`
  const interactive = status !== 'ok'

  return (
    <button
      type="button"
      onClick={interactive ? onOpen : undefined}
      disabled={!interactive}
      title={title}
      className={cn(
        'flex shrink-0 items-center gap-1 rounded-lx-sm px-1.5 py-1 text-[10.5px] transition-colors',
        tone,
        interactive
          ? 'hover:bg-[color-mix(in_srgb,var(--lx-text)_6%,transparent)]'
          : 'cursor-default',
      )}
    >
      <span
        className={cn(
          'h-1.5 w-1.5 shrink-0 rounded-full',
          status === 'ok' ? 'bg-lx-success' : 'bg-lx-warning',
        )}
        aria-hidden="true"
      />
      <span className="font-mono">{label}</span>
      {/* switch 时把"现在装的是哪个"直接写出来。只写"建议 cu130"是不够的：
          用户不知道现在是 cu126 还是压根没装，也就判断不出该不该点。
          title 里虽然有全句，但悬浮才看得到，等于没有。 */}
      {status === 'switch' && current && current !== target ? (
        <span className="hidden font-mono lg:inline">（现为 {current}）</span>
      ) : null}
      {status !== 'ok' && <span className="hidden sm:inline">· 点此处理</span>}
    </button>
  )
}

/**
 * 应用壳：左侧栏 + 顶栏 + 滚动主区。
 * 版式取自 lulynx 的 AppShell，但去掉了桌面端的 TitleBar（浏览器自带标题栏），
 * 主区宽度交给页面自己控制（页面根用 mx-auto max-w-6xl）。
 */
export function AppShell({
  items,
  actions,
  health,
  center,
  onOpenCenter,
  onOpenDeps,
  theme,
  onToggleTheme,
  children,
  footer,
  statusBar,
}: {
  items: NavItem[]
  /** 侧栏导航下方的操作按钮（重试失败项等），与页面切换不是一类 */
  actions?: ReactNode
  health: Health | null
  /** 调度中心的连接状态；null = 还没问到（按"没连上"显示） */
  center: CenterBrief | null
  onOpenCenter: () => void
  /** 点档位提示时跳去依赖页（只有需要处理时才可点） */
  onOpenDeps: () => void
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
      <Sidebar items={items} actions={actions} footer={footer} />

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
              className="hidden max-w-[300px] items-center gap-1.5 text-[11px] text-lx-muted lg:flex"
              title={health?.gpus?.join('\n') ?? '未检测到 NVIDIA 显卡'}
            >
              <Cpu size={12} className="shrink-0" />
              <span className="truncate font-mono">
                {gpu ? gpuName(gpu) : '未检测到显卡'}
              </span>
            </span>

            {/* 分隔线：不加的话档位紧贴显卡名，视觉上像同一个词
                （"RTX 4070 Ti S… ● cu126" 读起来是一个东西）。 */}
            {health?.torch_hint ? (
              <span className="hidden h-3 w-px shrink-0 bg-lx lg:block" aria-hidden="true" />
            ) : null}

            {/* torch 档位提示。放显卡名右边的理由：它就是"这张卡该配哪个轮子"，
                和显卡是同一件事。档位规则不在这里重写一遍——`torch_hint` 是从
                依赖页那份 torch_plan() 推出来的，两处各写一份迟早会互相打脸。
                窄屏不隐藏（换了机器才该看的就是这条），只把文字缩到最小。 */}
            <TorchHintChip hint={health?.torch_hint} onOpen={onOpenDeps} />

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
