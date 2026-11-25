# UI 并发测试

2026-11-25，工作区 H:\tagger。

## 使用

重启本机客户端后进入侧栏「环境 → 并发测试」，无需登录调度中心。

- 选择已准备好的本地模型、已有精度、推理引擎和设备。测试不自动下载模型或生成量化权重。
- 默认档位为 1、2、4、6、8；可一键扩展到 24，也可输入 1～256 的整数，最多 32 档，自动排序去重。
- 样本路径留空时读取最近分片；也可选自己的图片目录或 TAR、ZIP、Parquet。每轮固定同一批图片，所有档位分别准备模型、预热、测量。
- 结果包括实际并发、张/分钟、准备/预热/测速时间、成功失败数、采样峰值显存，以及达到最高速度 90% 的最小成功并发建议。实际并发重复或样本不足的档位会明确跳过。
- 支持停止，保留已完成档位。网页刷新可恢复运行状态；后端退出取消本轮测试并清理其服务，后端重启将未完成报告标记为中断。
- 测试使用独立输出，不保存到正式打标配置，不领取租约、不回传。结果目录可指定到其他磁盘。

## 实现范围

后端新增 `tagger/web/benchmark/` 的请求、目录、样本、执行与会话模块；通过现有 FastAPI 应用注册，复用 `InferenceRuntime`、现有推理锁和 `tagger client`。原 probe 执行器只增加可选进度回调。离线任务沿用 `runtime.working` 检查，避免与并发测试争用模型。

前端新增 `pages/Benchmark.tsx`、`components/benchmark/BenchmarkResults.tsx`、`lib/benchmark.ts`，并在 App 侧栏注册。dist 已构建。本轮没有更改其他会话的模型内核实现。

## 验证

- `tests.xml`：最终 28 项全部通过。覆盖档位、样本打包/取消、模型精度过滤和旧单 GGUF 配置、预热与测量分离、失败档继续、实际并发去重、HTTP 操作、真实应用路由、指定结果目录、报告恢复、关闭取消、异常清理及离线互斥。
- `tests-runtime.xml`：最终同批 28 项在客户端自带 Python 下也全部通过。
- TypeScript/Vite 构建通过。`browser.json`：Edge 对真实构建页面和隔离模拟 API 的交互通过，1440/1024/768/390 宽度无横向溢出、无页面异常。`ui.png`、`ui-results.png` 为模拟数据界面截图，不是 GPU 性能证据。
- `real.json`：真实 FastAPI 请求 → 模型准备 → llama.cpp 服务 → tagger client 打标完成，无模型或打标模拟。RTX 4070 Ti SUPER 16GB，Qwen3-VL 8B abliterated Q4，4 张真实图片，各档独立预热后测量。

| 请求/实际并发 | 成功/失败 | 测速时间 | 张/分钟 | 整卡采样峰值显存 |
| --- | --- | --- | --- | --- |
| 1 / 1 | 4 / 0 | 19.6 s | 12.2 | 8162 MiB |
| 2 / 2 | 4 / 0 | 14.2 s | 16.9 | 8143 MiB |
| 4 / 4 | 4 / 0 | 11.7 s | 20.5 | 8685 MiB |

真实测试结束时模型服务已停止、准备状态已清除，未建立调度身份。此结果验证该模型的 1/2/4 档完整流程；没有把其他模型、6 以上档位或长期稳定性计作 GPU 验收。报告与打标产物在 `D:/tagger-validation/concurrency-benchmark-20261125/concurrency-tests/d8f4c11ee055/`。

H 盘剩余约 1.1 GB，E 盘剩余比例不足现有阈值；实测输出改为 D 盘，未删除文件或降低磁盘保护。

临时测试脚本归档到 `H:/lulynx-trainer/.delete/tagger-concurrency-benchmark-20261125/backend/smoke/tagger-concurrency-benchmark-20261125`，不进入产品。未使用 Git reset/stash，也未 push。jcodemunch_guide 未出现在当前工具目录；内置浏览器因 sandboxPolicy 元数据错误不可用，改用本机 Edge 验证。
