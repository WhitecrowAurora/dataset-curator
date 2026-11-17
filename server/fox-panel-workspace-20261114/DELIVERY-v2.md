# fox-panel v2 交付记录

- 交付日期：2026-11-10（CST）
- 版本：**v2.0.0**（`panel.py` 的 `VERSION`，页面页脚与 `/api/meta.version` 同源）
- 范围：面板 v2（前端拆静态资源 + 4 个只读接口 + 设计系统与五页重做）同步到本机与公网，并重新打包
- 结论：两处服务均为 v2.0.0 且验收全绿；本文件记录全部命令、输出摘要、指纹、回滚步骤与遗留事项

---

## 1. 交付物与文件指纹

`sha256sum` 在 **workspace 源目录、本机运行目录（~/项目/fox-panel）、VPS 运行目录（/opt/fox-panel）**
三处逐字节一致（下列指纹在三个位置都核对过；服务端 `/static/app.css`、`/static/app.js` 的响应体
sha256 也与这两行相同，见 §4）。

| 文件 | 行数 | 字节 | sha256 |
|---|---|---|---|
| `panel.py` | 1596 | 68615 | `5f6b2efdc34a419c1233e4e04038ff55b4841ce20b10dacf9b1b508fe4325f75` |
| `web/index.html` | 241 | 10121 | `2688ceb0cf830191d3cdaff42f54ea4a44bf26e1196d30546d5909f22b3d5f18` |
| `web/login.html` | 46 | 1718 | `96a2afea46f4a0a8a401c86a6ace7bc9a6b141c63b4680724ece5c7c379f5e5a` |
| `web/app.css` | 1407 | 41396 | `41a90af5364a938a1196fa66f3cb3bb05ef84f0efd6bde4bb40eab5c4917b9c5` |
| `web/app.js` | 2283 | 81529 | `be7eb48cd74a5a9aeabbba040bbee43816abadafcb59cfd08458518a815bdb12` |
| `tests/test_panel.py` | 354 | 14775 | `50888ecf5db35b64548a3ab35064e3343d462842bcb712a9e36ea4ec447085e8` |
| `tests/test_panel_v2.py` | 1353 | 70087 | `e28e5a3f6593606779286dcb48363a378771563217938323ec98571d9064e241` |

> 前五行就是 t18 验证轮绑定的“当前字节”；本机与 VPS 的落盘 `sha256sum` 与上表完全相同，
> 说明“验证过的字节”就是“部署上去的字节”。

---

## 2. 部署动作（两处）

同步清单只有 5 个文件：`panel.py` + `web/{index.html,login.html,app.css,app.js}`；
配置文件（本机 `~/.config/fox-panel/config.json`、VPS `/root/.config/fox-panel/config.json`）
**未被读取内容、未被覆盖、未被改动**（部署前后 `ls -la` 的 mtime 均保持 2026-11-10 12:04）。

### 2.1 先备份再覆盖（回滚依据）

| 位置 | 备份路径 | 内容 |
|---|---|---|
| 本机 | `~/项目/fox-panel-rollback-v1-20261110/` | v1 的 `panel.py`(cf14a25f…)、`README.md`、`MANIFEST.txt`、`fox-panel.service`、`tests/test_panel.py` |
| 本机 | `~/项目/fox-panel-src-20261110-v1.zip` | 上一版整包（sha256 `a33134d4f44c3a23cdd13f42f2baf042cfda34be2645b26cb1081d5914a15d82`） |
| VPS | `/opt/fox-panel-rollback-v1-20261110/panel.py` | v1 `panel.py`(cf14a25f…，39290 字节) |

### 2.2 本机（systemd --user，端口 31832，上游 127.0.0.1:31831）

```bash
install -m 600 panel.py            ~/项目/fox-panel/panel.py
install -m 644 web/{index.html,login.html,app.css,app.js}  ~/项目/fox-panel/web/
install -m 600 tests/{test_panel.py,test_panel_v2.py}      ~/项目/fox-panel/tests/
systemctl --user restart fox-panel
```

- 重启前：`active`，MainPID 1223（v1.0.0）
- 重启后：`active (running)`，**MainPID 15361**，`ActiveEnterTimestamp = 2026-11-10 22:28:06 CST**
- 启动日志（`journalctl --user -u fox-panel`，逐字）：

```
fox-panel v2.0.0 监听 http://127.0.0.1:31832
  上游 : http://127.0.0.1:31831
  账号 : admin88273
  密钥 : admin.key                      ← 只打文件名，不含路径
  自检 : 上游正常 ✓ {"ok": true, "repos": 2, "packages": 135}
```

运行目录同时补齐了交付文档与校验清单（`README.md`、`SPEC-v2.md`、`DELIVERY-v2.md`、`MANIFEST.txt`，
取自压缩包内的封包版本），因此可在运行目录直接做 `grep -E '^[0-9a-f]{64}  ' MANIFEST.txt | sha256sum -c -`
—— 11 个文件全部「成功」，说明本机运行副本与交付包逐字节一致。

### 2.3 公网 VPS（systemd system 单元，`WorkingDirectory=/opt/fox-panel`，Python 3.13.5）

先用 `scp` 传到 `/tmp/fp-v2/` 暂存并核对指纹，再 `install` 到位（不在半同步状态重启）：

```bash
install -m 644 /tmp/fp-v2/panel.py            /opt/fox-panel/panel.py
install -m 644 /tmp/fp-v2/web/*               /opt/fox-panel/web/      # 新建目录
install -m 600 /tmp/fp-v2/tests/*.py          /opt/fox-panel/tests/
systemctl restart fox-panel
```

- 重启前：`active`（v1.0.0）
- 重启后：`active (running)`，**MainPID 7620**，`ActiveEnterTimestamp = Mon 2026-11-10 14:29:02 UTC`
  （= 2026-11-10 22:29:02 CST）
- 启动日志同样输出 `fox-panel v2.0.0` + `自检 : 上游正常 ✓ {"ok": true, "repos": 2, "packages": 135}`
- caddy 未改动：静态路径仍是 `/static/app.css`、`/static/app.js`，反代规则不变
- 运行目录同样补齐 `README.md`/`SPEC-v2.md`/`DELIVERY-v2.md`/`MANIFEST.txt`/`fox-panel.service`，
  在 `/opt/fox-panel` 里跑 `sha256sum -c` 也是 11 个文件全 OK（systemd 单元仍以 `/etc/systemd/system/fox-panel.service` 为准，
  目录里那份只是同内容副本，便于比对）

---

## 3. 验收清单（逐条命令 + 输出摘要）

一台机器跑两遍验收脚本（同一个脚本，参数是 base URL + 密码文件 + 期望包数；
密码只用于 `POST /login`，**从不打印、不写进任何文件**）：

```bash
python3 accept.py <base> <password-file> 135
```

### 3.1 本机 `http://127.0.0.1:31832` → **17/17 通过**

```
PASS  /login 未登录 200 text/html
PASS  登录 → 302 且拿到会话 cookie
PASS  / 200 且页面含 v2.0.0
PASS  / 的 CSP 含 script-src 'self' 且无 unsafe-inline
PASS  /static/app.css 200 text/css 且非空
PASS  app.css 带 ETag（<mtime_ns>-<size> 形状）
PASS  app.css Cache-Control: no-cache
PASS  If-None-Match 复请求 → 304 且空体
PASS  /static/app.js 200 application/javascript 且非空
PASS  /api/meta 200 且 version=2.0.0
PASS  /api/meta 里 statuses/sorts/page_sizes 齐全
PASS  /api/summary packages=135 且 available=135
PASS  /api/export.csv 200 text/csv + BOM + 与 X-Total-Rows 一致
PASS  /api/export.csv 表头 10 列固定顺序
PASS  非法 sort → 400（不是 v1 的静默回落）
PASS  /api/package 缺 id → 400
PASS  未登录 /api/meta → 401
```

本机 `GET /static/app.css` 响应头（实测）：

```
HTTP/1.1 200 OK
Content-Type: text/css; charset=utf-8
Content-Length: 41396
Cache-Control: no-cache
ETag: "1794320879691466015-41396"
```

`If-None-Match: "1794320879691466015-41396"` 复请求 → `HTTP/1.1 304 Not Modified`，**空体**。

### 3.2 公网（VPS 上对本机口与对公网 HTTPS 各跑一遍）

`python3 accept.py http://127.0.0.1:31832 … 135` → **17/17 通过**
`python3 accept.py https://label.scvxzf.asia … 135` → **17/17 通过**（经 caddy 反代，HTTP/2）

公网 `GET https://label.scvxzf.asia/static/app.css` 响应头（实测）：

```
HTTP/2 200
content-type: text/css; charset=utf-8
content-length: 41396
cache-control: no-cache
etag: "1794320941402409645-41396"
via: 1.1 Caddy
```

`If-None-Match` 复请求 → `HTTP/2 304`，空体。
（ETag 的数字段两处不同是正常的：它取自各机器上文件的 `mtime_ns`；后缀 `-41396` 是字节数，
两处一致，且与 `web/app.css` 的字节数相同。）

### 3.3 从本机（外部视角）看公网

```bash
curl -sS -o /dev/null -w '%{http_code} %{content_type}' https://label.scvxzf.asia/login
# 200 text/html; charset=utf-8
curl -sS https://label.scvxzf.asia/health
# {"ok": true, "repos": 2, "packages": 135}
curl -sS -o /dev/null -w '%{http_code}' https://label.scvxzf.asia/api/meta
# 401            ← 未登录必须是 401 JSON（不是 404：说明 /api/meta 已存在）
curl -sS https://label.scvxzf.asia/static/app.css | sha256sum
# 41a90af5364a938a1196fa66f3cb3bb05ef84f0efd6bde4bb40eab5c4917b9c5   ← == web/app.css
curl -sS https://label.scvxzf.asia/static/app.js  | sha256sum
# be7eb48cd74a5a9aeabbba040bbee43816abadafcb59cfd08458518a815bdb12   ← == web/app.js
```

本机 `http://127.0.0.1:31832/static/app.css|app.js` 的 sha256 同样是上面这两个值。

### 3.4 v1 → v2 翻转对照（本次上线的硬指标）

| 检查 | 部署前（v1.0.0） | 部署后（v2.0.0） |
|---|---|---|
| `/api/meta` | `404 {"error": "not found"}` | **200**，`version=2.0.0` |
| `/api/export.csv` | `404` | **200**，`text/csv; charset=utf-8` + BOM |
| `/static/app.css` | 200，**无 ETag** | 200，`ETag: "…-41396"`，`If-None-Match` → **304** |
| `/` 页脚版本 | v1.0.0 | **v2.0.0** |
| 非法 `?sort=evil` | 200（静默回落） | **400** |

### 3.5 两处复跑测试套件

| 位置 | `tests/test_panel.py` | `tests/test_panel_v2.py` |
|---|---|---|
| 本机（workspace 源目录） | **39/39 通过**，exit 0 | **204/204 通过**，exit 0 |
| 本机（`~/项目/fox-panel` 运行副本） | **39/39 通过**，exit 0 | **203/203 通过**，exit 0 |
| VPS（`/opt/fox-panel`） | **39/39 通过**，exit 0 | **203/203 通过**，exit 0 |

> 204 与 203 的差别只有一条，原因见下：v2 套件 `[12]` 段有个「真实 admin key 泄露」检查分支，
> 它按“面板目录的兄弟目录 `../fox-label/state/admin.key`”去找密钥。本机 workspace 正好是这个布局 → 204；
> 本机运行副本（`~/项目/fox-panel`，兄弟目录是 `~/项目/fox-label`）与 VPS（`/opt/fox-panel`，
> 兄弟目录是 `/opt/fox-label`）都不存在该路径，于是走“跳过”分支 → 203。
> 也就是说 **203 不是失败，是少跑了 1 条同义检查**；为不留空洞，我在两处都用**真实密钥**手工补做了同一项检查
> （不打印密钥内容）：

```
本机运行目录文件泄密检查: 通过（10 个文件均不含真实 admin key）
本机服务端页面泄密检查: 通过（/ 与 /login 均不含真实 admin key）
VPS 文件泄密检查: 通过（8 个文件均不含真实 admin key）
VPS 服务端页面泄密检查: 通过（/ 与 /login 均不含真实 admin key）
VPS 会话密钥泄密检查: 通过（页面不含会话密钥）
```

> 两处密钥位置不同：本机在 `<workspace>/fox-label/state/admin.key`，VPS 在 `/var/lib/fox-label/admin.key`
> （`admin_key_file` 配置项决定，面板只在进程内使用它）。

---

## 4. 打包

- 产物：`/home/scv/项目/fox-panel-src-20261110.zip`
- 目录前缀：`fox-panel/`（解包即得一个自包含目录）
- 内容（11 个文件，无 `__pycache__`、无 config、无截图）：

```
fox-panel/MANIFEST.txt            # 逐文件 sha256（本文件生成于打包时）
fox-panel/README.md
fox-panel/DELIVERY-v2.md          # 本文件（封包前版本，见 §4.2 说明）
fox-panel/SPEC-v2.md
fox-panel/panel.py
fox-panel/fox-panel.service
fox-panel/tests/test_panel.py
fox-panel/tests/test_panel_v2.py
fox-panel/web/index.html
fox-panel/web/login.html
fox-panel/web/app.css
fox-panel/web/app.js
```

### 4.1 校验方式

```bash
cd /tmp && unzip -q /home/scv/项目/fox-panel-src-20261110.zip && cd fox-panel
grep -E '^[0-9a-f]{64}  ' MANIFEST.txt | sha256sum -c -
sha256sum /home/scv/项目/fox-panel-src-20261110.zip     # 与 §4.3 记录比对
```

### 4.2 一处必须说明的自指限制

`DELIVERY-v2.md` 本身在压缩包里，所以**包内副本不含本文件的 zip sha256**（文件无法包含自身所在归档的哈希）。
包内副本与本文件的唯一差异是 §4.3 这一节；`MANIFEST.txt` 里 `DELIVERY-v2.md` 那一行的 sha256 对应的是
**包内副本**，`sha256sum -c` 因此可以严格自校验。

### 4.3 本次实际交付的 zip

> 本节由打包脚本在封包之后补记，记录最终 zip 的哈希。

- 包内副本不记录归档自身的哈希（文件无法自指）；最终 zip 的 sha256 见工作区 `DELIVERY-v2.md`
  与压缩包同目录的 `fox-panel-src-20261110.zip.sha256`（封包后计算）
- 上一版（v1）整包：`fox-panel-src-20261110-v1.zip`，
  `sha256 = a33134d4f44c3a23cdd13f42f2baf042cfda34be2645b26cb1081d5914a15d82`
---

## 5. 回滚方式

两处都保留了 v1 的原件，**回滚不需要重新下载任何东西**；任一处出问题都能在 1 分钟内退回 v1.0.0。

### 5.1 本机

```bash
systemctl --user stop fox-panel
install -m 600 ~/项目/fox-panel-rollback-v1-20261110/panel.py  ~/项目/fox-panel/panel.py
rm -rf ~/项目/fox-panel/web                                     # 关键：v1 没有 web/，留着会下发 v2 前端
cp -p ~/项目/fox-panel-rollback-v1-20261110/{README.md,MANIFEST.txt,fox-panel.service} ~/项目/fox-panel/
cp -p ~/项目/fox-panel-rollback-v1-20261110/tests/test_panel.py ~/项目/fox-panel/tests/
systemctl --user start fox-panel
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:31832/login   # 期望 200
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:31832/api/meta # 期望 404（= 已回到 v1）
```

### 5.2 公网 VPS

```bash
systemctl stop fox-panel
install -m 644 /opt/fox-panel-rollback-v1-20261110/panel.py /opt/fox-panel/panel.py
rm -rf /opt/fox-panel/web /opt/fox-panel/tests
systemctl start fox-panel && systemctl is-active fox-panel
curl -sS https://label.scvxzf.asia/health          # 期望 {"ok": true, …, "packages": 135}
curl -sS -o /dev/null -w '%{http_code}\n' https://label.scvxzf.asia/api/meta   # 期望 401（已回到 v1）
```

### 5.3 从上一版压缩包恢复（备份目录也不可用时的兜底）

```bash
cd /tmp && rm -rf fp-v1 && mkdir fp-v1 && cd fp-v1
unzip -q /home/scv/项目/fox-panel-src-20261110-v1.zip          # 先核对 sha256 = a33134d4…5d82
install -m 600 fox-panel/panel.py ~/项目/fox-panel/panel.py    # 本机
# VPS：scp fox-panel/panel.py fox-srv:/opt/fox-panel/panel.py 后 install -m 644 /opt/fox-panel/panel.py
systemctl --user restart fox-panel                              # VPS: systemctl restart fox-panel
```

> 回滚判据：`/api/meta` 从 200 变 404、`/static/app.css` 的 ETag 消失、页面页脚回到 v1.0.0。
> 注意 v1 没有 `web/` 目录，**必须删掉**，否则 v1 后端会继续下发 v2 前端（页面能开但接口对不上）。

---

## 6. 五条已知取舍（本轮有意如此，不是缺陷）

1. **登录页固定深色**：`web/login.html` 是手写静态页，不读 `localStorage.fp.theme`、不跟随系统主题
   （CSP 禁内联脚本，本轮不给登录页引入 JS）。仪表盘内的主题切换与持久化不受影响。
2. **<768 隐藏“自动刷新”勾选框**：手机宽度只保留标题 + 刷新/主题/帮助/登出图标；
   自动刷新状态仍从 `localStorage` 读取并生效（默认开、15 秒），只是不显示开关。
3. **768–1199 平板档吸顶栈 = 56（页头）+ 44（页签）+ 125（两行工具栏）= 225px**：
   在 800px 高的平板上占 28% 视口。这是 SPEC §2.6「工具栏与表头都吸顶」的直接代价，
   换来的是这档不出现横向滚动（`768–1199` 隐藏“已下载”列代替横向滚动）。
4. **Toast 恢复计时用完整 ttl，而不是剩余时间**：悬停/聚焦打断后重新计时 4.5 秒。
   SPEC 未规定该细节，行为可接受；若将来要求“精确剩余”，改 `app.js` 的 `resume()` 即可。
5. **审计页表头位置依赖 `--fp-toolbar-h` 的几何公式（audit 档 93px）**：该值是按审计工具栏
   “计数文字 + 一行控件”的实际高度拼出来的 calc。**今后审计工具栏增删元素必须同步这个 calc**，
   否则表头会与工具栏错开或被盖住。

### 6.1 追加两条观察（t18 验证轮提出，非阻塞）

6. **卡片内行内包名链接 `.fp-linkbtn` 渲染高 22px**（18 个），低于 SPEC §4 的「<768 可点目标 ≥44」。
   它属于 WCAG 2.5.8 的 inline 文本例外，本轮不修。**P2 候选**：若要严格 44px，需调整卡片行高，
   或由 SPEC 明确豁免行内链接。
7. **审计页几何以 t14 的浏览器 harness 为准**：t18 复测时临时面板未配置 `audit_file`，
   审计页没有表格（`noTable`），因此**本轮没能现场复测审计页的吸顶几何**；静态断言
   （`#page-audit` 的 `--fp-toolbar-h` 公式、`th` 吸顶 calc）已覆盖，t14 自报审计页 4/4。
   结合第 5 条，审计页是唯一“几何靠公式维持”的页面。

---

## 7. 两条下一轮跟进

1. **fox-label 的 `GET /packages/<pkg>` 对“同名包跨仓库”不抛 409，而是落成通用 500**。
   面板已兜底（无 `repo` 时 5xx 会回退全量扫描，把歧义还原成 `400 包名在多个仓库存在，请指定 repo：…`），
   所以功能正确、只是多花一次全量扫描。建议下一轮在 fox-label 侧 `_resolve_repo` 里抛 `Conflict`→409，
   面板侧即可省掉这次回退（`find_package` 的 409 分支已经在位）。
2. **平板档（768–1199）吸顶栈过高，可选两种缓解**：
   ① 该档工具栏改**单行**（仓库/状态/搜索/动作压成一行，`--fp-toolbar-h` 从 125 → 约 67）；
   ② 该档**页签不吸顶**（`--fp-sticky-top` 从 100 → 56）。
   两者都只需改 `app.css` 的 media 段与对应 calc，不动 JS 与后端。

---

## 8. 一条实践约定（来自 t15/t14，务必沿用）

> **className 字符串只能走 `className` 赋值，或按空白拆成 token 后逐个 `classList.add`；
> 绝不把多类名字符串整串喂给 `classList.add`。**

背景：t14 给表头加了 `"fp-col-first fp-align-right"` 这样的多类字符串，而 `buildTable()` 里
既有 `thCell` 用 `className` 写入、又有一处 `classList.add(col.cls)`，后者对含空格的字符串会抛
`DOMTokenList.add` 错误，直接让**整张审计表渲染中断**（页面显示「出错了…token can not contain whitespace」）。
修复方式是 `String(cls).split(/\s+/).forEach(tk => …classList.add(tk))` 并去重；
t18 的 `[12]` 段已把这条钉成断言（`split(/\s+/)` + `classList.contains(tk)` 必须同时存在）。

---

## 9. 相关文档

- 设计规格与接口契约：[SPEC-v2.md](SPEC-v2.md)
- 使用说明（v2）：[README.md](README.md)
- 测试：`tests/test_panel.py`（39 项）、`tests/test_panel_v2.py`（204 项，含字节绑定与前端修复核验）
