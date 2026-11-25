"""并发比较的独立请求，不修改用户的正式打标配置。"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Level = Annotated[int, Field(strict=True, ge=1, le=256)]


class BenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    model: str = Field(min_length=1, max_length=256)
    engine: Literal['native', 'llamacpp', 'vllm']
    precision: str = Field(min_length=1, max_length=16)


class BenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    configurations: list[BenchmarkConfig] = Field(min_length=1, max_length=4)
    device: str = 'auto'
    source: str = Field(default='', max_length=4096)
    output_dir: str = Field(default='', max_length=4096)
    levels: list[Level] = Field(default_factory=lambda: [1, 2, 4, 6, 8], min_length=1, max_length=32)
    sample_count: int = Field(default=24, ge=4, le=1024)

    @model_validator(mode='before')
    @classmethod
    def single_engine_request(cls, value):
        if isinstance(value, dict) and 'configurations' not in value:
            value = dict(value)
            value['configurations'] = [{key: value.pop(key) for key in ('model', 'engine', 'precision') if key in value}]
        return value

    @field_validator('configurations')
    @classmethod
    def distinct_configurations(cls, values):
        if len({(v.model, v.engine, v.precision) for v in values}) != len(values):
            raise ValueError('对比配置重复，请选择不同模型、引擎或精度')
        return values

    @field_validator('levels')
    @classmethod
    def ordered_levels(cls, values):
        return sorted(set(values))

    @field_validator('device')
    @classmethod
    def valid_device(cls, value):
        if value not in ('auto', 'cpu') and not (value.startswith('cuda:') and value[5:].isdigit()):
            raise ValueError('请选择自动、CPU 或可用 CUDA 设备')
        return value
