import { api, type CenterConfig, type CenterOptions, type ModelCatalog, type SetupState } from './api'
import { httpUrlError, localServiceUrlError } from './guidance'
import { enginePrecisions } from './nlEngines'

export const SETUP_STEPS = ['下载与授权', '后端与输出', '模型与权重准备', '使用方式'] as const
export const PRECISION_LABELS: Record<string, string> = {
  f16: 'F16', q4: 'Q4_K_M', q5: 'Q5_K_M', q6: 'Q6_K', q8: 'Q8_0',
  bf16: 'BF16', fp16: 'FP16', fp8: 'FP8', int8: 'INT8', w4a16: 'W4A16',
}
export type SetupDraft = {
  mode: 'tag' | 'nl' | 'both'
  engine: 'llamacpp' | 'vllm' | 'native' | 'existing'
  backend: string
  model: string
  precision: string
  tagPrecision: string
  tagDevice: string
  nlDevice: string
  url: string
  servedName: string
  input: string
  output: string
  endpoint: string
  proxy: string
  artifactFormat: string
  server: string
  userId: string
  hosting: 'active' | 'passive'
  autoUpload: boolean
  wait: boolean
  resume: boolean
  usage: SetupState['usage']
  packFormat: SetupState['pack_format']
}
export type SetupData = {
  state: SetupState
  draft: SetupDraft
  catalog: ModelCatalog
  options: CenterOptions
  center: CenterConfig
  linked: boolean
  tokenSet: boolean
}

export function localServiceSettings(url: string, servedName: string) {
  return {
    url: httpUrlError(url, '推理服务地址') || localServiceUrlError(url) ? 'http://127.0.0.1:8000' : url.trim(),
    servedName: servedName.trim() || 'tagger',
  }
}

const text = (value: unknown, fallback: string) => typeof value === 'string' ? value : fallback

export async function loadSetup(): Promise<SetupData> {
  const [state, saved, catalog, options, center, token] = await Promise.all([
    api.setup(), api.jobForm(), api.models(), api.centerOptions(), api.center(), api.hfToken(),
  ])
  const f = saved.form, c = center.config, useCenter = state.completed && state.usage === 'center'
  const engine = useCenter ? c.inference_source === 'existing' ? 'existing' : c.nl_engine
    : f.setupInferenceSource === 'existing' ? 'existing' : text(f.vllmEngine, 'llamacpp')
  const validEngine = engine === 'vllm' || engine === 'native' || engine === 'existing' ? engine : 'llamacpp'
  const mode = useCenter ? c.mode : text(f.mode, 'both')
  const precision = useCenter ? c.nl_precision : text(f.vllmPrecision, validEngine === 'vllm' ? 'bf16' : 'f16')
  const backend = useCenter ? c.tag_backend : text(f.backend, catalog.tag[0]?.backend ?? 'wd-eva02')
  const service = { url: useCenter ? c.tag_url : text(f.vlmUrl, 'http://127.0.0.1:8000'),
    servedName: useCenter ? c.served_name : text(f.vlmModel, 'tagger') }
  return {
    state, catalog, options, center: c, linked: Boolean(center.identity?.device_id),
    tokenSet: Boolean(token.saved),
    draft: {
      mode: state.completed && (mode === 'tag' || mode === 'nl') ? mode : 'both', engine: validEngine,
      backend: state.completed ? backend : catalog.tag.find((m) => m.backend === 'pixai')?.backend ?? backend,
      model: state.completed ? useCenter ? c.nl_model : text(f.vllmModelKey, '')
        : catalog.vlm.find((m) => m.key === 'qwen3vl-8b-abliterated')?.key ?? catalog.vlm[0]?.key ?? '',
      precision: enginePrecisions(validEngine).includes(precision)
        && state.completed ? precision : validEngine === 'vllm' ? 'bf16' : 'q6',
      tagPrecision: state.completed ? useCenter ? c.tag_precision : text(f.precision, 'bf16') : 'bf16',
      tagDevice: state.completed ? useCenter ? c.tag_device : text(f.device, 'auto') : 'auto',
      nlDevice: state.completed ? useCenter ? c.nl_device : text(f.vllmDevice, 'auto') : 'auto',
      ...(validEngine === 'existing' ? service : localServiceSettings(service.url, service.servedName)),
      input: text(f.inputDir, ''), output: useCenter ? c.out_dir : text(f.outDir, ''),
      endpoint: catalog.endpoint, proxy: catalog.proxy,
      artifactFormat: text(f.artifactFormat, 'jsonl'),
      server: c.server, userId: c.user_id, hosting: c.hosting_mode,
      autoUpload: c.auto_upload, wait: c.wait, resume: c.resume_on_startup,
      usage: state.completed ? state.usage : 'center', packFormat: state.pack_format,
    },
  }
}

export function setupFieldErrors(d: SetupDraft, step: number): Partial<Record<keyof SetupDraft, string>> {
  const errors: Partial<Record<keyof SetupDraft, string>> = {}
  if (step === 2) {
    if (d.mode !== 'tag' && d.engine !== 'existing' && !d.model) errors.model = '请选择一个自然语言模型。'
    if (d.mode !== 'nl' && (!d.backend || (d.mode === 'tag' && d.backend === 'none'))) errors.backend = '请选择 TAG 分类模型。'
  }
  if (step === 1 && d.engine === 'existing' && d.mode !== 'tag') {
    errors.url = httpUrlError(d.url, '已有服务地址')
    if (!d.servedName.trim()) errors.servedName = '请填写已有服务中的模型名。'
  }
  if (step === 3) {
    if (!d.output.trim()) errors.output = '请选择产物目录，打标结果会保存到这里。'
    if (d.usage === 'center') {
      errors.server = httpUrlError(d.server, '调度中心地址')
      if (!d.userId.trim()) errors.userId = '请填写调度中心的用户 ID。'
    } else if (!d.input.trim()) errors.input = '请选择本地素材目录。'
  }
  return errors
}

export function setupError(d: SetupDraft, step: number): string {
  return Object.values(setupFieldErrors(d, step)).find(Boolean) || ''
}

export async function saveSetup(data: SetupData, d: SetupDraft, secret: string, token = '') {
  const error = [1, 2, 3].map((step) => setupError(d, step)).find(Boolean)
  if (error) throw new Error(error)
  if (d.endpoint !== data.catalog.endpoint) await api.saveEndpoint(d.endpoint.trim())
  if (d.proxy !== data.catalog.proxy) await api.saveProxy(d.proxy.trim())
  if (d.usage === 'center') {
    if (!data.linked || secret.trim() || d.server.trim() !== data.center.server || d.userId.trim() !== data.center.user_id) {
      await api.centerLogin({ server: d.server.trim(), user_id: d.userId.trim(), secret: secret.trim() })
    }
    await api.centerConfigure({
      mode: d.mode, tag_backend: d.backend, tag_device: d.tagDevice, tag_precision: d.tagPrecision,
      nl_device: d.nlDevice,
      inference_source: d.engine === 'existing' ? 'existing' : 'local',
      nl_engine: d.engine === 'existing' ? 'llamacpp' : d.engine, nl_model: d.model,
      nl_precision: d.engine === 'existing' ? 'f16' : d.precision,
      tag_url: d.url.trim(), served_name: d.servedName.trim(), out_dir: d.output.trim(),
      hosting_mode: d.hosting, auto_upload: d.autoUpload, wait: d.wait,
      resume_on_startup: d.resume, auto_download: true,
      ...(token.trim() ? { hf_token: token.trim(), remember_hf: false } : {}),
    })
  }
  const latest = await api.jobForm()
  const model = data.catalog.tag.find((m) => m.backend === d.backend)
  const form = {
    ...latest.form, mode: d.mode, backend: d.backend, modelRepo: model?.repo ?? '',
    modelDir: model?.local_dir ?? '', tagDevices: [d.tagDevice], device: d.tagDevice,
    precision: d.tagPrecision, inputDir: d.input.trim(), outDir: d.output.trim(),
    hfRepo: '', hfFiles: [], packShardSel: [], multiGpu: false, hfProxy: '',
    setupInferenceSource: d.engine === 'existing' ? 'existing' : 'local',
    vlmProvider: 'local', vlmUrl: d.url.trim(), vlmModel: d.servedName.trim(),
    vllmEngine: d.engine === 'existing' ? 'llamacpp' : d.engine, vllmModelKey: d.model,
    vllmPrecision: d.engine === 'existing' ? 'f16' : d.precision,
    vllmDevice: d.nlDevice,
    vllmPort: d.mode === 'tag' ? 8000 : Number(new URL(d.url).port || 8000), artifactFormat: d.artifactFormat,
  }
  const saved = await api.saveJobForm({ form, strict: true })
  const state = await api.saveSetup({ completed: true, dismissed: false, usage: d.usage,
    pack_format: d.packFormat })
  return { form: saved.form, state }
}
