# 分片下载网络与续传修复

2026-11-25 开始调查，2026-11-26 完成；工作区 H:\tagger。

## 零的现场结论

- 用户确认服务器账号为中文「零」。仅通过 SSH 只读查询 /tagger/state/db.sqlite3，没有修改用户配置、租约或重启服务。
- 383 包大小 1,842,483,200 字节，曾上报下载 42%，后在 44% 失败；关闭原因包含 curl 56 / schannel: server closed abruptly (missing close_notify)。最新查询在下载 391 包，30%，392 包为预锁。
- 用户截图明确显示 391 包为 1757 MiB、4 连接；钉住 CDN 失败后回退 DNS，随后仍出现相同 TLS 断开。不是未开启并发。日志无法区分下载源、代理或中间网络哪一端关闭连接。
- 旧版「连接方式」只影响调度通信、接单和回传，外部素材下载使用系统代理。因此切换该设置不等于切换下载线路。
- 远端源有完整大小；本次另外修复的缺少 size 退成单流问题不用于解释零的当前包。

## 实现

- 「下载与交付」增加独立下载连接：系统代理、直连、指定 HTTP/HTTPS 代理、跟随调度连接。旧配置默认仍使用系统代理。经 center 本机配置和环境传给在线 worker、预取和离线下载，不改变本机推理网络。
- 代理下载跳过无效 CDN IP 测速；直连继续原选优逻辑。本地日志增加实际线路和待下载分段数、有效并发上限。
- Content-Length 探测实际读取最终响应头，并与下载使用同一网络策略；重定向前的长度不误用。
- Range 不支持只降级当前包，不再使后续所有包永久单连接。
- 将实际传输提取到 center_runtime/download_transfer.py，缩小 fox_worker；每次重试重新读取段文件长度后请求剩余 Range，避免 curl 向同一 stdout 自动重试导致重复拼接。
- 建连超时 15 秒，持续低于 1 字节/秒达 30 秒触发停滞重试；可恢复连接错误、HTTP 408/429/500/502/503/504 最多重试 3 次，间隔 2 秒。HTTP 403 不在传输层盲重试。现有包级重试和 CDN 回退仍保留，因此整个包的最终失败时间不等于 2 秒。
- 停止及错误时保留未完成分段；下次同源同大小复用原区间，改变并发也不损失已下数据。已有单流 .part 在切换并发后优先续传。支持续传的前提是源支持 Range；完整文件仍进行 SHA256 校验。
- 独立 worker.py 和 worker-sources.json 已按源码重新生成。与编辑前相比只更新四个下载/网络模块，无其他依赖变化。

## 验证

- tests.xml：19 项通过；真实 32 MiB HTTP Range 服务测得最大同时请求 4，验证已知/未知大小、重定向、代理实际请求、单流/分段连接中断后 2 秒重试、部分缓存改并发续传、取消再启动、完整 .part 直接校验而不请求 EOF、curl 56 TCP RST 续传、忽略 Range 后下一包恢复并发、503 重试/403保留、停滞检测、配置持久化、请求契约、客户端 Python 执行独立 worker 的真实下载。所有成功文件逐字节或 SHA256 一致。
- 停滞测试将阈值缩为 1 秒以节省时间；生产阈值为 30 秒。不是零网络吞吐的复现。
- reset.xml：真实 TCP RST 产生 curl 56，验证从 65536 字节处接续且哈希正确。此测试验证接收错误的恢复，不声称复现了用户 Schannel/TLS 关闭的网络根因。
- regression.xml：3 项通过，下载失败不回传、下载中断不回传、有有效源的打标部分失败仍回传。
- TypeScript/Vite 构建通过，仅原有大 chunk 提示。browser.json：Edge 验证下载方式/地址保存与恢复、四种模式、下载连接数、调度设置独立、1440/1024/768/390 无横向溢出，0 页面异常。download-settings.png 为隔离模拟状态页面。
- 内置浏览器启动仍报 sandboxPolicy 元数据缺失，按此前方式使用本机 Edge。工具清单无 jcodemunch_guide，使用直接源码和真实传输验证。

## 交付范围与产物

本轮客户端源码、UI 构建和本地独立 worker 已更新；没有部署服务器或更新零的电脑。需要更新客户端并重启才生效；下载线路在「调度中心 → 下载与交付 → 分片下载连接」选择。当前 4 路并发已确认，不能保证增加连接数就能解决 TLS 断线。

changes.diff 是相对本轮编辑前精确快照的差异，files.json 保存最终哈希。快照在 .delete/download-network-20261125/before；测试与 SSH/浏览器脚手架移至 H:\lulynx-trainer\.delete\tagger-download-network-20261125\backend\smoke\tagger-download-network-20261125。32 MiB 下载测试数据在 D:\tagger-validation\download-network-20261125，避免 H 盘空间压力。

未执行 Git push/reset/stash 或从 Git 恢复文件；保留其他会话改动。

最终 19 项传输测试脚本归档到 H:\lulynx-trainer\.delete\tagger-download-network-20261125-final\backend\smoke\tagger-download-network-20261125。
