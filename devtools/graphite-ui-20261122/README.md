# lulynx Tagger 石墨灰 UI（2026-11-22）

参考 Orca 的中性灰层次、轻边框和短过渡，采用用户选定的「石墨灰为主，少量橙色」。菜单位置沿用原布局，公共组件统一应用到现有页面。

## 变化

- 深色工作区、侧栏、卡片与输入框使用中性灰；浅色对应中性灰白。
- 表单单选、多选改为灰色选中面、清晰边框与细橙色线，减少整块橙色。
- 分组页签和侧栏选中面短促滑动；开关保留原生 checkbox 语义，增加滑块过渡。
- 高级选项沿用原生 details；支持自动高度过渡的浏览器有展开动画，其他浏览器正常直接展开。
- 进度条使用 transform 过渡；按钮去掉放大和持续呼吸光。
- 只读统计卡去掉按压和导航装饰；可点击卡保留简单方向提示。
- CSS 和已有 framer-motion 都遵循系统减少动画设置。没有增加动画依赖。
- 表单分组保持所有组件实例挂载，切换不会重置输入或折叠状态。

## 验证

- TypeScript 与 Vite 生产构建通过。已有单入口 900 kB 提示仍存在；当前入口约 945 kB，gzip 281 kB。
- 独立 headless Edge，使用隔离 FastAPI 后端和临时配置，完成 11 组交互验证，页面异常为 0。
- 调度中心 PixAI / Torii / native 清单、真实配置保存与刷新通过。
- 分组切换保留文本、数字、模型选择和展开状态；键盘方向键/Home/End、原生 details、开关空格与可见焦点通过。
- 新建任务「同时生成 + 自动分流」「仅描述 + 自动分流」都有正常内容，输入跨分组保留。
- 深浅主题、1100 / 900 / 768 像素宽窗口无主区横向溢出；顶栏菜单位于主区上方、Esc 关闭通过。
- 系统减少动画下，分组不播放入场动画，开关和 details 过渡缩短。
- 验证范围是 UI 及隔离配置保存，没有启动实际打标任务；外部调度、活动等无关接口在测试中使用隔离响应。详情见 ui-results.json。

## 产物

分发包：H:\tagger\dist\tagger-20261122-graphite-ui.zip

包大小：76,501,705 字节。

SHA256：c088de2e9a2691e69f3f1568d571299a043cdbe14664ccb43b16bc694cacc9bd

15 个关键源码与构建文件逐字节核对通过，本机 work/run/models/env/python、身份记录和测试脚本未进入包。沿用前一版 Torii 修复。更新客户端并重启，浏览器刷新即可；本次未部署服务器派单网页。

## 参考与截图

- Orca： https://github.com/stablyai/orca （MIT）。学习视觉与交互，未复制其组件源码。
- [调度中心深色](center-dark.png) / [浅色](center-light.png)
- [新建任务深色](new-dark.png) / [浅色](new-light.png)
- [改版前调度中心](before-center.png) / [改版前新建任务](before-new.png)
- 临时测试与参考文件归档到 H:\lulynx-trainer\.delete\tagger-graphite-ui-20261122\backend\smoke\tagger-orca-ui-20261121。
