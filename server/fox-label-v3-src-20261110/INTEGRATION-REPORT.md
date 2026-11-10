# fox-label ⇄ tagger 联动开发审计验收报告

- 执行时间：2026-11-10 01:20 → 01:31（两轮，自动推进）
- 联动对象：`fox-label`（本机 31831，2 仓库 / 106 包 / 178.3 GiB）⇄ `tagger-deploy`（队友交付，本机已验证 752 项测试）
- 交付物：`bridge.py`（联动桥，19 KB）、`tests/test_bridge.py`（34 项验收，17 KB）
- 结论：**两轮通过**。第 1 轮发现 1 个契约错配 + 1 个产物格式误判；第 2 轮完成真机端到端（真分片 / 真 GPU / 真服务端），自查出 2 个规模化隐患；另有一次只读独立审计抓到 2 个 blocker（试跑误提交、返工复用旧产物）。全部已修复并在真机上复验。

---

## 一、两边的接口契约（联动的事实基础）

| 方向 | 内容 | 形式 |
|---|---|---|
| fox-label → 成员 | 谁能干哪个分片 | `GET /packages`、`POST /packages/{pkg}/claim` |
| fox-label → 成员 | 分片元数据 | `repo / id / size / sha256(=HF lfs.oid) / status / owner / downloaded_at` |
| 成员 → 本地 | 分片文件 | HF `resolve/main` 直链（可断点续传） |
| 成员 → tagger | 输入 | `tagger.cli client --shard <tar> --out-dir <dir> [--limit] [--mode]` |
| tagger → 成员 | 产物 | `artifacts-<pkg>-<hash>-00000.jsonl`，每行 `{image_id, tag{…}, nl{…}, ts}` |
| 成员 → fox-label | 完成回执 | `POST /packages/{pkg}/submit`（note 带产出统计） |

联动桥只做三件事：**调用两边既有接口、校验接口之间的不变量、失败时不误提交**。不复制 tagger 的打标逻辑，也不改 fox-label 的服务端语义（除新增一个单包查询端点，见 3.3）。

---

## 二、第 1 轮：开发 + 审计

### 2.1 实现

```
认领(/claim) → 下载 + sha256 对齐 → tagger client 打标 → 产物校验 → 提交(/submit) → 回读确认
```

四条硬不变量与退出码：

| 不变量 | 违反时 |
|---|---|
| 只有认领人本人能提交 | 动手前查 owner，非本人 exit 3 |
| 下载分片 sha256 必须等于服务端登记值 | exit 4，**不进入打标**（一片要烧十几 GPU 小时） |
| 打标失败 / 零产物 / 无 tag / both 模式无 nl | exit 5，**不提交**，包留「已认领」可重跑 |
| 重复执行安全 | 分片按哈希跳过、tagger 按图续跑、已完成直接 exit 0 |

### 2.2 假打标器验收（24 项，全通过）

本地假 HF（支持 Range）+ 假 tagger，覆盖：happy path、哈希不符拦截、打标失败不提交、零产物不提交、`mode=both` 缺 nl 不提交、别人的包不能抢、未认领不能直接干、已完成跳过、`--local-shard` 跳网、审计留痕。

> 这些用假实现跑得快，但**测不出与真实 tagger 的契约错配**——那要靠第 2 轮，见下。

### 2.3 真机首跑暴露的契约错配（已修）

第一次用真数据跑 `--mode tag`，tagger 直接退出码 2：

```
[配置错误] --mode tag 不能配 CPU tag 后端：tag 由后端产出，VLM 就没有活干了。
          用 --mode nl 或 both，或者 --tag-backend none 让 VLM 出 tag。
```

- **根因**：`--mode tag` 与默认 `tag backend = pixai` 互斥；生产口径应是 `--mode both`（需先 `tagger serve`）。
- **修复**：桥新增 `--tag-backend` 与 `--url` 透传；退出码 2 时直接给出可执行提示，而不是让成员去翻文档。

### 2.4 产物格式误判（已修）

首跑成功后，桥报告「产物 0 个」并拒绝提交。实际 tagger 写的是**按分片落的 JSONL**：

```
artifacts-shard-000029-38c4deff…-00000.jsonl    2 行
每行：{"image_id": "jndfh-342401", "tag": {...2207 字符…}, "nl": {...1473 字符…}, "ts": …}
```

而部署报告里写的「逐图 `-tag.json` / `-nl.json`」是旧布局/Web 路径。桥的计数逻辑与 README 都已按真实契约改写：**同时认 JSONL（新）与逐图 JSON（旧）**，并按 `tag`/`nl` 字段分别统计。

---

## 三、第 2 轮：真实验收 + 审计

### 3.1 真机端到端（真分片 / 真 GPU / 真服务端）

分片：`MatchaCuratio/Anime-Fox768-400k/shard-000029.tar`，1,027,860,480 字节。

| 步骤 | 结果 |
|---|---|
| 本地分片 vs 服务端登记 sha256 | `4e26c98be568d3b4…` **完全一致**（980.2 MiB） |
| 认领 | `01:24:03 claim trial` |
| 打标（V100，`--mode both --limit 2`） | 完成 2 / 失败 0，4.46 s/张，墙钟 17.1 s |
| 产物 | 2 张图 / tag 2 / nl 2（1 个 JSONL 文件） |
| 提交 | `01:27:58 submit … tagger both / 2 张 / tag 2 / nl 2 / limit 2` |
| 回读确认 | 服务端状态 `已完成`，owner `trial` |

### 3.2 断点续跑与打回返工（都通过）

- 重跑同一命令：tagger 报告「跳过（已有）2」，桥仍正确统计 2 张并直接提交——**续跑不会重算**。
- 管理员 `reopen`（已完成 → 已认领，保留认领人）→ 成员重跑 → 再次 `submit` 成功，审计链完整：

```
01:29:39 reopen   -> 已认领; 验收：返工重跑
01:29:42 downloaded
01:29:54 submit  tagger both / 2 张 / tag 2 / nl 2 / limit 2
```

### 3.3 第 2 轮自查出的两个规模化问题（已修）

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | 桥用 `/packages?repo=X&limit=5000` 翻页找包 | 40 万包的仓库里目标包可能在第 200 页外 → **误报「找不到包」，成员完全没法开工** | 服务端新增 `GET /packages/{pkg}?repo=X` 单包端点（JOIN 取 `repo_type`，直接主键查询），桥改用它 |
| 2 | `--local-shard` 每次都整份拷贝到缓存 | 每跑一次多写 1 GB，SSD 寿命与耗时都白费 | 缓存里已有同名分片且 sha256 与大小都对上时跳过拷贝（实测复跑输出「跳过拷贝」） |

另外补了**提交后回读确认**：`submit` 之后再查一次单包状态，避免「本地以为交了、服务端没落库」。

### 3.4 回归

| 套件 | 结果 |
|---|---|
| `tests/test_bridge.py`（联动） | **34/34 通过**（含试跑闸门、返工、退出码、argv 断言） |
| `tests/test_flow.py`（服务端+客户端） | **73/73 通过**（服务端新增端点未破坏既有语义） |

### 3.5 独立审计（第三方，只读）发现的两个 blocker

在真机验收之外，另做了一次只读审计。它抓到两个我自己没想到、而且**被原测试焊成绿灯**的问题：

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| B1 | `--limit` 试跑后照样 `submit` | 一个分片 6000+ 张，只打 2 张也标「已完成」；之后重跑被「已完成跳过」挡住，只能找管理员 reopen。**覆盖率可低到 0.01%** | `--limit` 默认**不提交**（exit 0 + 明确提示）；确需部分交付加 `--allow-partial` |
| B2 | 返工/重跑把历史产物当本次产出 | 管理员打回后 tagger 逐图跳过、0 新行 → 桥数到旧行 → 立即「提交」同一批被打回的数据 | 打标前后各数一次，**要求新增 > 0**；为 0 时拒绝并提示 `--force` 重打 |

附带修复的 high/medium：

- **退出码失真**（README 承诺 0/2/3/4/5，实际只有 0/1）→ 统一 `die(code, msg)`，测试按码断言
- `--mode nl` 永远无法提交（`need_tag` 无条件要求 tag）→ 按实际 mode 分别判定
- 提交前不复核 owner/status（十小时窗口内可能被改派）→ 提交前再查一次，变了就退出 3 并保留产物
- `--force-download` + `--local-shard` 会把坏文件拷回去死循环 / 删掉唯一缓存 → 本地源本身坏则直接报错
- `--tagger-arg` 可覆盖 `--out-dir` 等关键参数、可把 `--api-key` 写进日志 → 保留字拒绝 + 日志脱敏
- 无 sha256 时仍打印「已对齐」→ 改为明确告警
- 缓存按 basename 展平 → 改为按仓库分目录（跨仓库同名分片不再互相覆盖）
- 产物统计按行不去重（`--force` 重开文件会重复计数）→ 改为按 `image_id` 去重

### 3.6 收尾

- 分片已归还公共池：`release → 30/30 未认领`，仓库汇总 `未认领 106`，服务 `/health` 正常。
- NL 服务已关闭，两张 V100 显存回落到 14 MiB。
- 验收产物留在 `tagger/work/foxjobs/MatchaCuratio__Anime-Fox768-400k__shard-000029.tar/`（2 条 JSONL + `.foxlink.json` 运行状态）。

---

## 四、审计发现汇总

| 级别 | 发现 | 状态 |
|---|---|---|
| **blocker** | `--limit` 试跑照样提交，整包被误标「已完成」 | 已修（默认不提交 + `--allow-partial`），真机复验 |
| **blocker** | 返工重跑把历史产物当新产出，提交被打回的数据 | 已修（新增 >0 闸门 + `--force` 重打），真机复验 |
| high | `--mode tag` 与默认 tag 后端互斥，首次真机运行直接失败 | 已修（透传 `--tag-backend` + 提示） |
| high | 产物是分片 JSONL，桥按逐图 JSON 计数 → 成功跑完却拒绝提交 | 已修（两种布局都认） |
| high | 大仓库下按分页找包会误报「找不到包」 | 已修（服务端单包端点） |
| medium | 每次运行重复拷贝 1 GB 分片 | 已修（缓存命中跳过） |
| medium | 提交后无回读，失败可能被当成成功 | 已修 |
| low | 重复登记「已下载」事件（审计噪声） | 已修（已记录则跳过） |
| low | 部署报告里的产物命名（`-tag.json`）与当前版本（JSONL）不一致 | 已在 README 澄清 |

## 五、遗留风险与建议

1. **产物上传未联动**：目前 `submit` 只做状态流转 + 统计，产物（`artifacts-*.jsonl`）仍靠成员把目录交回总机。要闭环需加对象存储或 fox-label 侧收件端点（建议下一轮）。
2. **owner 竞态窗口**：认领后、提交前若管理员 `release`/改派，提交会 409。当前行为是明确报错（不会误记完成），但白干；高频协调场景建议加「认领租约」。
3. **打标时长 ≠ 包大小**：一片 981 MB 约 6253 张、按 4.46 s/张 ≈ 7.7 小时。桥目前不估算 ETA，建议按 `--limit` 探针先跑一批再估整片。
4. **NL 服务依赖**：`--mode both` 需要先起 `tagger serve`；桥只透传 `--url`，不负责起服务（避免替成员管 GPU）。
5. **模型 revision 未 pin**（队友报告已提示）：正式产数据前应锁定 commit hash，否则同一包不同时间产出的标签不是一代。

## 六、复现命令

```bash
# 联动桥
cd fox-label
python bridge.py next
# 试跑（打标但不提交）
python bridge.py run --package MatchaCuratio/Anime-Fox768-400k/shard-000029.tar \
    --claim --local-shard ~/项目/tagger-deploy/testdata/shard-000029.tar \
    --limit 2 --mode both --cuda-visible 1
# 确认要按部分数据交付，再补交
python bridge.py run --package ... --out-dir <上面的产物目录> \
    --limit 2 --allow-partial --submit-only

# NL 服务（both 模式需要）
cd ~/项目/tagger-deploy
CUDA_VISIBLE_DEVICES=1 env/linux/bin/python -m tagger.cli serve --engine llamacpp \
    --port 8000 --tiers configs/tiers-v100-16g.yaml

# 两套回归
python tests/test_bridge.py     # 24 项
python tests/test_flow.py       # 73 项
```
