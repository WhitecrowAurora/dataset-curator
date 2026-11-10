# danbooru_artists.parquet

从 `MatchaCuratio/NewBie-Training-XML-Caption@main/train_dataest*2/` 抽取的
**danbooru post id → 画师** 对照表。给打标器当 `@画师` 的来源。

## 规模

| 项 | 值 |
|---|---|
| 行数（唯一 id） | 9,465,535 |
| 其中有画师 | 9,226,534（97.5%） |
| 文件大小 | 69.7 MB（zstd parquet） |
| 生成耗时 | 约 71 分钟（含一次断点续传） |
| 与第二批数据抽样比对 | 8000 条里 7619 条能查到画师 = **95.2%** |

## schema

```
id      int64                 danbooru post id（与 tar 成员名 / manifest.image_id 同源）
artists list<string>          该帖的 artist 类 tag，多个时按出现顺序；空列表 = 原数据未标注画师
sources list<string>          这个 id 在哪些源文件里出现过：danbooru2024 / dan_2025_add / gelbooru
```

文件级 metadata（`pq.read_schema(path).metadata`）：`builder` / `built_at` / `repo` / `excluded`。

## 口径

- **e621 排除**；gelbooru（`loli_*`）保留，用 `sources` 区分。
- 画师只取旧标注里 `<artists>` 元素的内容——**不是 tag 串的第一个**（Danbooru 的
  `tag_string` 是字母序，画师在 `tag_string_artist` 里）。
- 同一 id 在多个源出现时：画师取并集，源取并集。
- `artists` 为空的行**故意保留**：用来区分"这条没有画师标注"和"表里根本没有这条"。

## 查询示例

```python
import pyarrow.parquet as pq

t = pq.read_table("danbooru_artists.parquet", columns=["id", "artists"])
m = dict(zip(t.column("id").to_pylist(), t.column("artists").to_pylist()))
m.get(8349000)   # ['ban_syou']
```

按分片使用时建议只读 `id` + `artists` 两列（parquet 列存，不付 `sources` 的 IO）。

## 重新生成

`work/build_artist_table2.py`（本仓库）。第二趟带 Range 断点续传：源文件 26 GB
在直连时会中途 reset，v1 因此白跑过一次。
