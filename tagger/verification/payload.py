"""标注正文的统一完成判定，仅依赖标准库。"""
import json


def payload_done(kind: str, raw: object) -> bool:
    obj = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return False
        try:
            obj = json.loads(text)
        except ValueError:
            return text[0] not in '{["'
    if isinstance(obj, str):
        return bool(obj.strip())
    if not isinstance(obj, dict) or obj.get("ok") is False:
        return False
    value = obj.get("tag") if kind == "tag" else (obj.get("caption") or obj.get("nl"))
    if not isinstance(value, str):
        return False
    if value.strip():
        return True
    prov = obj.get("provenance")
    prov = prov if isinstance(prov, dict) else {}
    backend = obj.get("tag_backend") or prov.get("tag_backend")
    return (kind == "tag" and obj.get("ok") is True
            and (prov.get("mode") == "local-tag"
                 or (bool(backend) and backend != "vlm" and obj.get("tags") == [])))
