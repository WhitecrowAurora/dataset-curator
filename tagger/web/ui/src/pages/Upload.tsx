import { useEffect, useState } from 'react'
import { CheckCircle2, Circle, CloudUpload, FolderOpen, Loader2, RefreshCw, Square, XCircle } from 'lucide-react'
import { api, ApiError, type ArtifactInfo, type UploadState } from '../lib/api'
import { fmtBytes, fmtCount } from '../lib/format'
import { PageHeader, SectionPanel, SettingRow, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ChoiceGrid, ErrorBox } from '../components/ui/controls'
import { FolderPicker } from '../components/FolderPicker'

// 回传页：左边选目录、勾分片；右边填 FTP、点上传。
// 后端每个文件单独记账（成功/失败/续传），这里每秒轮询一次状态——
// 上传是几 MB/s 的搬运活，轮询比 WS 事件管道更省事也更不容易断。

const MODES = ['passive', 'active'] as const
const LAST_UPLOAD_KEY = 'tagger-last-upload'

const FILE_STATUS_LABEL: Record<string, string> = {
  pending: '等待',
  uploading: '上传中',
  done: '完成',
  failed: '失败',
  stopped: '未传',
}

export function Upload() {
  const [dir, setDir] = useState('')
  const [picker, setPicker] = useState(false)
  const [artifacts, setArtifacts] = useState<ArtifactInfo[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [host, setHost] = useState('')
  const [port, setPort] = useState(21)
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [remoteDir, setRemoteDir] = useState('/')
  const [mode, setMode] = useState<(typeof MODES)[number]>('passive')

  const [upload, setUpload] = useState<UploadState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 目录一变就拉分片清单（含每片的行数 = 处理好的图片数）
  useEffect(() => {
    if (!dir.trim()) {
      setArtifacts(null)
      setSelected(new Set())
      return
    }
    let alive = true
    api
      .artifacts(dir)
      .then((res) => {
        if (!alive) return
        setArtifacts(res.files)
        setSelected(new Set())
      })
      .catch((err) => {
        if (!alive) return
        setArtifacts(null)
        setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      alive = false
    }
  }, [dir])

  // 刷新页面后重新接上还在跑的回传
  useEffect(() => {
    const saved = (() => {
      try {
        return localStorage.getItem(LAST_UPLOAD_KEY)
      } catch {
        return null
      }
    })()
    if (!saved) return
    api
      .upload(saved)
      .then((st) => {
        if (st.status === 'pending' || st.status === 'running') setUpload(st)
        else localStorage.removeItem(LAST_UPLOAD_KEY)
      })
      .catch(() => localStorage.removeItem(LAST_UPLOAD_KEY))
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

  function toggle(name: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const allSelected = artifacts !== null && artifacts.length > 0 && selected.size === artifacts.length

  function toggleAll() {
    if (!artifacts) return
    setSelected(allSelected ? new Set() : new Set(artifacts.map((f) => f.name)))
  }

  const running = upload !== null && (upload.status === 'pending' || upload.status === 'running')
  const selRows =
    artifacts?.filter((f) => selected.has(f.name)).reduce((acc, f) => acc + (f.rows ?? 0), 0) ?? 0
  const selBytes =
    artifacts?.filter((f) => selected.has(f.name)).reduce((acc, f) => acc + f.bytes, 0) ?? 0
  const canStart = !running && selected.size > 0 && host.trim() !== '' && !busy

  async function start() {
    setBusy(true)
    setError(null)
    try {
      const st = await api.uploadStart(dir, [...selected], {
        host: host.trim(),
        port,
        user,
        password,
        remote_dir: remoteDir || '/',
        passive: mode === 'passive',
      })
      setUpload(st)
      try {
        localStorage.setItem(LAST_UPLOAD_KEY, st.id)
      } catch {
        // 存不了就只在内存里记着
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    if (!upload) return
    try {
      const res = await api.uploadStop(upload.id)
      if (res.snapshot) setUpload(res.snapshot as UploadState)
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
    upload === null
      ? 'neutral'
      : upload.status === 'done'
        ? 'success'
        : upload.status === 'failed'
          ? 'danger'
          : upload.status === 'stopped'
            ? 'warning'
            : 'accent'

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<CloudUpload size={17} />}
        eyebrow="回传"
        title="上传处理好的分片"
        description="勾选本地处理好的 parquet 分片，用 FTP 传到汇总机。传的是 .part 改名，中途断线重传会自动续。"
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

      {error && <ErrorBox error={error} title="操作失败" />}

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
          <SettingRow label="产物目录" help="VLM 任务的输出目录（里面的 pack-*.parquet）">
            <div className="flex gap-2">
              <input
                className="lx-input font-mono text-xs"
                value={dir}
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
                  const st = upload?.files.find((x) => x.name === f.name)
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

              {selected.size > 0 && (
                <p className="px-1 text-[11px] text-lx-muted">
                  已选 {selected.size} 个分片 · {fmtCount(selRows)} 张处理好的图片 · {fmtBytes(selBytes)}
                </p>
              )}
            </>
          )}

          {artifacts !== null && artifacts.length === 0 && (
            <p className="px-1 text-xs text-lx-muted">这个目录里没有 .parquet 文件。</p>
          )}
          {artifacts === null && !dir && (
            <p className="px-1 text-xs text-lx-muted">先选一个产物目录。</p>
          )}
        </SectionPanel>

        <div className="flex min-w-0 flex-col gap-3.5">
          <SectionPanel title="FTP 连接">
            <SettingRow label="主机">
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

            <SettingRow label="密码" help="只在这次上传里用，不落盘、不进日志">
              <input
                className="lx-input font-mono text-xs"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
              />
            </SettingRow>

            <SettingRow label="远端路径" help="不存在会逐级创建">
              <input
                className="lx-input font-mono text-xs"
                value={remoteDir}
                onChange={(e) => setRemoteDir(e.target.value)}
                placeholder="/"
                spellCheck={false}
              />
            </SettingRow>

            <SettingRow label="传输模式" help="拿不准就选被动（防火墙/NAT 后更稳）">
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
          </SectionPanel>

          {upload && (
            <SectionPanel
              title="上传状态"
              trailing={<StatusChip tone={overallTone}>
                {upload.status === 'running'
                  ? `${upload.done + upload.failed}/${upload.total}`
                  : upload.status === 'done'
                    ? '全部完成'
                    : upload.status === 'failed'
                      ? '有失败'
                      : upload.status === 'stopped'
                        ? '已停止'
                        : '等待'}
              </StatusChip>}
            >
              <div className="space-y-1">
                {upload.files.map((f) => (
                  <div key={f.name} className="flex items-center gap-2 text-[11px]">
                    {fileIcon(f.status)}
                    <span className="min-w-0 flex-1 truncate font-mono text-lx-secondary" title={f.name}>
                      {f.name}
                    </span>
                    <span className="shrink-0 tabular-nums text-lx-dim">
                      {FILE_STATUS_LABEL[f.status] ?? f.status}
                      {f.resumed ? ' · 续传' : ''}
                      {f.sent > 0 ? ` · ${fmtBytes(f.sent)}` : ''}
                    </span>
                  </div>
                ))}
              </div>
              {upload.error && <p className="pt-1.5 text-[11px] text-lx-danger">{upload.error}</p>}
              {(upload.status === 'running' || upload.status === 'pending') && (
                <p className="flex items-center gap-1.5 pt-2 text-[11px] text-lx-muted">
                  <RefreshCw size={11} className="animate-spin" />
                  正在传到 {upload.ftp.host}:{upload.ftp.port}
                  {upload.ftp.remote_dir} · 当前文件传完后停止按钮才生效
                </p>
              )}
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
