export function httpUrlError(value: string, label: string): string {
  const text = value.trim()
  if (!text) return `请填写${label}。`
  try {
    const url = new URL(text)
    if (/\s/.test(text) || !['http:', 'https:'].includes(url.protocol) || !url.hostname) throw new Error()
  } catch { return `${label}格式不正确，请填写完整的 http:// 或 https:// 地址。` }
  return ''
}

export function localServiceUrlError(value: string): string {
  return /^http:\/\/(localhost|127\.0\.0\.1|\[::1\]):\d+\/?$/i.test(value.trim()) ? ''
    : '本机启动请使用 http://127.0.0.1:端口；远端地址请切换为使用已有服务。'
}

type FailureContext = 'task' | 'center' | 'upload' | 'download' | 'service'

export function latestCenterFailure(logs: string[]): string {
  let failure = ''
  for (let i = logs.length - 1; i >= 0; i--) {
    if (logs[i].includes('[center] 启动：')) return failure
    if (!failure && /\[X\]|\bERROR\b|Traceback/i.test(logs[i])) failure = logs[i]
  }
  return ''
}

export function failureAdvice(error: string, context: FailureContext): string {
  if (context === 'center' && /回传失败|产物发布失败/i.test(error))
    return '本机产物保留。请在调度中心服务器面板检查储存目录是否可写，或 HF Token 是否有目标仓库写权限，修正后再开始。'
  if (/no space left|disk.*full|磁盘.*(不足|满)|空间不足|空间不够/i.test(error))
    return '目标磁盘空间不足，请换到有足够空间的目录后重试。'
  if (/permission denied|read.only|not writable|不可写|无写入权限|目录.*权限|拒绝访问/i.test(error))
    return '请换一个可写目录，或让管理员检查该目录的写入权限。'
  if (/out of memory|cuda.*memory|显存不足|显存.*不够/i.test(error))
    return '显存不足，请降低批大小或并发、选择更省显存的精度，然后重新启动。'
  if (/401|403|unauthori[sz]ed|forbidden|gated|鉴权|授权失败|无权访问|access.*denied/i.test(error)) {
    if (context === 'upload') return '请检查接收端要求的上传令牌或 FTP 账号权限，然后重试上传。'
    if (context === 'download' || /huggingface|hugging face|hf_|受限|私有|下载|download/i.test(error))
      return '请在 Hugging Face Token 处填写有读取权限的 Token；受限模型还需先在模型页面申请访问权限，再重试下载。'
    if (context === 'center') return '请检查调度中心地址、用户 ID 和连接密钥；下载受限资源时还需配置有读取权限的 HF Token。'
    return '请检查当前推理服务的 API 密钥和模型访问权限，然后重新测试连接。'
  }
  if (/weights|权重|gguf/i.test(error))
    return '请在模型管理中确认所选模型和精度的权重已准备好，再启动服务。'
  if (/model.*not found|模型.*不存在/i.test(error))
    return '请检查服务中的模型是否可用，以及填写的模型名是否与服务一致。'
  if (/connection|connect|timed? ?out|timeout|无法连接|连接失败|连接中断|超时|网络/i.test(error))
    return context === 'upload' ? '请检查接收端地址和网络，确认服务正在运行后重试上传。'
      : '请检查服务地址和网络，确认目标服务正在运行，再测试连接。'
  if (/cuda|device|gpu|设备不存在|无.*显卡/i.test(error))
    return '请重新选择当前可用的计算设备，并检查驱动和所选后端的设备支持。'
  if (context === 'upload') return '请查看失败文件的原因，修正连接或目录设置后重新上传。'
  if (context === 'download') return '请查看下载错误，检查下载源、网络和资源权限，然后重试下载。'
  if (context === 'service') return '请查看服务启动日志，确认权重、设备和精度设置后重新启动。'
  if (context === 'center') return '请查看本轮调度日志，检查模型服务、下载与回传设置，修正后再开始。'
  return '请查看任务日志，修正对应设置后恢复任务；已完成的产物可在结果页查看。'
}
