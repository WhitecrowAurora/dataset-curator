/**
 * 依赖检测：把 requirements-*.txt 里的每一条变成一行，能单独装 / 卸 / 重装。
 *
 * 与「部署自检」的分工：自检回答"这些包能不能 import"，这一页回答
 * "装了没、版本对不对、要不要现在动它"。
 *
 * 清单不在前端硬编码：后端现读现算，改了 requirements 文件刷新就能看到。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronUp,
  CircleAlert,
  CircleCheck,
  CircleX,
  Download,
  Loader2,
  Package,
  PackageCheck,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { api, type DepItem, type DepState, type DepTorchOption } from '../lib/api'
import { PageHeader, SectionPanel, SheetHead, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'

const STATUS: Record<string, { label: string; tone: StatusTone }> = {
  ok: { label: '已装', tone: 'success' },
  missing: { label: '缺失', tone: 'danger' },
  mismatch: { label: '版本不符', tone: 'warning' },
}

const ACTION_LABEL: Record<string, string> = {
  install: '安装',
  uninstall: '卸载',
  reinstall: '重装',
  'install-missing': '安装缺失的包',
  'uninstall-all': '卸载全部',
  verify: '检查完整性',
}

function fmtElapsed(sec: number): string {
  if (!sec || sec < 0) return '0s'
  if (sec < 60) return `${sec.toFixed(0)}s`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return `${m}m ${s}s`
}

/** 一行。版本不符时把两个版本并排放，让人一眼看出差在哪。 */
function DepRow({
  item,
  busy,
  pending,
  torchOptions,
  torchVariant,
  onTorchVariant,
  onAct,
}: {
  item: DepItem
  /** 有 pip 在跑时全表禁用：两个 pip 同时改 site-packages 会互相拆台 */
  busy: boolean
  pending: string
  /** 只有 torch / torchvision 这两行会收到 */
  torchOptions?: DepTorchOption[]
  torchVariant: string
  onTorchVariant: (key: string) => void
  onAct: (name: string, action: 'install' | 'uninstall' | 'reinstall') => void
}) {
  const status = STATUS[item.status] ?? STATUS.missing
  const mine = pending.startsWith(`${item.name}:`)
  const disabled = busy || mine
  const variant = torchOptions?.find((v) => v.key === torchVariant) ?? torchOptions?.find((v) => v.recommended)
  // 用户选了本机跑不起来的那一档：仍允许装（只推荐不强制），但按钮前要把
  // 后果说清——不然又是一个"装完每张图都报 no kernel image"。
  const blockedVariant = !!variant && !variant.ok

  return (
    <div className="lx-sheet-row flex flex-wrap items-center gap-x-4 gap-y-2 px-[18px] py-2.5 md:px-7">
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        <StatusChip tone={status.tone}>{status.label}</StatusChip>
        <div className="min-w-0">
          <div className="flex items-baseline gap-2 truncate text-[13px] font-medium text-lx">
            {item.name}
            {item.requirement && (
              <span className="font-mono text-[10.5px] font-normal text-lx-muted">
                {item.requirement}
              </span>
            )}
            {/* 附加依赖组里会再出现一次 torch / transformers 这些：它们在
                别的分组里也管着同一份 site-packages，不标一下会以为"卸载只
                影响这个功能"。 */}
            {item.shared && (
              <span
                className="rounded-full border border-lx px-1.5 py-px text-[9.5px] font-normal text-lx-dim"
                title="与其它分组共用同一份包：在这里卸载，那边也会少"
              >
                共用
              </span>
            )}
          </div>
          {/* 版本只在"对不上"时才并排显示：一致的时候两行都写 4.16.0 只是噪音，
              而真有差异时，光看一个数字根本不知道差在哪。
              "导入失败"优先于一切——它比版本号重要得多。 */}
          <div
            className="mt-0.5 min-h-[14px] truncate text-[10.5px] text-lx-muted"
            title={item.verified ? item.verified.detail : item.note}
          >
            {item.supported === false ? (
              /* 平台不支持优先于一切：说"导入失败"会让人以为是环境坏了，
                 实际是这台机器上根本装不了。 */
              <span className="text-lx-warning">
                仅 {item.platforms?.join(' / ') || '其他系统'} 可用
              </span>
            ) : item.verified && !item.verified.ok ? (
              <span className="text-lx-danger">导入失败 · {item.verified.detail}</span>
            ) : item.status === 'mismatch' ? (
              <>
                实装 <span className="font-mono text-lx-warning">{item.installed}</span>
                <span className="mx-1 text-lx-dim">→</span>
                清单要求 <span className="font-mono">{item.requirement.replace(/^==/, '')}</span>
              </>
            ) : item.status === 'missing' ? (
              <span>{item.note || '未安装'}</span>
            ) : (
              item.note || ''
            )}
          </div>
          {/* torch 的轮子按 CUDA 版本分目录、各自只编了一部分 sm_xxx：
              50 系（CC 12.0+）必须 cu130，cu126 跑起来是 no kernel image。
              这里只做推荐与标注——装哪一档由人定，页面不替他决定。 */}
          {torchOptions && torchOptions.length > 0 && (
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10.5px] text-lx-muted">
              <span className="text-lx-dim">轮子档位</span>
              {torchOptions.map((v) => {
                const active = (variant?.key ?? 'auto') === v.key
                return (
                  <button
                    key={v.key}
                    type="button"
                    disabled={disabled}
                    title={`${v.note}${v.reason ? `\n\n${v.reason}` : ''}`}
                    onClick={() => onTorchVariant(v.key)}
                    className={[
                      'rounded-full border px-2 py-px transition-colors',
                      active
                        ? 'border-lx-accent text-lx-accent'
                        : v.ok
                          ? 'border-lx text-lx-dim hover:text-lx'
                          : 'border-lx text-lx-dim/60 line-through',
                    ].join(' ')}
                  >
                    {v.label}
                    {v.recommended && <span className="ml-1 text-[9.5px]">推荐</span>}
                    {!v.ok && <span className="ml-1 text-[9.5px]">本机不可用</span>}
                  </button>
                )
              })}
              {variant?.key !== 'auto' && (
                <button
                  type="button"
                  disabled={disabled}
                  className="text-[10px] text-lx-dim underline-offset-2 hover:underline"
                  onClick={() => onTorchVariant('auto')}
                >
                  恢复自动
                </button>
              )}
            </div>
          )}
          {blockedVariant && (
            <div className="mt-0.5 text-[10px] leading-relaxed text-lx-warning">
              {variant.reason || '这一档在本机用不了，装了也跑不起来'}
            </div>
          )}
          {/* torch 与 torchvision 是同一套 ABI 编的，版本和档位都必须一致。
              只重装其中一个，pip 会因为依赖关系把另一个换掉，或者留下档位不
              一致的组合（import 时报 undefined symbol，看不出是档位选错）。
              后端已经会自动带上配对的那个，这里把这件事说在点击之前。 */}
          {item.torch_peer && (
            <div className="mt-0.5 text-[10px] leading-relaxed text-lx-dim">
              与 <span className="font-mono">{item.torch_peer}</span> 版本配套，
              点「安装」「重装」会把另一个一起装好（卸载不受影响）
            </div>
          )}
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        {item.status === 'missing' ? (
          <button
            type="button"
            className="lx-btn-primary inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
            disabled={disabled || item.supported === false}
            title={
              item.supported === false
                ? `仅 ${item.platforms?.join(' / ') || '其他系统'} 可用，这台机器装上也是坏的`
                : undefined
            }
            onClick={() => onAct(item.name, 'install')}
          >
            {mine ? <Loader2 size={13} className="animate-spin" /> : <Package size={13} />}
            安装
          </button>
        ) : (
          <button
            type="button"
            className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
            disabled={disabled || item.supported === false}
            title={
              item.supported === false
                ? `仅 ${item.platforms?.join(' / ') || '其他系统'} 可用`
                : item.status === 'mismatch'
                  ? `卸载后按清单装回 ${item.requirement}`
                  : '卸载再装一遍（包坏了、装了一半时用）'
            }
            onClick={() => onAct(item.name, 'reinstall')}
          >
            {mine ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />}
            重装
          </button>
        )}
        {/* 缺失的包没有"卸载"可点，摆一个禁用按钮只是噪音 */}
        {item.status !== 'missing' && (
          <button
            type="button"
            className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px] text-lx-danger"
            disabled={disabled}
            onClick={() => onAct(item.name, 'uninstall')}
          >
            <Trash2 size={13} /> 卸载
          </button>
        )}
      </div>
    </div>
  )
}

export function Deps() {
  const [state, setState] = useState<DepState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState('')
  const [loading, setLoading] = useState(false)
  const [showLog, setShowLog] = useState(true)
  // 选中的 torch 轮子档位：'' / 'auto' = 按本机推荐。只对这一页的 torch 两行
  // 生效，不写进配置——它是"这一次装哪个轮子"的选择，不是全局口径。
  const [torchVariant, setTorchVariant] = useState('auto')
  const logRef = useRef<HTMLPreElement>(null)

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      setState(await api.deps())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (!quiet) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const running = state?.job?.status === 'running'

  // pip 在跑时每秒拉一次：日志要能看到"正在下载 torch-2.5.1..."这种进展，
  // 十几分钟的安装没有输出是最难熬的。
  useEffect(() => {
    if (!running) return
    const t = window.setInterval(() => void load(true), 1000)
    return () => window.clearInterval(t)
  }, [running, load])

  // 跑完立刻再拉一次：这时磁盘上的版本才刚变，页面得跟上。
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !running) void load(true)
    wasRunning.current = !!running
  }, [running, load])

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [state?.job?.log.length])

  async function act(name: string, action: 'install' | 'uninstall' | 'reinstall') {
    // 装卸会改真实环境：版本不符的包点"重装"是把清单版本**装回去**，
    // 本机如果手动装过更新的 torch，这一下会把它降级、再下 2.5GB。
    // 装缺失的包不拦（那是纯加分），另外两个必须让人确认一次。
    if (action !== 'install') {
      const what = action === 'uninstall' ? '卸载' : '卸载后按清单重装'
      // 配对提示要写进弹窗：重装 torch 会连 torchvision 一起卸装，不说清
      // 白的话，用户看到批量列表多出一行会以为哪里点错了。
      const peer =
        action === 'reinstall'
          ? state?.groups.flatMap((g) => g.items).find((r) => r.name === name)?.torch_peer ?? ''
          : ''
      const extra = peer
        ? `\n\n注意：${name} 与 ${peer} 必须同版本配套，会一并重装（先 ${peer} 后 ${name}）。`
        : ''
      const ok = window.confirm(
        `${what} ${name}？\n\n` +
          '这会直接改动 env\\python 里的依赖。如果版本与清单不符，"重装"会把清单要求的版本装回来。' +
          extra,
      )
      if (!ok) return
    }
    setPending(`${name}:${action}`)
    setError(null)
    try {
      // torch 两行带上用户选的档位；其余包的 torch_variant 后端会忽略。
      const item = state?.groups.flatMap((g) => g.items).find((r) => r.name === name)
      const variant = item?.torch_index ? torchVariant : ''
      setState(await api.depAction(name, action, variant))
      setShowLog(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPending('')
    }
  }

  async function cancel() {
    try {
      setState(await api.depCancel())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  /** 三个批量动作走同一条路：都是起一个后台队列，然后轮询看进度。 */
  async function bulk(kind: 'missing' | 'uninstall' | 'verify') {
    if (kind === 'uninstall') {
      // 这一下会把界面自己赖以运行的 fastapi / uvicorn 一起卸掉：页面上还
      // 能看见是因为进程已经加载过模块，下次启动就起不来了。必须让人看清。
      const ok = window.confirm(
        `要卸载这 ${installedCount} 个包吗？\n\n` +
          '这会把 env\\python 退回裸解释器：打标器和这个界面都会立刻起不来，' +
          '只能重跑 install.bat 或到这台机器上逐个装回来。\n\n' +
          '产物文件不受影响。',
      )
      if (!ok) return
    }
    setPending(`bulk:${kind}`)
    setError(null)
    try {
      if (kind === 'missing') {
        setState(await api.depInstallMissing())
      } else if (kind === 'uninstall') {
        setState(await api.depUninstallAll())
      } else {
        setState(await api.depVerify())
      }
      setShowLog(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPending('')
    }
  }

  /** 附加依赖组（convert / quantize / …）：只装这一组的缺失项。
   *
   * 不走 `bulk('missing')`：那个是"整台机器的核心清单"，而这里每一组都可能
   * 装不上或不该装（vllm 在 Windows 上装不了、llmcompressor 几百 MB），
   * 让用户一组一组点，才知道自己在往盘里放什么。
   */
  async function installExtra(extra: string) {
    setPending(`extra:${extra}`)
    setError(null)
    try {
      setState(await api.depInstallExtra(extra))
      setShowLog(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPending('')
    }
  }

  const summary = state?.summary
  const job = state?.job
  const batch = job?.batch ?? []
  const batchDone = batch.filter((b) => b.status === 'ok' || b.status === 'failed' || b.status === 'skipped').length
  const current = batch.find((b) => b.status === 'running')
  const failedItems = batch.filter((b) => b.status === 'failed')
  const checked = (state?.verify_at ?? 0) > 0
  const installedCount = summary ? summary.total - summary.missing : 0
  // 缺的包里有一部分是这台机器根本装不了的（Windows 上的 vllm）：
  // 它们照常显示在行里，但不能算进"要装几个"，否则点一下必然报错。
  const blocked = summary?.blocked ?? 0
  const canInstall = (summary?.missing ?? 0) - blocked
  const blockedPlatforms = [
    ...new Set(
      (state?.groups ?? []).flatMap((g) =>
        g.items
          .filter((i) => i.status === 'missing' && i.supported === false)
          .flatMap((i) => i.platforms ?? []),
      ),
    ),
  ].join(' / ')

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<PackageCheck size={17} />}
        eyebrow={state?.python ? `装到 ${state.python}` : '读清单中'}
        title="依赖检测"
        description="可对单个包装卸或重装。"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1.5"
              disabled={!!running || !installedCount}
              title={
                checked
                  ? '元数据在、包却坏了（下载中断、DLL 被删）也会被判出来'
                  : '逐个真正 import 一遍，验证包是能用的'
              }
              onClick={() => void bulk('verify')}
            >
              {pending === 'bulk:verify' || (running && job?.action === 'verify') ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <ShieldCheck size={14} />
              )}
              检查完整性
            </button>
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1.5 text-lx-danger"
              disabled={!!running || !installedCount}
              title="卸载清单里所有已装的包，环境退回裸解释器"
              onClick={() => void bulk('uninstall')}
            >
              <Trash2 size={14} /> 卸载全部
            </button>
            <button
              type="button"
              className="lx-btn-primary inline-flex items-center gap-1.5"
              disabled={!!running || !canInstall}
              title={
                canInstall
                  ? `装上这 ${canInstall} 个缺失的包（不动版本不符的；下面各附加依赖组用组内的「安装本组」）`
                  : blocked
                    ? `缺的 ${blocked} 个只有 ${blockedPlatforms || '别的系统'} 版，这台机器装上也是坏的`
                    : '没有缺失的包'
              }
              onClick={() => void bulk('missing')}
            >
              {pending === 'bulk:missing' || (running && job?.action === 'install-missing') ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <Download size={14} />
              )}
              安装缺失
              {canInstall ? (
                <span className="rounded-full bg-lx-on-accent/20 px-1.5 text-[10.5px] tabular-nums">
                  {canInstall}
                </span>
              ) : null}
            </button>
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1.5"
              disabled={loading || running}
              onClick={() => void load()}
            >
              <RefreshCw size={14} className={loading || running ? 'animate-spin' : undefined} />
              刷新
            </button>
          </div>
        }
      />

      {error && <ErrorBox error={error} title="操作没有成功" />}

      {summary && (
        <div className={`grid gap-3 ${checked ? 'grid-cols-2 md:grid-cols-4' : 'grid-cols-3'}`}>
          {[
            { label: '正常', value: summary.ok, tone: 'text-lx-success' },
            { label: '缺失', value: summary.missing, tone: summary.missing ? 'text-lx-danger' : 'text-lx' },
            {
              label: '版本不符',
              value: summary.mismatch,
              tone: summary.mismatch ? 'text-lx-warning' : 'text-lx',
            },
            // 第四张卡只在查过之后出现：没查过就摆个 0，会让人以为已经验证过了
            ...(checked
              ? [
                  {
                    label: '导入失败',
                    value: summary.broken,
                    tone: summary.broken ? 'text-lx-danger' : 'text-lx',
                  },
                ]
              : []),
          ].map((card) => (
            <div key={card.label} className="lx-sheet px-4 py-3">
              <div className={`text-[22px] font-semibold tabular-nums leading-none ${card.tone}`}>
                {card.value}
              </div>
              <div className="mt-1 text-[11px] text-lx-muted">
                {card.label}
                <span className="text-lx-dim"> / {summary.total}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 正在装的那一包：状态 + 日志。pip 的输出是这个页面唯一能让人放心的东西 */}
      {job && (
        <section className="lx-sheet overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-[18px] py-3 md:px-7">
            {job.status === 'running' ? (
              <Loader2 size={15} className="animate-spin text-lx-accent" />
            ) : job.status === 'done' ? (
              <CircleCheck size={15} className="text-lx-success" />
            ) : job.status === 'cancelled' ? (
              <CircleAlert size={15} className="text-lx-warning" />
            ) : (
              <CircleX size={15} className="text-lx-danger" />
            )}
            <div className="min-w-0 flex-1">
              <div className="truncate text-[12.5px] font-medium text-lx">
                {ACTION_LABEL[job.action] ?? job.action}
                {/* 批量给"第几个/共几个"，单包给包名——两个数字都摆出来反而多一层读 */}
                {batch.length > 1 ? ` · ${batchDone}/${batch.length}` : ` ${job.target}`}
                {job.status === 'running' ? ' …' : ''}
              </div>
              <div className="mt-0.5 text-[10.5px] text-lx-muted tabular-nums">
                {current && batch.length > 1 ? `正在 ${current.name} · ` : ''}
                {fmtElapsed(job.elapsed)}
                {job.exit_code !== null && job.exit_code !== undefined
                  ? ` · 退出码 ${job.exit_code}`
                  : ''}
                {job.status === 'failed' ? ' · 失败，日志末尾通常写着原因' : ''}
              </div>
              {/* 批量里失败的项要一眼看到：36 个包里坏了一个，混在日志里等于没看见 */}
              {failedItems.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-x-2.5 gap-y-0.5 text-[10.5px] text-lx-danger">
                  {failedItems.slice(0, 6).map((b) => (
                    <span key={b.name} title={b.detail}>
                      {b.name}
                      {b.detail ? `：${b.detail}` : ''}
                    </span>
                  ))}
                  {failedItems.length > 6 && <span>…共 {failedItems.length} 个失败</span>}
                </div>
              )}
            </div>
            {job.status === 'running' && (
              <button
                type="button"
                className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
                onClick={() => void cancel()}
              >
                <CircleX size={13} /> 取消
              </button>
            )}
            <button
              type="button"
              className="lx-btn-ghost inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
              onClick={() => setShowLog((v) => !v)}
            >
              {showLog ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
              {showLog ? '收起日志' : '看日志'}
            </button>
          </div>
          {showLog && job.log.length > 0 && (
            <pre
              ref={logRef}
              className="max-h-[260px] overflow-auto border-t border-lx bg-lx-surface-raised px-[18px] py-2.5 font-mono text-[10.5px] leading-relaxed text-lx-muted md:px-7"
            >
              {job.log.join('\n')}
            </pre>
          )}
        </section>
      )}

      {state ? (
        state.groups.map((group) => {
          // 缺的里也有本机装不了的（serve 那一组在 Windows 上整个都装不了）：
          // 按钮可不可点按"能装的"算，不然点下去只会得到一条报错。
          const blocked = group.blocked ?? 0
          const installable = (group.missing ?? 0) - blocked
          const only = [
            ...new Set(
              group.items
                .filter((i) => i.status === 'missing' && i.supported === false)
                .flatMap((i) => i.platforms ?? []),
            ),
          ].join(' / ')
          return (
            <section key={group.key} className="lx-sheet overflow-hidden">
              <SheetHead
                title={group.title}
                count={`${group.ok}/${group.total}`}
                hint={group.hint}
                trailing={
                  group.kind === 'extra' && group.extra ? (
                    <button
                      type="button"
                      className="lx-btn-primary inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px]"
                      disabled={
                        !!running || pending === `extra:${group.extra}` || !installable
                      }
                      title={
                        installable
                          ? `只装这一组缺的 ${installable} 个，不碰已装的`
                          : blocked
                            ? `这一组只有 ${only || '别的系统'} 版，这台机器装了也起不来`
                            : '这一组已经装齐了'
                      }
                      onClick={() => void installExtra(group.extra as string)}
                    >
                      {pending === `extra:${group.extra}` ||
                      (running && job?.action === `install-${group.extra}`) ? (
                        <Loader2 size={13} className="animate-spin" />
                      ) : (
                        <Download size={13} />
                      )}
                      安装本组
                      {installable ? (
                        <span className="rounded-full bg-lx-on-accent/20 px-1.5 text-[10.5px] tabular-nums">
                          {installable}
                        </span>
                      ) : null}
                    </button>
                  ) : undefined
                }
              />
              <div className="border-t border-lx">
                {group.items.map((item) => (
                  <DepRow
                    key={item.name}
                    item={item}
                    busy={!!running}
                    pending={pending}
                    torchOptions={item.torch_index ? state?.torch_options : undefined}
                    torchVariant={torchVariant}
                    onTorchVariant={setTorchVariant}
                    onAct={(name, action) => void act(name, action)}
                  />
                ))}
              </div>
            </section>
          )
        })
      ) : (
        !error && (
          <SectionPanel title="读取中">
            <p className="text-xs text-lx-muted">正在对照清单检查这台机器…</p>
          </SectionPanel>
        )
      )}

      <p className="px-1 text-[10.5px] leading-relaxed text-lx-dim">
        改动只影响 <span className="font-mono">env\python</span> 这个环境，不需要重跑 install.bat。
        装卸完成后建议重启打标器——当前进程里已经加载的模块不会因为卸载而消失。
        {state?.torch_index ? (
          <>
            <br />
            {torchVariant && torchVariant !== 'auto' ? (
              <>
                已选轮子档位：
                <span className="font-mono">
                  {state.torch_options?.find((v) => v.key === torchVariant)?.index ?? state.torch_index}
                </span>
                （点 torch 那一行的「档位」可换回自动）
              </>
            ) : (
              <>
                torch / torchvision 走 PyTorch 索引：
                <span className="font-mono">{state.torch_index}</span>
                （本机推荐；页面只给建议，换档位在 torch 那一行）
              </>
            )}
          </>
        ) : null}
        {/* 退到 CPU 版时必须说清为什么：页面上一切正常，只是慢十倍，
            不说一句用户只会以为这软件就这速度。 */}
        {state?.torch_note ? (
          <>
            <br />
            <span className="text-lx-warning">{state.torch_note}</span>
          </>
        ) : null}
      </p>
    </div>
  )
}
