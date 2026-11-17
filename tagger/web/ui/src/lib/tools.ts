import { json } from './api'

export type JsonlCleanJob = {
  id: string
  status: 'running' | 'done' | 'completed_with_errors'
  input: string
  output_dir: string
  total_files: number
  processed_files: number
  failed_files: number
  rows: number
  changed_rows: number
  removed_scores: number
  invalid_rows: number
  current: string
  results: {
    source: string
    output?: string
    rows?: number
    changed_rows?: number
    removed_scores?: number
    invalid_rows?: number
    invalid_lines?: number[]
    error?: string
  }[]
}

export const toolsApi = {
  cleanJsonl: (path: string, recursive: boolean) =>
    json<JsonlCleanJob>('/api/tools/jsonl/clean', {
      method: 'POST', body: JSON.stringify({ path, recursive }),
    }),
  jsonlJob: (id: string) => json<JsonlCleanJob>(`/api/tools/jsonl/${encodeURIComponent(id)}`),
}
