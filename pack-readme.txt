Tagger 打标器 —— 解压之后怎么跑
================================

第 1 步   解压
    放哪个盘都行（路径带空格、带中文都没问题），但别放进 C:\Program Files，
    那里普通权限写不进去，安装会失败。

第 2 步   运行 install.bat（只跑一次，首次约 10 分钟）
    它做四件事：解出仓库自带的 Python（env\python）、装依赖包、装打标运行时
    （torch 系，约 2.5 GB）、装 llama.cpp（llama-server，约 570 MB）。
    这一步不能省——跳过的话双击 exe 只会看到"本地服务刚启动就退出了"。

    机器没网的话，如果包里带了 env\initialization\wheels，改成：
        install.bat -WheelDir env\initialization\wheels
    只想打 tag、不跑 NL 描述：install.bat -SkipTag
    不想装 llama.cpp（570 MB）：install.bat -NoLlama

第 3 步   双击 Tagger.Shell.exe
    它会起一个本地服务，然后在窗口里打开界面。关窗口即停止服务。

第 4 步   下载模型权重（只跑一次，几 GB）
    进界面 →「模型管理」→ 找到默认那条（破限版 abliterated），点「下载」。
    权重不在安装包里，必须单独下；没下的话点「启动服务」会报
    "GGUF 权重不存在"，那不是在让你去装依赖，是缺权重文件。
    机器没网：把别人 models\nl\ 下的整个目录拷过来即可。

让 AI 帮你操作（可选）
--------------------
    包里有 mcp\ 和 agent.md。支持 MCP 的 AI 客户端（Claude Desktop、
    Cursor、CodeBuddy 等）照 agent.md 里的片段加一个服务器，就能让它
    替你建任务、看进度、封包；片段里三处路径换成这台电脑的解压路径。
    用之前先按第 3 步把服务开起来。

环境要求
--------
* Windows 10 / 11，64 位
* .NET 8 桌面运行时（Desktop Runtime）
  缺了会弹窗说明，点"确定"直接跳到微软官方下载页。
  注意是 Desktop Runtime，只装 Console 或 ASP.NET Core 运行时不够。
* Microsoft Edge WebView2 运行时
  近几年的 Windows 基本都预装了，没装的话壳子会提示并提供下载入口。

出问题看这里
------------
* 窗口一闪而过，或提示"本地服务刚启动就退出了"
  → install.bat 没跑，或者跑失败了。重跑一次，看它的输出。

* 点「启动服务」报"GGUF 权重不存在"
  → 权重没下。见第 4 步。（注意看报错里给的完整路径，确认是不是
    「模型管理」里那条你下过的模型。）

* 想确认 llama-server 装没装
  → env\python\python.exe -m tagger.cli llama status
    没装就：env\python\python.exe -m tagger.cli llama install

* 想排查具体原因
  → 日志在 %LOCALAPPDATA%\Tagger\shell.log

* 界面能打开但里面是"前端还没构建"的说明页
  → 打包时漏了 tagger\web\ui\dist。正常的包里应该有
    tagger\web\ui\dist\index.html。

* 用的不是自己电脑，想确认环境对不对
  → 在仓库目录里跑：env\python\python.exe -m tagger.cli --help
    能打出帮助就说明 Python 这边是好的。
