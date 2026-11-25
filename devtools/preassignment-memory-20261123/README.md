# 管理员上次成功派单配置记忆 · 2026-11-23

已部署到现用调度中心。按工作区在服务端保存最近一次成功的用户预派单配置，页面刷新、浏览器重开或换浏览器后均能带入。包含打标内容、执行来源、TAG/NL 模型与引擎、精度、批大小／并发、画师设置。

快速助手优先带入上次成功配置，提供「使用工作区默认模板」按钮；普通预派提供上次配置／默认模板／成员本机配置选项。未提交或失败派单不更新记忆。保存或清除默认模板会清空上次配置记忆，下一次带入新模板。各工作区独立；客户端默认值不改；派单配置依旧保存快照，不重写旧任务。

验证：`tests.xml` 57 项，56 通过，1 项因 trainer 环境缺 pyarrow 跳过；`browser.json` 实际 HTTP + Edge 刷新恢复并发 3、切回模板 6、正常派单及零新租约通过；`candidate-validation.json` 生产副本 schema/integrity、原 16 分项两种排序三种导出、记忆快照／清除／租约不变通过，测试数据库已清理；`deployment.json` 8 文件精确 SHA 基线部署，两服务 active、health 与公网静态一致；`live-browser.json` 正式网页只读验收快速助手、模板按钮、NL/TAG 显隐、画师、分包核对、离线、分派表、顶部速度与移动宽度均正常。

第一次正式网页验收遇到分包列表上游读取超时，再次完整验收通过；保留初次报告 `live-browser-first-attempt.log`。没有将这次超时解释为本功能导致，也未宣称网络问题已根治。

代码备份 `/tagger/.delete/preassignment-memory-20261123`；本地测试脚手架按原目录归档到 `H:/lulynx-trainer/.delete/tagger-preassignment-memory-20261123/backend/smoke/tagger-preassignment-memory-20261123`。本轮不需要新客户端包。
