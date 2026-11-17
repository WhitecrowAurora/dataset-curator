/**
 * 下载源（镜像站）。镜像与官方是同一套 API / resolve 路径，只换前缀；
 * 国内镜像可直连，所以它是「下载代理」的替代品——设了镜像就不必挂梯子。
 *
 * 抽到这里的理由是它就是一份常量清单：下载源只在「设置」页改，别处
 * （模型页、新建任务页）只显示当前生效值，不各自维护一份预设。
 */
export const ENDPOINT_PRESETS = [
  { key: '', label: '默认（hf-mirror.com 镜像，国内直连）' },
  { key: 'https://hf-mirror.com', label: '镜像 hf-mirror.com' },
  { key: 'https://huggingface.co', label: '官方 huggingface.co（要梯子）' },
] as const

/** 当前地址对应的下拉选项；不在预设里就是 'custom' */
export const endpointPresetOf = (url: string): string =>
  ENDPOINT_PRESETS.some((p) => p.key === url) ? url : 'custom'
