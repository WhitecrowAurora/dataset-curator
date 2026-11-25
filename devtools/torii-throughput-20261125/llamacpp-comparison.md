# Torii Q6：llama.cpp 与优化后 native 速度对照

测试时间：2026-11-25，Windows / RTX 4070 Ti SUPER 16 GiB。结论：native 相比本项目旧实现显著提速，但没有全面超过 llama.cpp。单并发 llama.cpp 更快、更省显存；4～8 并发接近，采样设置会影响排序。未在 5090、4090、V100 或 RTX 6000 Pro 上实测。

## 方法

- 同一 ToriiGate-0.5 Q6_K、FP16 mmproj、官方模板、现行描述 prompt；8 张不同原图 1/10/101/102/103/104/105/106.jpg，生成预算 2048、temperature=0.5、top_p=0.9、repeat_penalty=1、固定独立 seed。
- HTTP 完整链路：请求/解图/视觉/预填充/解码/采样/JSON 返回，关闭跨请求提示词缓存。每轮 8 图，固定长度对照每图 128 token、temperature=0；首轮用于新形状编译预热，表中固定 token/s 取后续热运行均值。完整描述两轮都自然结束、合法 JSON；不含后续质量重试、TAG、磁盘写入和占空比休眠。
- 公平参数组显式 top_k=0、min_p=0，native max_pixels=999424（与产品 1000000 对此样本的取整等效），llama.cpp min/max image tokens=64/976。两个引擎对每张图均报告 1175 个输入 token；每槽 context=8192。
- llama.cpp 0.9.0-dev build 12188 / commit 095668452，全 GPU、Flash Attention on，8 CPU 线程；native 0.6 + torch 2.13.0+cu132 / transformers 5.14.1 / Triton 3.7.1 / FLA 0.6.0，全程保留量化权重。
- 一张 GPU 串行测试，不停止用户桌面/游戏。显存为 nvidia-smi 采样的整卡峰值（包括约 1.4 GiB 背景及 allocator 保留）；采样间隔约 0.25 秒，有低估瞬时峰值的可能。不是纯模型 allocated，不能与先前 torch allocated 10 GiB 直接比较。

## 相同参数对照

| 引擎 | 并发 | 固定 128 token 总 token/s | 完整描述 图/分钟 | 完整描述 token/s | 整卡峰值 GiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| llamacpp | 1 | 76.6 | 14.74 | 86.3 | 6.20 |
| llamacpp | 4 | 139.6 | 22.53 | 140.4 | 6.97 |
| llamacpp | 8 | 165.0 | 25.49 | 152.9 | 8.29 |
| native | 1 | 52.1 | 10.48 | 59.6 | 7.63 |
| native | 4 | 151.9 | 23.60 | 133.4 | 9.99 |
| native | 8 | 193.8 | 31.16 | 180.3 | 13.15 |

8 并发此组 native 的图吞吐约高 22%，固定输出 token 吞吐约高 17%；单并发仍落后。两种引擎输出文字不完全相同，不能把图吞吐比当成纯算子倍率。GPU 利用率高也不表示吞吐必然高：此组 native 8 路约 92%，llama.cpp 约 52%，仍需结合实际 token/s 判断。

## llama.cpp 默认采样与实际图片配置

显式 top_k=0/min_p=0 对 llama.cpp 的速度影响很大，所以另测保留 llama.cpp 默认 top-k/min-p 的正常使用路径。其他 prompt、图片、预算与并发不变，不将两种采样设置混称严格同算法比较。

| llama.cpp 配置 | 并发 | 图/分钟 | token/s | 整卡峰值 GiB | 每图输入 token |
| --- | ---: | ---: | ---: | ---: | ---: |
| 默认采样，同图片 token 预算 | 8 | 32.96 | 193.6 | 8.20 | 1175 |
| 默认采样，客户端原图片配置 | 1 | 17.34 | 100.4 | 6.37 | 1715 |
| 默认采样，客户端原图片配置 | 8 | 28.76 | 173.7 | 8.72 | 1715 |

默认采样、相同图片预算下 llama.cpp 约 33 图/分钟、native 约 31 图/分钟；小样本结果接近，不能断言 native 更快。按客户端原图片配置（1024～4096 image tokens），llama.cpp 8 槽约 28.8 图/分钟，但其视觉输入较多，不能归因于引擎差异。

当前产品 Torii llama.cpp 仍固定 1 槽，8 槽 llama.cpp 是外部测试服务，未在本轮放开产品设置。实际现有 1 槽约 17.3 图/分钟，native 8 槽约 31 图/分钟；这包含并发与图片预处理差异，不是纯引擎性能提升。生产处理 3000 万张时，不能用这 8 图直接推算总工期。

## 初次启动、复测与正确性

- native 加载/重排模型约 26.6 秒，llama.cpp ready 约 3.6～4.4 秒。native 8 并发第一轮固定输出 6.19 秒，热运行约 5.14～5.43 秒；新环境首次安装和全部内核编译可能更久。
- 更换测试顺序后，native 8 并发完整描述 15.49/15.45 秒，约 31 图/分钟，与主结果一致。随后 llama.cpp 测试进程仍受同一 Python 进程保留 CUDA 资源影响，`engine-followup.json` 的 llama.cpp 显存不用于正式表格。默认采样已在独立进程重新测量，以 `engine-llamacpp-default.json` 为准。
- 主对照 96 条完整描述、默认采样 16 条、原图片设置 32 条，全部返回自然 EOS 和可解析 JSON。固定 128 token 对照用于速度，不把截短文本算合格描述。
- 量化验证为同一 Q6 权重两条执行路径的 logits，先前 PSNR=84.046 dB；不是 BF16/Q6/FP8 量化精度比较。没有宣称语义质量完全一致。
- 256 行算子与配置链路通过，但完整 256 槽未在本机 16 GiB 卡验证。新增 UI、保存配置和相关回归均通过。临时脚手架归档后不进入产品。

原始数据：engine-comparison.json、engine-llamacpp-default.json、engine-llamacpp-production.json；简表 comparison-summary.json。engine-followup.json 仅作顺序敏感性记录。
