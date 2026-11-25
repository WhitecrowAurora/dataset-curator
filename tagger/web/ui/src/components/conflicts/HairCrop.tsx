import { useRef, useState } from 'react'

export function HairCrop({ src, onChange }: { src: string; onChange: (box: number[] | null) => void }) {
  const image = useRef<HTMLImageElement>(null)
  const dragging = useRef(false)
  const origin = useRef<number[] | null>(null)
  const [start, setStart] = useState<number[] | null>(null)
  const [end, setEnd] = useState<number[] | null>(null)
  const point = (x: number, y: number) => {
    const bounds = image.current!.getBoundingClientRect()
    return [Math.max(0, Math.min(1, (x - bounds.left) / bounds.width)), Math.max(0, Math.min(1, (y - bounds.top) / bounds.height))]
  }
  return <div className="flex flex-col gap-1">
    <div className="relative w-fit max-w-full touch-none select-none" onPointerDown={(e) => {
      if (!image.current?.naturalWidth) return
      e.currentTarget.setPointerCapture(e.pointerId)
      dragging.current = true
      const p = point(e.clientX, e.clientY); origin.current = p; setStart(p); setEnd(p); onChange(null)
    }} onPointerMove={(e) => { if (dragging.current) setEnd(point(e.clientX, e.clientY)) }} onPointerCancel={() => { dragging.current = false; setStart(null); setEnd(null); onChange(null) }} onPointerUp={(e) => {
      dragging.current = false
      const first = origin.current
      if (!first || !image.current) return
      const p = point(e.clientX, e.clientY); setEnd(p)
      const width = image.current.naturalWidth, height = image.current.naturalHeight
      const box = [Math.floor(Math.min(first[0], p[0]) * width), Math.floor(Math.min(first[1], p[1]) * height), Math.ceil(Math.max(first[0], p[0]) * width), Math.ceil(Math.max(first[1], p[1]) * height)]
      onChange(box[2] > box[0] && box[3] > box[1] ? box : null)
    }}>
      <img ref={image} src={src} alt="框选角色头部" draggable={false} className="block max-h-80 max-w-full rounded" />
      {start && end && <div className="pointer-events-none absolute border-2 border-lx-accent bg-lx-accent/10" style={{ left: `${Math.min(start[0], end[0]) * 100}%`, top: `${Math.min(start[1], end[1]) * 100}%`, width: `${Math.abs(end[0] - start[0]) * 100}%`, height: `${Math.abs(end[1] - start[1]) * 100}%` }} />}
    </div>
    <div className="flex items-center gap-2 text-xs text-lx-muted"><span>拖动框选一个角色的头部</span><button className="lx-btn-ghost text-xs" onClick={() => { setStart(null); setEnd(null); onChange(null) }}>清除裁框</button></div>
  </div>
}
