import { useEffect, useRef, useState } from 'react'
import type { ModelCatalog } from '../../lib/api'
import type { SetupDraft } from '../../lib/setup'
import { preparationKey, prepareModels, type PreparationProgress } from './preparation'

export function useModelPreparation(draft: SetupDraft | null, beforeDownload: () => Promise<void>,
  onCatalog: (catalog: ModelCatalog) => void, token: string) {
  const [progress, setProgress] = useState<PreparationProgress | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [completed, setCompleted] = useState('')
  const control = useRef<AbortController | null>(null)
  const key = draft ? preparationKey(draft) : ''
  useEffect(() => () => control.current?.abort(), [])
  const start = async () => {
    if (!draft || control.current) return
    const controller = new AbortController()
    control.current = controller
    setRunning(true); setError(''); setCompleted(''); setProgress({ stage: '准备开始', percent: 0, logs: [] })
    try {
      await beforeDownload()
      await prepareModels(draft, token.trim(), controller.signal, setProgress, onCatalog)
      setCompleted(key)
    } catch (e) { setError(controller.signal.aborted ? '已停止准备，可重新开始并复用已完成的文件。' : e instanceof Error ? e.message : String(e)) }
    finally { control.current = null; setRunning(false) }
  }
  return { progress, running, error, ready: Boolean(key && completed === key), start,
    cancel: () => control.current?.abort() }
}
