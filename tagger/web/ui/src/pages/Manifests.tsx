import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle, CheckCircle2, CircleSlash, FileJson, Loader2, RefreshCw, ShieldCheck,
} from 'lucide-react'
import { api, ApiError, type BlobManifests, type BlobShardReport } from '../lib/api'
import { fmtBytes, fmtCount } from '../lib/format'
import {
  PageHeader, SectionPanel, StatusChip, type StatusTone,
} from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'

// 打标清单：汇总机上已上传的每一片标注，以及"合成整份"还差什么。
//
// 为什么单独一页而不是塞进「回传」：回传页回答"这台机器怎么把东西送出去"，
// 这一页回答"服务器上现在有多少、齐不齐、能不能开始训练"。视角不同，
// 一个人看自己，一个人看全局。

const KIND_LABEL: Record<string, string> = {
  skipped: '输入无效',
  artist: '无画师',
  excused: '重试后剔除',
  excluded: '其他剔除',
}

function toneOf(ok: boolean | undefined): StatusTone {
  if (ok === true) return 'success'
  if (ok === false) return 'warning'
  return 'neutral'
}

/** 概览小卡。项目里没有通用 StatCard，这里就地实现，样式跟随现有面板。 */
function Metric({
  label, value, hint, tone,
}: { label: string; value: string; hint?: string; tone?: StatusTone }) {
  return (
    <div className="rounded-lx border border-lx bg-lx-surface px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-wide text-lx-dim">{label}</div>
      <div
        className={`mt-0.5 text-lg font-medium tabular-nums ${
          tone === 'warning' ? 'text-lx-warning' : 'text-lx-primary'
        }`}
      >
        {value}
      </div>
      {hint && <div className="mt-0.5 text-[10px] leading-tight text-lx-muted">{hint}</div>}
    </div>
  )
}

function ShardRow({ s }: { s: BlobShardReport }) {
  const rep = s.verified
  // 一片的"健康度"只看两件事：有没有空行、上次校验过没过。
  // 缺某一侧不算错——只补 tag 的片本来就该只有 tag。
  const bad = s.empty > 0 || rep?.ok === false
  return (
    <div className="border-b border-lx py-2.5 last:border-b-0">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          {rep === null || rep === undefined ? (
            <CircleSlash size={13} className="shrink-0 text-lx-dim" />
          ) : bad ? (
            <AlertTriangle size={13} className="shrink-0 text-lx-warning" />
          ) : (
            <CheckCircle2 size={13} className="shrink-0 text-lx-success" />
          )}
          <span className="truncate font-mono text-[11px] text-lx-secondary" title={s.name}>
            {s.name}
          </span>
        </div>
        <StatusChip tone={toneOf(rep?.ok)}>
          {rep === null || rep === undefined ? '未校验' : rep.ok ? '通过' : '有问题'}
        </StatusChip>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 pl-[21px] text-[11px] text-lx-muted">
        <span>
          记录 <span className="tabular-nums text-lx-secondary">{fmtCount(s.rows)}</span>
        </span>
        <span>
          有效 <span className="tabular-nums text-lx-secondary">{fmtCount(s.tagged)}</span>
        </span>
        <span>
          两侧齐 <span className="tabular-nums text-lx-secondary">{fmtCount(s.both)}</span>
        </span>
        <span>
          仅 tag <span className="tabular-nums text-lx-secondary">{fmtCount(s.only_tag)}</span>
        </span>
        <span>
          仅 nl <span className="tabular-nums text-lx-secondary">{fmtCount(s.only_nl)}</span>
        </span>
        {s.excluded > 0 && (
          <span className="text-lx-secondary">
            剔除 {fmtCount(s.excluded)}
            {Object.entries(s.excluded_kinds).map(([k, v]) => (
              <span key={k} className="ml-1 text-lx-muted">
                {KIND_LABEL[k] ?? k} {fmtCount(v)}
              </span>
            ))}
          </span>
        )}
        {s.empty > 0 && <span className="text-lx-warning">空 {fmtCount(s.empty)}</span>}
        <span>{fmtBytes(s.bytes)}</span>
      </div>
      {rep?.problems && rep.problems.length > 0 && (
        <ul className="mt-1 space-y-0.5 pl-[21px] text-[11px] text-lx-warning">
          {rep.problems.slice(0, 3).map((p, i) => (
            <li key={i}>· {p}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function Manifests() {
  const [data, setData] = useState<BlobManifests | null>(null)
  const [busy, setBusy] = useState(false)
  const [verifying, setVerifying] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    setBusy(true)
    api
      .blobManifests()
      .then((d) => {
        setData(d)
        setError(null)
      })
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : String(err))
        setData(null)
      })
      .finally(() => setBusy(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function reverify(name: string) {
    setVerifying(name)
    try {
      await api.blobManifestVerify(name)
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setVerifying(null)
    }
  }

  const sum = data?.summary
  const unbalanced = sum ? sum.only_tag + sum.only_nl : 0
  const failed = data?.shards.filter((s) => s.verified?.ok === false).length ?? 0

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="打标清单"
        actions={
          <button
            type="button"
            className="lx-btn-ghost text-[11px]"
            onClick={load}
            disabled={busy}
          >
            {busy ? (
              <Loader2 size={12} className="mr-1.5 inline animate-spin" />
            ) : (
              <RefreshCw size={12} className="mr-1.5 inline" />
            )}
            刷新
          </button>
        }
      />

      <div className="min-h-0 flex-1 overflow-auto p-4">
        {error && (
          <div className="mb-3">
            <ErrorBox error={error} />
          </div>
        )}

        {/* 四个数是恒等式：记录总数 = 有效标注 + 已剔除 + 空记录。
            四个数一起摆，谁都能当场对上，不用自己拿总数减一遍——
            只报"可用标注"时，那个减法每个人算的结果都可能不一样。 */}
        <div className="mb-3 grid grid-cols-2 gap-2.5 lg:grid-cols-4">
          <Metric
            label="记录总数"
            value={sum ? fmtCount(sum.rows) : '—'}
            hint={sum ? `${sum.shards} 片 / ${fmtBytes(sum.bytes)}` : undefined}
          />
          <Metric
            label="有效标注"
            value={sum ? fmtCount(sum.tagged) : '—'}
            hint={
              sum ? `两侧齐 ${fmtCount(sum.both)} · 单侧 ${fmtCount(unbalanced)}` : undefined
            }
          />
          <Metric
            label="已剔除"
            value={sum ? fmtCount(sum.excluded) : '—'}
            hint={
              sum
                ? Object.entries(sum.kinds)
                    .map(([k, v]) => `${KIND_LABEL[k] ?? k} ${fmtCount(v)}`)
                    .join(' · ') || '无'
                : undefined
            }
          />
          <Metric
            label="空记录"
            value={sum ? fmtCount(sum.empty) : '—'}
            tone={(sum?.empty ?? 0) > 0 ? 'warning' : undefined}
            hint="既无内容也不是剔除记录"
          />
        </div>

        {(failed > 0 || (sum?.empty ?? 0) > 0) && (
          <div className="mb-3 flex items-start gap-2 rounded-lx border border-lx-warning/40 bg-lx-warning/5 px-3 py-2 text-[11px] text-lx-warning">
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
            <div className="space-y-0.5">
              {failed > 0 && <p>{failed} 片校验未通过，展开下面看具体原因。</p>}
              {(sum?.empty ?? 0) > 0 && (
                <p>
                  共 {sum?.empty} 条记录既无内容也不是剔除记录，通常是写盘中断，建议重传那几片。
                </p>
              )}
            </div>
          </div>
        )}

        <SectionPanel
          title="分片"
          trailing={
            <span className="font-mono text-[10px] text-lx-dim">{data?.root ?? ''}</span>
          }
        >
          {!data?.shards.length ? (
            <div className="py-8 text-center text-[11px] text-lx-muted">
              <FileJson size={18} className="mx-auto mb-2 opacity-50" />
              {busy ? '读取中…' : '还没有分片上到这里'}
            </div>
          ) : (
            <div>
              {data.shards.map((s) => (
                <div key={s.name} className="group relative">
                  <ShardRow s={s} />
                  <button
                    type="button"
                    className="absolute right-0 top-2 hidden text-[11px] text-lx-muted hover:text-lx-secondary group-hover:block"
                    onClick={() => void reverify(s.name)}
                    disabled={verifying === s.name}
                  >
                    {verifying === s.name ? (
                      <Loader2 size={11} className="animate-spin" />
                    ) : (
                      <ShieldCheck size={11} className="mr-0.5 inline" />
                    )}
                    重新校验
                  </button>
                </div>
              ))}
            </div>
          )}
        </SectionPanel>

        {/* 合并入口指向「数据打包」的拼接态：那套引擎已经在处理分片归并、
            去重与冲突裁决，这里再写一个只会是第二份口径。 */}
        <SectionPanel title="合成整份">
          <p className="text-[11px] leading-relaxed text-lx-muted">
            训练用的是把所有分片合并成的一份大 jsonl。合并会按 image_id 归并、
            区分「幂等重传」与「真冲突」，并把剔除记录排除在外——这一步在
            <span className="text-lx-secondary"> 数据打包 → 拼接 </span>
            里做，选这个仓库的目录即可。
          </p>
          {sum && (
            <p className="mt-2 text-[11px] leading-relaxed text-lx-muted">
              合成后 <span className="text-lx-secondary">{fmtCount(sum.tagged)}</span> 条
              （记录 {fmtCount(sum.rows)} − 剔除 {fmtCount(sum.excluded)} − 空{' '}
              {fmtCount(sum.empty)}）
              {unbalanced > 0 && (
                <>
                  ；其中 <span className="text-lx-secondary">{fmtCount(unbalanced)}</span> 条
                  只有单侧内容，确认这是任务口径要的形态
                </>
              )}
              。
            </p>
          )}
        </SectionPanel>
      </div>
    </div>
  )
}
