# 调度中心 native 启动 gpu_mem=0.0 修复

2026-11-21。截图中调度中心已连接服务器，但准备本机 NL 服务时失败：`gpu_mem=0.0 不合理：取 0.1~0.98`。

根因：`ExecutionConfig` 的显存比例 0 表示自动；native 不使用显存比例，因此 `InferenceRuntime.prepare` 将有效配置置 0。构造 `VllmService.start` 参数时仍原样传入 0，而服务契约用 `None` 表示未指定，并拒绝 0。错误发生在模型加载之前，无法据此判断显卡或驱动有问题。native 分支会覆盖手填比例，所以改填 0.8 不能绕过这个缺陷。

修复位于 `tagger/web/center_runtime/inference.py`：进入服务接口前，将 0 转为 None。有效配置及保存配置仍保留原语义，服务复用使用相同的规范化参数。未放宽非法比例校验，未修改模型、精度、提示词、前端或服务器。

验证：

- 修复前直接调用真实启动接口，复现截图原错误。
- 20 项临时专项通过：16 组配置覆盖 native CUDA / CPU、llama.cpp、vLLM，NL / TAG+NL，比例 0 / 0.85；调用真实 prepare、start 校验、快照与复用逻辑，仅替换模型加载及硬件/权重依赖。验证第一次启动成功、第二次复用服务、并发保持、保存配置不被修改。
- 4 组非法比例继续被配置模型及服务接口拒绝。
- 加上既有 Web 服务、中心 watchdog、槽位回归，共 89 项通过。1 条现有 Starlette/httpx 弃用警告。`git diff --check` 通过。
- 本轮未启动 GPU 模型、未连接或派发生产任务；此次是参数契约修复，不是再次验证模型生成性能。此前 native 性能测试参见 `devtools/native-performance-20261121/README.md`。

交付：`dist/tagger-20261121-native-center-fix.zip`，包含此前 native 性能优化。不含 Python 运行环境、模型和个人配置；已有用户退出客户端后更新程序并重启，再点击调度中心开始。无需修改服务器或重装模型。

原始测试结果见 results.xml，包大小、SHA256、源码比对见 package.json。临时测试脚手架归档到 `H:\lulynx-trainer\.delete\tagger-center-gpu-mem-20261121\backend\smoke\tagger-center-gpu-mem-20261121`。未 commit / push。
