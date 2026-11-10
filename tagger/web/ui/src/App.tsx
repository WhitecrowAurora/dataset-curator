import { useCallback, useEffect, useState } from 'react'
import {
  Boxes,
  ChartSpline,
  Copy,
  Download,
  GitCompareArrows,
  History,
  Layers,
  ListChecks,
  Merge,
  Package,
  PlayCircle,
  PlusCircle,
  Settings2,
  ShieldCheck,
  UploadCloud,
  Waypoints,
} from 'lucide-react'
import { api, type CenterBrief, type Health } from './lib/api'
import { cn } from './lib/cn'
import { applyTheme, readTheme, type Theme } from './lib/theme'
import { readProgressInterval, writeProgressInterval } from './lib/prefs'
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
import { Models } from './pages/Models'
import { Downloads } from './pages/Downloads'
import { TagManage } from './pages/TagManage'
import { Conflicts } from './pages/Conflicts'
import { Deps } from './pages/Deps'
import { Settings } from './pages/Settings'
import { Center } from './pages/Center'
import { GlobalProgress } from './components/GlobalProgress'
import { SelfCheckDialog } from './components/SelfCheckDialog'
import { ResumeBanner } from './components/ResumeBanner'
import { ActivityNotice } from './components/ActivityNotice'

// 手写 hash 路由：七个页面、零依赖，比引 react-router 更省事也更不容易踩版本。
// 用 hash 而不是 history 还有一个实际好处——右键"在新标签打开"不会 404。

type Route =
  | { name: 'new' }
  | { name: 'run'; id: string }
  | { name: 'results'; id: string }
  | { name: 'upload' }
  | { name: 'dedup' }
  | { name: 'packaging' }
  | { name: 'conflicts' }
  | { name: 'models' }
  | { name: 'downloads' }
  | { name: 'tag-manage' }
  | { name: 'stats' }
  | { name: 'history' }
  | { name: 'deps' }
  | { name: 'center' }
  | { name: 'settings' }

const LAST_KEY = 'tagger-last-job'

function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, '') || '/'
  const match = path.match(/^\/(run|results)\/([\w-]+)$/)
  if (match) return { name: match[1] as 'run' | 'results', id: match[2] }
  if (path === '/history') return { name: 'history' }
  if (path === '/upload') return { name: 'upload' }
  if (path === '/dedup') return { name: 'dedup' }
  if (path === '/packaging') return { name: 'packaging' }
  if (path === '/conflicts') return { name: 'conflicts' }
  if (path === '/models') return { name: 'models' }
  if (path === '/downloads') return { name: 'downloads' }
  if (path === '/tag-manage') return { name: 'tag-manage' }
  if (path === '/stats') return { name: 'stats' }
  if (path === '/deps') return { name: 'deps' }
  if (path === '/center') return { name: 'center' }
  if (path === '/settings') return { name: 'settings' }
  return { name: 'new' }
}

function go(path: string) {
  location.hash = path
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.hash))
  const [health, setHealth] = useState<Health | null>(null)
  // 顶栏那盏灯只管"有没有身份"，所以走 /api/center/status（只读本地文件），
  // 不拉 /api/center —— 后者要问一次调度中心，服务端挂掉时每轮要等 6 秒。
  const [center, setCenter] = useState<CenterBrief | null>(null)
  const [theme, setTheme] = useState<Theme>(() => readTheme())
  // 自检是弹窗不是页面：它只回答"这台机器现在能不能干活"，
  // 没有需要反复回来看的状态，占一个路由位不值得。
  const [selfCheckOpen, setSelfCheckOpen] = useState(false)
  // 底部进度条的轮询间隔，存 localStorage：这是"看着舒服"的本机偏好，
  // 不该跟服务端配置漂移。默认 5s。
  const [progressInterval, setProgressInterval] = useState(() => readProgressInterval())
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

  // 比 health 勤一点：在调度中心页点了「连接」之后，顶栏的灯要跟着变色，
  // 等 15 秒才亮像是在说"没连上"。
  const refreshCenter = useCallback(() => {
    api.centerBrief().then(setCenter).catch(() => setCenter(null))
  }, [])

  useEffect(() => {
    refreshCenter()
    // 人看不见这一屏时不用问（切到别的窗口、最小化）：探测本身要打一次远端，
    // 没人看的时候白打。切回来立刻补一次，不会停在旧状态上。
    const tick = () => {
      if (document.visibilityState === 'visible') refreshCenter()
    }
    const t = window.setInterval(tick, 8000)
    document.addEventListener('visibilitychange', tick)
    return () => {
      window.clearInterval(t)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [refreshCenter])

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

  const changeProgressInterval = useCallback((ms: number) => {
    setProgressInterval(ms)
    writeProgressInterval(ms)
  }, [])

  const runningId = health?.running_job ?? (route.name === 'run' ? route.id : '') ?? ''
  const resultsId = route.name === 'results' ? route.id : lastJobId

  const items: NavItem[] = [
    // 排在最前：这是"活从哪来"的入口。从总机领分片和本机自建任务是两条并列的
    // 起点，而前者决定了这台机器默认在干什么，所以先问它。
    {
      key: 'center',
      label: '调度中心',
      icon: <Waypoints size={15} />,
      active: route.name === 'center',
      hint: '连上总机领分片打标；开始/停止与租约看板都在这一页',
      onClick: () => go('/center'),
    },
    {
      key: 'new',
      label: '新建任务',
      icon: <PlusCircle size={15} />,
      active: route.name === 'new',
      onClick: () => go('/'),
    },
    {
      key: 'selfcheck',
      label: '部署自检',
      icon: <ShieldCheck size={15} />,
      active: selfCheckOpen,
      hint: '装完点一下：逐项验证环境、目录与服务，并真跑一张图',
      onClick: () => setSelfCheckOpen(true),
    },
    {
      key: 'deps',
      label: '依赖检测',
      icon: <Package size={15} />,
      active: route.name === 'deps',
      hint: '逐个查看依赖装没装、版本对不对，可单独装卸',
      onClick: () => go('/deps'),
    },
    {
      key: 'models',
      label: '模型管理',
      icon: <Layers size={15} />,
      active: route.name === 'models',
      hint: '增删可选模型（NL 的 Qwen-VL / tag 分类头）',
      onClick: () => go('/models'),
    },
    {
      key: 'downloads',
      label: '分片下载',
      icon: <Download size={15} />,
      active: route.name === 'downloads',
      hint: '提前把 HF 仓库的分片备到本地，打标时直接命中',
      onClick: () => go('/downloads'),
    },
    {
      key: 'tag-manage',
      label: '打标管理',
      icon: <Merge size={15} />,
      active: route.name === 'tag-manage',
      hint: '把多个 json 合成一个单体 json（tag/nl 按图配对、字段取并集）',
      onClick: () => go('/tag-manage'),
    },
    {
      key: 'conflicts',
      label: '冲突与决策',
      icon: <GitCompareArrows size={15} />,
      active: route.name === 'conflicts',
      hint: '同一张图两份不同内容（换模型 / 新旧版本）：逐条对比后决定留哪份',
      onClick: () => go('/conflicts'),
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
    {
      key: 'settings',
      label: '设置',
      icon: <Settings2 size={15} />,
      active: route.name === 'settings',
      hint: '界面偏好：底栏刷新频率等',
      onClick: () => go('/settings'),
    },
  ]

  return (
    <AppShell
      items={items}
      health={health}
      center={center}
      onOpenCenter={() => go('/center')}
      theme={theme}
      onToggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
      footer={
        <>
          {/* 左下角：一条能点的状态行。侧栏导航要滚才能看到调度中心，
              而"连没连上"是随时要瞟一眼的东西，放这儿不用找。 */}
          <button
            type="button"
            onClick={() => go('/center')}
            title={
              center?.online
                ? `已连接调度中心${center.user_id ? `（${center.user_id}）` : ''}`
                : center?.linked
                  ? `连不上调度中心：${center.reason || '检查网络或总机是否在跑'}`
                  : '还没连调度中心'
            }
            className="flex w-full items-center gap-2 rounded-lx-sm px-1.5 py-1.5 text-left text-[10.5px] text-lx-muted transition-colors hover:bg-[color-mix(in_srgb,var(--lx-text)_5%,transparent)] hover:text-lx-secondary"
          >
            <span
              className={cn(
                'h-1.5 w-1.5 shrink-0 rounded-full',
                center?.online ? 'bg-lx-success' : 'bg-lx-warning',
              )}
              aria-hidden="true"
            />
            <span className="truncate">
              {center?.online
                ? center.running
                  ? `调度中 · ${center.user_id || '已连接'}`
                  : `已连接 · ${center.user_id || '待命'}`
                : center?.linked
                  ? '调度中心连不上'
                  : '未连接调度中心'}
            </span>
          </button>
          <p className="px-1 pt-1 text-[10px] leading-relaxed text-lx-dim">
            本机服务 · 只监听 127.0.0.1
          </p>
        </>
      }
      statusBar={
        <GlobalProgress interval={progressInterval} onOpen={go} />
      }
    >
      {/* 放在所有页面之前：用户下次打开往往就是来接着昨天那个活儿的，
          不该先去某个页面里找一圈。 */}
      <ResumeBanner onOpen={openJob} />

      {/* 任务收尾弹窗：长任务跑完/失败时，用户多半不在那个页面上。 */}
      <ActivityNotice interval={progressInterval} onOpen={go} />

      {/* 新建页常驻挂载，只是藏起来。卸载再挂载会把所有 useState 清零，
          用户去「模型管理」看一眼回来就得重填一遍路径和参数。 */}
      <div className={route.name === 'new' ? undefined : 'hidden'}>
        <NewJob
          active={route.name === 'new'}
          onStarted={openJob}
          onOpenModels={() => go('/models')}
        />
      </div>
      {route.name === 'models' && <Models />}
      {route.name === 'downloads' && <Downloads />}
      {route.name === 'tag-manage' && <TagManage />}
      {route.name === 'conflicts' && <Conflicts />}
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
      {route.name === 'deps' && <Deps />}
      {/* 登录成功要立刻唤醒顶栏的灯，别等下一轮轮询 */}
      {route.name === 'center' && <Center onLinked={refreshCenter} />}
      {route.name === 'settings' && (
        <Settings
          progressInterval={progressInterval}
          onProgressInterval={changeProgressInterval}
        />
      )}

      <SelfCheckDialog open={selfCheckOpen} onClose={() => setSelfCheckOpen(false)} />
    </AppShell>
  )
}
