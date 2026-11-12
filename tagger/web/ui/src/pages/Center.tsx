/**
 * 调度中心：把 fox-label v3 的 worker 托管成一个可启停的子进程。
 *
 * 这一页刻意只做三件事——填身份、按开始/停止、看日志。派工、心跳、预锁、
 * 租约回收全在 worker 里，这边一行都不复刻：网页里再实现一遍，两边就会
 * 各说各话（谁在续约、谁算数的租约），而"到底哪条算数"是没法从界面上看出来的。
 *
 * 「当前租约」是**只读**看板：后端拿同一套凭据打一次 status，不派工、不续约。
 * 所以打开这一页不会顺手领走一个包——这点很关键，否则"看一眼"就等于开工。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CircleDot,
  FolderOpen,
  KeyRound,
  Loader2,
  Play,
  PlugZap,
  RefreshCw,
  Square,
  Terminal,
} from 'lucide-react'
import { api, ApiError, type CenterState } from '../lib/api'
import { ChoiceGrid, ErrorBox, ToggleRow } from '../components/ui/controls'
import { useFolderPicker } from '../components/FolderPicker'
import {
  MiniMetricCard,
  PageHeader,
  SectionPanel,
  SettingRow,
  StatusChip,
} from '../components/ui/launcher'

const MODES = ['both', 'tag', 'nl']
const MODE_LABEL: Record<string, string> = { both: 'tag + NL', tag: '只出 tag', nl: '只出 NL' }

/**
 * 派工倾向。**不是**"产出什么"，而是"服务端把哪种片排在我前面"——
 * 产出什么由上方的「产出模式」决定，两者是正交的：
 *   只出 tag + 优先 NL  = 我只会做 tag，却想先拿缺 NL 的片 → 接不到能交的活
 * 所以下面按产出模式筛选项，把这类矛盾直接藏起来而不是让它报错。
 */
const PREF_OPTIONS: { value: string; label: string; hint: string }[] = [
  { value: '', label: '不挑', hint: '接任何还欠内容的片（两侧都做）' },
  { value: 'pref-tag', label: '优先 tag', hint: '先接缺 tag 的片；另一侧也能做，会兜底' },
  { value: 'pref-nl', label: '优先 NL', hint: '先接缺 NL 的片；另一侧也能做，会兜底' },
  { value: 'tag', label: '只接 tag', hint: '只接缺 tag 的片；没有就空转等，不白下载' },
  { value: 'nl', label: '只接 NL', hint: '只接缺 NL 的片；没有就空转等，不白下载' },
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
  // 两种错误分开存：
  // pollError 是"这一轮没拉到数据"，下一轮成功就该消失；
  // actionError 是"你刚点的那个按钮失败了"，得留着让人看清为什么——
  // 混成一个的话，连接失败的红条会在 5 秒后被轮询悄悄抹掉。
  const [pollError, setPollError] = useState('')
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState('')
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
  const [waitMode, setWaitMode] = useState(true)
  // 存成字符串而不是数字：输入框清空时得能表达"没填"，转成 NaN 再回填会变成 0
  const [maxPkgs, setMaxPkgs] = useState('0')
  // 下载口径：连接数同样存字符串（清空后按默认 4 处理），选优是开关
  const [dlConns, setDlConns] = useState('4')
  const [cdnProbe, setCdnProbe] = useState(true)
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
        setUserId(s.config.user_id)
        setMode(s.config.mode || 'both')
        setPartsPref(s.config.parts_pref ?? '')
        setTagUrl(s.config.tag_url)
        setOutDir(s.config.out_dir)
        setWaitMode(s.config.wait ?? true)
        setMaxPkgs(String(s.config.max_packages ?? 0))
        setDlConns(String(s.config.download_conns ?? 4))
        setCdnProbe(s.config.cdn_probe ?? true)
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
      setState(await fn())
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  const running = state?.running ?? false
  const ident = state?.identity ?? {}
  const board = state?.board
  const cfg = state?.config
  const leases = board?.leases ?? []
  // 只有连上看板、且服务端真的给了这项统计才显示（老服务端 → undefined）
  const pool = board?.ok ? board.parts : undefined
  // 打标服务与产物目录空着就不让开始：后端对空串是"保持原值"（免得把配置
  // 清成空以后再起不来），所以输入框显示空、实际却用着旧地址，是更难查的错。
  const startBlocked = !ident.device_id
    ? '先在上面连接'
    : !tagUrl.trim()
      ? '打标服务地址不能为空'
      : !outDir.trim()
        ? '产物目录不能为空'
        : ''

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-5">
      <PageHeader
        icon={<CircleDot size={18} />}
        eyebrow="局域网协作"
        title="调度中心"
        description="自动分工，分到的分片在本机打标"
        badges={
          running ? (
            <StatusChip tone="success">运行中 · {fmtUptime(state?.uptime_s ?? 0)}</StatusChip>
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

      {(actionError || pollError) && <ErrorBox error={actionError || pollError} />}

      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <MiniMetricCard
          icon={<PlugZap size={14} />}
          label="连接"
          value={ident.server ? '已连接' : '未连接'}
          detail={ident.server || '填地址与 ID'}
          tone={ident.server ? 'success' : 'neutral'}
        />
        <MiniMetricCard
          icon={<Terminal size={14} />}
          label="当前派工"
          value={String(leases.length)}
          detail={board?.reason || (board?.ok ? `档位 ${board?.tier || '?'}` : '—')}
          tone={leases.length > 0 ? 'accent' : 'neutral'}
        />
        {/* 全池余量：谁想补哪一侧，看这两个数决定要不要开工。
            老服务端没有这项统计——此时整组都不显示，免得把"没统计"读成"一片不缺"。 */}
        {pool && (
          <>
            <MiniMetricCard
              icon={<CircleDot size={14} />}
              label="全池剩余"
              value={String(pool.remaining)}
              detail={`共 ${pool.total} 片 · 已完成 ${pool.done}`}
              tone={pool.remaining > 0 ? 'accent' : 'success'}
            />
            <MiniMetricCard
              icon={<CircleDot size={14} />}
              label="缺 NL"
              value={String(pool.missing_nl)}
              detail="只等描述的那一侧"
              tone={pool.missing_nl > 0 ? 'warning' : 'success'}
            />
            <MiniMetricCard
              icon={<CircleDot size={14} />}
              label="缺 TAG"
              value={String(pool.missing_tag)}
              detail="只等标签的那一侧"
              tone={pool.missing_tag > 0 ? 'warning' : 'success'}
            />
          </>
        )}
        <MiniMetricCard
          icon={<Loader2 size={14} />}
          label="上次退出码"
          value={state?.last_rc === null || state?.last_rc === undefined ? '—' : String(state.last_rc)}
          detail={running ? '正在跑' : state?.last_rc === 0 ? '正常结束' : ''}
          tone={
            state?.last_rc === null || state?.last_rc === undefined
              ? 'neutral'
              : state.last_rc === 0
                ? 'success'
                : 'warning'
          }
        />
      </div>

      <SectionPanel
        title="身份"
        description={running ? '运行中不能改，先停止' : '换 ID 将按新身份单独记录'}
      >
        <SettingRow
          label="调度中心地址"
          help="形如 http://1.2.3.4:31831"
          required
        >
          <input
            className="lx-input font-mono text-xs"
            value={server}
            onChange={(e) => setServer(e.target.value)}
            placeholder="http://1.2.3.4:31831"
            spellCheck={false}
            disabled={running}
          />
        </SettingRow>
        <SettingRow label="ID" help="服务端按它记录你的产出" required>
          <input
            className="lx-input text-xs"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder="zhangsan"
            spellCheck={false}
            disabled={running}
          />
        </SettingRow>
        <SettingRow
          label="密钥"
          help="留空 = 本机自动生成；同名被别的机器占着时，让管理员先清空该用户"
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
              disabled={running || busy === 'login'}
              onClick={() =>
                void act('login', async () => {
                  const s = await api.centerLogin({ server, user_id: userId, secret })
                  setSecret('')
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
                {ident.beat_interval ? `${ident.beat_interval}s` : '—'}
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

      <SectionPanel
        title="运行"
        description="启动一个 worker 子进程，自动领片、打标、上报"
        trailing={
          running ? (
            <button
              type="button"
              className="lx-btn-danger"
              disabled={busy === 'stop'}
              onClick={() => void act('stop', api.centerStop)}
            >
              {busy === 'stop' ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <Square size={13} />
              )}
              <span className="ml-1.5">停止</span>
            </button>
          ) : (
            <button
              type="button"
              className="lx-btn-primary"
              disabled={busy === 'start' || Boolean(startBlocked)}
              title={startBlocked}
              onClick={() =>
                void act('start', () =>
                  api.centerStart({
                    tag_url: tagUrl,
                    mode,
                    out_dir: outDir,
                    // 心跳留空 = 用服务端建议值；这里的数只是"上次用的"，
                    // 页面不让人乱调（调小了对服务端是额外的负载）。
                    interval: cfg?.interval,
                    extra_args: cfg?.extra_args,
                    wait: waitMode,
                    max_packages: Number(maxPkgs || 0),
                    download_conns: Math.max(1, Math.min(16, Number(dlConns) || 4)),
                    cdn_probe: cdnProbe,
                    parts_pref: partsPref,
                  }),
                )
              }
            >
              {busy === 'start' ? <Loader2 size={14} className="animate-spin" /> : <Play size={13} />}
              <span className="ml-1.5">开始</span>
            </button>
          )
        }
      >
        <SettingRow label="产出模式" help="both 出 tag 与 NL；单模式更快">
          <ChoiceGrid
            options={MODES}
            value={mode}
            onChange={(v) => {
              setMode(v)
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
        {/* 派工倾向：服务端据此决定"先把哪种片派给你"。它与产出模式正交，
            但正交不等于可以随便组合——单侧模式下只有同侧才有意义。 */}
        <SettingRow
          label="派工倾向"
          help={
            mode === 'both'
              ? '只影响先派哪种片，不改变本机产出什么'
              : `产出模式是单侧，只能接缺 ${mode === 'tag' ? 'tag' : 'NL'} 的片`
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
              <span className="text-[10.5px] leading-relaxed text-lx-dim">
                {PREF_OPTIONS.find((o) => o.value === partsPref)?.hint}
              </span>
            </div>
          ) : (
            <span className="text-xs text-lx-muted">
              自动：只接缺 {mode === 'tag' ? 'tag' : 'NL'} 的片
            </span>
          )}
        </SettingRow>
        <SettingRow label="打标服务" help="worker 调用的 serve 地址">
          <input
            className="lx-input font-mono text-xs"
            value={tagUrl}
            onChange={(e) => setTagUrl(e.target.value)}
            placeholder="http://127.0.0.1:8000"
            spellCheck={false}
            disabled={running}
            aria-invalid={!tagUrl.trim()}
          />
        </SettingRow>
        <SettingRow label="产物目录" help="分片与打标产物都落这里">
          <div className="flex gap-2">
            <input
              className="lx-input font-mono text-xs"
              value={outDir}
              onChange={(e) => setOutDir(e.target.value)}
              placeholder="work/fox-work"
              spellCheck={false}
              disabled={running}
              aria-invalid={!outDir.trim()}
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

        <div className="mt-4 border-t border-lx pt-3">
          <ToggleRow
            checked={waitMode}
            onChange={setWaitMode}
            disabled={running}
            label="挂着等派工（池子空了也不停）"
            hint="关掉 = 手头的干完就停"
          />
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-lx-muted">本次最多打多少片</span>
              <input
                className="lx-input w-24 font-mono text-xs tabular-nums"
                inputMode="numeric"
                value={maxPkgs}
                onChange={(e) => setMaxPkgs(e.target.value.replace(/[^\d]/g, ''))}
                placeholder="0"
                disabled={running || !waitMode}
              />
            </label>
            <p className="flex-1 text-[10.5px] leading-relaxed text-lx-dim">
              0 = 不限，跑够就停并归还多派的片
              {!waitMode && '（仅「挂着等派工」时生效）'}
            </p>
          </div>
        </div>

        {/* 下载口径。两件事一起决定"这片多久能到"：单条 TCP 常被限速（多连接），
            以及 DNS 给的那个边缘未必最快（开工前测速选优）。 */}
        <div className="mt-3 border-t border-lx pt-3">
          <ToggleRow
            checked={cdnProbe}
            onChange={setCdnProbe}
            disabled={running}
            label="下载前测速选最快 CDN"
            hint="每片开工前拉 2MiB 探一次，v4/v6 都测，钉住最快的那条边缘"
          />
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-lx-muted">下载连接数</span>
              <input
                className="lx-input w-24 font-mono text-xs tabular-nums"
                inputMode="numeric"
                value={dlConns}
                onChange={(e) => setDlConns(e.target.value.replace(/[^\d]/g, ''))}
                placeholder="4"
                disabled={running}
              />
            </label>
            <p className="flex-1 text-[10.5px] leading-relaxed text-lx-dim">
              1 = 单连接；国内单条 TCP 常被限速，4~8 条足够，再多只是抢同一份带宽
            </p>
          </div>
        </div>

        <details className="mt-3 border-t border-lx pt-3">
          <summary className="cursor-pointer text-[11px] text-lx-muted">高级：实际执行的命令</summary>
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10px] leading-relaxed text-lx-muted">
            {state?.cmd || '—'}
          </pre>
          <p className="mt-1.5 text-[10px] leading-relaxed text-lx-dim">
            由后端按上面的配置拼出，不支持逐字编辑
          </p>
        </details>
      </SectionPanel>

      <SectionPanel
        title={leases.length ? `当前租约 · ${leases.length}` : '当前租约'}
        description="只读，不会领走任务"
      >
        {!board?.ok && (
          <p className="text-[11px] text-lx-muted">{board?.reason || '还没连上调度中心。'}</p>
        )}
        {board?.ok && leases.length === 0 && (
          <p className="text-[11px] text-lx-muted">当前没有租约</p>
        )}
        {leases.length > 0 && (
          <div className="flex flex-col divide-y divide-lx">
            {leases.map((l) => (
              <div key={l.lease_id} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-2">
                <StatusChip tone={l.kind === 'active' ? 'accent' : 'neutral'}>
                  {l.kind === 'active' ? '在跑' : '预占'}
                </StatusChip>
                <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-lx-secondary" title={`${l.repo}/${l.package}`}>
                  {l.repo}/{l.package}
                </span>
                <span className="shrink-0 font-mono text-[11px] tabular-nums text-lx-muted">
                  剩 {Math.round(l.expires_in)}s
                </span>
              </div>
            ))}
          </div>
        )}
      </SectionPanel>

      <SectionPanel title="日志" description="最近 120 行">
        {state && state.log.length > 0 ? (
          <pre className="m-0 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[10.5px] leading-relaxed text-lx-muted">
            {state.log.join('\n')}
          </pre>
        ) : (
          <p className="text-[11px] text-lx-muted">暂无输出</p>
        )}
      </SectionPanel>

      {folder.dialog}
    </div>
  )
}
