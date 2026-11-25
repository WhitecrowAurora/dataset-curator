import { useCallback, useEffect, useState } from 'react'
import {
  CheckCircle2, Circle, CloudUpload, FolderOpen, Loader2, RefreshCw, Square, Trash2,
  XCircle,
} from 'lucide-react'
import {
  api, ApiError, type ArtifactInfo, type BlobAuth, type BlobStats, type BlobUploadState,
  type UploadState,
} from '../lib/api'
import { fmtBytes, fmtCount } from '../lib/format'
import { PageHeader, SectionPanel, SettingRow, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ChoiceGrid, ErrorBox } from '../components/ui/controls'
import { FolderPicker } from '../components/FolderPicker'
import { FieldNotice } from '../components/ui/FieldNotice'
import { failureAdvice, httpUrlError } from '../lib/guidance'

// 回传页：左边选目录、勾分片；右边填连接方式、点上传。
// 两条通道：
//   FTP        —— 对端只给账号时用；续传靠 SIZE + 尾部比对，粒度是文件。
//   分块 HTTP  —— 对端跑着本服务时用；块寻址，断点粒度是块、重复传是秒传。
// 后端各自逐文件记账，这里每秒轮询一次状态——搬文件用事件管道是浪费。

type Channel = 'ftp' | 'blob'
const CHANNELS = ['blob', 'ftp'] as const
const MODES = ['passive', 'active'] as const
const LAST_UPLOAD_KEY = 'tagger-last-upload'
const LAST_BLOB_KEY = 'tagger-last-blob-upload'

const FILE_STATUS_LABEL: Record<string, string> = {
  pending: '等待',
  uploading: '上传中',
  done: '完成',
  failed: '失败',
  stopped: '未传',
}

export function Upload() {
  const [channel, setChannel] = useState<Channel>('blob')
  const [dir, setDir] = useState('')
  const [picker, setPicker] = useState(false)
  const [artifacts, setArtifacts] = useState<ArtifactInfo[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [listNote, setListNote] = useState('')

  // FTP 侧
  const [host, setHost] = useState('')
  const [port, setPort] = useState(21)
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [remoteDir, setRemoteDir] = useState('/')
  const [mode, setMode] = useState<(typeof MODES)[number]>('passive')

  // 分块侧
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [blobRemote, setBlobRemote] = useState('')
  const [auth, setAuth] = useState<BlobAuth | null>(null)

  const [upload, setUpload] = useState<UploadState | null>(null)
  const [blobJob, setBlobJob] = useState<BlobUploadState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 本机仓库占用 + 定时清扫状态。只有本机（或带令牌）才查得到，查不到就整块隐藏。
  const [stats, setStats] = useState<BlobStats | null>(null)
  const [gcBusy, setGcBusy] = useState(false)

  const active = channel === 'blob' ? blobJob : upload
  const running =
    active !== null && (active.status === 'pending' || active.status === 'running')

  // 目录一变就拉分片清单（含每片的行数 = 处理好的图片数）
  useEffect(() => {
    if (!dir.trim()) {
      setArtifacts(null)
      setSelected(new Set())
      setListNote('')
      return
    }
    let alive = true
    // 分块通道认 .tar/.zip（封包产物）；FTP 后端只收 parquet/jsonl
    const loader =
      channel === 'blob' ? api.blobUploadFiles(dir) : api.artifacts(dir)
    loader
      .then((res) => {
        if (!alive) return
        setArtifacts(res.files)
        setSelected(new Set())
        setListNote('')
      })
      .catch((err) => {
        if (!alive) return
        setArtifacts(null)
        setListNote(err instanceof Error ? err.message : String(err))
      })
    return () => {
      alive = false
    }
  }, [dir, channel])

  // 这里只能读取本机接收端的鉴权设置，不能据此判断任意远端是否需要令牌。
  useEffect(() => {
    api.blobAuth().then(setAuth).catch(() => setAuth(null))
  }, [])

  const refreshStats = useCallback(() => {
    api
      .blobStats()
      .then(setStats)
      .catch(() => setStats(null))
  }, [])

  // 仓库占用随上传在变，跟着状态面板一起刷；没有上传时半分钟拉一次就够。
  useEffect(() => {
    refreshStats()
    const timer = window.setInterval(refreshStats, running ? 5000 : 30000)
    return () => window.clearInterval(timer)
  }, [refreshStats, running])

  async function gcNow() {
    setGcBusy(true)
    try {
      await api.blobGcRunNow()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setGcBusy(false)
      refreshStats()
    }
  }

  // 刷新页面后重新接上还在跑的回传
  useEffect(() => {
    const saved = (() => {
      try {
        return localStorage.getItem(LAST_UPLOAD_KEY)
      } catch {
        return null
      }
    })()
    if (saved) {
      api
        .upload(saved)
        .then((st) => {
          if (st.status === 'pending' || st.status === 'running') {
            setChannel('ftp')
            setUpload(st)
          } else localStorage.removeItem(LAST_UPLOAD_KEY)
        })
        .catch(() => localStorage.removeItem(LAST_UPLOAD_KEY))
    }
    const savedBlob = (() => {
      try {
        return localStorage.getItem(LAST_BLOB_KEY)
      } catch {
        return null
      }
    })()
    if (savedBlob) {
      api
        .blobUpload(savedBlob)
        .then((st) => {
          if (st.status === 'pending' || st.status === 'running') setBlobJob(st)
          else localStorage.removeItem(LAST_BLOB_KEY)
        })
        .catch(() => localStorage.removeItem(LAST_BLOB_KEY))
    }
  }, [])

  // 进行中的回传每秒轮询
  useEffect(() => {
    if (!upload || (upload.status !== 'pending' && upload.status !== 'running')) return
    const timer = window.setInterval(() => {
      api
        .upload(upload.id)
        .then((st) => {
          setUpload(st)
          if (st.status !== 'pending' && st.status !== 'running') {
            try {
              localStorage.removeItem(LAST_UPLOAD_KEY)
            } catch {
              // 忽略
            }
          }
        })
        .catch(() => undefined)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [upload])

  useEffect(() => {
    if (!blobJob || (blobJob.status !== 'pending' && blobJob.status !== 'running')) return
    const timer = window.setInterval(() => {
      api
        .blobUpload(blobJob.id)
        .then((st) => {
          setBlobJob(st)
          if (st.status !== 'pending' && st.status !== 'running') {
            try {
              localStorage.removeItem(LAST_BLOB_KEY)
            } catch {
              // 忽略
            }
          }
        })
        .catch(() => undefined)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [blobJob])

  function toggle(name: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const allSelected = artifacts !== null && artifacts.length > 0 && selected.size === artifacts.length
  const dirNotice = !dir.trim() ? '请先选择存放打标产物的目录。' : listNote
    ? `${listNote} 请检查目录是否存在且可读取。` : ''
  const urlNotice = channel === 'blob' ? httpUrlError(url, '回传地址') : ''
  const uploadFailure = active?.status === 'failed' || (!running && (active?.failed ?? 0) > 0)
  const uploadError = active?.error || active?.files.find((f) => f.status === 'failed')?.error || '部分产物上传失败'

  function toggleAll() {
    if (!artifacts) return
    setSelected(allSelected ? new Set() : new Set(artifacts.map((f) => f.name)))
  }

  const selRows =
    artifacts?.filter((f) => selected.has(f.name)).reduce((acc, f) => acc + (f.rows ?? 0), 0) ?? 0
  const selBytes =
    artifacts?.filter((f) => selected.has(f.name)).reduce((acc, f) => acc + f.bytes, 0) ?? 0
  const canStart =
    !running &&
    selected.size > 0 &&
    !busy &&
    (channel === 'blob' ? url.trim() !== '' : host.trim() !== '')

  const remember = useCallback((key: string, id: string) => {
    try {
      localStorage.setItem(key, id)
    } catch {
      // 存不了就只在内存里记着
    }
  }, [])

  async function start() {
    setBusy(true)
    setError(null)
    try {
      if (channel === 'blob') {
        const st = await api.blobUploadStart({
          dir,
          files: [...selected],
          url: url.trim(),
          token: token.trim(),
          remote_dir: blobRemote.trim(),
        })
        setBlobJob(st)
        remember(LAST_BLOB_KEY, st.id)
      } else {
        const st = await api.uploadStart(dir, [...selected], {
          host: host.trim(),
          port,
          user,
          password,
          remote_dir: remoteDir || '/',
          passive: mode === 'passive',
        })
        setUpload(st)
        remember(LAST_UPLOAD_KEY, st.id)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    try {
      if (channel === 'blob') {
        if (!blobJob) return
        const res = await api.blobUploadStop(blobJob.id)
        if (res.snapshot) setBlobJob(res.snapshot)
      } else {
        if (!upload) return
        const res = await api.uploadStop(upload.id)
        if (res.snapshot) setUpload(res.snapshot)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  function fileIcon(status: string) {
    if (status === 'done') return <CheckCircle2 size={14} className="shrink-0 text-lx-success" />
    if (status === 'failed') return <XCircle size={14} className="shrink-0 text-lx-danger" />
    if (status === 'uploading')
      return <Loader2 size={14} className="shrink-0 animate-spin text-lx-accent" />
    if (status === 'stopped') return <Square size={12} className="shrink-0 text-lx-dim" />
    return <Circle size={12} className="shrink-0 text-lx-dim" />
  }

  const overallTone: StatusTone =
    active === null
      ? 'neutral'
      : active.status === 'done'
        ? 'success'
        : active.status === 'failed'
          ? 'danger'
          : active.status === 'stopped'
            ? 'warning'
            : 'accent'

  const fileStates =
    channel === 'blob'
      ? (blobJob?.files ?? []).map((f) => ({
          name: f.name,
          status: f.status,
          note:
            f.status === 'uploading'
              ? `${f.chunks ? `${f.uploaded_chunks}/${f.chunks} 块` : '切块中'}`
              : `${FILE_STATUS_LABEL[f.status] ?? f.status}`
                + (f.skipped_chunks ? ` · 秒传 ${f.skipped_chunks} 块` : '')
                + (f.sent ? ` · ${fmtBytes(f.sent)}` : '')
                // 服务端在 commit 时就校验好了，直接把结论摆出来：
                // 传完还要再去别的页面确认一遍，等于没校验
                + (f.verify
                  ? f.verify.ok === false
                    ? ' · 校验未通过'
                    : ' · 校验通过'
                  : ''),
        }))
      : (upload?.files ?? []).map((f) => ({
          name: f.name,
          status: f.status,
          note:
            `${FILE_STATUS_LABEL[f.status] ?? f.status}`
            + (f.resumed ? ' · 续传' : '')
            + (f.sent > 0 ? ` · ${fmtBytes(f.sent)}` : ''),
        }))

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<CloudUpload size={17} />}
        eyebrow="回传"
        title="产物回传"
        actions={
          running ? (
            <button type="button" className="lx-btn-ghost" onClick={() => void stop()}>
              <Square size={14} className="mr-1.5 inline" />
              停止上传
            </button>
          ) : (
            <button
              type="button"
              className="lx-btn-primary lx-launch-glow inline-flex items-center gap-1.5"
              disabled={!canStart}
              onClick={() => void start()}
            >
              <CloudUpload size={14} />
              {busy ? '创建中…' : selected.size ? `上传 ${selected.size} 个分片` : '上传分片'}
            </button>
          )
        }
      />

      {error && <ErrorBox error={error} title="操作失败" nextStep={failureAdvice(error, 'upload')} />}

      <div className="grid gap-3.5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <SectionPanel
          title="本地分片"
          trailing={
            artifacts && artifacts.length > 0 ? (
              <StatusChip tone={selected.size ? 'accent' : 'neutral'}>
                已选 {selected.size} / {artifacts.length}
              </StatusChip>
            ) : undefined
          }
        >
          <SettingRow label="产物目录" notice={dirNotice}>
            <div className="flex gap-2">
              <input
                className="lx-input font-mono text-xs"
                value={dir}
                aria-label="产物目录"
                aria-invalid={Boolean(dirNotice)}
                onChange={(e) => setDir(e.target.value)}
                placeholder="work\out 或 D:\shards\out"
                spellCheck={false}
              />
              <button
                type="button"
                className="lx-btn-ghost shrink-0 px-3"
                onClick={() => setPicker(true)}
              >
                <FolderOpen size={14} className="mr-1 inline" />
                浏览
              </button>
            </div>
          </SettingRow>

          {artifacts !== null && artifacts.length > 0 && (
            <>
              <div className="flex items-center justify-between gap-2 px-1 pt-1">
                <label className="flex cursor-pointer items-center gap-2 text-xs text-lx-secondary">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleAll}
                    className="accent-lx-accent"
                  />
                  全选
                </label>
                <span className="text-[11px] tabular-nums text-lx-muted">
                  共 {fmtCount(artifacts.reduce((a, f) => a + (f.rows ?? 0), 0))} 行 ·{' '}
                  {fmtBytes(artifacts.reduce((a, f) => a + f.bytes, 0))}
                </span>
              </div>

              <div className="lx-sheet-inset max-h-[26rem] space-y-0.5 overflow-y-auto p-2">
                {artifacts.map((f) => {
                  const checked = selected.has(f.name)
                  const st = fileStates.find((x) => x.name === f.name)
                  return (
                    <label
                      key={f.name}
                      className={`flex cursor-pointer items-center gap-2.5 rounded-lx-sm px-2 py-1.5 text-xs transition-colors hover:bg-lx-accent-subtle ${
                        checked ? 'bg-lx-accent-subtle' : ''
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(f.name)}
                        className="accent-lx-accent"
                      />
                      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-lx-secondary" title={f.name}>
                        {f.name}
                      </span>
                      <span className="shrink-0 tabular-nums text-[11px] text-lx-muted">
                        {f.rows === null ? '—' : `${fmtCount(f.rows)} 张`}
                      </span>
                      <span className="w-20 shrink-0 text-right tabular-nums text-[11px] text-lx-dim">
                        {fmtBytes(f.bytes)}
                      </span>
                      {st && fileIcon(st.status)}
                    </label>
                  )
                })}
              </div>

              <FieldNotice message={selected.size === 0 ? '请勾选要上传的产物，或点击全选。' : ''} />
              {selected.size > 0 && (
                <p className="px-1 text-[11px] text-lx-muted">
                  已选 {selected.size} 个分片 · {fmtCount(selRows)} 张处理好的图片 · {fmtBytes(selBytes)}
                </p>
              )}
            </>
          )}

          {artifacts !== null && artifacts.length === 0 && (
            <p className="px-1 text-xs text-lx-muted">
              {listNote || (channel === 'blob'
                ? '这个目录里没有 .parquet / .jsonl / .tar / .zip 产物。'
                : '这个目录里没有 .parquet / .jsonl 产物。')}
            </p>
          )}
        </SectionPanel>

        <div className="flex min-w-0 flex-col gap-3.5">
          <SectionPanel title="连接方式">
            <SettingRow label="通道">
              <ChoiceGrid
                options={CHANNELS}
                value={channel}
                onChange={(v) => {
                  setChannel(v as Channel)
                  setError(null)
                }}
                className="grid-cols-2"
                size="sm"
                renderLabel={(v) => (v === 'blob' ? '分块 HTTP' : 'FTP')}
                ariaLabel="回传通道"
              />
            </SettingRow>

            {channel === 'blob' ? (
              <>
                <SettingRow label="回传地址" notice={urlNotice}>
                  <input
                    className="lx-input font-mono text-xs"
                    value={url}
                    aria-label="回传地址"
                    aria-invalid={Boolean(urlNotice)}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="http://192.168.1.10:8000"
                    spellCheck={false}
                  />
                </SettingRow>
                <SettingRow
                  label="令牌"
                  help={
                    auth?.required
                      ? `本机接收端已开启鉴权${auth.token_hint ? `（${auth.token_hint}）` : ''}`
                      : undefined
                  }
                >
                  <input
                    className="lx-input font-mono text-xs"
                    type="password"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    placeholder="接收端要求鉴权时填写"
                    autoComplete="off"
                  />
                </SettingRow>
                <SettingRow label="远端子目录">
                  <input
                    className="lx-input font-mono text-xs"
                    value={blobRemote}
                    onChange={(e) => setBlobRemote(e.target.value)}
                    placeholder="batch-2026-11（留空放仓库根）"
                    spellCheck={false}
                  />
                </SettingRow>
              </>
            ) : (
              <>
                <SettingRow label="主机" notice={!host.trim() ? '请填写 FTP 服务器的 IP 或域名。' : ''}>
                  <input
                    className="lx-input font-mono text-xs"
                    value={host}
                    onChange={(e) => setHost(e.target.value)}
                    placeholder="192.168.1.10"
                    spellCheck={false}
                  />
                </SettingRow>
                <SettingRow label="端口">
                  <input
                    type="number"
                    className="lx-input font-mono tabular-nums"
                    min={1}
                    max={65535}
                    value={port}
                    onChange={(e) => setPort(Math.min(65535, Math.max(1, Number(e.target.value) || 21)))}
                  />
                </SettingRow>
                <SettingRow label="用户名">
                  <input
                    className="lx-input font-mono text-xs"
                    value={user}
                    onChange={(e) => setUser(e.target.value)}
                    placeholder="anonymous"
                    spellCheck={false}
                    autoComplete="off"
                  />
                </SettingRow>
                <SettingRow label="密码">
                  <input
                    className="lx-input font-mono text-xs"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete="new-password"
                  />
                </SettingRow>
                <SettingRow label="远端路径">
                  <input
                    className="lx-input font-mono text-xs"
                    value={remoteDir}
                    onChange={(e) => setRemoteDir(e.target.value)}
                    placeholder="/"
                    spellCheck={false}
                  />
                </SettingRow>
                <SettingRow label="传输模式" help="NAT / 防火墙后建议被动">
                  <ChoiceGrid
                    options={MODES}
                    value={mode}
                    onChange={setMode}
                    className="grid-cols-2"
                    size="sm"
                    renderLabel={(v) => (v === 'passive' ? '被动' : '主动')}
                    ariaLabel="传输模式"
                  />
                </SettingRow>
              </>
            )}
          </SectionPanel>

          {active && (
            <SectionPanel
              title="上传状态"
              trailing={<StatusChip tone={overallTone}>
                {active.status === 'running'
                  ? `${active.done + active.failed}/${active.total}`
                  : active.status === 'done'
                    ? '全部完成'
                    : active.status === 'failed'
                      ? '有失败'
                      : active.status === 'stopped'
                        ? '已停止'
                        : '等待'}
              </StatusChip>}
            >
              <div className="space-y-1">
                {fileStates.map((f) => (
                  <div key={f.name} className="flex items-center gap-2 text-[11px]">
                    {fileIcon(f.status)}
                    <span className="min-w-0 flex-1 truncate font-mono text-lx-secondary" title={f.name}>
                      {f.name}
                    </span>
                    <span className="shrink-0 tabular-nums text-lx-dim">{f.note}</span>
                  </div>
                ))}
              </div>
              {uploadFailure && <ErrorBox title="上传失败" error={uploadError}
                nextStep={`本机产物保留。${failureAdvice(uploadError, 'upload')} 在左侧勾选失败文件后，点击顶部上传按钮即可补传，不会重新打标。`} />}
              {blobJob && channel === 'blob' && blobJob.skipped_chunks > 0 && (
                <p className="pt-1.5 text-[11px] text-lx-muted">
                  秒传 {fmtCount(blobJob.skipped_chunks)} 块（对端已有，未占带宽）
                </p>
              )}
              {/* 校验的具体原因只说一次，放在汇总处；逐文件只是打标 */}
              {blobJob && channel === 'blob' && (() => {
                const bad = blobJob.files.filter((f) => f.verify?.ok === false)
                if (!bad.length) return null
                return (
                  <div className="mt-1.5 space-y-1 rounded-lx border border-lx-warning/40 bg-lx-warning/5 px-2.5 py-2 text-[11px] text-lx-warning">
                    <p>{bad.length} 个分片校验没通过，已照常落盘（不撤提交，撤了也只是重传同样的数据）：</p>
                    {bad.slice(0, 3).map((f) => (
                      <p key={f.name} className="font-mono text-[10px]">
                        {f.name}：{(f.verify?.problems ?? []).slice(0, 2).join('；')}
                      </p>
                    ))}
                  </div>
                )
              })()}
              {running && (
                <p className="flex items-center gap-1.5 pt-2 text-[11px] text-lx-muted">
                  <RefreshCw size={11} className="animate-spin" />
                  {channel === 'blob'
                    ? `正在传到 ${blobJob?.target.url ?? ''}${blobJob?.target.remote_dir ? `/${blobJob.target.remote_dir}` : ''}`
                    : `正在传到 ${upload?.ftp.host}:${upload?.ftp.port}${upload?.ftp.remote_dir ?? ''}`}
                  {channel === 'ftp' && ' · 当前文件传完后停止按钮才生效'}
                </p>
              )}
            </SectionPanel>
          )}

          {/* 只在本机能查到仓库占用；远程/无令牌时整块不显示，不留空壳 */}
          {stats && (
            <SectionPanel
              title="仓库占用"
              trailing={
                <StatusChip tone={stats.janitor?.last_error ? 'warning' : 'neutral'}>
                  {fmtBytes(stats.bytes)} / {fmtCount(stats.chunks)} 块
                </StatusChip>
              }
            >
              <div className="space-y-1 text-[11px] text-lx-muted">
                <div className="flex items-center justify-between gap-2">
                  <span>已提交成品</span>
                  <span className="tabular-nums text-lx-secondary">
                    {fmtCount(stats.files)} 个
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span>被引用的块</span>
                  <span className="tabular-nums text-lx-secondary">
                    {fmtCount(stats.referenced)} / {fmtCount(stats.chunks)}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span>定时清扫</span>
                  <span className="tabular-nums text-lx-secondary">
                    {stats.janitor?.enabled
                      ? `每 ${stats.janitor.interval_hours} 小时`
                      : '已关闭'}
                  </span>
                </div>
                {stats.janitor && stats.janitor.enabled && (
                  <div className="flex items-center justify-between gap-2">
                    <span>上次清扫</span>
                    <span className="tabular-nums text-lx-secondary">
                      {stats.janitor.last_run
                        ? `${Math.round((Date.now() / 1000 - stats.janitor.last_run) / 60)} 分钟前`
                        : '还没跑过'}
                      {stats.janitor.last_result?.orphans
                        ? ` · 删 ${fmtCount(stats.janitor.last_result.orphans)} 块`
                        : ''}
                    </span>
                  </div>
                )}
                {stats.janitor?.last_error && (
                  <p className="pt-1 text-lx-warning">
                    上次清扫出错：{stats.janitor.last_error}
                  </p>
                )}
                <p className="pt-0.5 font-mono text-[10px] text-lx-dim" title={stats.root}>
                  {stats.root}
                </p>
              </div>
              <div className="pt-2">
                <button
                  type="button"
                  className="lx-btn-ghost w-full justify-center text-[11px]"
                  disabled={gcBusy || running}
                  onClick={() => void gcNow()}
                  title="按定时任务的口径立即清一次孤儿块；正在引用的块不受影响"
                >
                  {gcBusy ? (
                    <Loader2 size={12} className="mr-1.5 inline animate-spin" />
                  ) : (
                    <Trash2 size={12} className="mr-1.5 inline" />
                  )}
                  立即清理孤儿块
                </button>
              </div>
            </SectionPanel>
          )}
        </div>
      </div>

      <FolderPicker
        open={picker}
        start={dir}
        onClose={() => setPicker(false)}
        onPick={(path) => {
          setDir(path)
          setPicker(false)
        }}
      />
    </div>
  )
}
