import { useState } from 'react'
import { Check, Copy } from 'lucide-react'

/**
 * 错误展示：原文照抄 + 一键复制。
 *
 * 为什么不"翻译成人话"：翻译过的错误搜不到、贴给同事也复现不了。错误原文
 * 才是可执行的信息；人话应该由**产生错误的地方**给（后端已经这么做了，
 * 例如 llama_runtime._human_error），而不是在这里二次概括。
 */
export function ErrorBox({
  text,
  className = '',
}: {
  text: string
  className?: string
}) {
  const [copied, setCopied] = useState(false)

  if (!text) return null

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* 剪贴板不可用（无 https / 无权限）：保持原样，不弹二次错误 */
    }
  }

  return (
    <div
      className={`group relative rounded-lx-sm border border-lx-danger/30 bg-lx-danger/5 px-2.5 py-2 ${className}`}
    >
      <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10.5px] leading-relaxed text-lx-danger">
        {text}
      </pre>
      <button
        type="button"
        className="lx-btn-ghost absolute right-1.5 top-1.5 inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
        onClick={() => void copy()}
        title="复制错误原文"
      >
        {copied ? <Check size={10} /> : <Copy size={10} />}
        {copied ? '已复制' : '复制'}
      </button>
    </div>
  )
}
