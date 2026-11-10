# fox-label

基于 HuggingFace 仓库的打标包分发 / 认领 / 状态跟踪系统，**支持多仓库**。
服务端与客户端都是命令行工具，纯 Python 标准库实现，**无需安装任何第三方依赖**。

```
                        ┌──────────────────────────────┐
  打标用户 A ─┐         │  fox-label 服务端             │
  打标用户 B ─┼─HTTP/SSE┤  (端口 31831, SQLite)        │
  打标用户 C ─┘         │  · 密钥鉴权 + 按仓库授权       │
                        │  · 多仓库包索引 · 状态机       │
                        │  · 认领仲裁 · 审计留痕         │
                        └───────────┬──────────────────┘
                                    │ 只读清单 + 下载
                                    ▼
                    ┌───────────────────────────────────┐
                    │  HF 仓库 1 / 仓库 2 / 仓库 3 …     │
                    │  shard-*.tar + manifest.jsonl     │
                    └───────────────────────────────────┘
   用户下载走自己的 HF 账号直连 CDN，不经过服务端、不消耗服务器带宽。
```

## 数据模型（v2）

```
repos              仓库：repo / repo_type / 包匹配正则 / 公共清单 / 启停
packages           包：主键 (repo, id) —— 同名分片可存在于不同仓库
repo_grants        按仓库授权：为空表示该密钥可用全部仓库
keys               密钥：role(admin|user) / 吊销标记
events + audit     事件流与精简审计
```

> 从 v1（单仓库）升级：服务端首次运行任何命令时会自动迁移（重建 `packages` 主键、
> 回填 `events.repo`），无需手工操作。

## 状态机

```
未认领 available ──claim──> 已认领 claimed ──submit──> 已完成 completed
      ^                          │                          │
      │                          │<──── reopen(管理员) ─────┘
      └──── release(本人/管理员) ─┘
```

- `claim`：任何人可认领**未认领**的包；独占，冲突返回 409
- `submit`：只有认领人本人可提交
- `release`：认领人可自愿放弃；管理员可强制释放
- `reopen`：管理员把**已完成**打回（默认回已认领、保留认领人；`--to available` 退回公共池）
- 旁路字段 `downloaded_at`：记录是否已成功下载（不属于业务状态）

---

## 一、服务端

Python 3.9+，无需 pip 安装。

### 1. 登记仓库

```bash
python server.py repo add \
  --repo MatchaCuratio/Anime-Fox768-400k \
  --repo-type dataset \
  --include '^shard-\d+\.tar$'

# 后续再接入新仓库，同样一条命令
python server.py repo add --repo your-org/another-dataset --repo-type dataset
```

- 需要 HF token（`--token` > `HF_TOKEN` > `~/.cache/huggingface/token`）
- 每个仓库可独立设置包匹配正则（`--include`）与清单名（`--manifest-name`）
- 首个仓库执行时会打印**管理员密钥**并写入 `state/admin.key`（600）
- `manifest.jsonl` 自动登记为公共清单

```bash
python server.py repo list                      # 各仓库包数与状态汇总
python server.py repo sync --repo X [--prune]   # 补录新增文件 / 清理消失的未认领包
python server.py repo disable --repo X          # 停用（保留数据，客户端不可见、不可认领）
python server.py repo enable  --repo X
python server.py repo remove  --repo X [--force]
```

### 2. 启动服务

```bash
python server.py serve --port 31831              # 默认只监听 127.0.0.1
python server.py serve --host 0.0.0.0 --port 31831   # 局域网对外提供服务
```

可调参数（环境变量）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `FOXLABEL_HTTP_TIMEOUT` | 30 | 单次请求 socket 超时（秒，最小 5），防半截请求占死线程 |

请求体上限 64 KiB（超出返回 413），SSE 订阅上限 64 条连接。

### 3. 签发密钥（可限定仓库）

```bash
python server.py enroll --name alice                          # 全部仓库（all_repos=1）
python server.py enroll --name bob --repo org/repo-a          # 仅某个仓库（可重复 --repo）
python server.py keys                                         # 含授权仓库列
python server.py revoke --name alice                          # 立即失效（含 SSE 长连接）
```

授权采用 **fail-closed**：密钥只被授权某些仓库时，一旦这些仓库被 `repo remove`
删掉，该密钥就**没有任何仓库权限**，绝不会因为"授权行为空"而升级成全仓库访问。

密钥生命周期约定：

- 用户被吊销后，`enroll` 同名用户会**复用原行重发密钥**（不会卡在 UNIQUE 冲突上）
- 允许吊销管理员密钥，但**拒绝吊销最后一个有效管理员**
- `admin` / `root` / `system` 为保留名，普通用户不能占用

### 4. 运营命令

```bash
python server.py status                                   # 全部仓库看板（含按用户统计）
python server.py status --repo org/repo-a
python server.py release --package org/repo-a/shard-000003.tar --reason "超期"
python server.py reopen  --package shard-000003.tar --repo org/repo-a --reason "返工"
python server.py export  --out audit.jsonl [--repo X]
```

> 管理命令直接操作本地数据库，须在服务器上执行；服务进程会实时感知并推送 SSE。

### 5. 常驻部署（systemd 用户服务）

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/fox-label.service <<EOF
[Unit]
Description=fox-label package claim server
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$PWD
ExecStart=$(command -v python3) $PWD/server.py --data-dir $PWD/state serve --host 0.0.0.0 --port 31831
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now fox-label.service
```

---

## 二、客户端

依赖 Python 3.9+ 与 `curl`（Win10+ 自带 `curl.exe`）。把 `client.py` 单文件发给用户即可。

### 包引用写法

```bash
shard-000001.tar                          # 唯一时可直接用裸名
org/repo-a/shard-000001.tar               # 多仓库同名时显式指定
shard-000001.tar --repo org/repo-a        # 或这样
```

同名包存在于多个仓库且未指定仓库时，服务端会返回 409 并列出候选仓库。

### 命令

```bash
python client.py login --server http://192.168.5.185:31831 --key flk_xxx
python client.py repos                      # 我能访问的仓库
python client.py list [--repo X] [--status 未认领] [--watch]
python client.py claim org/repo-a/shard-000003.tar
python client.py download --mine --out ./data
python client.py submit org/repo-a/shard-000003.tar --note "已交付"
python client.py release org/repo-a/shard-000003.tar
python client.py my
```

### 下载目录规则

| 场景 | 落盘位置 |
|---|---|
| 只涉及一个仓库 | `./data/shard-000003.tar`、`./data/manifest.jsonl`（平铺） |
| 涉及多个仓库 | `./data/org__repo-a/...`、`./data/org__repo-b/...`（按仓库分子目录） |

避免不同仓库的同名 `manifest.jsonl` / 同名分片互相覆盖。
下载具备：断点续传、停滞检测自动重连、sha256 校验、校验失败自动删文件、已存在且校验通过则跳过，
成功后自动上报「已下载」。

---

## 三、HTTP API（供二次开发）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 只回 `{ok, repos, packages}` 计数（无需鉴权、不做全表装载） |
| GET | `/repos` | 有权访问**且已启用**的仓库 |
| GET | `/packages?repo=X&status=已认领&limit=500&offset=0` | 包列表，带 `page` 分页信息 |
| GET | `/me` | 身份、授权仓库、我的包 |
| GET | `/events?since=<seq>` | SSE 实时事件，**按订阅者仓库授权过滤**，首帧下发 `cursor` 供断点续传 |
| POST | `/packages/{pkg}/{claim\|release\|submit\|downloaded}?repo=X` | 状态变更 |

`/packages` 的 `summary` 用 SQL 聚合（几十万包也是毫秒级）；`packages` 只返回当前页。

鉴权：`Authorization: Bearer flk_xxx`。

---

## 四、模拟功能验证

```bash
python tests/test_flow.py
```

使用本地假 HF 端点（支持 Range），真实子进程跑双端 CLI，覆盖 45 项断言：

- 多仓库登记 / 重复登记拒绝 / `repo list` 包数
- 按仓库授权（全仓库 vs 单仓库）、授权未登记仓库被拒
- 仓库可见性隔离；未授权仓库认领被拒
- **同名包裸引用被拒（歧义）**、显式仓库引用、`--repo` 形式
- 两仓库同名包独立记录 owner
- 跨用户并发认领同一包，恰好一个成功
- **多仓库下载隔离**：分仓库子目录、两份 manifest 互不覆盖、已下载标记分仓库记录
- `repo sync` 增量补录
- 停用后列表不可见且认领被拒，重新启用恢复
- 状态机全路径：提交 / 越权拒绝 / 打回（保留认领人）/ 管理员释放 / 自愿释放
- SSE 事件携带 repo 字段
- 吊销即时 401
- 审计覆盖仓库操作与状态流转、带 repo 标识、精简无请求日志

---

## 五、留痕与日志

| 位置 | 内容 | 粒度 |
|---|---|---|
| `state/audit.jsonl` | 每次变更一行：`时间/动作/仓库/包/操作者/备注` | 精简摘要，永久保留 |
| `state/db.sqlite3` | `events` 表（同上，供 SSE 推送） | 结构化 |
| 服务端 stderr | 仅「变更」与「错误」 | 无 routine 访问日志 |

---

## 六、运维与安全要点

- **`repo sync --prune` 是安全的**：删除是 `... AND status='available'` 的条件删除，
  已认领/已完成的包不会被误删；清单条目数骤降（超过一半）时直接拒绝执行；
  清单一条都没匹配到也会中止，避免把库清空。
- **HF tree 分页**：`fetch_tree` 会跟随 `Link: rel="next"` 翻页，40 万文件的仓库不会只拿到首页。
- **审计文件不可写不会拖垮业务**：`events` 表是权威记录，`audit.jsonl` 写失败只告警。
- **锁与并发**：包索引重建（`apply_tree`）在单个 `BEGIN IMMEDIATE` 事务内完成，
  与认领/提交互斥，不会出现"边同步边认领导致丢包"。
- **面向 20~30 人**：监听 backlog 128，`last_seen` 每分钟最多写一次库，SSE 上限 64 连接。

## 七、已知限制

1. **无自动过期**：包被认领后不会自动回收，需管理员 `release`。
2. **包粒度 = 仓库内的文件**：无法只取 tar 内一部分（未实现成员偏移索引）。
3. **不校验交付物**：`已完成` 仅是状态声明。
4. **单机单点**：SQLite 存本机，未做异地备份。
5. **明文 HTTP**：`serve` 默认只监听 127.0.0.1；对外暴露请显式 `--host 0.0.0.0`
   并置于 TLS 反代或隧道之后，否则管理员密钥等于裸奔。
6. **管理员密钥无二次认证**：管理命令依赖服务器本机文件权限。

---

## 八、与打标客户端（tagger）联动

成员的机器上装的是 tagger（`~/项目/tagger-deploy`），它负责把分片里的图片跑成
`-tag.json` / `-nl.json`。`bridge.py` 把两边的状态机接起来：

```
认领(fox-label) → 下载+sha256 校验 → tagger client 打标 → 产物校验 → 提交完成(fox-label)
```

```bash
# 一条命令走完
python bridge.py run --package org/repo/shard-000003.tar --claim --limit 20 --mode both

# 分片已在本地（省一次 1 GB 下载）
python bridge.py run --package org/repo/shard-000003.tar --claim \
    --local-shard ~/项目/tagger-deploy/testdata/shard-000003.tar --limit 4 --mode tag

# 只看队列 / 演练
python bridge.py next
python bridge.py run --package ... --dry-run      # 只认领+下载校验
python bridge.py run --package ... --no-submit    # 打标但不交
python bridge.py run --package ... --submit-only  # 产物已存在，只走闸门+提交
```

**`--limit` 是试跑口径**：只打标、**不会提交**（否则整包会被标成已完成，之后重跑会被
「已完成跳过」挡住，只能找管理员 reopen）。确认要按部分数据交付时再加 `--allow-partial`，
或用 `--submit-only` 补交已有产物。

常用参数：`--tagger-root`（默认 `~/项目/tagger-deploy`）、`--tagger-python`（默认项目内 venv）、
`--out-dir`（默认 `tagger/work/foxjobs/<repo>__<包名>`）、`--cache-dir`（分片缓存，默认
`~/.cache/fox-label/shards`）、`--limit/--mode/--concurrency/--cuda-visible`（透传 tagger）、
`--tagger-arg`（任意额外参数，可重复）。

### 四条硬不变量

| # | 不变量 | 违反时的行为 |
|---|---|---|
| 1 | 只有**认领人本人**能提交 | 动手前查 owner，非本人退出 3；**提交前再复核一次**（十小时打标期间可能被改派） |
| 2 | 下载分片的 sha256 必须等于服务端登记的哈希 | 不符退出 4，不进入打标——一个分片要烧十几 GPU 小时 |
| 3 | 打标失败 / 零产物 / 缺 tag / 缺 nl / **本次 0 新增** 时绝不提交 | 退出 5，包留「已认领」可重跑；返工必须 `--force` 重打 |
| 4 | `--limit` 试跑不提交 | 退出 0 但状态不变；显式 `--allow-partial` 才交付部分数据 |
| 5 | 重复执行安全 | 分片按哈希跳过、tagger 按图续跑、已完成的包直接跳过（exit 0） |

> 第 3 条的「0 新增」是返工场景的关键：被打回后 tagger 会把历史产物判为已完成而全部跳过，
> 若照旧提交，等于把上一轮被打回的数据原样再交一次。

退出码：`0` 正常/已跳过/未提交（试跑），`2` 用法或服务端错误，`3` 权限/归属，`4` 哈希不符，
`5` 打标失败或产物不达标。

### 与 tagger 的边界

- 本桥**不复制** tagger 的打标逻辑，只调用 `tagger.cli client`；
- 产物目录结构由 tagger 决定。当前版本按分片落 JSONL：
  `<out>/artifacts-<包名>-<进程号>-00000.jsonl`，每行一张图
  `{image_id, tag:{…}, nl:{…}, ts}`；老版本才是逐图 `<out>/tags/<id>-tag.json`。
  桥两种都认，并按 `image_id` 去重统计（`--force` 会新开文件，不去重会重复计数）；
- 分片按 tagger 需要的形式直接给 tar 路径，不预先解包。

### 环境变量

| 变量 | 用途 |
|---|---|
| `HF_ENDPOINT` | 换成镜像站（如 `https://hf-mirror.com`）或测试端点，下载与清单都跟着走 |
| `HF_TOKEN` | 下载 gated 仓库所需（或放在 `~/.cache/huggingface/token`） |
| `FOXLABEL_TAGGER_ROOT` | tagger 项目目录（默认 `~/项目/tagger-deploy`） |

分片缓存按仓库分目录（`~/.cache/fox-label/shards/<org__repo>/<包名>`），
避免不同仓库的同名 `shard-000000.tar` 互相覆盖。

## 九、v3：服务端派工 + 心跳租约（推荐）

v2 是"客户端自己抢"（`claim`），v3 改成**服务端派活**：客户端只说"我是谁、我在干什么"，
服务端决定给谁什么。冲突从设计里消失，设备失联自动回收。

### 服务端

```bash
python3 server.py serve --server-id fox-01            # 起服务（下发 server_id）
python3 server.py devices                             # 看设备：档位/速度/当前派工
python3 server.py leases                              # 看进行中的租约
python3 server.py unassign --device D…                # 踢设备：租约作废、包回池
python3 server.py reset-secret --user-id 张三         # 重签 secret（换机/重装/泄漏）
```

接口：

```
POST /api/v1/handshake  → 首次签发 secret、自动生成 device_id、派 active(+prelock)
POST /api/v1/heartbeat  → 续约 + 报进度 + 领下一片（一次请求三件事）
POST /api/v1/done       → 包转已完成，prelock 自动升 active 并补新 prelock
POST /api/v1/fail       → 包回池（同一设备 1 小时内不会拿回同一个包）
GET  /api/v1/work       → 看板（管理员）
```

规则：每设备 1 active + 最多 1 prelock；预锁只在"空闲 > max(5, 10%)"且设备不是慢机时发；
快设备优先拿大包、慢设备优先拿小包；active TTL 360s / prelock TTL 240s；
进度 6 小时不动告警、24 小时强制收回；提交必须带 `lease_id`（作废后一律拒绝）。

### 客户端 worker.py

```bash
# 首次登录：secret 与 device_id 自动落盘（600）
python3 worker.py login --server http://192.168.5.185:31831 --user-id 张三

python3 worker.py show                     # 身份 + 当前派工
python3 worker.py run                      # 等派工并干活，循环到底
python3 worker.py run --once               # 只处理一个包就退出
python3 worker.py run --cmd "python3 -m tagger.cli client --shard {shard} --out-dir {out}"
                                           # 用占位符把打标命令接进来
python3 worker.py run --no-download        # 只演练协调层（不下载、不干活）
```

`--cmd` 里可用 `{shard} {package} {out} {lease}`；子进程还能读到
`FOXLABEL_PACKAGE / FOXLABEL_LEASE_ID / FOXLABEL_REPO / FOXLABEL_OUT / FOXLABEL_PROGRESS`。
子进程往 `$FOXLABEL_PROGRESS` 写 `{"done": 已处理张数, "total": 总数}`，心跳会把它报给服务端。

退出码：`0` 正常（含池子空），`2` 用法/连不上，`3` 认证失败，`4` 下载或校验失败，
`5` 租约已被收回（产物保留，重跑续传），`6` 任务命令失败（已上报 fail）。

**心跳失败不会停工**：网络抖动只告警，本地继续跑；只有服务端明确回"你的租约失效了"才停手。

### 换机/重装怎么办

`secret` 只在首次握手签发一次，本地丢了就进不来（这是防止别人冒用你的 ID）：

```bash
# 管理员：重签并把新 secret 给本人
python3 server.py reset-secret --user-id 张三
# 本人：带上新 secret 重新登录
python3 worker.py login --server http://… --user-id 张三 --secret <新secret>
```
