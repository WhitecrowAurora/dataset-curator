# 梦斋接单暂停排查

2026-11-24 16:28–16:34（UTC+8），仅只读检查服务器数据库与日志，未改变运行意图、分派或离线工单。

- 用户梦斋，在线设备 714115626688，RTX 5070 Ti，工作区 Dru768，仅 NL，当前选择 Torii/native/Q6。
- 当前会话正常上报，status=managed_idle、running=false、preparing=false、error 为空。managed_policy 为 desired=paused、pause_reason=manual。
- 当前会话 last_seq=0、next_seq=1，没有开始指令。当天开始指令查询为空，两个服务 active。设备状态请求日志返回 200。
- 当前在线设备没有 open/submitted 租约。shard-000015 的 NL 仍预派给梦斋、尚未完成，未见开放执行租约或源核验待处理记录。
- 16:33 进一步核查主数据库 artifact_conflicts：shard-000015 仍有 8614 条 pending 冲突；该片受冲突阻止，不能承诺恢复后领取或直接重复打标。应先由管理员核验裁决。
- 管理员于 16:24 生成 Oa6941fed7b3b3ab2，42 包离线工单，ready；它使用独立离线身份，不会直接成为该设备的在线执行任务。未取消或转换此工单。
- 服务端 managed/store.py、scheduling/onboarding.py 哈希与已部署 auto-workspace 候选一致。

确认结论：恢复操作尚未在服务器形成有效开始。没有证据表明本轮卡在模型启动或网络断连。服务端没有保存每次 poll 的 intent 原文，不能仅凭当前 policy 断言请求完全没到达。

本地读取实际发布 ZIP 验证：tagger-20261124-center-recovery.zip 的 center.py 没有 self.runtime.resume()；tagger-20261124-auto-dispatch.zip 有该恢复路径。旧包在已有托管会话中点击开始不请求恢复，是当前首要疑点；梦斋实际使用包名仍待确认。client_ver=3.4.0 不能区分这些发布包。

未修改主动/被动模式或执行代码。管理员可以在设备面板选择该在线设备并下发“开始打标”，绕过本地旧按钮；若要执行 42 包离线工单，则应在客户端导入离线工单并从离线任务开始。
