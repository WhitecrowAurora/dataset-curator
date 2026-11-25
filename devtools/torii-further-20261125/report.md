# Torii native Q6 第二轮提速 · 2026-11-25

本轮已实现并验证三个改动：解码注意力共享 KV、融合单 token 卷积与状态更新、减少 Q6 单行投影的布局转换。权重继续常驻 Q6；运行时版本为 `lulynx-qwen35-0.7`。

## 相对上一版 native

RTX 4070 Ti SUPER 16 GB，ToriiGate-0.5 Q6_K，FP16 mmproj，原提示词，1,000,000 像素上限，8192 上下文。GPU 测试串行进行。固定长度每图 128 token，温度 0.5、top_p 0.9、repeat_penalty 1、独立固定 seed；首轮预热，表中为两轮热运行均值。包含视觉与 prefill，不含 HTTP 和产品质量复检。

| 测试 | 修改前 | 修改后 | 吞吐提高 |
|---|---:|---:|---:|
| 1 图、128 token | 2.212 s | 1.981 s | 11.6% |
| 8 图、各 128 token | 4.862 s | 3.850 s | 26.3% |
| 8 图、自然结束的完整描述 | 14.107 s | 9.816 s | 43.7% |

完整描述合计 2777 token；8 条产物逐 token 与修改前相同，全部自然 EOS、合法 JSON。固定长度测试产物也相同。完整描述的提升高于固定 128 token，是因为较长生成中 decode 占比更高。

## 本轮 HTTP 引擎对照

同机同 Q6/mmproj、8 并发、同 8 张图、同提示词，每图 prompt 均为 1175 token。两种引擎在独立进程中顺序测试，使用同一 GPU 互斥锁。llama.cpp 为本机 build 12188 / commit 095668452，开启 Flash Attention，KV 为默认 FP16。native 图片像素上限 999424，llama 图片 token 范围 64～976，对这组图产生相同 prompt 长度。

| 引擎 | 固定 128 token 热运行总 tok/s | 完整描述图/分钟，两轮 | 采样整卡显存峰值 |
|---|---:|---:|---:|
| llama.cpp，默认 top-k/min-p | 263.3 | 32.27 / 30.45 | 8.25 GiB |
| native 本轮版，top_k=0 / min_p=0 | 246.8 | 45.50 / 46.18 | 12.47 GiB |

完整描述使用 temperature=0.5、top_p=0.9、repeat_penalty=1，llama 保留自身默认 top-k/min-p。两引擎采样设置和生成文本不同，完整描述吞吐只能说明这组任务表现，不能当作纯算子倍率。固定 128 token 使用 temperature=0，结果显示 llama 仍略快。显存为 nvidia-smi 采样的整卡峰值，包含桌面背景和 allocator 缓存。全部完整描述自然结束且为合法 JSON。

额外安排的 llama top_k=0/min_p=0 重测在 GPU 排队期间遇到另一会话测试，本轮未执行，已停止自己的等待进程；不引用上一轮数据充当本轮同采样对照。已完成的默认采样对照数据见 `engine-llamacpp-default.json` 和 `engine-native.json`。

## 数值与功能验证

- 1 槽、8 槽各 64 步，同输入 teacher forcing 比较完整词表 logits：最大误差 0、RMSE 0，PSNR 为无穷大。这是该测试集的数值一致性，不能替代所有输入或其他 GPU 的验证。
- 融合卷积测试 batch 1/8/32/256、每种连续更新 4 步，输出和缓存状态逐值一致；带 bias 使用原路径。256 是算子测试，未在 16 GB 卡上跑完整模型 256 并发。
- 真实 HTTP 连续两轮各 12 个不同长度请求，全部返回 200；错误模型、超上下文、正常请求并行返回 400/400/200，完整描述和关闭服务通过。
- 混合 831/2016 prompt token，跨 2048/4096 Graph 桶，Graph 开关和槽位长短复用产物一致。
- CPU 回退、模型实例隔离、提示词回归：4 项通过。
- 最终 ZIP 已校验 CRC、7 个变更源码 SHA256，其余条目与上一版已验证补丁逐项一致。

## 实现边界

`qwen35/attention.py` 只改变该 Torii 模型实例的 SDPA 派发。单 token 时把同一 KV head 的 query heads 放到 query-length 轴，避免 Transformers `repeat_kv` 的复制；prefill 和 CPU 走原实现。

`qwen35/kernels/convolution.py` 在一次 kernel 中完成深度卷积、原位历史更新和 SiLU。保留 FP16 中间舍入，SiLU 使用 libdevice exp 与精确除法。早期近似 sigmoid 候选有微小误差，未进入最终包。

Q6 GEMV 在读取布局中累加，将布局转换移到 K 循环之后，保留 FP32 累加和最终归约顺序。仅 Torii 的 Q6 单行投影启用；共享 Qwen3VL 调用默认沿用旧路径。

## 限制

仅本机 RTX 4070 Ti SUPER 实测；5090、4090、V100、RTX 6000 Pro 尚未实测。没有新增 FP8，也没有把模型常驻展开成 FP16。本轮显存改善有限，主要收益是减少重复计算与复制。

测试只有 8 张图片，不能把当前图/分钟直接外推为 3000 万张的最终工期。线上还有下载、画师注入、质量复检、产物落盘与设备占空比的影响。

原始 profile 和日志保存在 `E:/tagger-validation/torii-further-20261125`；本目录保留结果 JSON、测试 XML、源码哈希和包哈希。

补丁：`H:/tagger/dist/tagger-20261125-torii-throughput-v2-patch.zip`。关闭客户端和 NL 服务后解压覆盖原目录，保留现有模型、配置与产物。基于上一版已验证发行快照叠加本轮推理文件，未混入另一会话仍在开发的测速面板。
