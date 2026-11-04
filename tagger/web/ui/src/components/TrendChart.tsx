/**
 * 手写 SVG 折线图——统计页的趋势图。
 *
 * 不引图表库：本项目前端零依赖（App.tsx 的路由注释同款原则），而这张图
 * 的需求（几条 polyline + 悬停读数）手写恰好是最短的路径。视觉全部走
 * lx token：网格线用 --lx-border，文字用 --lx-text-dim，线色由调用方给。
 *
 * 坐标约定：左轴是占比 0~100%，右轴是质检分 1~5。两条轴的刻度线故意
 * 共用同一组水平网格（5 格），图面只有一套网格线，不会花。
 */
import { useEffect, useRef, useState } from 'react'
import type { JobStatsBucket } from '../lib/api'
import { fmtCount } from '../lib/format'

export type TrendSeries = {
  key: string
  label: string
  /** css 颜色值：token（var(--lx-success)）或调好的常量 */
  color: string
  /** pct = 左轴 0~1；score = 右轴 1~5 */
  kind: 'pct' | 'score'
  /** 每桶一个值；null = 该桶无数据（线在这里断开） */
  values: (number | null)[]
  /** 悬停读数里的绝对张数；score 线不传 */
  counts?: (number | null)[]
}

const PAD_L = 44
const PAD_R = 36
const PAD_T = 14
const PAD_B = 26

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v))
}

/** 把含 null 的值列切成若干段连续折线（score 线在没分的桶会断） */
function segments(values: (number | null)[],
                  x: (i: number) => number, y: (v: number) => number) {
  const segs: string[] = []
  let cur: string[] = []
  values.forEach((v, i) => {
    if (v === null || !Number.isFinite(v)) {
      if (cur.length > 1) segs.push(cur.join(' '))
      cur = []
    } else {
      cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    }
  })
  if (cur.length > 1) segs.push(cur.join(' '))
  return segs
}

/** 桶序号 → 紧凑刻度：一万以内全写，再大用 k */
function tickLabel(start: number) {
  const v = start + 1
  if (v >= 100000) return `${Math.round(v / 1000)}k`
  return fmtCount(v)
}

export function TrendChart({ buckets, series, height = 300 }: {
  buckets: JobStatsBucket[]
  series: TrendSeries[]
  height?: number
}) {
  const boxRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  const [hover, setHover] = useState<number | null>(null)

  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0
      setWidth(w)
    })
    ro.observe(el)
    setWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])

  useEffect(() => { setHover(null) }, [buckets.length])

  const n = buckets.length
  const plotW = Math.max(0, width - PAD_L - PAD_R)
  const plotH = height - PAD_T - PAD_B
  const xOf = (i: number) => (n <= 1 ? PAD_L + plotW / 2 : PAD_L + (i / (n - 1)) * plotW)
  const yOf = (s: TrendSeries, v: number) =>
    s.kind === 'pct'
      ? PAD_T + (1 - clamp(v, 0, 1)) * plotH
      : PAD_T + (1 - (clamp(v, 1, 5) - 1) / 4) * plotH

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    if (n === 0 || plotW <= 0) return
    const rect = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - rect.left
    if (px < PAD_L - 10 || px > width - PAD_R + 10) {
      setHover(null)
      return
    }
    const t = (px - PAD_L) / plotW
    setHover(clamp(Math.round(t * (n - 1)), 0, n - 1))
  }

  const hb = hover !== null ? buckets[hover] : null
  // 悬停框靠哪边：点在右半场就往左摆，避免出界
  const tipLeft = hover !== null && xOf(hover) > width * 0.58
  const tipW = 190

  return (
    <div ref={boxRef} className="relative w-full select-none">
      {width > 0 && n > 0 && (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label="质检趋势折线图"
          className="block"
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        >
          {/* 水平网格：同一组线同时是百分比刻度和分数刻度 */}
          {[0, 25, 50, 75, 100].map((p) => {
            const y = PAD_T + (1 - p / 100) * plotH
            return (
              <g key={p}>
                <line
                  x1={PAD_L} x2={width - PAD_R} y1={y} y2={y}
                  stroke={p === 0 ? 'var(--lx-border-hover)' : 'var(--lx-border)'}
                  strokeWidth={1}
                />
                <text x={PAD_L - 8} y={y + 3} textAnchor="end"
                      fontSize={10} fill="var(--lx-text-dim)">
                  {p}%
                </text>
                <text x={width - PAD_R + 8} y={y + 3} textAnchor="start"
                      fontSize={10} fill="var(--lx-text-dim)">
                  {1 + p / 25}
                </text>
              </g>
            )
          })}
          {/* X 刻度 */}
          {Array.from({ length: n }, (_, i) => i)
            .filter((i) => i % Math.max(1, Math.ceil(n / 6)) === 0 || i === n - 1)
            .map((i) => (
              <text key={i} x={xOf(i)} y={height - 8} textAnchor="middle"
                    fontSize={10} fill="var(--lx-text-dim)">
                {tickLabel(buckets[i].start)}
              </text>
            ))}
          {/* 悬停竖线 */}
          {hover !== null && (
            <line
              x1={xOf(hover)} x2={xOf(hover)} y1={PAD_T} y2={PAD_T + plotH}
              stroke="var(--lx-border-hover)" strokeWidth={1} strokeDasharray="3 3"
            />
          )}
          {/* 折线：段式渲染，断点自然分开 */}
          {series.map((s) => {
            const segs = segments(s.values, xOf, (v) => yOf(s, v))
            const lastIdx = (() => {
              for (let i = s.values.length - 1; i >= 0; i--) {
                if (s.values[i] !== null) return i
              }
              return -1
            })()
            return (
              <g key={s.key}>
                {segs.map((pts, k) => (
                  <polyline key={k} points={pts} fill="none" stroke={s.color}
                            strokeWidth={1.5} strokeLinecap="round"
                            strokeLinejoin="round" opacity={0.95} />
                ))}
                {/* 单点也要看得见 */}
                {segs.length === 0 && lastIdx >= 0 && (
                  <circle cx={xOf(lastIdx)} cy={yOf(s, s.values[lastIdx] as number)}
                          r={3} fill={s.color} />
                )}
                {/* 线头小圆点：视线落在"现在" */}
                {lastIdx >= 0 && segs.length > 0 && (
                  <circle cx={xOf(lastIdx)} cy={yOf(s, s.values[lastIdx] as number)}
                          r={2.5} fill={s.color} />
                )}
              </g>
            )
          })}
          {/* 悬停读数点 */}
          {hover !== null && series.map((s) => {
            const v = s.values[hover]
            if (v === null || v === undefined) return null
            return (
              <circle key={s.key} cx={xOf(hover)} cy={yOf(s, v)} r={3.5}
                      fill="var(--lx-surface)" stroke={s.color} strokeWidth={1.5} />
            )
          })}
        </svg>
      )}
      {width > 0 && n === 0 && (
        <div className="flex h-40 items-center justify-center text-xs text-lx-dim">
          这个任务还没有结算任何图片
        </div>
      )}
      {/* 悬停读数框 */}
      {hb && hover !== null && (
        <div
          className="pointer-events-none absolute z-10 rounded-lx-sm border border-lx bg-lx-surface-raised px-3 py-2 shadow-lx-card-hover"
          style={{
            top: 8,
            ...(tipLeft
              ? { right: Math.max(0, width - xOf(hover) + 12) }
              : { left: Math.min(width - tipW - 4, xOf(hover) + 12) }),
            minWidth: tipW,
          }}
        >
          <div className="mb-1.5 text-[11px] font-medium text-lx-secondary">
            第 {fmtCount(hb.start + 1)}–{fmtCount(hb.end + 1)} 张
            <span className="ml-1.5 text-lx-muted">结算 {fmtCount(hb.settled)}</span>
          </div>
          <div className="flex flex-col gap-1">
            {series.map((s) => {
              const v = s.values[hover]
              if (v === null || v === undefined) return null
              const c = s.counts?.[hover]
              return (
                <div key={s.key} className="flex items-center gap-2 text-[11px]">
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full"
                        style={{ background: s.color }} />
                  <span className="text-lx-secondary">{s.label}</span>
                  <span className="ml-auto font-mono tabular-nums text-lx">
                    {s.kind === 'pct' ? `${(v * 100).toFixed(1)}%` : v.toFixed(2)}
                    {c !== null && c !== undefined && (
                      <span className="ml-1 text-lx-muted">· {fmtCount(c)} 张</span>
                    )}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
