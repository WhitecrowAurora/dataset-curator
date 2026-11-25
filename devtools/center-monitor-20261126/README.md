# 调度中心顶部监控改版

新增 ExecutionMonitor / CenterOverview，复用 executionMode 保持当前分片 TAG/NL 显示口径。Center 页面移除重复状态和六张原统计卡；执行状态保留交付、告警及全部动作。

验证：npm run build 成功（现有单包体积警告）；Edge 浏览器九组 UI 场景通过，1440 深浅主题、900、390 像素监控区无横向溢出。固定左侧菜单在极窄屏幕仍占宽度，未改全局导航。

verification.json 为最终测试结果，截图使用模拟运行数据，非实际测速。验证了未知/已知总量、NL/TAG 模式、暂停、旧进度、轮询失败、无 GPU/多 GPU、展开详情、错误可见性，以及恢复/强制继续请求和释放二次确认。测试服务隔离，不连接真实服务器或启动推理。

内置浏览器因 sandboxPolicy 元数据错误不可连接，使用本机 Edge。收尾时本地 27100 服务未运行，真实运行状态回放未完成；不将模拟场景视作真实模型测试。

更新包仅包含前端源码和 dist。临时测试脚本归档到 H:/lulynx-trainer/.delete/tagger-center-monitor-20261126/backend/smoke/tagger-center-monitor-20261126。
