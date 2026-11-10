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
import {
  MiniMetricCard,
  PageHeader,
  SectionPanel,
  SettingRow,
  StatusChip,
} from '../components/ui/launcher'

const MODES = ['both', 'tag', 'nl']
const MODE_LABEL: Record<string, string> = { both: 'tag + NL', tag: '只出 tag', nl: '只出 NL' }

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
  const [tagUrl, setTagUrl] = useState('')
  const [outDir, setOutDir] = useState('')
  const [waitMode, setWaitMode] = useState(true)
  // 存成字符串而不是数字：输入框清空时得能表达"没填"，转成 NaN 再回填会变成 0
  const [maxPkgs, setMaxPkgs] = useState('0')
  const seeded = useRef(false)

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
        setTagUrl(s.config.tag_url)
        setOutDir(s.config.out_dir)
        setWaitMode(s.config.wait ?? true)
        setMaxPkgs(String(s.config.max_packages ?? 0))
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
        description="连上总机后由它分片，这台机器负责打标。开始/停止都在这里按。"
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

      <div className="grid gap-3 sm:grid-cols-3">
        <MiniMetricCard
          icon={<PlugZap size={14} />}
          label="连接"
          value={ident.server ? '已连接' : '未连接'}
          detail={ident.server || '填地址与姓名'}
          tone={ident.server ? 'success' : 'neutral'}
        />
        <MiniMetricCard
          icon={<Terminal size={14} />}
          label="当前派工"
          value={String(leases.length)}
          detail={board?.reason || (board?.ok ? `档位 ${board?.tier || '?'}` : '—')}
          tone={leases.length > 0 ? 'accent' : 'neutral'}
        />
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
        description={
          running ? '跑着的时候不能换身份，先停止。' : '姓名就是你的工位：换一个就等于换个人记账。'
        }
      >
        <SettingRow
          label="调度中心地址"
          help="总机跑 fox-label 服务的地址，形如 http://1.2.3.4:31831。"
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
        <SettingRow label="姓名 / ID" help="服务端按它记账，也是取回工位的唯一依据。" required>
          <input
            className="lx-input text-xs"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder="张三"
            spellCheck={false}
            disabled={running}
          />
        </SettingRow>
        <SettingRow
          label="密钥"
          help="管理员签发的 secret。服务端免签时留空即可；登录成功后不再回显。"
        >
          <div className="flex items-center gap-2">
            <input
              type="password"
              className="lx-input font-mono text-xs"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder={ident.device_id ? '已登录，改身份才需要重填' : '留空 = 免签'}
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
              <dt className="text-lx-muted">姓名</dt>
              <dd className="truncate text-lx-secondary">{ident.user_id || '—'}</dd>
            </div>
          </dl>
        )}

        {ident.device_id && (
          <div className="mt-3 flex items-center justify-between gap-3 border-t border-lx pt-3">
            <p className="text-[10.5px] leading-relaxed text-lx-dim">
              断开只忘掉本机的设备身份，地址和姓名留着——重连不用再抄一遍。
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
        description="启动的是 worker 子进程：它自己下载分片、调打标、上报结果。"
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
        <SettingRow label="产出模式" help="both = tag 与 NL 都出；单模式更快，但另一份得再跑一遍。">
          <ChoiceGrid
            options={MODES}
            value={mode}
            onChange={setMode}
            disabled={running}
            size="sm"
            ariaLabel="产出模式"
            renderLabel={(v) => MODE_LABEL[v] ?? v}
          />
        </SettingRow>
        <SettingRow label="打标服务" help="worker 调用的本机 serve 地址；改它等于换一台机器干活。">
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
        <SettingRow label="产物目录" help="分片下载与打标产物都落在这里。">
          <input
            className="lx-input font-mono text-xs"
            value={outDir}
            onChange={(e) => setOutDir(e.target.value)}
            placeholder="work/fox-work"
            spellCheck={false}
            disabled={running}
            aria-invalid={!outDir.trim()}
          />
        </SettingRow>

        <div className="mt-4 border-t border-lx pt-3">
          <ToggleRow
            checked={waitMode}
            onChange={setWaitMode}
            disabled={running}
            label="挂着等派工（池子空了也不停）"
            hint="关掉就是“把手上的干完就停”。开着的话，点一次开始就能一直挂在线上，别人往池子里放活它就接。"
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
              0 = 不限。填了数，跑够就自己停手，并把多派的那片还回池子。
              {!waitMode && '（只在“挂着等派工”时生效）'}
            </p>
          </div>
        </div>

        <details className="mt-3 border-t border-lx pt-3">
          <summary className="cursor-pointer text-[11px] text-lx-muted">高级：实际执行的命令</summary>
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lx-sm bg-lx-surface-raised px-2.5 py-2 font-mono text-[10px] leading-relaxed text-lx-muted">
            {state?.cmd || '—'}
          </pre>
          <p className="mt-1.5 text-[10px] leading-relaxed text-lx-dim">
            由后端按上面的配置拼出来，页面不提供逐字编辑：改坏一个引号，路径里有空格就会
            断成两条命令，报错还看不出是这里出的问题。
          </p>
        </details>
      </SectionPanel>

      <SectionPanel
        title={leases.length ? `当前租约 · ${leases.length}` : '当前租约'}
        description="只读：看一眼不会领走任务。"
      >
        {!board?.ok && (
          <p className="text-[11px] text-lx-muted">{board?.reason || '还没连上调度中心。'}</p>
        )}
        {board?.ok && leases.length === 0 && (
          <p className="text-[11px] text-lx-muted">手上没有租约（要么收工了，要么还在等派工）。</p>
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

      <SectionPanel title="日志" description="worker 的 stdout，最近 120 行。">
        {state && state.log.length > 0 ? (
          <pre className="m-0 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[10.5px] leading-relaxed text-lx-muted">
            {state.log.join('\n')}
          </pre>
        ) : (
          <p className="text-[11px] text-lx-muted">还没有输出。点「开始」之后这里会滚动。</p>
        )}
      </SectionPanel>
    </div>
  )
}
