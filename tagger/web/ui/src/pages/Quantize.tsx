import { useEffect, useState } from 'react'
import {
  CheckCircle2,
  Gauge,
  Loader2,
  Play,
  RefreshCw,
  Square,
  XCircle,
} from 'lucide-react'
import {
  api,
  ApiError,
  type GgufTaskState,
  type QuantOverview,
  type QuantTaskState,
} from '../lib/api'
import { PageHeader, SectionPanel, SettingRow, StatusChip } from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'

// 模型量化页：**把四条路线摆在同一屏**，因为它们的本质不同，
// 混成一个「量化」按钮只会让人选错：
//
//   FP8     运行时档：vLLM 加载时转，没有产物
//   W4A16   离线：llm-compressor 吃 safetensors，产物是目录（给 vLLM）
//   Q4_K_M  离线：llama-quantize 吃 **F16 GGUF**，产物是单个 .gguf
//   Q6_K    同上
//
// 前两条走 GPU，后两条只要 CPU 和 llama.cpp 的可执行文件。所以能力探测
// 也是分开的：缺 torch 不影响 Q4，缺 llama-quantize 也不影响 W4A16。

function toneOf(status: string) {
  if (status === 'done') return 'success' as const
  if (status === 'error') return 'danger' as const
  if (status === 'canceled') return 'warning' as const
  if (status === 'running') return 'accent' as const
  return 'neutral' as const
}

const STATUS_LABEL: Record<string, string> = {
  running: '进行中',
  done: '完成',
  error: '失败',
  canceled: '已取消',
}

function Bar({ percent }: { percent: number }) {
  const width = Math.max(0, Math.min(100, percent))
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-[color-mix(in_srgb,var(--lx-text)_8%,transparent)]">
      <div
        className="h-full rounded-full bg-lx-accent transition-[width] duration-300"
        style={{ width: `${width}%` }}
      />
    </div>
  )
}

/** 两条链路共用的进度块：阶段 + 百分比 + 日志尾。 */
function TaskView({
  task,
  onCancel,
  busy,
}: {
  task: QuantTaskState | GgufTaskState
  onCancel: () => void
  busy: boolean
}) {
  const running = task.status === 'running'
  return (
    <div className="mt-3 space-y-2 rounded-lg border border-lx-line/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <StatusChip tone={toneOf(task.status)}>
            {STATUS_LABEL[task.status] ?? task.status}
          </StatusChip>
          <span className="text-xs text-lx-secondary">{task.stage}</span>
          <span className="font-mono text-[11px] tabular-nums text-lx-dim">
            {task.percent.toFixed(0)}% · {task.elapsed.toFixed(0)}s
          </span>
        </div>
        {running && (
          <button className="lx-btn-ghost" onClick={onCancel} disabled={busy}>
            <Square size={12} /> 取消
          </button>
        )}
      </div>
      <Bar percent={task.percent} />
      {task.message && (
        <p className="text-xs text-lx-muted">{task.message}</p>
      )}
      {task.log_tail.length > 0 && (
        // 日志用等宽 + 可滚：量化失败的原因几乎全在这里，藏起来等于让人重跑
        <pre className="max-h-40 overflow-auto rounded border border-lx-line/60 bg-[color-mix(in_srgb,var(--lx-text)_3%,transparent)] p-2 text-[11px] leading-relaxed text-lx-muted">
          {task.log_tail.slice(-60).join('\n')}
        </pre>
      )}
    </div>
  )
}

function TierCard({
  name,
  kind,
  detail,
  state,
  stateTone,
}: {
  name: string
  kind: string
  detail: string
  state: string
  stateTone: 'success' | 'danger' | 'warning' | 'neutral'
}) {
  return (
    <div className="rounded-lg border border-lx-line/60 p-3">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[13px] font-semibold text-lx">{name}</span>
        <span className="text-[10px] uppercase tracking-wide text-lx-dim">{kind}</span>
      </div>
      <p className="mb-2 text-[11px] leading-relaxed text-lx-muted">{detail}</p>
      <StatusChip tone={stateTone}>{state}</StatusChip>
    </div>
  )
}

export function Quantize() {
  const [overview, setOverview] = useState<QuantOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // W4A16
  const [model, setModel] = useState('')
  const [method, setMethod] = useState('gptq')
  const [outDir, setOutDir] = useState('')

  // GGUF：输出留空 = 后端按源文件名 + 档位自动命名
  const [ggufSource, setGgufSource] = useState('')
  const [ggufTarget, setGgufTarget] = useState('q4_k_m')
  const [ggufOut, setGgufOut] = useState('')

  const [busy, setBusy] = useState(false)
  // 点「开始」后立刻拉一次，不等下一个 5 秒轮询——否则那 5 秒会被当成卡住。
  const [tick, setTick] = useState(0)

  const running =
    overview?.w4a16.task?.status === 'running' ||
    overview?.gguf.task?.status === 'running'

  // 有任务在跑时 1.5s 一次，其余 5s：进度是阶段级的，再密看不出差别。
  useEffect(() => {
    let alive = true
    const load = () => {
      api
        .quantizeOverview(model || undefined)
        .then((data) => {
          if (!alive) return
          setOverview(data)
          setError(null)
        })
        .catch((err) => {
          if (!alive) return
          setError(err instanceof Error ? err.message : String(err))
        })
        .finally(() => {
          if (alive) setLoading(false)
        })
    }
    load()
    const timer = window.setInterval(load, running ? 1500 : 5000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [model, running, tick])

  const startW4a16 = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.quantizeStart({ model: model || undefined, method, out_dir: outDir })
      setTick((n) => n + 1)
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : String(err),
      )
    } finally {
      setBusy(false)
    }
  }

  const startGguf = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.ggufQuantStart({
        source: ggufSource,
        target: ggufFlag,
        out_file: ggufOut,
      })
      setTick((n) => n + 1)
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : String(err),
      )
    } finally {
      setBusy(false)
    }
  }

  const cancel = async (which: 'w4a16' | 'gguf') => {
    setBusy(true)
    try {
      if (which === 'w4a16') await api.quantizeCancel()
      else await api.ggufQuantCancel()
    } catch {
      // 取消失败不阻塞页面，下一次轮询会给出真实状态
    } finally {
      setBusy(false)
      setTick((n) => n + 1)
    }
  }

  const w4 = overview?.w4a16
  const gguf = overview?.gguf
  const fp8 = overview?.fp8
  const modelInfo = overview?.models.find((m) => m.key === (model || w4?.model))

  // 实际传给内核的类型名（Q4_K_M / Q4_K / Q6_K）
  const ggufFlag = gguf?.targets.find((t) => t.key === ggufTarget)?.flag ?? ggufTarget

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow="模型"
        title="模型量化"
        description="在自己的机器上把 bf16 权重压小。四条路线的依赖和产物都不一样，先看下面哪一条在这台机器上能跑。"
        icon={<Gauge size={20} />}
        actions={
          <button
            className="lx-btn-ghost"
            onClick={() => setLoading(true)}
            disabled={loading}
          >
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> 刷新
          </button>
        }
      />

      {error && <ErrorBox error={error} title="操作失败" />}

      <SectionPanel
        title="四条路线"
        description="前三张是离线产物，FP8 是运行时档位，不落盘。"
      >
        <div className="grid gap-2 md:grid-cols-2">
          <TierCard
            name="FP8"
            kind="运行时"
            detail={
              fp8?.note ??
              'vLLM 加载时把 bf16 转成 FP8，显存约省一半。没有文件产物。'
            }
            state={
              fp8?.supported ? '显卡支持（CC ≥ 8.9）' : '这张卡没有 FP8 tensor core'
            }
            stateTone={fp8?.supported ? 'success' : 'neutral'}
          />
          <TierCard
            name="W4A16"
            kind="离线"
            detail={
              '4bit 权重 + 16bit 激活，用 llm-compressor 跑 GPTQ/AWQ。' +
              '产物是目录，给 vLLM 用；需要 torch 与 NVIDIA 显卡，几十分钟。'
            }
            state={
              w4?.available
                ? '这台机器可以跑'
                : (w4?.reason || '正在检测…')
            }
            stateTone={w4?.available ? 'success' : 'danger'}
          />
          <TierCard
            name="Q4_K_M"
            kind="离线"
            detail={
              gguf?.targets.find((t) => t.key === 'q4_k_m')?.note ??
              '最小的 GGUF 档，给 llama.cpp 用。'
            }
            state={
              gguf?.available ? '这台机器可以跑' : (gguf?.reason || '正在检测…')
            }
            stateTone={gguf?.available ? 'success' : 'danger'}
          />
          <TierCard
            name="Q6_K"
            kind="离线"
            detail={
              gguf?.targets.find((t) => t.key === 'q6_k')?.note ??
              '介于 Q4 与 Q8 之间的 GGUF 档。'
            }
            state={
              gguf?.available ? '这台机器可以跑' : (gguf?.reason || '正在检测…')
            }
            stateTone={gguf?.available ? 'success' : 'danger'}
          />
        </div>
      </SectionPanel>

      <SectionPanel
        title="W4A16（safetensors → 目录）"
        description="吃原始 bf16 权重，产出 compressed-tensors 目录，完成后会写进模型配置。"
        trailing={
          w4?.available ? (
            <StatusChip tone="success" icon={<CheckCircle2 size={11} />}>
              可用
            </StatusChip>
          ) : (
            <StatusChip tone="danger" icon={<XCircle size={11} />}>
              不可用
            </StatusChip>
          )
        }
      >
        <div className="space-y-2">
          <SettingRow label="模型" help="只有配了原始权重的条目才列出来">
            <select
              className="lx-input"
              value={model || w4?.model || ''}
              onChange={(e) => setModel(e.target.value)}
              disabled={running || busy}
            >
              {(overview?.models ?? []).map((m) => (
                <option key={m.key} value={m.key}>
                  {m.key}
                  {m.has_quantized ? '（已有 4bit 权重）' : ''}
                </option>
              ))}
            </select>
          </SettingRow>

          <div className="grid grid-cols-2 gap-2">
            <SettingRow label="算法" help="产物格式相同，只是量化误差分布不同">
              <select
                className="lx-input"
                value={method}
                onChange={(e) => setMethod(e.target.value)}
                disabled={running || busy}
              >
                {(w4?.methods ?? ['gptq', 'awq']).map((m) => (
                  <option key={m} value={m}>
                    {m.toUpperCase()}
                  </option>
                ))}
              </select>
            </SettingRow>
            <SettingRow label="输出目录" help="留空 = models/nl/<key>-w4a16">
              <input
                className="lx-input font-mono text-xs"
                value={outDir}
                onChange={(e) => setOutDir(e.target.value)}
                placeholder={w4?.suggested_dir || 'models/nl/…-w4a16'}
                spellCheck={false}
                disabled={running || busy}
              />
            </SettingRow>
          </div>

          {modelInfo?.has_quantized && (
            <p className="px-1 text-[11px] text-lx-muted">
              这个模型已经有 4bit 权重（{modelInfo.quantized_path}）。再跑一次会覆盖它。
            </p>
          )}

          {!w4?.available && w4?.reason && (
            <p className="rounded border border-lx-line/60 p-2 text-[11px] leading-relaxed text-lx-muted">
              {w4.reason}
            </p>
          )}

          <div className="flex items-center gap-2 pt-1">
            <button
              className="lx-btn-primary"
              onClick={startW4a16}
              disabled={!w4?.available || running || busy}
            >
              {running && w4?.task?.status === 'running' ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Play size={13} />
              )}
              开始量化
            </button>
            {w4?.task?.out_dir && (
              <span className="truncate font-mono text-[11px] text-lx-dim">
                → {w4.task.out_dir}
              </span>
            )}
          </div>

          {w4?.task && (
            <TaskView
              task={w4.task}
              busy={busy}
              onCancel={() => cancel('w4a16')}
            />
          )}
        </div>
      </SectionPanel>

      <SectionPanel
        title="GGUF（F16 → Q4_K_M / Q6_K 或自定义）"
        description="调 llama.cpp 的 llama-quantize，几十秒到几分钟，不需要显卡。源文件必须是 GGUF。"
        trailing={
          gguf?.available ? (
            <StatusChip tone="success" icon={<CheckCircle2 size={11} />}>
              可用
            </StatusChip>
          ) : (
            <StatusChip tone="warning" icon={<XCircle size={11} />}>
              不可用
            </StatusChip>
          )
        }
      >
        <div className="space-y-2">
            <SettingRow
              label="源文件"
              help="F16 或 BF16 的 .gguf。safetensors 要先转成 GGUF，这一步页面不做。"
            >
              <input
                className="lx-input font-mono text-xs"
                value={ggufSource}
                onChange={(e) => setGgufSource(e.target.value)}
                placeholder="models/nl/Qwen3VL-8B-Instruct-F16.gguf"
                spellCheck={false}
                disabled={running || busy}
              />
            </SettingRow>

            <div className="grid grid-cols-2 gap-2">
              <SettingRow
                label="目标档位"
                help="预设是实测过的两档；其余类型选「自定义」手填"
              >
                <select
                  className="lx-input"
                  value={ggufTarget}
                  onChange={(e) => setGgufTarget(e.target.value)}
                  disabled={running || busy}
                >
                  {(gguf?.targets ?? []).map((t) => (
                    <option key={t.key} value={t.key}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </SettingRow>
              <SettingRow label="输出文件" help="留空 = 源文件名 + 档位">
                <input
                  className="lx-input font-mono text-xs"
                  value={ggufOut}
                  onChange={(e) => setGgufOut(e.target.value)}
                  placeholder="models/nl/…-Q4_K_M.gguf"
                  spellCheck={false}
                  disabled={running || busy}
                />
              </SettingRow>
            </div>

            <div className="flex items-center gap-2 pt-1">
              <button
                className="lx-btn-primary"
                onClick={startGguf}
                disabled={
                  !gguf?.available || running || busy || !ggufSource.trim() || !ggufFlag
                }
              >
                {gguf?.task?.status === 'running' ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Play size={13} />
                )}
                开始量化
              </button>
              {gguf?.task?.out_file && (
                <span className="truncate font-mono text-[11px] text-lx-dim">
                  → {gguf.task.out_file}
                </span>
              )}
            </div>

            {/* 说清楚"这是谁在算"：不装任何外部程序，就是本机 numpy 在跑 */}
            <p className="px-1 text-[11px] text-lx-dim">
              引擎：{gguf?.engine ?? '内置 numpy'}
              {gguf?.reason ? ` · ${gguf.reason}` : ''}
            </p>

            {gguf?.task && (
              <TaskView
                task={gguf.task}
                busy={busy}
                onCancel={() => cancel('gguf')}
              />
            )}
        </div>
      </SectionPanel>
    </div>
  )
}
