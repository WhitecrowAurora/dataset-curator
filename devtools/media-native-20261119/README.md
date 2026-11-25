# 混合图片自动分流与 Torii native 评估（2026-11-19）

混合批次已接入逐图媒体判定；Torii native 已接入可选实验适配并完成 CPU 生成及托管 HTTP 验证。默认仍使用 llama.cpp，当前 native 没有获得性能优势。GPU 被其他程序占用，本轮所有模型测试使用 CPU，不能外推 CUDA 时延。

## 使用方式

新任务选择 NL 或同时打标，开启「描述使用动漫 Torii / 真人 Qwen」，数据类型选「混合 / 未知 · 逐图自动选择 Torii / Qwen」。明确的数据集媒体字段优先；其余图片用 CPU 分类器，分数至少 0.8 才选动漫/照片，低分回退 Qwen。两个描述组串行执行，有实际待处理图片才启动对应服务，完成产物仍可续做复用。

「冲突与决策 → 发色复核设置 → Torii 引擎」可选择 native 实验适配，依赖使用「lulynx 专用推理（实验）」组，Transformers 5.14.1。首次下载官方处理器小文件，复用现有 Q6_K GGUF 和 FP16 mmproj，无需再下载完整 safetensors。默认模型路径保持 models 下相对目录；本机的 E 盘设置只用于测试。

媒体模型 `deepghs/anime_real_cls/mobilenetv3_v1.4_dist` 固定 revision `097a6c1e9e62866a435810c883a45a7cb2c7077d`，模型管理第 4 张卡可下载到 models/media，权重约 16 MB，原仓库声明 OpenRAIL。`model-download.json` 记录实际大小和 SHA256。不要与 anime_classification 的动漫内部风格分类器混淆。

## 分流验证

- 126 张旧素材真实 CPU 分类，结果 anime 15 / photo 103 / unknown 8。去掉首图冷初始化后，125 张平均约 **70 ms/图**，包含解码、ONNX 与 SQLite 操作；这不是人工真值准确率。`media-results.json`。
- 受控推理 fixture 下真实 JobManager、client、zip、JSONL 和存储：混合 5 图只分配一个描述模型，limit=3 保持原输入前 3 图，统计正确；续做不推理，JSONL 字节不变。缺失复核 PNG 不阻断后续图片。
- `mixed-cpu.json` 真实两图：hutao/107 的输入媒体均 unknown，CPU 媒体分类器分别判为 anime/photo，真实 PixAI 标签 → Torii/Qwen 普通描述 → 发色复核 → 产物写回完成，0 failed。hutao brown accepted；107 的 silver/grey 分歧 review。两图均带实际模型路径与 runtime_sha，续做零推理、JSONL 字节不变、所有托管服务已退出。
- 原相关检查 `acceptance.xml` **155 passed / 1 deselected**；未执行的旧归档检查断言 eager 启动，与当前延迟启动设计不符，没有修改旧测试隐藏差异。最新修复定向检查 `closeout-final.xml` **21 passed**，其中多项与 155 重叠，不相加。
- `browser-results.json`：native 设置保存刷新、模型就绪、390px 无横溢出、新任务混合选项可操作，4 项通过且无页面异常。关键页面截图已查看。
- TypeScript/Vite 构建通过，产物 index-oqI6YxC-.js / index-CEvozeTg.css；原有大 chunk 警告保留。PowerShell 安装脚本 AST 与 bash -n install.sh 通过；没有重装用户环境。

## native 的同图 CPU 比较

同一张 key=2 图、相同 Q6_K / FP16 mmproj、官方 Torii system/user/template，8 CPU 线程，greedy、repeat_penalty=1、max_tokens=1024。两边图像 64 token（约 65536 像素预算），prompt 均 281 token。两次均完整 JSON 正常 stop。

| 项目 | llama.cpp | 当前 native |
| --- | ---: | ---: |
| 加载到可用 | 2.95 s | 88.88 s |
| 生成总耗时 | 37.05 s | 233.32 s |
| prefill | 4.30 s | 9.27 s |
| 输出速度 | 5.50 token/s | 约 0.77 token/s |
| 输出 token | 181 | 172 |
| 进程 RSS | 4.60 GiB | 托管服务约 20.37 GiB |

证据 `llama-cpu.json`、`native-cpu.json`、`native-service.json` 和日志。加载数据有系统文件缓存影响，不当作首次从冷硬盘读取；native RSS 来自后续同模型低像素托管运行，不是前一进程峰值。总生成时间约慢 6.3 倍，按 token 速度约慢 7.2 倍，输出长度不同。

主要描述都对应可见画面：草地路径、悬浮岛、条纹帽与黄色鸟；措辞不完全相同。原图肉眼更接近灰棕卷发，两边都写 brown，不能作为细分银灰白质量通过，也不能称逐 token 数值一致。

native 是 GGUF 反量化至官方 Qwen3.5 混合注意力模块，CPU FP32 / CUDA FP16，并非 packed 量化内核。24 层线性注意力与 8 层完整注意力，不复用旧 Qwen3-VL decoder。官方 GGUF 导出存在 norm+1、A=-exp(A_log)、V head 重排列，加载时已逆转；官方 safetensors 的 A/dt/conv 少量直接核对误差 0，见 `tensor-transform-check.json`，不代表完整参数精度校验。

本机没有 fla/causal-conv1d，使用官方 torch fallback，仍有展开权重、CPU 读带宽、初始化和线性注意力内核成本。16/32 GiB 内存机器不适合把本版本 native 当默认；下一步如继续优化，优先评估 GatedDeltaNet 的受支持内核与量化矩阵表示，再在 GPU 空闲时做同图质量/显存/时延比较。

## 服务验证与本轮修复

`native-service.json` 通过实际 ReviewRuntime 启动，HTTP /health、/version、/v1/models、非法模型 400，交替 key=2 / hutao / key=2 三请求；回到原图的 greedy 输出一致，切换图片输出不同，endpoint 复用同 PID，stop 后进程不存在。三次请求用 16/32/16 token，只验证 HTTP、状态与缓存隔离，完整输出证据由另一次 172 token JSON 提供。实际托管再次启动到就绪 94.92 s。

修复了延迟启动后未生成 runtime_sha 的溯源问题，使用原 _prov_base 摘要并保留本组像素预算；修复 native 嵌入式 Python 子进程不能导入项目、共享版本路由名字遮蔽导致返回不正确。旧 Qwen stop 限制保留。

历史日志保留：原手写生成先漏 mm_token_type_ids，后来重复输入整段 history；均被官方 generate 缓存生命周期替换。native-first/cpu/retry/final 日志不能作为当前性能结论。mixed-cpu/mixed-diagnose 初次失败来自测试把 httpx.Client 替换成函数，触发 HF 类型注解，已改测试用子类，不改生产代码。

## 尚未验证

GPU native 的速度、显存、FP16 数值质量、优化内核均未验收；本机 GPU 约 5 GiB 被其他程序使用，没有停止其他窗口。百万级媒体缓存/复核 PNG 存储与 HF 分组两遍重读成本未验收，HF 临时分片仍沿用处理后清理策略，可能再次读取或下载；不要把 70ms CPU 分类成本当作所有输入方式的总增量。应用内串行锁不等于跨窗口 GPU 仲裁。

RAM 保留仍主要是可回收 mmap/系统文件页面，不是初始化完的 CPU VLM 常驻。已完成产物的续做默认复用历史结果；更换模型后要重做已有图片，沿用「强制重打」。

临时测试与隔离状态归档 `.delete/media-native-tests-20261119/backend/smoke/media-native`，日志与结果保留本目录。未 commit/push/reset/stash，未更改其他窗口服务。请求的 jcodemunch_guide 本会话工具列表缺失，源码结论来自当前磁盘读取。
