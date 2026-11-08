import { useEffect, useMemo } from 'react'
import {
  Activity,
  Ban,
  CheckCheck,
  CircleSlash,
  Clock,
  Gauge,
  ListChecks,
  Loader2,
  PackageOpen,
  PackageCheck,
  Pause,
  Play,
  RotateCcw,
  ShieldAlert,
  Square,
  Wand2,
} from 'lucide-react'
import { useLive } from '../lib/store'
import { basename, fmtCount, fmtPct, fmtSeconds } from '../lib/format'
import { PageHeader, SectionPanel, SheetHead, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'
import { OverviewFigure, OverviewFigureRow } from '../components/ui/OverviewFigures'
import { ProgressBar } from '../components/ui/ProgressBar'

const STATUS: Record<string, { label: string; tone: StatusTone }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '运行中', tone: 'accent' },
  paused: { label: '已暂停', tone: 'warning' },
  done: { label: '已完成', tone: 'success' },
  stopped: { label: '已停止', tone: 'warning' },
  failed: { label: '失败', tone: 'danger' },
}

function phaseLabel(status: string | undefined, phase: string, enabled: boolean): string {
  if (status === 'running') {
    if (phase === 'pack') return '封包中'
    const base = '打标中'
    return enabled ? `${base} · 之后封包` : base
  }
  return STATUS[status ?? 'pending']?.label ?? '—'
}

export function RunJob({
  jobId,
  onResults,
  onNew,
}: {
  jobId: string
  onResults: () => void
  onNew: () => void
}) {
  const { snap, events, connected, polling, error, attach, detach, stop, pause, resume } =
    useLive()

  useEffect(() => {
    attach(jobId)
    return () => detach()
  }, [jobId, attach, detach])

  const items = useMemo(
    () =>
      events
        .filter((e) => e.kind === 'item' || e.kind === 'pack_item' || e.kind === 'guard')
        .slice(-120)
        .reverse(),
    [events],
  )
  const logs = useMemo(() => events.filter((e) => e.kind === 'log'), [events])
  // 最近一次 guard 通知：非 ok 级别要顶到页面上，不能让它只躺在日志里
  const guardEv = useMemo(
    () => [...events].reverse().find((e) => e.kind === 'guard'),
    [events],
  )
  const finished = snap ? ['done', 'stopped', 'failed'].includes(snap.status) : false
  const status = STATUS[snap?.status ?? 'pending'] ?? STATUS.pending
  const pack = snap?.pack
  const packing = snap?.phase === 'pack'
  const sealed = pack?.packs ?? []

  const pct = (snap?.progress ?? 0) * 100
  const speed = snap && snap.rate_per_s > 0 ? `${snap.rate_per_s.toFixed(2)} 张/s` : '—'
  const perImage = snap && snap.rate_per_s > 0 ? `${(1 / snap.rate_per_s).toFixed(2)}` : '—'
  const total = snap?.counts.total ?? 0
  const settled = snap?.counts.settled ?? 0
  // progress 为 null = 流式任务（HF 分片），总量事先不知道：
  // 显示"已结算 N 张 + 速率"，不硬造百分比
  const streaming = snap !== null && snap?.progress === null
  const title = snap
    ? snap.spec.engine === 'vlm' && snap.spec.hf_repo
      ? `HF ${snap.spec.hf_repo}`
      : basename(snap.spec.input_dir) || '…'
    : '…'

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<Activity size={17} />}
        eyebrow={
          finished
            ? '任务已结束'
            : packing
              ? '正在读产物封包'
              : '正在逐张推理'
        }
        title={title}
        description={
          snap
            ? `${snap.out_dir || '（默认输出目录）'} · ${
                snap.spec.engine === 'vlm' ? `VLM ${snap.spec.vlm_model} · ` : ''
              }${snap.spec.backend} · ${snap.spec.precision} · batch ${snap.spec.batch}${
                snap.spec.preprocess === 'tensor' ? ' · GPU 预处理' : ''
              }${
                pack?.enabled
                  ? ` · 封包 ${pack.formats.join('/')} q${pack.quality}`
                  : ''
              }`
            : '正在读取任务信息…'
        }
        badges={
          <StatusChip
            tone={status.tone}
            icon={snap?.status === 'running' ? <Loader2 size={11} className="animate-spin" /> : undefined}
          >
            {phaseLabel(snap?.status, snap?.phase ?? '', Boolean(pack?.enabled))}
          </StatusChip>
        }
        actions={
          finished ? (
            <>
              <button type="button" className="lx-btn-ghost inline-flex items-center gap-1.5" onClick={onNew}>
                <RotateCcw size={14} /> 再跑一批
              </button>
              <button
                type="button"
                className="lx-btn-primary inline-flex items-center gap-1.5"
                onClick={onResults}
              >
                <ListChecks size={14} /> 查看结果
              </button>
            </>
          ) : (
            <>
              {snap?.status === 'paused' ? (
                <button
                  type="button"
                  className="lx-btn-primary inline-flex items-center gap-1.5"
                  onClick={() => void resume()}
                >
                  <Play size={13} /> 继续
                </button>
              ) : (
                <button
                  type="button"
                  className="lx-btn-ghost inline-flex items-center gap-1.5"
                  onClick={() => void pause()}
                  disabled={!snap || snap.phase === 'pack'}
                  title={
                    snap?.phase === 'pack'
                      ? '封包阶段不能暂停：tar/zip 不支持追加，停下只会留下半成品'
                      : '在途的几张打完就挂起，产物留在盘上'
                  }
                >
                  <Pause size={13} /> 暂停
                </button>
              )}
              <button
                type="button"
                className="lx-btn-danger inline-flex items-center gap-1.5"
                onClick={() => void stop()}
                disabled={!snap}
              >
                <Square size={13} /> 停止
              </button>
            </>
          )
        }
      />

      {error && <ErrorBox error={error} title="进度推送中断" />}
      {polling && !connected && (
        <ErrorBox
          tone="warning"
          title="实时推送断开"
          error="已退回每秒轮询：数据会慢一拍，但不会停。"
        />
      )}
      {snap?.status === 'failed' && <ErrorBox error={snap.error || '任务失败'} title="任务失败" />}
      {guardEv && guardEv.level !== 'ok' && (
        <ErrorBox
          tone={guardEv.level === 'stop' ? 'danger' : 'warning'}
          title="硬件保护介入"
          error={`${guardEv.level === 'stop' ? '已熔断停止' : '降速/暂停中'}：${guardEv.message ?? ''}`}
        />
      )}
      {snap?.status === 'paused' && (
        <ErrorBox
          tone="warning"
          title="已暂停"
          error="在途的几张打完就停住了，产物都在盘上。可以直接关掉打标器——下次打开会出现「继续上次任务」，从断点接着做，不重打。"
        />
      )}
      {snap?.status === 'stopped' && (
        <ErrorBox
          tone="warning"
          title="已停止"
          error="已算完的部分都落盘了，重新开始会自动跳过它们。停止的任务不封包——半批数据不该进下游。"
        />
      )}
      {snap?.status === 'done' && snap.spec.engine === 'local' && !snap.spec.pack && (
        <div className="flex items-start gap-3 rounded-lx-sm border-l-2 border-lx-accent bg-lx-accent-subtle px-4 py-3">
          <PackageOpen className="mt-0.5 h-4 w-4 shrink-0 text-lx-accent" />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-lx-accent">下一步</div>
            <p className="mt-1 text-xs text-lx-secondary">
              tags 产物已就绪。到左侧「数据打包」页选「封包」，把它封成训练用分片。
            </p>
          </div>
        </div>
      )}

      <section className="lx-sheet">
        <div className="px-[18px] py-5 md:px-7">
          <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
            <div className="min-w-0">
              {streaming ? (
                <>
                  <div className="font-display text-[38px] font-semibold leading-none tabular-nums tracking-tight text-lx">
                    {fmtCount(settled)}
                  </div>
                  <div className="mt-2 text-xs text-lx-muted">
                    张已结算 · 总量未知（流式读取分片）
                    {snap?.current && !finished && (
                      <span className="ml-2 truncate font-mono text-[11px] text-lx-dim">
                        {snap.current}
                      </span>
                    )}
                  </div>
                </>
              ) : (
                <>
                  <div className="font-display text-[38px] font-semibold leading-none tabular-nums tracking-tight text-lx">
                    {fmtPct(snap?.progress ?? 0)}
                  </div>
                  <div className="mt-2 text-xs text-lx-muted">
                    {packing && pack
                      ? `${fmtCount(pack.settled)} / ${fmtCount(pack.total)} 张已封包`
                      : `${fmtCount(settled)} / ${fmtCount(total)} 张已结算`}
                    {snap?.current && !finished && (
                      <span className="ml-2 truncate font-mono text-[11px] text-lx-dim">
                        {snap.current}
                      </span>
                    )}
                  </div>
                </>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[11.5px] text-lx-muted">
              <span className="flex items-center gap-1.5">
                <Clock size={12} /> 已用 {fmtSeconds(snap?.elapsed_s ?? 0)}
              </span>
              <span className="flex items-center gap-1.5">
                <Gauge size={12} />{' '}
                {packing && pack && pack.rate_per_s > 0
                  ? `${pack.rate_per_s.toFixed(2)} 张/s（封包）`
                  : speed}
              </span>
              {snap?.eta_s != null && !finished && !packing && (
                <span className="tabular-nums">约剩 {fmtSeconds(snap.eta_s)}</span>
              )}
            </div>
          </div>

          {!streaming && (
            <ProgressBar
              percent={pct}
              tone={snap?.status === 'failed' ? 'danger' : snap?.status === 'done' ? 'success' : 'accent'}
              heightClass="h-1.5"
              className="mt-4"
            />
          )}
        </div>

        <div className="px-[18px] pb-5 md:px-7">
          <OverviewFigureRow>
            <OverviewFigure label="完成" value={fmtCount(snap?.counts.done ?? 0)} tone="success" first />
            <OverviewFigure label="跳过（已有）" value={fmtCount(snap?.counts.skipped ?? 0)} />
            {(snap?.counts.busy ?? 0) > 0 && (
              <OverviewFigure label="其他进程处理中" value={fmtCount(snap?.counts.busy ?? 0)} tone="warning" />
            )}
            <OverviewFigure
              label="失败"
              value={fmtCount(snap?.counts.failed ?? 0)}
              tone={snap?.counts.failed ? 'danger' : undefined}
            />
            {(snap?.counts.blacklisted ?? 0) > 0 && (
              <OverviewFigure label="拉黑" value={fmtCount(snap?.counts.blacklisted ?? 0)} tone="warning" />
            )}
            {/* token 只在本机模式是 0：本机不计费，整天挂着一行 0 是噪音。 */}
            {(snap?.usage.prompt_tokens ?? 0) > 0 && (
              <OverviewFigure
                label="输入 token"
                value={fmtCount(snap?.usage.prompt_tokens ?? 0)}
                note={snap?.usage.cost != null ? '计费大头' : undefined}
              />
            )}
            {(snap?.usage.rate_limited ?? 0) > 0 && (
              <OverviewFigure
                label="被限流"
                value={fmtCount(snap?.usage.rate_limited ?? 0)}
                note="429"
                tone="warning"
              />
            )}
            {snap?.usage.cost != null && (
              <OverviewFigure
                label="估算费用"
                value={snap.usage.cost.toFixed(3)}
                note="按 nl_api.json 价目"
              />
            )}
            <OverviewFigure label="平均每张" value={perImage} note="秒" />
          </OverviewFigureRow>
        </div>

        <div className="border-t border-lx px-[18px] py-3 md:px-7">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-[11px] text-lx-muted">
            <span>
              解码合计{' '}
              <span className="font-mono tabular-nums text-lx-secondary">
                {((snap?.timings_ms.decode ?? 0) / 1000).toFixed(1)}s
              </span>
            </span>
            <span>
              前向合计{' '}
              <span className="font-mono tabular-nums text-lx-secondary">
                {((snap?.timings_ms.forward ?? 0) / 1000).toFixed(1)}s
              </span>
            </span>
            <span className="ml-auto">{connected ? '实时推送中' : polling ? '轮询中' : '—'}</span>
          </div>
        </div>
      </section>

      <div className="grid gap-3.5 lg:grid-cols-[minmax(0,1fr)_318px]">
        <section className="lx-sheet flex min-h-0 flex-col overflow-hidden">
          <SheetHead
            title={packing ? '封包活动' : '实时活动'}
            count={items.length}
            trailing={
              <span className="text-[11px] text-lx-muted">
                {connected ? '最新在顶部' : '轮询中'}
              </span>
            }
          />
          <div className="max-h-[430px] overflow-y-auto border-t border-lx">
            {items.length === 0 ? (
              <div className="flex items-center gap-2 px-5 py-10 text-xs text-lx-muted">
                <Wand2 size={13} /> 还没有产出。第一批通常在几秒内出现。
              </div>
            ) : (
              items.map((e) => (
                <div
                  key={e.seq}
                  className="lx-sheet-row flex items-center gap-2.5 px-[18px] py-2 text-xs md:px-7"
                >
                  {e.kind === 'guard' ? (
                    <>
                      <ShieldAlert
                        size={13}
                        className={`shrink-0 ${
                          e.level === 'stop'
                            ? 'text-lx-danger'
                            : e.level === 'ok'
                              ? 'text-lx-success'
                              : 'text-lx-warning'
                        }`}
                      />
                      <span className="min-w-0 flex-1 truncate text-[11px] text-lx-secondary">
                        硬件保护 · {e.level === 'ok' ? '恢复正常' : (e.level ?? '')}
                      </span>
                      <span className="hidden min-w-0 flex-1 truncate text-[10.5px] text-lx-dim sm:block">
                        {e.message}
                      </span>
                    </>
                  ) : (
                    <>
                      {e.skipped ? (
                        <CircleSlash size={13} className="shrink-0 text-lx-muted" />
                      ) : e.ok ? (
                        e.kind === 'pack_item' ? (
                          <PackageCheck size={13} className="shrink-0 text-lx-success" />
                        ) : (
                          <CheckCheck size={13} className="shrink-0 text-lx-success" />
                        )
                      ) : (
                        <Ban size={13} className="shrink-0 text-lx-danger" />
                      )}
                      <span
                        className="min-w-0 flex-1 truncate font-mono text-[11px] text-lx-secondary"
                        title={e.image_id}
                      >
                        {e.image_id}
                      </span>
                      {e.kind === 'pack_item' ? (
                        <span className="shrink-0 text-[11px] text-lx-muted">
                          {e.skipped ? '已封过' : e.missing ? '无产物' : e.ok ? '已写入' : '失败'}
                        </span>
                      ) : e.skipped ? (
                        <span className="shrink-0 text-[11px] text-lx-muted">已存在</span>
                      ) : e.ok ? (
                        <>
                          <span className="shrink-0 text-[11px] tabular-nums text-lx-muted">
                            {e.tag_count} tag
                          </span>
                          <span className="hidden min-w-0 flex-1 truncate text-[10.5px] text-lx-dim sm:block">
                            {(e.tags ?? []).slice(0, 3).join(' · ')}
                          </span>
                        </>
                      ) : (
                        <span className="min-w-0 flex-1 truncate text-[11px] text-lx-danger" title={e.error}>
                          {e.error}
                        </span>
                      )}
                    </>
                  )}
                </div>
              ))
            )}
          </div>
        </section>

        <aside className="flex min-w-0 flex-col gap-3.5">
          {pack?.enabled && (
            <SectionPanel title="封包" trailing={<StatusChip tone={sealed.length ? 'success' : 'neutral'}>{sealed.length} 个分片</StatusChip>}>
              <div className="space-y-1.5 text-xs">
                <div className="flex justify-between gap-2">
                  <span className="text-lx-muted">写入</span>
                  <span className="tabular-nums text-lx-secondary">
                    {fmtCount(pack.packed)} / {fmtCount(pack.total)}
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-lx-muted">跳过（已封过）</span>
                  <span className="tabular-nums text-lx-secondary">{fmtCount(pack.skipped)}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-lx-muted">缺产物</span>
                  <span className={`tabular-nums ${pack.missing ? 'text-lx-warning' : 'text-lx-secondary'}`}>
                    {fmtCount(pack.missing)}
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-lx-muted">失败</span>
                  <span className={`tabular-nums ${pack.failed ? 'text-lx-danger' : 'text-lx-secondary'}`}>
                    {fmtCount(pack.failed)}
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-lx-muted">分片体积</span>
                  <span className="tabular-nums text-lx-secondary">
                    {(pack.bytes / 1073741824).toFixed(2)} GB
                  </span>
                </div>
              </div>
              {sealed.length > 0 && (
                <div className="mt-3 max-h-[160px] space-y-1 overflow-y-auto border-t border-lx pt-2 font-mono text-[10.5px] text-lx-muted">
                  {sealed
                    .slice()
                    .reverse()
                    .map((p) => (
                      <div key={p.name} className="flex justify-between gap-2">
                        <span className="min-w-0 truncate" title={p.name}>
                          {p.name}
                        </span>
                        <span className="shrink-0 tabular-nums">
                          {fmtCount(p.count)} · {(p.bytes / 1048576).toFixed(1)} MB
                        </span>
                      </div>
                    ))}
                </div>
              )}
            </SectionPanel>
          )}

          <SectionPanel title="失败原因">
            {Object.keys(snap?.by_reason ?? {}).length === 0 ? (
              <p className="text-xs text-lx-muted">暂无失败。</p>
            ) : (
              <ul className="space-y-1.5 text-xs">
                {Object.entries(snap?.by_reason ?? {})
                  .sort((a, b) => b[1] - a[1])
                  .slice(0, 8)
                  .map(([reason, n]) => (
                    <li key={reason} className="flex items-start justify-between gap-2">
                      <span className="min-w-0 break-all text-lx-muted">{reason}</span>
                      <span className="shrink-0 font-mono tabular-nums text-lx-danger">{n}</span>
                    </li>
                  ))}
              </ul>
            )}
          </SectionPanel>

          <SectionPanel title="日志">
            {logs.length === 0 ? (
              <p className="flex items-center gap-1.5 text-xs text-lx-muted">
                <Wand2 size={12} /> 等后端加载模型…
              </p>
            ) : (
              <ul className="space-y-1.5 font-mono text-[11px] leading-relaxed text-lx-muted">
                {logs.slice(-8).map((l) => (
                  <li key={l.seq} className="break-words">
                    {l.message}
                  </li>
                ))}
              </ul>
            )}
          </SectionPanel>
        </aside>
      </div>
    </div>
  )
}
