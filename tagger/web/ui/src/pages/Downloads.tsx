import { useEffect, useMemo, useState } from 'react'
import {
  CheckCircle2,
  Circle,
  Download,
  HardDriveDownload,
  Loader2,
  PauseCircle,
  RefreshCw,
  Square,
  Trash2,
  XCircle,
} from 'lucide-react'
import { api, ApiError, type DownloadItem, type DownloadsState } from '../lib/api'
import { fmtBytes } from '../lib/format'
import {
  PageHeader,
  SectionPanel,
  SettingRow,
  StatusChip,
  type StatusTone,
} from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'

// 下载页：**只搬运分片，不打标**。
// 下好的分片落在 run/hf-cache/<org--name>/，与打标任务同一个目录，
// 所以任务跑起来会直接命中（不再下载）、处理完立刻删。
// 它与任务解耦的意义：网络差的机器可以先把几片备好，到点开跑就是纯算力。

const FORMATS = [
  { value: 'auto', label: '自动识别' },
  { value: 'webdataset', label: 'tar 分片' },
  { value: 'parquet', label: 'parquet 分片' },
] as const

const STATUS_LABEL: Record<DownloadItem['status'], string> = {
  queued: '排队',
  running: '下载中',
  cancelling: '收尾中',
  cancelled: '已暂停',
  done: '完成',
  error: '失败',
}

function toneOf(status: DownloadItem['status']): StatusTone {
  if (status === 'done') return 'success'
  if (status === 'error') return 'danger'
  if (status === 'cancelled' || status === 'cancelling') return 'warning'
  if (status === 'running') return 'accent'
  return 'neutral'
}

function pct(done: number, size: number): number {
  if (!size) return 0
  return Math.min(100, Math.max(0, (done / size) * 100))
}

export function Downloads() {
  const [repo, setRepo] = useState('')
  const [token, setToken] = useState('')
  const [revision, setRevision] = useState('main')
  const [format, setFormat] = useState<'auto' | 'parquet' | 'webdataset'>('auto')

  const [files, setFiles] = useState<string[]>([])
  const [sizes, setSizes] = useState<Record<string, number>>({})
  const [detected, setDetected] = useState('')
  const [cached, setCached] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [state, setState] = useState<DownloadsState | null>(null)
  const [reserveGb, setReserveGb] = useState(2)
  // 多线程下载开关 + 线程数。关掉即 conns=1（单连接），
  // 小文件与不支持 Range 的服务端总是自动退回单连接。
  const [multi, setMulti] = useState(true)
  const [conns, setConns] = useState(4)
  // 代理：与模型下载共用同一份全局设置。只在第一次拿到状态时填一次，
  // 否则每秒轮询会把用户正在编辑的输入框覆盖掉。
  const [proxy, setProxy] = useState('')
  const [proxyLoaded, setProxyLoaded] = useState(false)
  const [proxyBusy, setProxyBusy] = useState(false)
  const [proxyMsg, setProxyMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 队列每秒拉一次。这里有正在下的任务时是唯一需要高频的东西，
  // 用轮询而不是 WS：下载进度本来就是"慢变量"，断了也不影响正确性。
  useEffect(() => {
    let alive = true
    const tick = () => {
      api
        .downloads()
        .then((st) => {
          if (alive) setState(st)
        })
        .catch(() => {
          // 队列接口挂了不该把整页打红，用户还能看清单
        })
    }
    tick()
    const timer = window.setInterval(tick, 1200)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  // 只回填一次：之后用户在改，轮询不该抢方向盘
  useEffect(() => {
    if (proxyLoaded || !state) return
    setProxy(state.proxy ?? '')
    setProxyLoaded(true)
  }, [state, proxyLoaded])

  const repoKey = repo.trim()
  // 同名文件以最新一条为准：先 done 后重新排队的场景不该显示成"完成"
  const queueOf = useMemo(() => {
    const map = new Map<string, DownloadItem>()
    for (const item of state?.items ?? []) {
      if (item.repo === repoKey) map.set(item.filename, item)
    }
    return map
  }, [state, repoKey])

  const selBytes = useMemo(
    () => [...selected].reduce((acc, name) => acc + (sizes[name] ?? 0), 0),
    [selected, sizes],
  )
  const freeTone: StatusTone =
    state === null
      ? 'neutral'
      : state.free_bytes < state.reserve_bytes + selBytes
        ? 'warning'
        : 'neutral'

  async function loadFiles() {
    if (!repoKey) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.hfFiles(repoKey, token, revision || 'main', format)
      setFiles(res.files)
      setSizes(res.sizes ?? {})
      setDetected(res.format)
      setSelected(new Set())
      const local = await api.downloadsCached(repoKey)
      setCached(new Set(local.files.map((f) => f.filename)))
    } catch (err) {
      setFiles([])
      setSizes({})
      setSelected(new Set())
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function start() {
    if (!repoKey || selected.size === 0) return
    setBusy(true)
    setError(null)
    try {
      await api.downloadsStart({
        repo: repoKey,
        token,
        revision: revision || 'main',
        files: [...selected],
        sizes,
        reserve_gb: reserveGb,
        conns: multi ? conns : 1,
      })
      setSelected(new Set())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function cancelAll() {
    try {
      await api.downloadsCancelAll()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  async function saveProxy() {
    setProxyBusy(true)
    setProxyMsg(null)
    try {
      const res = await api.saveProxy(proxy)
      setProxy(res.proxy)
      setProxyMsg(res.proxy ? '已保存，后续下载都会走它' : '已清空：直连')
    } catch (err) {
      setProxyMsg(err instanceof ApiError ? err.message : String(err))
    } finally {
      setProxyBusy(false)
    }
  }

  async function testProxy() {
    setProxyBusy(true)
    setProxyMsg(null)
    try {
      const res = await api.testProxy(proxy)
      setProxyMsg(
        res.ok
          ? `通：${res.via}，${res.latency_ms} ms`
          : `不通：${res.error ?? '未知原因'}`,
      )
    } catch (err) {
      setProxyMsg(err instanceof ApiError ? err.message : String(err))
    } finally {
      setProxyBusy(false)
    }
  }

  async function clearDone() {
    try {
      await api.downloadsClear()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  function toggle(name: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const active = (state?.running ?? 0) + (state?.queued ?? 0) > 0
  const todo = files.filter((f) => !cached.has(f))

  return (
    <div className="lx-page-enter mx-auto flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<HardDriveDownload size={17} />}
        eyebrow="下载"
        title="分片下载"
        description="提前把分片备到本地，用完自动删除"
        actions={
          active ? (
            <button type="button" className="lx-btn-ghost" onClick={() => void cancelAll()}>
              <PauseCircle size={14} className="mr-1.5 inline" />
              全部暂停
            </button>
          ) : (
            <button
              type="button"
              className="lx-btn-primary lx-launch-glow inline-flex items-center gap-1.5"
              disabled={!selected.size || busy}
              onClick={() => void start()}
            >
              <Download size={14} />
              {busy ? '提交中…' : selected.size ? `下载 ${selected.size} 片` : '下载分片'}
            </button>
          )
        }
      />

      {error && <ErrorBox error={error} title="操作失败" />}

      <div className="grid gap-3.5 lg:grid-cols-[minmax(0,1fr)_380px]">
        <SectionPanel
          title="仓库分片"
          trailing={
            files.length > 0 ? (
              <StatusChip tone={selected.size ? 'accent' : 'neutral'}>
                已选 {selected.size} / {files.length}
              </StatusChip>
            ) : undefined
          }
        >
          <SettingRow label="仓库" help="org/name；私有仓库需 token">
            <div className="flex gap-2">
              <input
                className="lx-input font-mono text-xs"
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder="org/dataset"
                spellCheck={false}
              />
              <button
                type="button"
                className="lx-btn-ghost shrink-0 px-3"
                disabled={!repoKey || busy}
                onClick={() => void loadFiles()}
              >
                {busy ? (
                  <Loader2 size={14} className="mr-1 inline animate-spin" />
                ) : (
                  <RefreshCw size={14} className="mr-1 inline" />
                )}
                读取
              </button>
            </div>
          </SettingRow>

          <SettingRow label="格式" help="网络差时整片下到本地更快">
            <select
              className="lx-input"
              value={format}
              onChange={(e) =>
                setFormat(e.target.value as 'auto' | 'parquet' | 'webdataset')
              }
            >
              {FORMATS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </SettingRow>

          <div className="grid grid-cols-2 gap-2">
            <SettingRow label="版本" help="分支 / tag / commit">
              <input
                className="lx-input font-mono text-xs"
                value={revision}
                onChange={(e) => setRevision(e.target.value)}
                placeholder="main"
                spellCheck={false}
              />
            </SettingRow>
            <SettingRow label="Token" help="私有仓库才需要">
              <input
                type="password"
                className="lx-input font-mono text-xs"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="hf_…"
                autoComplete="off"
              />
            </SettingRow>
          </div>

          {files.length === 0 ? (
            <p className="px-1 text-xs text-lx-dim">
              {detected ? `识别为 ${detected}，点「读取」列出分片` : '填仓库地址后点「读取」'}
            </p>
          ) : (
            <>
              <div className="flex items-center justify-between gap-2 px-1 pt-1">
                <label className="flex cursor-pointer items-center gap-2 text-xs text-lx-secondary">
                  <input
                    type="checkbox"
                    className="accent-lx-accent"
                    checked={selected.size === todo.length && todo.length > 0}
                    onChange={(e) =>
                      setSelected(
                        e.target.checked ? new Set(todo) : new Set(),
                      )
                    }
                  />
                  全选（跳过本地已有的）
                </label>
                <span className="font-mono text-[11px] tabular-nums text-lx-dim">
                  已选 {fmtBytes(selBytes)}
                </span>
              </div>

              <div className="mt-1 max-h-[46vh] overflow-auto rounded-lg border border-lx-line/60">
                {files.map((name) => {
                  const item = queueOf.get(name)
                  const ready = cached.has(name)
                  const size = sizes[name] ?? 0
                  const running = item?.status === 'running' || item?.status === 'queued'
                  return (
                    <div
                      key={name}
                      className="flex items-center gap-2 border-b border-lx-line/40 px-2.5 py-1.5 last:border-b-0 hover:bg-lx-raised/40"
                    >
                      <input
                        type="checkbox"
                        className="accent-lx-accent"
                        checked={selected.has(name)}
                        disabled={ready || running}
                        onChange={() => toggle(name)}
                      />
                      {ready ? (
                        <CheckCircle2 size={13} className="shrink-0 text-lx-success" />
                      ) : running ? (
                        <Loader2
                          size={13}
                          className="shrink-0 animate-spin text-lx-accent"
                        />
                      ) : item?.status === 'error' ? (
                        <XCircle size={13} className="shrink-0 text-lx-danger" />
                      ) : (
                        <Circle size={12} className="shrink-0 text-lx-dim" />
                      )}
                      <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
                        {name}
                      </span>
                      {ready && (
                        <span className="shrink-0 text-[10px] text-lx-success">已就绪</span>
                      )}
                      <span className="shrink-0 font-mono text-[10px] tabular-nums text-lx-dim">
                        {size ? fmtBytes(size) : '—'}
                      </span>
                    </div>
                  )
                })}
              </div>
            </>
          )}
        </SectionPanel>

        <div className="flex flex-col gap-3.5">
          <SectionPanel
            title="磁盘"
            trailing={
              <StatusChip tone={freeTone}>
                {state ? `剩 ${fmtBytes(state.free_bytes)}` : '—'}
              </StatusChip>
            }
          >
            <SettingRow
              label="给产物留的余量（GB）"
              help="不够这一片 + 余量就排队等"
            >
              <input
                type="number"
                className="lx-input font-mono tabular-nums"
                min={0}
                step={1}
                value={reserveGb}
                onChange={(e) => setReserveGb(Math.max(0, Number(e.target.value) || 0))}
              />
            </SettingRow>
            <SettingRow
              label="多线程下载"
              help="分段并发拉取，更容易跑满线路"
            >
              <label className="flex cursor-pointer items-center gap-2 text-xs text-lx-secondary">
                <input
                  type="checkbox"
                  className="accent-lx-accent"
                  checked={multi}
                  onChange={(e) => setMulti(e.target.checked)}
                />
                启用（小文件 / 不支持 Range 时自动退回单连接）
              </label>
            </SettingRow>
            {multi && (
              <SettingRow
                label="并发线程数"
                help="4~8 条足够；再多只是抢同一份带宽"
              >
                <input
                  type="number"
                  className="lx-input font-mono tabular-nums"
                  min={2}
                  max={16}
                  value={conns}
                  onChange={(e) =>
                    setConns(Math.max(2, Math.min(16, Number(e.target.value) || 2)))
                  }
                />
              </SettingRow>
            )}
            {state && (
              <p className="px-1 text-[11px] leading-relaxed text-lx-dim">
                分片落点 <span className="font-mono">{state.cache_root}</span>，处理完即删
              </p>
            )}
          </SectionPanel>

          <SectionPanel
            title="网络代理"
            trailing={
              <StatusChip tone={proxy ? 'accent' : 'neutral'}>
                {proxy ? '走代理' : '直连'}
              </StatusChip>
            }
          >
            <SettingRow
              label="代理地址"
              help="全局生效；留空 = 直连"
            >
              <input
                className="lx-input font-mono text-xs"
                value={proxy}
                onChange={(e) => setProxy(e.target.value)}
                placeholder="http://127.0.0.1:7890"
                spellCheck={false}
                autoComplete="off"
              />
            </SettingRow>
            <div className="flex items-center gap-2 px-1">
              <button
                type="button"
                className="lx-btn-ghost px-2.5 py-1 text-[11px]"
                disabled={proxyBusy}
                onClick={() => void saveProxy()}
              >
                {proxyBusy ? '处理中…' : '保存'}
              </button>
              <button
                type="button"
                className="lx-btn-ghost px-2.5 py-1 text-[11px]"
                disabled={proxyBusy}
                onClick={() => void testProxy()}
              >
                测试连通
              </button>
              <span className="min-w-0 flex-1 truncate text-[10px] text-lx-dim">
                {proxyMsg ?? ''}
              </span>
            </div>
            <p className="px-1 text-[11px] leading-relaxed text-lx-dim">
              支持 http / https / socks5（socks5 需要{' '}
              <span className="font-mono">pip install socksio</span>）。
              已排队的任务沿用入队时的代理，改这里只影响之后的下载。
            </p>
          </SectionPanel>

          <SectionPanel
            title="队列"
            trailing={
              <div className="flex items-center gap-1.5">
                {state && state.items.length > 0 && (
                  <button
                    type="button"
                    className="lx-btn-ghost px-2 py-1 text-[11px]"
                    onClick={() => void clearDone()}
                  >
                    <Trash2 size={12} className="mr-1 inline" />
                    清空已结束
                  </button>
                )}
              </div>
            }
          >
            {state === null ? (
              <p className="px-1 text-xs text-lx-dim">读取队列状态…</p>
            ) : state.items.length === 0 ? (
              <p className="px-1 text-xs text-lx-dim">队列是空的。</p>
            ) : (
              <div className="flex max-h-[56vh] flex-col gap-2 overflow-auto">
                {state.items
                  .slice()
                  .reverse()
                  .map((item) => {
                    const tone = toneOf(item.status)
                    return (
                      <div
                        key={item.id}
                        className="rounded-lg border border-lx-line/60 px-2.5 py-2"
                      >
                        <div className="flex items-center gap-2">
                          <StatusChip tone={tone}>{STATUS_LABEL[item.status]}</StatusChip>
                          <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
                            {item.filename}
                          </span>
                          {(item.status === 'running' ||
                            item.status === 'queued') && (
                            <button
                              type="button"
                              className="lx-btn-ghost shrink-0 px-1.5 py-0.5 text-[10px]"
                              onClick={() => void api.downloadCancel(item.id)}
                              title="暂停（保留 .part，再下会续传）"
                            >
                              <Square size={10} className="mr-1 inline" />
                              暂停
                            </button>
                          )}
                        </div>
                        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-lx-line/60">
                          <div
                            className={
                              item.status === 'error'
                                ? 'h-full bg-lx-danger'
                                : item.status === 'running'
                                  ? 'h-full bg-lx-accent'
                                  : 'h-full bg-lx-success'
                            }
                            style={{ width: `${pct(item.done, item.size)}%` }}
                          />
                        </div>
                        <div className="mt-1 flex items-center justify-between gap-2 font-mono text-[10px] tabular-nums text-lx-dim">
                          <span>
                            {fmtBytes(item.done)}
                            {item.size ? ` / ${fmtBytes(item.size)}` : ''}
                            {item.size
                              ? ` · ${pct(item.done, item.size).toFixed(0)}%`
                              : ''}
                            {item.conns > 1 ? ` · ${item.conns} 连接` : ''}
                          </span>
                          <span>
                            {item.status === 'running' && item.speed > 0
                              ? `${fmtBytes(item.speed)}/s`
                              : ''}
                          </span>
                        </div>
                        {item.message && (
                          <p className="mt-1 text-[10px] leading-relaxed text-lx-dim">
                            {item.message}
                          </p>
                        )}
                      </div>
                    )
                  })}
              </div>
            )}
          </SectionPanel>
        </div>
      </div>
    </div>
  )
}
