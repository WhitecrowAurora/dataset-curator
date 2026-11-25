/**
 * 本机模型准备、主动领片与被动托管。任务和租约仍由 worker 处理；
 * 打开页面只读取状态，不领片。托管期间本机执行配置锁定。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CircleDot,
  FolderOpen,
  KeyRound,
  Loader2,
  Play,
  RefreshCw,
  Square,
} from 'lucide-react'
import { api, ApiError, type CenterState, type CenterExecution } from '../lib/api'
import { DEFAULT_EXECUTION, ExecutionSettings, ExecutionCredentials, ExecutionDownloads, ExecutionLifecycle } from '../components/center/ExecutionSettings'
import { AdvancedSettings, FormSections } from '../components/ui/FormSections'
import { ExecutionStatus } from '../components/center/ExecutionStatus'
import { ExecutionMonitor } from '../components/center/ExecutionMonitor'
import { CenterOverview } from '../components/center/CenterOverview'
import { LeasePanel } from '../components/center/LeasePanel'
import { ConnectionNetwork, type ProxyMode } from '../components/center/ConnectionNetwork'
import { DownloadNetwork } from '../components/center/DownloadNetwork'
import { OfflineTasks } from '../components/center/OfflineTasks'
import { DispatchPlans } from '../components/center/DispatchPlans'
import { HistoryArtifacts } from '../components/center/HistoryArtifacts'
import { ChoiceGrid, ErrorBox, ToggleRow } from '../components/ui/controls'
import { failureAdvice, httpUrlError, localServiceUrlError, latestCenterFailure } from '../lib/guidance'
import { useFolderPicker } from '../components/FolderPicker'
import {
  PageHeader,
  SectionPanel,
  SettingRow,
  StatusChip,
} from '../components/ui/launcher'

const MODES = ['both', 'tag', 'nl']
const MODE_LABEL: Record<string, string> = { both: 'TAG + NL', tag: 'TAG', nl: 'NL' }

/**
 * 派工倾向。**不是**"产出什么"，而是"服务端把哪种片排在我前面"——
 * 产出什么由上方的「产出模式」决定，两者是正交的：
 *   只出 tag + 优先 NL  = 我只会做 tag，却想先拿缺 NL 的片 → 接不到能交的活
 * 所以下面按产出模式筛选项，把这类矛盾直接藏起来而不是让它报错。
 */
const PREF_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: '不挑' },
  { value: 'pref-tag', label: '优先 TAG' },
  { value: 'pref-nl', label: '优先 NL' },
  { value: 'tag', label: '仅 TAG' },
  { value: 'nl', label: '仅 NL' },
]

/** 某个倾向是否与产出模式相容：倾向指向的侧必须真能产出来。 */
function prefFitsMode(pref: string, mode: string): boolean {
  if (mode === 'both' || !pref) return true
  return pref.endsWith(mode)
}

function fmtUptime(sec: number): string {
  if (!sec || sec < 0) return '—'
  if (sec < 60) return `${sec.toFixed(0)}s`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m}m ${Math.round(sec % 60)}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

export function Center({ onLinked }: { onLinked?: () => void }) {
  const [state, setState] = useState<CenterState | null>(null)
  const [execution, setExecution] = useState<CenterExecution>(DEFAULT_EXECUTION)
  const [hfToken, setHfToken] = useState('')
  const [rememberHf, setRememberHf] = useState(false)
  // 两种错误分开存：
  // pollError 是"这一轮没拉到数据"，下一轮成功就该消失；
  // actionError 是"你刚点的那个按钮失败了"，得留着让人看清为什么——
  // 混成一个的话，连接失败的红条会在 5 秒后被轮询悄悄抹掉。
  const [pollError, setPollError] = useState('')
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState('')
  const [serviceLog, setServiceLog] = useState<{ path: string; tail: string[] } | null>(null)
  const [logLocation, setLogLocation] = useState('')
  const [section, setSection] = useState('work')
  // 身份三项是**草稿**：存下来的值只在第一次拉到时灌进来。否则用户改了一半
  // 又被轮询覆盖回去，会以为输入框坏了。
  const [server, setServer] = useState('')
  const [userId, setUserId] = useState('')
  const [secret, setSecret] = useState('')
  // 运行参数同样是草稿：只在第一次拉到时灌入，之后不跟轮询走（见上）。
  const [mode, setMode] = useState('both')
  // 派工倾向跟着 mode 走：把 mode 切成单侧时，矛盾的值必须当场清掉，
  // 否则表单里留着"只出 tag + 只接 NL"，看起来能点、后端也会 400。
  const [partsPref, setPartsPref] = useState('')
  const [tagUrl, setTagUrl] = useState('')
  const [outDir, setOutDir] = useState('')
  // 存成字符串而不是数字：输入框清空时得能表达"没填"，转成 NaN 再回填会变成 0
  const [maxPkgs, setMaxPkgs] = useState('0')
  // 下载口径：连接数同样存字符串（清空后按默认 4 处理），选优是开关
  const [dlConns, setDlConns] = useState('4')
  const [cdnProbe, setCdnProbe] = useState(true)
  const [proxyMode, setProxyMode] = useState<ProxyMode>('auto')
  const [proxyUrl, setProxyUrl] = useState('')
  const [downloadProxyMode, setDownloadProxyMode] = useState<ProxyMode | 'center'>('auto')
  const [downloadProxyUrl, setDownloadProxyUrl] = useState('')
  const [autoUpload, setAutoUpload] = useState(true)
  const [autoStartOnConnect, setAutoStartOnConnect] = useState(true)
  const seeded = useRef(false)
  const folder = useFolderPicker()

  const pull = useCallback(async () => {
    try {
      const s = await api.center()
      setState(s)
      setPollError('')
      if (!seeded.current) {
        seeded.current = true
        setServer(s.config.server)
        setProxyMode(s.config.proxy_mode ?? 'auto')
        setProxyUrl(s.config.proxy_url ?? '')
        setDownloadProxyMode(s.config.download_proxy_mode ?? 'auto')
        setDownloadProxyUrl(s.config.download_proxy_url ?? '')
        setExecution(Object.fromEntries(Object.keys(DEFAULT_EXECUTION).map((key) =>
          [key, s.config[key as keyof CenterExecution] ?? DEFAULT_EXECUTION[key as keyof CenterExecution]])) as CenterExecution)
        setRememberHf(s.credentials?.remember_hf ?? false)
        setUserId(s.config.user_id)
        setMode(s.config.mode || 'both')
        setPartsPref(s.config.parts_pref ?? '')
        setTagUrl(s.config.tag_url)
        setOutDir(s.config.out_dir)
        setMaxPkgs(String(s.config.max_packages ?? 0))
        setDlConns(String(s.config.download_conns ?? 4))
        setCdnProbe(s.config.cdn_probe ?? true)
        setAutoUpload(s.config.auto_upload ?? true)
        setAutoStartOnConnect(s.config.auto_start_on_connect ?? true)
      }
      if (s.runtime?.active && s.runtime.hosting_mode === 'passive') {
        setExecution(Object.fromEntries(Object.keys(DEFAULT_EXECUTION).map((key) =>
          [key, s.config[key as keyof CenterExecution] ?? DEFAULT_EXECUTION[key as keyof CenterExecution]])) as CenterExecution)
        setMode(s.config.mode)
        setPartsPref(s.config.parts_pref ?? '')
        setTagUrl(s.config.tag_url)
      }
    } catch (e) {
      setPollError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  // 用 setTimeout 串行轮询而不是 setInterval：后端取 board 时要连一次调度中心
  // （最长 6s），定时器会在慢网下叠起来，把线程池全占住。
  useEffect(() => {
    let alive = true
    const tick = async () => {
      await pull()
      if (alive) t = window.setTimeout(tick, 5000)
    }
    let t = window.setTimeout(tick, 0)
    return () => {
      alive = false
      window.clearTimeout(t)
    }
  }, [pull])

  const act = async (key: string, fn: () => Promise<CenterState>) => {
    setBusy(key)
    setActionError('')
    try {
      const s = await fn()
      setState(s)
      setRememberHf(s.credentials?.remember_hf ?? false)
      if (key === 'start' || key === 'save' || key === 'clear-token') setHfToken('')
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  const running = Boolean(state?.runtime?.active || state?.runtime?.preparing || state?.running || state?.history?.running)
  const releasing = Boolean(state?.release?.running)
  const ident = state?.identity ?? {}
  const serverNotice = seeded.current ? httpUrlError(server, '调度中心地址') : ''
  const idNotice = seeded.current && !userId.trim() ? '请填写用户 ID，调度中心按这个 ID 记录产出。' : ''
  const proxyNotice = proxyMode === 'custom' ? httpUrlError(proxyUrl, '代理地址') : ''
  const downloadProxyNotice = downloadProxyMode === 'custom' ? httpUrlError(downloadProxyUrl, '下载代理地址') : ''
  const serviceNotice = seeded.current && mode !== 'tag'
    ? httpUrlError(tagUrl, '推理服务地址') || (execution.inference_source === 'local' ? localServiceUrlError(tagUrl) : '') : ''
  const outNotice = seeded.current && !outDir.trim() ? '请选择产物目录，下载的分片和打标结果会保存到这里。' : ''
  const runtimeFailure = state?.runtime?.error || (state?.runtime?.status === 'failed' ? state.runtime.message || '本轮调度执行失败' : '')
  const failureDetail = state?.runtime?.error || (runtimeFailure && state?.last_rc != null ? latestCenterFailure(state.log) : '') || runtimeFailure
  // 打标服务与产物目录空着就不让开始：后端对空串是"保持原值"（免得把配置
  // 清成空以后再起不来），所以输入框显示空、实际却用着旧地址，是更难查的错。
  const startBlocked = proxyNotice || downloadProxyNotice || (!ident.device_id
    ? '先到「连接与账号」连接调度中心'
    : mode !== 'tag' && !tagUrl.trim()
      ? '打标服务地址不能为空'
      : !outDir.trim()
        ? '产物目录不能为空'
        : '')
  const payload = () => ({ ...execution, tag_url: tagUrl, mode, out_dir: outDir,
    hosting_mode: 'passive' as const, wait: false, idle_timeout: 0, max_packages: Number(maxPkgs || 0),
    download_conns: Math.max(1, Math.min(16, Number(dlConns) || 4)),
    cdn_probe: cdnProbe, parts_pref: partsPref, auto_upload: autoUpload, auto_start_on_connect: autoStartOnConnect,
    proxy_mode: proxyMode, proxy_url: proxyUrl,
    download_proxy_mode: downloadProxyMode, download_proxy_url: downloadProxyUrl,
    remember_hf: rememberHf, ...(hfToken.trim() ? { hf_token: hfToken.trim() } : {}),
  })

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-5">
      <PageHeader
        icon={<CircleDot size={18} />}
        eyebrow="局域网协作"
        title="调度中心"
        badges={
          running ? (
            <StatusChip tone="success">{state?.running ? `运行中 · ${fmtUptime(state?.uptime_s ?? 0)}`
              : state?.runtime?.hosting_mode === 'passive' ? '托管中' : '准备中'}</StatusChip>
          ) : (
            <StatusChip tone="neutral">已停止</StatusChip>
          )
        }
        actions={
          <button
            type="button"
            className="lx-btn-ghost-icon"
            title="立刻刷新"
            onClick={() => void pull()}
          >
            <RefreshCw size={15} />
          </button>
        }
      />

      <ExecutionMonitor state={state} unavailable={Boolean(pollError)} />

      {(actionError || pollError) && <ErrorBox error={actionError || pollError}
        nextStep={failureAdvice(actionError || pollError, 'center')} />}
      {runtimeFailure && <ErrorBox error={failureDetail} title="调度运行失败"
        nextStep={failureAdvice(failureDetail, 'center')}
        actions={<button type="button" className="lx-btn-ghost"
          onClick={() => {
            setSection('records')
            requestAnimationFrame(() => document.getElementById('center-logs')?.scrollIntoView({ block: 'start' }))
          }}>查看调度日志</button>} />}
      <CenterOverview state={state} />

      <ExecutionStatus state={state} busy={Boolean(busy) || releasing}
        onResume={() => void act('resume', () => api.centerStart({}))}
        onForceContinue={() => void act('force', () => api.centerStart({force_continue: true}))} />
      <LeasePanel state={state} busy={Boolean(busy)} onRelease={(id, kind) => void act('release', () => api.centerRelease(id, kind))} />
      <DispatchPlans connected={!!ident.device_id} locked={running || Boolean(state?.history?.running) || releasing || Boolean(busy)} onApplied={() => { seeded.current = false; void pull() }} />
      <SectionPanel
        title="接单控制"
        description="退出托管保留租约 48 小时；归还分片请点停止并释放"
        trailing={
          running ? (
            <button
              type="button"
              className="lx-btn-danger inline-flex items-center"
              disabled={Boolean(busy) || releasing}
              onClick={() => void act('stop', api.centerStop)}
            >
              {busy === 'stop' ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <Square size={13} />
              )}
              <span className="ml-1.5">{state?.runtime?.hosting_mode === 'passive' ? '退出托管' : '停止'}</span>
            </button>
          ) : (
            <div className="flex gap-2">
            <button type="button" className="lx-btn-ghost" disabled={Boolean(busy) || !seeded.current}
              onClick={() => void act('save', () => api.centerConfigure(payload()))}>保存配置</button>
            <button type="button" className="lx-btn-ghost" disabled={Boolean(busy) || !seeded.current}
              onClick={() => void act('probe', async () => {
                await api.centerConfigure(payload())
                return api.centerProbe()
              })}>探测并发</button>
            <button
              type="button"
              className="lx-btn-primary inline-flex items-center"
              disabled={Boolean(busy) || releasing || !seeded.current || Boolean(startBlocked)}
              title={startBlocked}
              onClick={() =>
                void act('start', () =>
                  api.centerStart(payload()),
                )
              }
            >
              {busy === 'start' ? <Loader2 size={14} className="animate-spin" /> : <Play size={13} />}
              <span className="ml-1.5">开始自动接单</span>
            </button>
            </div>
          )
        }
      >
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-lx-muted">
          <span>{MODE_LABEL[mode] ?? mode} · 由中心统一派发</span>
          {!ident.device_id && <button type="button" className="lx-btn-ghost text-xs" onClick={() => setSection('connection')}>先连接调度中心</button>}
        </div>
        {startBlocked && <p className="mt-2 text-xs text-lx-warning">{startBlocked}</p>}
      </SectionPanel>

      <FormSections label="调度中心选项分类" value={section} onChange={setSection} sections={[
        { id: 'work', title: '接单与托管', content: (<>
          <SectionPanel title="接单与托管">
            <SettingRow label="产出模式">
              <ChoiceGrid
                options={MODES}
                value={mode}
                onChange={(v) => {
                  setMode(v)
                  if (v === 'tag' && execution.tag_backend === 'none') setExecution({ ...execution, tag_backend: 'wd-eva02' })
                  // 切成单侧时，指向另一侧的倾向当场清掉。留着的话表单看起来
                  // 正常，点「开始」才被后端 400 拦下——那是最难自查的一类错。
                  if (!prefFitsMode(partsPref, v)) setPartsPref('')
                }}
                disabled={running}
                size="sm"
                ariaLabel="产出模式"
                renderLabel={(v) => MODE_LABEL[v] ?? v}
              />
            </SettingRow>
            <AdvancedSettings title="高级：派工偏好"><SettingRow
              label="派工倾向"
              help={
                mode === 'both'
                  ? '仅影响优先派发类型'
                  : undefined
              }
            >
              {mode === 'both' ? (
                <div className="flex flex-col gap-1">
                  <select
                    className="lx-select text-xs"
                    value={partsPref}
                    onChange={(e) => setPartsPref(e.target.value)}
                    disabled={running}
                    aria-label="派工倾向"
                  >
                    {PREF_OPTIONS.map((o) => (
                      <option key={o.value || 'none'} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
              ) : (
                <span className="text-xs text-lx-muted">
                  自动：只接缺 {mode === 'tag' ? 'tag' : 'NL'} 的片
                </span>
              )}
            </SettingRow></AdvancedSettings>
            <div className="mt-3 flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[11px] text-lx-muted">分片上限</span>
                <input
                  className="lx-input w-24 font-mono text-xs tabular-nums"
                  inputMode="numeric"
                  value={maxPkgs}
                  onChange={(e) => setMaxPkgs(e.target.value.replace(/[^\d]/g, ''))}
                  placeholder="不限"
                  disabled={running}
                />
              </label>
              <p className="flex-1 text-[10.5px] leading-relaxed text-lx-dim">
                0 或留空为不限；达上限后暂停接单
              </p>
            </div>
            <AdvancedSettings title="高级：自动恢复与服务行为">
              <ExecutionLifecycle value={execution} onChange={setExecution} mode={mode} disabled={running} />
            </AdvancedSettings>
          </SectionPanel>
        </>) },
        { id: 'connection', title: '连接与账号', content: (<>
          <SectionPanel
                  title="连接与账号"
                  description={running ? '运行中不可修改' : undefined}
                >
                  <SettingRow
                    label="调度中心地址"
                    notice={serverNotice}
                    required
                  >
                    <input
                      className="lx-input font-mono text-xs"
                      value={server}
                      aria-label="调度中心地址"
                      aria-invalid={Boolean(serverNotice)}
                      onChange={(e) => setServer(e.target.value)}
                      placeholder="http://1.2.3.4:31831"
                      spellCheck={false}
                      disabled={running}
                    />
                  </SettingRow>
                  <ConnectionNetwork mode={proxyMode} address={proxyUrl} onMode={setProxyMode}
                    onAddress={setProxyUrl} disabled={running || Boolean(busy)} notice={proxyNotice} />
                  <ToggleRow checked={autoStartOnConnect} onChange={setAutoStartOnConnect} disabled={running || Boolean(busy)}
                    label="连接后自动接单" />
                  <SettingRow label="ID" required notice={idNotice}>
                    <input
                      className="lx-input text-xs"
                      value={userId}
                      aria-label="用户 ID"
                      aria-invalid={Boolean(idNotice)}
                      onChange={(e) => setUserId(e.target.value)}
                      placeholder="zhangsan"
                      spellCheck={false}
                      disabled={running}
                    />
                  </SettingRow>
                  <SettingRow
                    label="密钥"
                    help="同名冲突需管理员清理旧设备"
                  >
                    <div className="flex items-center gap-2">
                      <input
                        type="password"
                        className="lx-input font-mono text-xs"
                        value={secret}
                        onChange={(e) => setSecret(e.target.value)}
                        placeholder={ident.device_id ? '已登录' : '留空 = 本机自动生成'}
                        autoComplete="off"
                        spellCheck={false}
                        disabled={running}
                      />
                      <button
                        type="button"
                        className="lx-btn-primary shrink-0"
                        disabled={running || Boolean(busy) || Boolean(serverNotice || idNotice || proxyNotice)}
                        onClick={() =>
                          void act('login', async () => {
                            await api.centerConfigure(payload())
                            const s = await api.centerLogin({ server, user_id: userId, secret,
                              proxy_mode: proxyMode, proxy_url: proxyUrl })
                            setSecret('')
                            setSection('work')
                            // 身份落盘了，顶栏那盏灯该立刻变绿：
                            // 页面内虽然已经显示设备号，但用户看的是全屏的角落。
                            onLinked?.()
                            return s
                          })
                        }
                      >
                        {busy === 'login' ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
                        <span className="ml-1.5">连接</span>
                      </button>
                    </div>
                  </SettingRow>

                  {ident.device_id && (
                    <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1.5 text-[11px] sm:grid-cols-2">
                      <div className="flex justify-between gap-2">
                        <dt className="text-lx-muted">设备</dt>
                        <dd className="truncate font-mono text-lx-secondary" title={ident.device_id}>
                          {ident.device_id}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt className="text-lx-muted">服务端</dt>
                        <dd className="truncate font-mono text-lx-secondary" title={ident.server_id || ident.server}>
                          {ident.server_id || ident.server || '—'}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt className="text-lx-muted">心跳</dt>
                        <dd className="font-mono tabular-nums text-lx-secondary">
                          {state?.runtime.active && state.runtime.hosting_mode === 'passive' ? '托管中每 5 秒轮询上报' : state?.running ? '执行中每 30 秒上报' : '执行已停止'}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt className="text-lx-muted">ID</dt>
                        <dd className="truncate text-lx-secondary">{ident.user_id || '—'}</dd>
                      </div>
                    </dl>
                  )}

                  {ident.device_id && (
                    <div className="mt-3 flex items-center justify-between gap-3 border-t border-lx pt-3">
                      <p className="text-[10.5px] leading-relaxed text-lx-dim">
                        断开只清除设备身份，地址与 ID 保留
                      </p>
                      <button
                        type="button"
                        className="lx-btn-ghost shrink-0"
                        disabled={running || busy === 'logout'}
                        title={running ? '跑着的时候不能断开，先停止' : ''}
                        onClick={() => void act('logout', api.centerLogout)}
                      >
                        {busy === 'logout' ? <Loader2 size={14} className="animate-spin" /> : null}
                        <span className={busy === 'logout' ? 'ml-1.5' : ''}>断开</span>
                      </button>
                    </div>
                  )}
                </SectionPanel>
          <SectionPanel title="下载凭证">
            <ExecutionCredentials token={hfToken} onToken={setHfToken} remember={rememberHf} onRemember={setRememberHf}
              tokenSet={state?.credentials?.hf_token_set ?? false} disabled={running}
              onClear={() => void act('clear-token', () => api.centerConfigure({ hf_token: '', remember_hf: false }))} />
          </SectionPanel>
        </>) },
        { id: 'models', title: '模型与执行', content: (<>
          <SectionPanel title="模型与执行">
            <ExecutionSettings modelsOnly value={execution} onChange={setExecution} mode={mode} disabled={running}
              token={hfToken} onToken={setHfToken} remember={rememberHf} onRemember={setRememberHf}
              tokenSet={state?.credentials?.hf_token_set ?? false}
              onClear={() => void act('clear-token', () => api.centerConfigure({ hf_token: '', remember_hf: false }))} />
            {mode !== 'tag' && <SettingRow label={execution.inference_source === 'local' ? '本机服务地址' : '打标服务地址'}
              notice={serviceNotice}>
              <input
                className="lx-input font-mono text-xs"
                value={tagUrl}
                onChange={(e) => setTagUrl(e.target.value)}
                placeholder="http://127.0.0.1:8000"
                spellCheck={false}
                disabled={running}
                aria-label="推理服务地址"
                aria-invalid={Boolean(serviceNotice)}
              />
            </SettingRow>}
          </SectionPanel>
        </>) },
        { id: 'delivery', title: '下载与交付', content: (<>
          <SectionPanel title="下载与交付">
            <SettingRow label="产物目录" notice={outNotice}>
              <div className="flex gap-2">
                <input
                  className="lx-input font-mono text-xs"
                  value={outDir}
                  onChange={(e) => setOutDir(e.target.value)}
                  placeholder="work/fox-work"
                  spellCheck={false}
                  disabled={running}
                  aria-label="产物目录"
                  aria-invalid={Boolean(outNotice)}
                />
                <button
                  type="button"
                  className="lx-btn-ghost shrink-0 px-3"
                  disabled={running}
                  onClick={() => folder.pick(outDir || undefined, setOutDir)}
                >
                  <FolderOpen size={14} className="mr-1 inline" />
                  浏览
                </button>
              </div>
            </SettingRow>
            <ExecutionDownloads value={execution} onChange={setExecution} disabled={running} />
            <ToggleRow
              checked={autoUpload}
              onChange={setAutoUpload}
              disabled={running}
              label="自动回传（推荐）"
            />
            {!autoUpload && <p className="text-xs text-lx-warning">关闭后产物保留在本地，需在「任务与记录」手动上报。</p>}
            <DownloadNetwork mode={downloadProxyMode} address={downloadProxyUrl} onMode={setDownloadProxyMode}
              onAddress={setDownloadProxyUrl} disabled={running} notice={downloadProxyNotice} />
            <AdvancedSettings title="高级：下载连接与 CDN 测速">
              <div className="mt-3 border-t border-lx pt-3">
                <ToggleRow
                  checked={cdnProbe}
                  onChange={setCdnProbe}
                  disabled={running}
                  label="CDN 测速（仅直连时选路）"
                />
                <div className="mt-3 flex flex-wrap items-end gap-3">
                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] text-lx-muted">下载连接数</span>
                    <input
                      className="lx-input w-24 font-mono text-xs tabular-nums"
                      inputMode="numeric"
                      aria-label="下载连接数"
                      value={dlConns}
                      onChange={(e) => setDlConns(e.target.value.replace(/[^\d]/g, ''))}
                      placeholder="4"
                      disabled={running}
                    />
                  </label>
                  <p className="flex-1 text-[10.5px] leading-relaxed text-lx-dim">
                    建议 4~8 条
                  </p>
                </div>
              </div>
            </AdvancedSettings>
          </SectionPanel>
        </>) },
        { id: 'records', title: '任务与记录', content: (<>
                <OfflineTasks />
                <HistoryArtifacts locked={running || Boolean(state?.history?.running)} />

                <div id="center-logs" className="scroll-mt-4" />
                <SectionPanel title="日志">
                  <div className="mb-2 flex flex-wrap gap-2">
                    {(['center', 'service'] as const).map((kind) => <button key={kind} type="button" className="lx-btn-ghost"
                      onClick={async () => {
                        try {
                          const result = await api.centerRevealLogs(kind)
                          setLogLocation([result.detail, result.path].filter(Boolean).join(' · '))
                        } catch (e) { setActionError(String(e)) }
                      }}>{kind === 'center' ? '打开调度日志目录' : '打开模型日志目录'}</button>)}
                    <button type="button" className="lx-btn-ghost" onClick={async () => {
                      try { setServiceLog(await api.centerLogs('service')) } catch (e) { setActionError(String(e)) }
                    }}>查看最近模型日志</button>
                  </div>
                  {logLocation && <p className="mb-2 break-all text-xs text-lx-muted">{logLocation}</p>}
                  {state?.log_path && <p className="mb-2 break-all text-xs text-lx-dim">{state.log_path}</p>}
                  {state && state.log.length > 0 ? (
                    <pre className="m-0 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[10.5px] leading-relaxed text-lx-muted">
                      {state.log.join('\n')}
                    </pre>
                  ) : (
                    <p className="text-[11px] text-lx-muted">暂无输出</p>
                  )}
                  {serviceLog && <div className="mt-3 border-t border-lx pt-3">
                    <p className="mb-2 break-all text-xs text-lx-dim">{serviceLog.path || '暂无模型日志'}</p>
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-[10.5px] text-lx-muted">{serviceLog.tail.join('\n')}</pre>
                  </div>}
                </SectionPanel>

          <SectionPanel title="执行诊断">
            <details className="mt-3 border-t border-lx pt-3">
              <summary className="cursor-pointer text-[11px] text-lx-muted">高级：实际执行的命令</summary>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10px] leading-relaxed text-lx-muted">
                {state?.cmd || '—'}
              </pre>
            </details>
          </SectionPanel>
        </>) },
      ]} />

      {folder.dialog}
    </div>
  )
}
