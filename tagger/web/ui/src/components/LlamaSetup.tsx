import { useEffect, useRef, useState } from 'react'
import { Check, Download, RefreshCw, TriangleAlert } from 'lucide-react'

import { api, type LlamaRuntime } from '../lib/api'
import { ProgressBar } from './ui/ProgressBar'
import { StatusChip } from './ui/launcher'
import { ErrorBox } from './ui/ErrorBox'

/**
 * llama.cpp 运行时（llama-server）的安装入口。
 *
 * 为什么需要这一块：默认引擎是 llama.cpp，而 llama-server 不是 pip 包，
 * 官方只发 GitHub release 的 zip。没有它，「启动服务」必然失败，报的还是
 * "找不到 llama-server"——用户此时唯一能做的动作是离开页面去翻命令行。
 * 这里把那一步收进来：点一下，下好、解开、就地生效。
 *
 * 只在引擎选了 llama.cpp 时由上层渲染；已安装时缩成一行状态，不占地方。
 */

const STAGE_TEXT: Record<string, string> = {
  probing: '准备中',
  downloading: '下载中',
  extracting: '解压中',
  verifying: '校验中',
  cancelled: '已取消',
  error: '失败',
}

function mib(n: number): string {
  return n > 0 ? `${Math.round(n / (1 << 20))} MB` : ''
}

export function LlamaSetup({ onInstalled }: { onInstalled?: () => void }) {
  const [snap, setSnap] = useState<LlamaRuntime | null>(null)
  const [acting, setActing] = useState(false)
  const [error, setError] = useState('')
  const [manual, setManual] = useState('')

  // 回调放 ref：父组件传的是内联箭头函数，把它写进依赖会让轮询
  // 每次重渲染都重启一遍定时器（表现是进度条一顿一顿地不动）。
  const ready = useRef(onInstalled)
  ready.current = onInstalled
  const wasInstalled = useRef(false)

  // 装好后必须通知父组件重探能力：快照里的 local_ok 有 TTL 缓存，
  // 不清的话「启动服务」按钮会一直按旧结论灰着。
  useEffect(() => {
    let alive = true
    let timer = 0
    const tick = async () => {
      try {
        const next = await api.llamaStatus()
        if (!alive) return
        setSnap(next)
        if (next.installed && !wasInstalled.current) {
          wasInstalled.current = true
          ready.current?.()
        }
        // 安装中看细一点，闲着就放慢：这个接口会读盘但不联网。
        timer = window.setTimeout(tick, next.busy ? 1000 : 8000)
      } catch {
        if (alive) timer = window.setTimeout(tick, 8000)
      }
    }
    void tick()
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [])

  const run = async (url = '', force = false) => {
    setActing(true)
    setError('')
    try {
      const next = await api.installLlama({ url, force })
      setSnap(next)
      if (!next.started && next.reason) setError(next.reason)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setActing(false)
    }
  }

  const cancel = async () => {
    try {
      setSnap(await api.cancelLlamaInstall())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  if (!snap) return null

  const pct = snap.total ? Math.min(100, (snap.done / snap.total) * 100) : 0
  const stage = STAGE_TEXT[snap.stage] ?? '安装中'

  if (snap.busy) {
    return (
      <div className="border-b border-lx py-2.5">
        <div className="flex items-baseline justify-between gap-2 text-[11px]">
          <span className="text-lx-secondary">
            {stage}
            {snap.total ? ` · ${Math.round(pct)}%` : ''}
          </span>
          <span className="font-mono tabular-nums text-lx-dim">
            {snap.total ? `${mib(snap.done)} / ${mib(snap.total)}` : mib(snap.done)}
          </span>
        </div>
        <ProgressBar percent={pct} heightClass="h-1" className="mt-1.5" />
        <button
          type="button"
          className="lx-btn-ghost mt-2 px-2 py-1 text-[11px]"
          onClick={() => void cancel()}
        >
          取消
        </button>
      </div>
    )
  }

  if (snap.installed) {
    return (
      <div className="flex items-center justify-between gap-2 border-b border-lx py-2">
        {/* 原来是 11px 灰字挤在行里，等于没说：这一行的作用就是让人一眼
            确认"引擎能起来"，状态必须比周围的输入项更醒目。 */}
        <StatusChip tone="success" icon={<Check size={12} />}>
          llama.cpp 已就绪{snap.version ? ` · ${snap.version}` : ''}
        </StatusChip>
        <button
          type="button"
          className="lx-btn-ghost inline-flex shrink-0 items-center gap-1 px-2 py-1 text-[11px]"
          disabled={acting}
          onClick={() => void run('', true)}
          title={`重装到 ${snap.install_root}`}
        >
          <RefreshCw size={11} />
          重装
        </button>
      </div>
    )
  }

  return (
    <div className="border-b border-lx py-2">
      <div className="flex flex-wrap items-center gap-2">
        <StatusChip tone="warning" icon={<TriangleAlert size={12} />}>
          llama.cpp 未安装
        </StatusChip>
        <button
          type="button"
          className="lx-btn-primary inline-flex items-center gap-1.5 px-3 py-1.5 text-xs"
          disabled={acting || !snap.supported}
          onClick={() => void run()}
        >
          <Download size={12} />
          {acting ? '启动中…' : '下载并安装 llama.cpp'}
        </button>
        <span className="text-[11px] text-lx-muted">
          {snap.supported ? '约 200 MB，来自 GitHub releases' : '此平台无官方二进制，需自行编译'}
        </span>
      </div>

      {/* 备用路径：GitHub 不通时用户手上往往已经有一个 zip（镜像/同事传的）。
          安装失败时默认展开——否则用户看到一行红字，唯一能做的动作（贴 zip 链接）
          还藏在下面这个折叠块里。 */}
      <details className="mt-1.5" open={Boolean(snap.error || error)}>
        <summary className="cursor-pointer select-none text-[11px] text-lx-muted hover:text-lx-secondary">
          下载不动？用现成的 zip
        </summary>
        <div className="mt-1.5 flex gap-2">
          <input
            className="lx-input font-mono text-xs"
            value={manual}
            onChange={(e) => setManual(e.target.value)}
            placeholder="https://.../llama-b7123-bin-win-cuda-12.4-x64.zip"
            spellCheck={false}
          />
          <button
            type="button"
            className="lx-btn-ghost inline-flex shrink-0 items-center px-3"
            disabled={acting || !manual.trim()}
            onClick={() => void run(manual.trim())}
          >
            安装
          </button>
        </div>
      </details>

      {(snap.error || error) && (
        <ErrorBox text={error || snap.error} className="mt-1.5" />
      )}
    </div>
  )
}
