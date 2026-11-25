"""随分项保存的标注选项；领取时冻结到租约，不改设备执行环境。"""
import json

FIELDS = {"nl": "artist_inject", "tag": "artist_inject_tags"}


def options(value):
    if not isinstance(value, dict) or set(value) - set(FIELDS.values()):
        raise ValueError("标注选项只支持 NL / TAG 画师注入")
    if any(type(v) is not bool for v in value.values()):
        raise ValueError("画师注入选项必须为布尔值")
    return dict(value)


def ensure_schema(c, dispatch):
    from annotation.store import ensure_schema
    ensure_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS package_annotation (repo TEXT NOT NULL,pkg TEXT NOT NULL,"
              "part TEXT NOT NULL,source_sha256 TEXT,options TEXT NOT NULL,PRIMARY KEY(repo,pkg,part))")
    dispatch._add_columns(c, "leases", (("annotation_json", "TEXT NOT NULL DEFAULT '{}'"),))


def save(c, package, part, value):
    field = FIELDS[part]
    if field not in value:
        c.execute("DELETE FROM package_annotation WHERE repo=? AND pkg=? AND part=?",
                  (package["repo"], package["id"], part))
        return
    c.execute("INSERT INTO package_annotation VALUES(?,?,?,?,?) ON CONFLICT(repo,pkg,part) DO UPDATE SET "
              "source_sha256=excluded.source_sha256,options=excluded.options",
              (package["repo"], package["id"], part, package.get("sha256"), json.dumps({field: value[field]})))


def for_package(c, package, parts):
    from annotation.store import options as defaults
    result = defaults(c, package['repo'], parts)
    for row in c.execute("SELECT part,source_sha256,options FROM package_annotation WHERE repo=? AND pkg=?",
                         (package["repo"], package["id"])):
        if row["part"] in parts and row["source_sha256"] == package.get("sha256"):
            result.update(options(json.loads(row["options"])))
    return result
