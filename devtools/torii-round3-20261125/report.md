# Torii native Q6 第三轮优化

2026-11-25 开始，2026-11-26 完成；运行时从 `lulynx-qwen35-0.7` 更新至 `0.8`。

本轮只采用归一化后半段的融合：保留 PyTorch 原来的 FP32 平方均值归约，把缩放、权重与门控计算合到一个 Triton kernel。保留官方 FP16 中间舍入和 SiLU 的 exp/除法。仅安装到 Torii 的语言模型实例；CPU、BF16 继续走官方实现，视觉模型和 Qwen3VL 不变。

没有使用投机解码，没有新增量化格式，Q6 权重仍以压缩形式驻留显存。

## 实测收益

RTX 4070 Ti SUPER 16 GB，Torch 2.13.0+cu132、Transformers 5.14.1、Triton 3.7.1、FLA 0.6.0。相同 Q6/mmproj、8 张固定图片、正式提示词、像素上限 1000000、上下文 8192、独立 seed、temperature=0.5、top_p=0.9、repeat_penalty=1。

| 测试 | 0.7 | 0.8 | 吞吐提高 |
|---|---:|---:|---:|
| 单路，128 token，3 次热运行均值 | 1.967 秒 | 1.839 秒 | 6.97% |
| 8 路，各 128 token，3 次热运行均值 | 3.883 秒 | 3.599 秒 | 7.90% |
| 8 图完整描述，2 轮交替对照热运行均值 | 9.823 秒 | 9.178 秒 | 7.03% |

这里包含视觉、prefill 和 decode，不包含图片 CPU 准备、HTTP、产品质量复检、落盘和占空比。完整描述合计 2777 token，均自然 EOS。对照中的每条输出逐 token 一致。峰值显存基本不变。

## 验证

- 单路、8 路各 64 步 teacher forcing，全词表 logits 最大误差和 RMSE 均为 0，64/64 步 argmax 一致。
- 正式源码单独加载后再次生成 8 条完整描述，与 0.7 基线逐 token 一致。
- 归一化 64 组设备/类型/宽度/批量组合，每组连续和有跨步的布局、3 个输入尺度，全部逐值一致；覆盖 CPU FP32/BF16、CUDA BF16/FP16、batch 1/8/32/256。256 仅是算子测试，不代表完整模型 256 并发已通过。
- 非目标模型实例的 forward 未被修改；CPU/BF16 回退通过。
- 保留官方 `x=`、`hidden_states=`、`gate=` 关键字调用契约，CPU 验证通过。
- HTTP 连续两轮各 12 个错开时间、不同输出长度的请求全部 200；错误模型、超上下文、正常请求并行返回 400/400/200；完整描述与服务正常关闭通过。

## 384 / 512 token 约束

128 token 仅是测速用的固定生成预算，未改产品提示词或最终训练文本限制。正式 Torii 提示词动态追加 384 软目标与 512 硬上限；最终 `_nl_payload` 在画师注入与标签确定后，使用 Qwen3.5-0.8B tokenizer 对训练模板整体计数并执行硬上限。

本轮 8 条完整描述按当前训练 tokenizer 计数为 239、432、284、373、241、187、429、225 token，6 条不超过软目标，全部不超过硬上限，无需裁剪。原始 JSON 的键、引号与 None 等结构不属于最终描述，因此原始生成 526 token 的一条对应训练描述 429 token。额外超长文本加必要前缀的测试保留前缀并收敛至 510 token。详见 `length-check.json`。没有把 512 当作包含 JSON 格式开销的生成硬停点。

## 未采用的候选

量化 MMA 的不同分块与流水线参数没有稳定整体系数收益；合并多路采样 Graph 没有提速，未改采样路径。完全融合归一化的归约会造成少量 FP16 数值差异，未采用。最终实现保留原归约，既获得速度提升，也通过本轮逐值与逐 token 检查。

## 限制与证据

HTTP 对照采用同模型、同图片、同提示词、每图 1175 prompt token、8 个 slots、每 slot 8192 context；进程串行运行，关闭 prompt cache。llama.cpp 为 build 12188 / commit 095668452。pixel budget 按两边处理器规则对齐至约 100 万。

| 服务 | 热运行固定 128 token | 完整描述图/分钟，两次 | 完整描述 token/秒，两次 | 整卡观测峰值 |
|---|---:|---:|---:|---:|
| native 0.8 | 262.64 token/秒 | 49.23 / 49.08 | 284.80 / 283.97 | 12839 MiB |
| llama.cpp 默认候选过滤 | 255.36 token/秒 | 42.87 / 35.06 | 239.36 / 208.34 | 8388 MiB |
| llama.cpp top_k=0、min_p=0 | 164.95 token/秒 | 29.48 / 29.62 | 170.68 / 171.46 | 8433 MiB |

默认过滤更快，说明采样配置也明显影响 llama.cpp 的耗时。完整描述分别生成 2777、2680/2852、2779 token，均合法 JSON、自然 EOS；跨引擎文字并不完全一致。固定长度基准 temperature=0；完整描述 temperature=0.5。不能把上述图吞吐差直接当作纯计算倍率，也不能用上轮默认过滤结果冒充本轮匹配采样结果。native 显存仍高于 llama.cpp。记录在 `engine-native.json`、`engine-llamacpp-default.json`、`engine-llamacpp.json`。

结果只来自本机 4070 Ti SUPER 与这组输入，不代表 5090、4090、V100 或 RTX 6000 Pro 的速度，也不能直接估算 3000 万张数据的完整工期。没有对不同 GPU 做数值一致性保证。

本目录 `summary.json`、`compare.json`、`production.json`、`norm-boundaries.json`、`http.json` 保存结论依据。原始 profiler trace 和运行日志在 `E:/tagger-validation/torii-round3-20261125`。

更新包 `dist/tagger-20261126-torii-native-v3-patch.zip` 仅包含本轮 4 个推理源码文件与安装说明，供上一版 Torii throughput v2 / runtime 0.7 客户端覆盖更新。关闭客户端与 NL 服务后解压到原目录。工作区中的其他调度、UI 等改动未混入或覆盖。包 CRC、源码哈希与条目范围均已验证，详见 `package.json`。
