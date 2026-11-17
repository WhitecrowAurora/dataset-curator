export const NATIVE_PRECISIONS = ['q4', 'q5', 'q6', 'q8', 'fp8', 'int8'] as const
export const GGUF_TARGETS: Record<string, string> = {
  f16: 'f16', q4: 'q4_k_m', q5: 'q5_k_m', q6: 'q6_k', q8: 'q8_0',
}
export const ENGINE_LABELS: Record<string, string> = {
  llamacpp: 'llama.cpp', vllm: 'vLLM', native: 'lulynx', existing: '已有服务',
}
export function enginePrecisions(engine: string): readonly string[] {
  return engine === 'native' ? NATIVE_PRECISIONS : engine === 'vllm'
    ? ['bf16', 'fp8', 'w4a16'] : ['f16', 'q4', 'q6', 'q8']
}
