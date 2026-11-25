# 工作区列表空白修复 · 2026-11-23

生产数据库及工作区接口中的 Dru768_仅nl打标、Fox768Test、dan 均存在。
本轮只修改 fox-panel/web/app.js：重建工作区选择控件后清空旧实例的列表签名，确保相同数据也会填入新控件。

## 原因与复现

工作区请求失败时，错误提示替换原列表 DOM。重试成功后重新挂载选择控件，但旧 pickerSignature 仍然相同，跳过 setItems，导致新控件显示 0 个工作区。
返回空工作区列表后重新加载相同数据也受同一问题影响。

before.json 与 live-before.json 分别记录隔离 HTTP 服务、真实生产面板的失败→重试复现：请求有数据，列表从 1／3 项变成 0 项，无 JavaScript 异常。

## 验证结果

after.json 与 live-after.json 均通过：

- 失败→重试恢复完整列表。
- 空列表→相同数据恢复完整列表。
- 选择详情、分派表导出入口、返回列表、切页返回、搜索与清空搜索正常。
- 顶部速度统计可见，手机页面无横向溢出。
- 最后撤销全部响应拦截并重新加载真实接口，生产页面显示完整 3 个工作区。
- page_errors 为空，Node 语法检查及 git diff --check 通过。

浏览器在本机 Edge 中运行；直连公网 IP 被本机浏览器阻止，因此生产网页经 SSH 转发真实 panel 31832 服务，API／静态资源来自生产服务。仅在故障复现步骤冻结只读 API 响应，未改生产数据。公网 app.js 另通过服务器 HTTP 读取核对。

## 部署

备份：/tagger/.delete/workspace-list-20261123（网页源码及 SQLite 在线备份）。
deployment.json 记录源码与公网静态内容匹配、两个服务 active、进程 PID 未变、工作区完整及数据库 integrity_check 通过。
仅原子替换网页 app.js，无服务重启，无派工／配置／租约修改。

初次部署校验脚本因替换表达式末尾空格不一致失败，发生在写入前；deployment-validation-error.json 保留该记录，修正脚本后部署成功。

测试脚本与隔离数据归档至 H:/lulynx-trainer/.delete/tagger-workspace-list-20261123/backend/smoke/tagger-workspace-list-20261123。
