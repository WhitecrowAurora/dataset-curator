/**
 * NL 提示词的编辑/新建弹窗。
 *
 * 提示词放在仓库根的 `prompt/*.json` 里（见 prompt/README.md）：文件名就是
 * 下拉框里显示的名字，这个弹窗改的就是那份文件本身——不是另一份副本。
 * 所以保存后无需重启，下次建任务就用新内容；已在跑的任务用的是它启动时读到
 * 的那一份（provenance 里记的是文本 sha，改了就自然对不上）。
 *
 * 新建时先用**内置提示词**把文本框填满：对着空白框从零写一套完整指令不现实，
 * 在现成的那份上改才是真实用法。
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { FilePen, Loader2, X } from 'lucide-react'
import { api, type PromptDoc } from '../lib/api'
import { ErrorBox } from './ui/ErrorBox'

function msg(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export function PromptDialog({
  open,
  mode,
  promptId,
  onClose,
  onSaved,
}: {
  open: boolean
  /** edit = 改选中的那份；create = 新建一份（先取内置内容当底稿） */
  mode: 'edit' | 'create'
  promptId?: string
  onClose: () => void
  onSaved: (doc: PromptDoc) => void
}) {
  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('')
  const [promptZh, setPromptZh] = useState('')
  const [system, setSystem] = useState('')
  const [note, setNote] = useState('')
  const [path, setPath] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 每次打开重新取：文件可能刚被人在编辑器里改过，缓存一份旧的等于允许两边打架。
  useEffect(() => {
    if (!open) return
    let alive = true
    setError(null)
    setLoading(true)
    setName('')
    setNote('')
    setPath('')
    const load =
      mode === 'edit' && promptId
        ? api.prompt(promptId).then((doc) => {
            if (!alive) return
            setName(doc.name)
            setPrompt(doc.prompt)
            setPromptZh(doc.prompt_zh)
            setSystem(doc.system)
            setNote(doc.note)
            setPath(doc.path)
          })
        : api.promptTemplate().then((tpl) => {
            if (!alive) return
            setPrompt(tpl.prompt)
            setPromptZh(tpl.prompt_zh)
            setSystem(tpl.system)
          })
    load
      .catch((err) => alive && setError(msg(err)))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [open, mode, promptId])

  async function save() {
    if (saving) return
    if (!prompt.trim()) {
      setError('提示词不能为空')
      return
    }
    if (mode === 'create' && !name.trim()) {
      setError('给这份提示词起个名字（它就是文件名）')
      return
    }
    setSaving(true)
    setError(null)
    const body = { prompt, prompt_zh: promptZh, system, note }
    try {
      const doc =
        mode === 'create'
          ? await api.createPrompt({ name: name.trim(), ...body })
          : await api.savePrompt(promptId ?? '', body)
      onSaved(doc)
      onClose()
    } catch (err) {
      setError(msg(err))
    } finally {
      setSaving(false)
    }
  }

  const title = mode === 'create' ? '新建自定义提示词' : `编辑提示词 · ${name || promptId || ''}`

  // 与 GgufConvertDialog 同样的理由：页面根节点带 .lx-page-enter 的 transform，
  // 留在页面 DOM 里会让 fixed 相对整页容器定位，弹窗跑到页面中间。
  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[10000] flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget && !saving) onClose()
          }}
          role="dialog"
          aria-modal="true"
          aria-label={title}
        >
          <div
            className="absolute inset-0"
            style={{
              backgroundColor: 'color-mix(in srgb, var(--lx-bg) 62%, transparent)',
              backdropFilter: 'blur(3px)',
            }}
          />
          <motion.div
            className="lx-sheet relative flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden"
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 4 }}
            transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          >
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-lx px-[18px] py-3">
              <div className="flex min-w-0 items-center gap-2">
                <FilePen size={15} className="shrink-0 text-lx-muted" />
                <h3 className="truncate text-[15px] font-semibold text-lx">{title}</h3>
              </div>
              <button
                className="lx-btn-ghost-icon"
                onClick={onClose}
                disabled={saving}
                aria-label="关闭"
              >
                <X size={16} />
              </button>
            </header>

            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-[18px] py-3.5">
              {loading && (
                <div className="flex items-center gap-2 py-2 text-[12px] text-lx-muted">
                  <Loader2 size={14} className="animate-spin" /> 读取提示词…
                </div>
              )}

              {!loading && (
                <>
                  {mode === 'create' && (
                    <label className="block space-y-1">
                      <span className="text-[12px] font-medium text-lx">名字</span>
                      <input
                        className="lx-input"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="例如：更口语的描述（中文、空格都可以）"
                        autoFocus
                      />
                      <span className="block text-[11px] leading-relaxed text-lx-dim">
                        名字就是文件名（prompt/&lt;名字&gt;.json），也是下拉框里显示的那一行。
                      </span>
                    </label>
                  )}

                  {mode === 'edit' && path && (
                    <p className="break-all font-mono text-[10px] text-lx-dim">{path}</p>
                  )}

                  <label className="block space-y-1">
                    <span className="text-[12px] font-medium text-lx">提示词（英文）</span>
                    <textarea
                      className="lx-input min-h-[220px] resize-y font-mono text-[12px] leading-relaxed"
                      value={prompt}
                      onChange={(e) => setPrompt(e.target.value)}
                      spellCheck={false}
                    />
                    <span className="block text-[11px] leading-relaxed text-lx-dim">
                      整段替换内置提示词（不是追加）。长度/数量要求要与页面上的
                      「描述长度」「max_tokens」对得上，否则会被质检判废再重打。
                    </span>
                  </label>

                  <label className="block space-y-1">
                    <span className="text-[12px] font-medium text-lx">
                      中文提示词
                      <span className="ml-1.5 font-normal text-lx-dim">可选</span>
                    </span>
                    <textarea
                      className="lx-input min-h-[90px] resize-y font-mono text-[12px] leading-relaxed"
                      value={promptZh}
                      onChange={(e) => setPromptZh(e.target.value)}
                      spellCheck={false}
                      placeholder="留空 = 这批不出中文（不会退回去用内置中文版）"
                    />
                  </label>

                  <label className="block space-y-1">
                    <span className="text-[12px] font-medium text-lx">
                      system 提示词
                      <span className="ml-1.5 font-normal text-lx-dim">可选</span>
                    </span>
                    <textarea
                      className="lx-input min-h-[56px] resize-y font-mono text-[12px] leading-relaxed"
                      value={system}
                      onChange={(e) => setSystem(e.target.value)}
                      spellCheck={false}
                    />
                  </label>

                  <label className="block space-y-1">
                    <span className="text-[12px] font-medium text-lx">
                      备注
                      <span className="ml-1.5 font-normal text-lx-dim">可选，只给自己看</span>
                    </span>
                    <input
                      className="lx-input"
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                    />
                  </label>

                  <ErrorBox text={error ?? ''} />
                </>
              )}
            </div>

            <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-lx px-[18px] py-3">
              <button className="lx-btn-ghost" onClick={onClose} disabled={saving}>
                取消
              </button>
              <button
                className="lx-btn-primary inline-flex items-center gap-1.5"
                onClick={() => void save()}
                disabled={loading || saving}
              >
                {saving && <Loader2 size={13} className="animate-spin" />}
                {mode === 'create' ? '创建' : '保存'}
              </button>
            </footer>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
}
