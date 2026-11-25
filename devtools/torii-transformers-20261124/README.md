# Torii native Transformers 兼容修复（2026-11-24）

用户报告 ToriiGate-0.5 / native 启动时缺少 `torch_causal_conv1d_update`。在官方 Transformers 5.22.2 的隔离依赖环境中复现；故障发生于本地模型加载，不属于调度中心网络、分片派发或显存错误。

## 原因与修改

- 新版将 `torch_causal_conv1d_update` 更名为 `causal_conv1d_update`，线性注意力 forward 也从实例函数改为全局函数。只替换名称无法保留原实例的 torch / FLA 选择。
- 同一加载链还会遇到文本位置编码构造器不再接受 `device`、视觉位置编码改为接受配置对象。这两个错误也已在实际新版环境中分别复现。
- `linear.py` 保留旧实例路径。新版复制官方 forward 的函数命名空间，使用官方 PyTorch 卷积和选定的 delta-rule 内核，保留官方缓存算法和 accelerate 卷积钩子；不修改共享 Transformers 模块或 site-packages。支持可选 `kernels` 把函数包装成 Module 的形式。
- `weights.py` 根据实际视觉模块区分旧维度参数与新配置参数，在目标设备上重建文本和视觉旋转位置编码。GGUF 参数映射、反量化、head 排列和模型权重保持原逻辑。
- Torii runtime 由 `lulynx-qwen35-0.3` 更新为 `lulynx-qwen35-0.4`，使服务指纹可辨认。正式依赖仍锁定 `transformers==5.14.1`，没有升级或改写共享 Python 环境。

源码变更限于 `tagger/inference/qwen35/linear.py`、`weights.py`、`runtime.py`。旧文件从上次发行包保存到 `baseline-files/`，没有从 Git 恢复文件。

## 验证

使用 trainer 的 Python 3.12.9 / torch 2.13.0+cu132，CPU 实测。新版 Transformers 5.22.2、Hub 1.33.0、tokenizers 0.23.1 与可选 kernels 0.17.2 均只安装在临时测试目录中。官方 Transformers wheel SHA256 为 `829c2820aff91730b8ec45a4b8ff4be07f586a140b68a9073998b36fa35297dc`，下载后已核对。

| 文件 | 环境与验证 | 结果 |
| --- | --- | --- |
| `old-checks.xml` | Transformers 5.14.1 的21项兼容专项 | 全通过 |
| `new-checks.xml` | Transformers 5.22.2 的同组21项 | 全通过 |
| `new-kernels-checks.xml` | 新版加真实 kernels 包的21项 | 全通过 |
| `caption-checks.xml` / `runtime-checks.xml` | 旧版现有提示词、描述、NL引擎、依赖、槽位与服务生命周期，共157项 | 全通过 |
| `new-existing-checks.xml` | 新版同组157项 | 全通过 |

专项覆盖1/2/4张、左填充、预填充、单步和连续缓存解码、缓存状态、图像生成、FP32/BF16、meta构造后的旋转编码恢复、实例隔离、CPU不调用可选GPU实现，以及通过替身检查FLA两类内核是否实际被当前实例调用。torch分支的小模型 logits、缓存和生成 token 与同版本官方实现完全一致。FLA替身不代表真实FLA CUDA数值或性能验证。

`new-real-cpu.json` 使用既有完整 ToriiGate-0.5 Q6_K + FP16 mmproj、固定官方metadata、正式提示词、面积预算1,000,000、上下文8192，通过真正的 `runtime.load_engine` 加载全部模型。确认没有残留meta参数或缓冲，logits有限，单张、两张合批、两请求8/12 token上限均通过。16-token请求是短预算功能探针，不能算完整描述质量验收。

`new-real-cpu-full.json` 在最终源码上重新加载真实模型，使用2048生成预算完成一条130 token描述，正常EOS，返回JSON能被既有 `caption_text` 解析。输出和原始logits保留在本目录。完整描述耗时约214秒是CPU测试耗时，不是CUDA性能承诺；本轮未比较新版与旧版完整模型数值。

本机4070 Ti SUPER当时约5.6～5.7GiB显存占用、53%～62%GPU使用率，包含用户其他应用。本轮没有启动GPU测试或停止其他进程。真实GPU/FLA、4090/5090/V100、长时间连续打标仍需对应环境复测。Python AST、ZIP CRC和包内源码一致性检查通过，现有库弃用警告没有变成失败。

## 交付

- 小补丁：`H:\tagger\dist\tagger-20261124-torii-patch.zip`，6,704字节。
- 完整客户端：`H:\tagger\dist\tagger-20261124-torii-compat.zip`，77,070,961字节。
- 精确SHA256、包内变化和28份独立worker源码核对见 `package.json`。

小补丁仅包含上述三个Torii源码和说明。停止打标，关闭客户端及native模型服务，把补丁里的 `tagger` 文件夹合并到原客户端根目录并覆盖，再重新打开客户端。不需要重新下载模型或为此重装依赖。

完整包由现有 `pack-dist.bat` 生成，保留全部旧文件，仅修改三个Torii源码，另外补入此前已存在的 `mcp/center/bridge.py` / `README.md`。不含env/python、模型、个人配置或本地任务产物。服务器无需部署此客户端修复。

临时测试与隔离依赖归档到 `H:\lulynx-trainer\.delete\tagger-torii-transformers-20261124\backend\smoke\tagger-torii-transformers-20261124`，没有新增生产测试入口。没有Git提交、推送、reset或stash操作。当前工具目录未提供 `jcodemunch_guide`，使用当前源文件、官方上游源码与实际运行核对。
