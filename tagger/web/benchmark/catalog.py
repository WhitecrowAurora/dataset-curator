"""只列出现有文件能够启动的模型与精度组合。"""
import json
from pathlib import Path

from .. import models, downloads
from ..center_runtime.inference import options
from ..center_runtime.config import ExecutionConfig


def available(center):
    from ...config import ConfigError, GGUF_PRECISIONS, normalize_quant_precision
    catalog = options()
    result = []
    for item in catalog['nl_models']:
        variants = []
        torii = item['key'] == 'torii-gate-0.5'
        # Torii's fixed Q6 bundle reports a boolean; other models report tiers.
        gguf = [] if torii else list(item.get('gguf_ready') or [])
        # Older custom entries can name one GGUF without a per-precision map.
        default = item.get('precision')
        if item.get('gguf') and default in GGUF_PRECISIONS and default not in (item.get('gguf_quants') or {}):
            ready, _ = models._gguf_tiers_state({**item, 'gguf_quants': {default: item['gguf']}}, Path(item['local_abs']))
            gguf.extend(p for p in ready if p not in gguf)
        for engine in catalog['engines']:
            if torii:
                precisions = ['q6'] if item['ready'] and engine in ('native', 'llamacpp') else []
            else:
                if engine == 'native' and not str(item.get('family', '')).startswith('qwen3vl'):
                    continue
                precisions = list(gguf) if engine != 'vllm' else []
                if item.get('raw_ready'):
                    # FP8/INT8 are runtime conversions, not existing quantized files.
                    precisions += ['fp16', 'bf16'] if engine == 'native' else ['bf16'] if engine == 'vllm' else []
                if engine != 'llamacpp' and item.get('quantized'):
                    raw = Path(item['quantized_path'])
                    quant = raw if raw.is_absolute() else downloads.ROOT / raw
                    try:
                        metadata = json.loads((quant / 'config.json').read_text(encoding='utf-8'))
                        if any(quant.glob('*.safetensors')) and (metadata.get('quantization_config') or {}).get('quant_method') == 'compressed-tensors':
                            precisions.append('w4a16')
                    except (OSError, ValueError):
                        pass
                if engine == 'native' and not (Path(item['local_abs']) / 'config.json').is_file():
                    continue
            ready = []
            for precision in precisions:
                try:
                    precision = normalize_quant_precision(precision, engine=engine)
                    ExecutionConfig(mode='nl', nl_model=item['key'], nl_engine=engine, nl_precision=precision)
                except (ValueError, ConfigError):
                    continue
                if engine != 'vllm' and not torii and precision in ('f16', 'q4', 'q5', 'q6', 'q8'):
                    if models.missing_weight_files({**item, 'precision': precision}, Path(item['local_abs'])):
                        continue
                if precision not in ready:
                    ready.append(precision)
            if ready:
                variants.append(dict(engine=engine, precisions=ready))
        if variants:
            result.append(dict(key=item['key'], title=item.get('title') or item['key'], variants=variants))
    from .samples import recent_source
    source = recent_source(center)
    return dict(models=result, devices=catalog['devices'], recent_source=str(source) if source else '',
                default_levels=[1, 2, 4, 6, 8], engine_status=catalog['engine_status'])


def validate(request, center):
    catalog = available(center)
    for index, selected in enumerate(request.configurations, 1):
        item = next((item for item in catalog['models'] if item['key'] == selected.model), None)
        if not item or not any(v['engine'] == selected.engine and selected.precision in v['precisions'] for v in item['variants']):
            raise ValueError(f'配置 {index} 的模型/精度尚未完整准备或引擎不可用，请刷新清单或到模型管理检查')
    if request.device.startswith('cuda:') and request.device not in {d['value'] for d in catalog['devices']}:
        raise ValueError('所选显卡当前不可用')
    if request.device == 'cpu' and any(c.engine == 'vllm' for c in request.configurations):
        raise ValueError('vLLM 不支持 CPU 测试')
    from .samples import source_path
    source_path(center, request.source)
