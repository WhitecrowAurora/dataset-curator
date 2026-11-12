import { useLayoutEffect, useRef, useState } from 'react'
import { RunJob } from '../pages/RunJob'

function ResidentJob({ id, active, onResults, onNew, onRestarted }: {
  id: string; active: boolean; onResults: (id: string) => void; onNew: () => void
  onRestarted: (id: string) => void
}) {
  const panel = useRef<HTMLDivElement>(null)
  const scroll = useRef(0)
  useLayoutEffect(() => {
    if (!active) return
    const main = panel.current?.closest('main')
    if (!main) return
    main.scrollTop = scroll.current
    const remember = () => { scroll.current = main.scrollTop }
    main.addEventListener('scroll', remember)
    return () => main.removeEventListener('scroll', remember)
  }, [active])
  return (
    <div ref={panel} hidden={!active}>
      <RunJob jobId={id} onResults={() => onResults(id)} onNew={onNew} onRestarted={onRestarted} />
    </div>
  )
}

export function ResidentJobs({ jobId, onResults, onNew, onRestarted }: {
  jobId: string | null; onResults: (id: string) => void; onNew: () => void
  onRestarted: (id: string) => void
}) {
  const [visited, setVisited] = useState<string[]>(() => jobId ? [jobId] : [])
  if (jobId && !visited.includes(jobId)) setVisited([...visited, jobId])
  return <>{visited.map((id) => (
    <ResidentJob key={id} id={id} active={id === jobId} onResults={onResults} onNew={onNew}
      onRestarted={onRestarted} />
  ))}</>
}
