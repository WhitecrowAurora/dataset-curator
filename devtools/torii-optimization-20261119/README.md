# Torii native 优化与验收（2026-11-19）

已接入现有 Torii native worker：GPU 直接反量化加载、官方 FLA GatedDeltaNet 可选加速、按空闲显存选择展开矩阵或保留 GGUF 量化块。默认仍为 llama.cpp；本轮没有改变发色级联、阈值、提示词、人工审核入口或降低生产像素预算。

## 实现与使用

- `tagger/inference/qwen35/weights.py` 复用已有 NVRTC 反量化；CUDA FP16 避免先在 CPU 展开整个模型。五个实际 Q6_K 矩阵与旧 NumPy 反量化后的 FP16 **逐元素一致**，见 `dequantization.json`。CPU 保持 FP32 原实现。
- `matrices.py` 复用已有 GGUF/Triton 矩阵，恢复 Qwen3.5 GGUF 的 value head 排列，不重新量化。`memory.py` 根据启动时空闲显存、展开权重估算、最大单矩阵临时空间及 3 GiB 余量选择；支持 Ampere+ 且具备 Triton/Gluon 时才使用 packed。此估算不是跨窗口显存预留，也不保证所有分辨率均不 OOM。
- `linear.py` 只配置当前 Torii 实例的官方 FLA chunk/recurrent 运算；固定原 RMSNormGated 和 PyTorch 卷积，不修改共享 transformers/site-packages。无加速依赖、CPU 或旧 GPU 使用 PyTorch 路径。FLA 运算存在浮点差异，不能称数值或全部描述逐字一致。
- 采样仅在重复惩罚启用时读取最后 repeat_last_n 个历史 token，避免无用的全历史 GPU→CPU 同步；随机种子、概率采样和惩罚规则保持原契约。
- native 版本 `lulynx-qwen35-0.2`。`/version` 提供实际 device/precision/linear_attention/max_pixels 和依赖版本，ReviewRuntime 将这些写入原 provenance/runtime_sha。历史续做仍复用旧产物；需要重算应使用原「强制重打」。
- 修复托管子进程默认 GBK 读取 PyTorch 内核模板导致的 UnicodeDecodeError，native 子进程启用 PYTHONUTF8。只影响该子进程。
- WebUI 原「Torii 引擎」选择保持；依赖管理新增 **Torii native CUDA 加速（可选）**，先装专用推理组。`torii-accel` 固定 flash-linear-attention/fla-core 0.6.0，Windows 使用 triton-windows，Linux 使用 triton；不使用 FLA `[cuda]` 以免 Windows 拉入 Linux triton。单包按钮现保留 extra 的版本与平台约束。install.bat 委托的 PowerShell 和 install.sh 提供同一安装说明，默认 llama.cpp 不装加速包。

本轮使用 `H:\lulynx-trainer\backend\env\python-flashattention\python.exe`，torch 2.13.0+cu132 / transformers 5.14.1 / triton-windows 3.7.1.post27。FLA 在临时测试目录隔离安装，**未改共享环境或项目 env/python**。项目 env/python 当前 transformers 5.22.2，不等于本次验收的 5.14.1；依赖管理会报告专用推理组版本不符，需按组要求安装后使用 native。CPU 原版较慢，本轮未实现 CPU 加速。

## 同设备性能

RTX 4070 Ti SUPER 16 GB，CPU 8 threads，既有 Torii Q6_K + FP16 mmproj，相同官方 prompt/template。GPU 请求/模型进程全部串行；未停止用户其他进程。桌面/游戏背景负载会影响时间，本表为实测单轮，不作跨机器承诺。

| 项目 | 原 native dense | 加载优化 + PyTorch | 加载优化 + FLA | packed + FLA |
| --- | ---: | ---: | ---: | ---: |
| 模型加载 | 79.75 s | 21.40 s | 22.78 s | 22.93 s |
| 常驻 tensor 显存 | 9.73 GiB | 9.73 GiB | 9.73 GiB | 4.36 GiB |
| 正式分辨率生成峰值 | 未测 | 10.16 GiB | 10.16 GiB | 4.79 GiB |
| 八次生成总耗时 | 未测 | 164.17 s | 133.45 s | 171.91 s |
| 八次输出 token 总数 | 未测 | 2046 | 1974 | 1974 |

原 dense 加载来自 `dense-gpu.json`，64图像token；加载优化后三列来自 `quality-*.json`，生产预算 max_pixels=1,000,000。加载不依赖输入图，但生成时间只比较相同预算的后三列。八次生成包含6个独立输入、一次胡桃回图及一次mostima原图别名重复；历史 mostima-head 此批实际读了原图，不能作裁图证据，另见下面独立裁框测试。

FLA 总耗时降低 **18.7%**，其中胡桃输出由314变278token；按每图 ms/token 折算平均降低 **16.0%**（约12–20%），不能把输出变短全算内核收益。packed 相比 dense+FLA 更慢，主要用于降低显存，显存充足不强制使用。

首次 FLA 运算会 JIT 编译：`dense-fla.json` 首次281token cache prefill包含约7.47s编译等待，随后相同长度约0.12s；冷启动不能按热缓存时间计算。native 仍明显慢于此前 llama.cpp 的约100 token/s，默认保持 llama.cpp。此前 CPU native 233s 与本轮 GPU 时间不能作为同设备优化倍率。

## 质量与服务验证

- `quality-torch.json` / `quality-fla.json` / `quality-packed.json`：24次真实生成，全部完整 JSON 且正常 stop。6个独立输入的发色候选、多人/多色/可见头发字段相同；5个独立输入逐 token 相同，胡桃措辞不同，回图可重复。胡桃原版及加速版都仍会猜角色名、普通描述有细节幻觉；没有全库人工真值。
- 独立按历史裁框真正裁剪：`crops-dense.json` / `crops-packed.json`，蓝发 mostima-head 和浅色202共8次完整生成。每分支 torch/FLA逐 token相同；蓝发候选blue、浅色silver+grey，保留歧义。dense裁图14.03→11.19s及15.33→12.53s；packed15.88→13.17s及17.66→15.06s。
- 银灰照片107均grey，浅色裁图均silver+grey，灰度106均保留审核；**本轮提升运行效率，未解决银灰白识别**，也不能宣布标注准确率提升。
- `service-final.json` / `service-packed.json`：真实 ReviewRuntime 托管，HTTP health/version/models、错误模型400、同PID复用、两图交替/回原图一致、胡桃完整JSON、stop后无残留。packed以实际4070上模拟8GiB空闲值触发自动选择；不是在真正8GiB卡上验收。26–29s托管启动包含worker启动和已有文件页缓存预热；不是纯load_model时间。
- `final-checks.xml`：22项定向检查通过，涵盖矩阵排列/采样语义/CPU路径/显存选择/平台依赖/版本接口及旧混合任务、续做。与 `acceptance.xml` 是同一套复跑，不相加。旧归档测试的ROOT在测试conftest内修正，不修改历史脚本。
- `browser-results.json`：5项通过，settings选择与刷新持久化、第4模型卡、390px布局、混合新任务、可选FLA依赖组；errors为空。内置浏览器连接报 sandboxPolicy 字段错误后使用既有 Playwright + Edge headless `--disable-gpu`，未安装新浏览器。
- `ui-build.log`：tsc/vite通过，最终index-DbSKEiYf.js / index-CEvozeTg.css，原大chunk警告保留。Python AST、PowerShell AST、bash -n及git diff --check通过。未重建安装环境，未做Linux/其他GPU/全库质量验收。

当前工具目录不提供用户指定的 jcodemunch_guide，本轮使用新鲜源码和真实运行证据完成核对，未以旧索引代替验收。

归一化顺序实验保留在 `quality-final.json` / log，其效果与直接FLA相同但更慢，**没有保留在生产代码**。`kernel-checks.json` 单内核耗时及浮点误差仅是调研，不能当整体加速倍数。权重为既有固定GGUF，未下载10GB原始权重。

临时测试及隔离依赖归档 `.delete/torii-optimization-tests-20261119/backend/smoke/torii-opt`，生产测试目录清空；报告、JSON、日志和截图保留本目录。未 commit/push/reset/stash，应用内串行锁不等于跨窗口 GPU 仲裁。
