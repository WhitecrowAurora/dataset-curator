# 中心结果分享与本地图像核验

服务器已部署，客户端使用 dist/tagger-20261123-result-review.zip。

管理员入口：工作区详情 → 选择已有回传分片 → 已回收结果下载与分享。
也可在包列表 → 分片详情生成单包导出。默认分享 7 天，可设置 1～30 天并撤销；
撤销只禁用外部链接，管理员仍可下载。结果快照保留全部冲突候选和源包身份。

客户端入口：冲突与决策 → 导入中心结果与图片核验。
粘贴分享链接或选择 ZIP / JSONL / Parquet / JSON，查看每包结果数、TAG / NL、
冲突 / 失败、核验数量和下载状态。查询原包版本和大小后勾选下载；已校验缓存复用。
原图、TAG、NL 并排核验，支持通过、保存修改、重做和导出核验记录。
修改保留原文，文本检查 512 token 硬上限；持久记录保存在 work/result-review。
升级保留 work / run / configs / models / env。私有 HF 原图需本机具备访问权限。
本次不自动回写中心结果，且不重跑推理模型。

总表定位：中心 ZIP 本身带实际分包来源。目标仓库 manifest.jsonl 的 src_shard
是重打包前源包名，例如 0000.tar，不能当成当前 shard-000000.tar。
目标仓库的 844 个实际分包都有对应 artist/*.artist.parquet，使用其完整 ID 清单
补充定位，不依赖作者字段是否非空。自动 HF 定位直接下载这些较小清单并持久
缓存 SQLite 索引，避免为定位下载 1.19 GB 总表。其他包含实际分包路径的总表
继续按用户配置列读取。多包或缺失关联保持未定位，不猜包。

验证证据：
- tests.xml：9 项新增功能/边界测试和 50 项现有 Web/冲突回归，共 59 项通过。
- http-browser.json：真实管理员包详情、工作区和客户端页面，下载、匿名链接导入、
  缓存复用、图片展示、核验导出、撤销及管理员保留下载通过，无 pageerror。
- remote-snapshot-validation.json：服务器真实 DB 副本，15 包 8678 条、8614 冲突
  候选完整保留，17 包 10009 条；源包 SHA / 版本保留，冲突决议未改。
- real-source.json / real-mapping.json：固定 HF revision 的真实总表片段、17 包
  10009 条 ID 清单与 Range 取得的原始 TAR 图片样本。图像 ID 4214020 定位和
  缩略图通过；获取素材后的定位测试模拟 HF API，不是整库真实下载。
- index-coverage.json：844 包均有 sidecar ID 清单，未缺失。
- live-import.json / live-revoke-public.json：生产公网链接导入真实 17 包 10009 条
  结果，公网撤销后拒绝读取，管理员下载仍可用。验证链接已撤销。
- deployment.json：12 个部署文件校验、两服务 active、健康与静态资源/API通过；
  生产 15 包仍为 8614 pending。没有替管理员采用结果。
- package.json：ZIP CRC、关键源码/UI同源、worker同源、私人状态和凭据扫描通过。

本轮没有下载 1.84 GB 完整原图包或建立整个 800 万 ID 索引，没有运行 GPU 推理。
大型下载复用既有断点下载器，新增测试确认有效缓存、源哈希不一致拒绝和直接 TAR
偏移读取。源码按导出、读取、索引、图像、状态、编排分别分模块。

压缩包 76,726,126 字节，SHA256：
fa4dcae52ff801b99755609a709e2f2a06ea8d4454c1c2a05f41a3c239d72cfc

部署前源码和 DB 备份：/tagger/.delete/result-review-20261123。
临时测试代码归档：H:/lulynx-trainer/.delete/tagger-result-review-20261123/backend/smoke/tagger-result-review-20261123。
未使用 git reset / stash drop / stash clear，未 git push 或从 Git 恢复文件。
本轮工具目录没有 jcodemunch_guide，无法调用；使用当前源码和真实接口核验。
