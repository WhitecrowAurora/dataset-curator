/**
 * 下载源（镜像站）。镜像与官方是同一套 API / resolve 路径，只换前缀；
 * 国内镜像可直连，所以它是「下载代理」的替代品——设了镜像就不必挂梯子。
 *
 * 抽到这里是因为模型页与新建任务页都要用：两处各留一份常量，
 * 迟早出现"一边改了预设、另一边还是旧清单"。
 */
export const ENDPOINT_PRESETS = [
  { key: '', label: '默认（hf-mirror.com 镜像，国内直连）' },
  { key: 'https://hf-mirror.com', label: '镜像 hf-mirror.com' },
  { key: 'https://huggingface.co', label: '官方 huggingface.co（要梯子）' },
] as const

/** 当前地址对应的下拉选项；不在预设里就是 'custom' */
export const endpointPresetOf = (url: string): string =>
  ENDPOINT_PRESETS.some((p) => p.key === url) ? url : 'custom'
