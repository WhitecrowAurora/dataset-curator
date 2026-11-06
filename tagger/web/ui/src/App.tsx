import { useCallback, useEffect, useState } from 'react'
import { Boxes, ChartSpline, Copy, History, ListChecks, PlayCircle, PlusCircle, UploadCloud } from 'lucide-react'
import { api, type Health } from './lib/api'
import { applyTheme, readTheme, type Theme } from './lib/theme'
import { AppShell } from './layout/AppShell'
import type { NavItem } from './layout/Sidebar'
import { NewJob } from './pages/NewJob'
import { RunJob } from './pages/RunJob'
import { Results } from './pages/Results'
import { History as HistoryPage } from './pages/History'
import { Stats } from './pages/Stats'
import { Packaging } from './pages/Packaging'
import { Upload } from './pages/Upload'
import { Dedup } from './pages/Dedup'

// 手写 hash 路由：七个页面、零依赖，比引 react-router 更省事也更不容易踩版本。
// 用 hash 而不是 history 还有一个实际好处——右键"在新标签打开"不会 404。

type Route =
  | { name: 'new' }
  | { name: 'run'; id: string }
  | { name: 'results'; id: string }
  | { name: 'upload' }
  | { name: 'dedup' }
  | { name: 'packaging' }
  | { name: 'stats' }
  | { name: 'history' }

const LAST_KEY = 'tagger-last-job'

function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, '') || '/'
  const match = path.match(/^\/(run|results)\/([\w-]+)$/)
  if (match) return { name: match[1] as 'run' | 'results', id: match[2] }
  if (path === '/history') return { name: 'history' }
  if (path === '/upload') return { name: 'upload' }
  if (path === '/dedup') return { name: 'dedup' }
  if (path === '/packaging') return { name: 'packaging' }
  if (path === '/stats') return { name: 'stats' }
  return { name: 'new' }
}

function go(path: string) {
  location.hash = path
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.hash))
  const [health, setHealth] = useState<Health | null>(null)
  const [theme, setTheme] = useState<Theme>(() => readTheme())
  const [lastJobId, setLastJobId] = useState<string>(() => {
    try {
      return localStorage.getItem(LAST_KEY) ?? ''
    } catch {
      return ''
    }
  })

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  useEffect(() => {
    const onChange = () => setRoute(parseRoute(location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  useEffect(() => {
    const tick = () => api.health().then(setHealth).catch(() => setHealth(null))
    tick()
    const t = window.setInterval(tick, 15000)
    return () => window.clearInterval(t)
  }, [])

  // 记住最近一个任务：侧栏的"结果"和顶栏状态都靠它，刷新页面也还能回到上次的活儿。
  useEffect(() => {
    if (route.name !== 'run' && route.name !== 'results') return
    setLastJobId(route.id)
    try {
      localStorage.setItem(LAST_KEY, route.id)
    } catch {
      // 存不了就只在内存里记着，不影响使用
    }
  }, [route])

  const openJob = useCallback((id: string) => go(`/run/${id}`), [])

  const runningId = health?.running_job ?? (route.name === 'run' ? route.id : '') ?? ''
  const resultsId = route.name === 'results' ? route.id : lastJobId

  const items: NavItem[] = [
    {
      key: 'new',
      label: '新建任务',
      icon: <PlusCircle size={15} />,
      active: route.name === 'new',
      onClick: () => go('/'),
    },
    {
      key: 'run',
      label: '运行中',
      icon: <PlayCircle size={15} />,
      active: route.name === 'run',
      disabled: !runningId,
      hint: runningId ? '查看正在跑的任务' : '当前没有正在跑的任务',
      onClick: () => runningId && openJob(runningId),
    },
    {
      key: 'history',
      label: '历史任务',
      icon: <History size={15} />,
      active: route.name === 'history',
      onClick: () => go('/history'),
    },
    {
      key: 'stats',
      label: '统计',
      icon: <ChartSpline size={15} />,
      active: route.name === 'stats',
      hint: '通过率 / 失败率 / 质检分的分桶趋势',
      onClick: () => go('/stats'),
    },
    {
      key: 'dedup',
      label: '去重',
      icon: <Copy size={15} />,
      active: route.name === 'dedup',
      hint: '发分包前查重复图，产出排除清单',
      onClick: () => go('/dedup'),
    },
    {
      key: 'packaging',
      label: '数据打包',
      icon: <Boxes size={15} />,
      active: route.name === 'packaging',
      hint: '分包 / 拼接 / 封包，与打标分开走',
      onClick: () => go('/packaging'),
    },
    {
      key: 'upload',
      label: '回传',
      icon: <UploadCloud size={15} />,
      active: route.name === 'upload',
      hint: '把处理好的 parquet 传到汇总机',
      onClick: () => go('/upload'),
    },
    {
      key: 'results',
      label: '查看结果',
      icon: <ListChecks size={15} />,
      active: route.name === 'results',
      disabled: !resultsId,
      hint: resultsId ? '浏览最近一次任务的产物' : '还没有跑过任务',
      onClick: () => resultsId && go(`/results/${resultsId}`),
    },
  ]

  return (
    <AppShell
      items={items}
      health={health}
      theme={theme}
      onToggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
      footer={
        <p className="px-1 text-[10px] leading-relaxed text-lx-dim">
          本机服务 · 只监听 127.0.0.1
        </p>
      }
    >
      {route.name === 'new' && <NewJob onStarted={openJob} />}
      {route.name === 'run' && (
        <RunJob
          jobId={route.id}
          onResults={() => go(`/results/${route.id}`)}
          onNew={() => go('/')}
        />
      )}
      {route.name === 'results' && (
        <Results jobId={route.id} onBack={() => go(`/run/${route.id}`)} />
      )}
      {route.name === 'history' && (
        <HistoryPage
          currentId={runningId || lastJobId}
          onOpenRun={openJob}
          onOpenResults={(id) => go(`/results/${id}`)}
        />
      )}
      {route.name === 'upload' && <Upload />}
      {route.name === 'dedup' && <Dedup />}
      {route.name === 'packaging' && <Packaging />}
      {route.name === 'stats' && <Stats />}
    </AppShell>
  )
}
