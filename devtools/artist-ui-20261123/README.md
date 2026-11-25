# 画师注入格式说明修正

当前 PixAI + Qwen3VL / Torii 配置使用的实际格式：

- TAG：`@wlop, 1girl, solo`
- 英文 NL：`In the style of @wlop, a girl outdoors`
- 中文 NL：`使用 @wlop 的风格，一位少女站在户外`

TAG 和 NL 中的 @ 都用于标识同一个画师，NL 默认在自然语言风格句内保留
这个标识。画师名来自已有可信来源，注入由系统完成，不让模型猜名字。
TAG / NL 两个开关独立；多画师时 TAG 注入第一位，NL 最多列前三位。

客户端新建任务中 VLM 区域仍用旧文案「@画师 放在 NL 最前面」，没有说明
自然语言模板，容易误解。已修正为「NL 开头加入画师风格句」并列出中英文示例，
同时修正总开关说明。服务器画师表设置也增加三种实际输出示例，已部署静态文件。
没有改变打标内容或注入逻辑。

本地封包对尚未注入的旧正文仍有裸名前缀兼容逻辑，显式配置 artist.nl_style: bare
也会使用旧格式；本轮没有把这些兼容路径伪装成自然句式或改写既有产物。

[formats.json](formats.json) 记录真实 TaggerClient 输出方法、中英文补注入、独立开关、
幂等性及服务器同源验证通过；既有句式自检 54 项通过。TypeScript/Vite 构建及
服务器 JS 语法检查通过。当前工具目录没有 jcodemunch_guide，无法调用。

[deployment.json](deployment.json) 记录服务器静态资源与部署源码哈希一致，备份在
/tagger/.delete/artist-ui-20261123-1435；无需重启服务。

[新版客户端](../../dist/tagger-20261123-artist-ui.zip) 保留前轮功能；
[package.json](package.json) 记录 ZIP CRC、关键源码/UI、worker 同源和私人数据排除验证。
压缩包 77,034,836 字节，SHA256：
`2571ac9b9aab1fecb9a03bc973c1a76e16db60e92010db1907378dda42743cad`。

升级保留 work / run / configs / models / env，旧压缩包保留。
本轮验证直接执行现有方法，没有新增永久测试文件。
