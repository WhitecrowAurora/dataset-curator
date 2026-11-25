"""将已确认发色同步到描述；保留引文、其他物体颜色及可撤销的修改。"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

FIELDS = ("caption", "caption_dense", "caption_medium", "caption_short")
_QUOTES = re.compile(r'("[^"\n]*"|“[^”\n]*”|「[^」\n]*」|(?<!\w)\x27[^\x27\n]*\x27(?!\w))')
_COLORS = r"(?:silver(?:y)?|gr[ae]y|white|black|blond(?:e)?|golden|brown|auburn|chestnut|red|orange|pink|purple|violet|blue|navy|cyan|teal|turquoise|aqua|green|yellow|dark|light|rainbow|multicolou?red|two[- ]tone)"
_COLOR = rf"(?:(?:very|pale|light|dark|bright|deep|lightly)\s+)?{_COLORS}(?:(?:[- /]+|\s+and\s+){_COLORS})*"
_STYLE = r"(?:long|short|straight|curly|wavy|flowing|silky|shiny|tousled|messy|spiky|braided|tied|shoulder-length|waist-length)"
_ACCESSORY = r"(?![- ]+(?:clips?|ribbons?|ornaments?|ties|tie|accessor\w*|bands?|bows?|pins?)\b)"
_BEFORE = re.compile(rf"\b(?P<color>{_COLOR})(?P<tail>(?:[\s,-]+{_STYLE}){{0,5}}[\s-]+(?:hair(?:ed)?{_ACCESSORY}|locks|tresses|bangs|ponytails?|braids?))\b", re.I)
_AFTER = re.compile(rf"(?P<head>\bhair\s+(?:(?:is|looks|appears|seems|dyed|colou?red)\s+|has\s+(?:a\s+)?(?:shade|colou?r)\s+of\s+))(?P<color>{_COLOR})\b", re.I)
_ZH = re.compile(r"(?P<color>(?:浅|深|亮|淡)?(?:银灰|灰白|蓝紫|粉紫|黑|白|银|灰|金|棕|褐|红|橙|粉|紫|蓝|青|绿|黄)(?:色)?)(?P<tail>(?:的)?(?:长|短|卷|直|柔顺|蓬松|飘逸|齐肩|及腰|双马尾|马尾){0,3}(?:头发|秀发|长发|短发|发丝|发辫|卷发|直发|发))(?!夹|饰|带|绳|箍|卡)")
_ZH_NAMES = {"aqua": "青", "blue": "蓝", "green": "绿", "brown": "棕", "orange": "橙", "pink": "粉", "purple": "紫", "red": "红", "black": "黑", "white": "白", "silver": "银", "grey": "灰", "blonde": "金"}
_HAIR = re.compile(rf"\bhair(?:ed)?\b{_ACCESSORY}|\b(?:locks|tresses|bangs|pony[- ]?tails?|braids?|mane|hairdo|coiffure|brunette|blond(?:e)?|redhead(?:ed)?)\b|头发|秀发|长发|短发|发丝|发辫|卷发|直发|[银灰金棕褐红橙粉蓝青绿紫黑白黄]发(?!夹|饰|带|绳|箍|卡)", re.I)
_INDIRECT = re.compile(r"\b(?:hue|tint|streak|highlight|gradient|ombre|fade|shimmer|shine|glow|reflect|tinge)\w*\b|渐变|挑染|泛着|泛出|透出|色泽|光泽|染成", re.I)
_NEGATED = re.compile(r"\b(?:no|not|without|bald|hairless)\b|\b\w+n['’]t\b|没有|无发|并非|不是|并不", re.I)


def _role_fact(record, chinese):
    roles = [r for r in record.get("confirmed_roles", []) if r.get("crop")]
    if len(roles) < 2:
        return ""
    centers = [[(r["crop"][i] + r["crop"][i + 2]) / 2 for i in (0, 1)] for r in roles]
    spreads = [max(c[i] for c in centers) - min(c[i] for c in centers) for i in (0, 1)]
    axis = int(spreads[1] > spreads[0])
    roles.sort(key=lambda r: r["crop"][axis] + r["crop"][axis + 2])
    # 裁框沿该方向重叠时，不臆造明确的左右或上下角色对应。
    ordered = all(a["crop"][axis + 2] <= b["crop"][axis] for a, b in zip(roles, roles[1:]))
    descriptions = [("无可见头发" if not r["selected"] else "、".join(_ZH_NAMES.get(c, c) + "色头发" for c in r["selected"]))
                    if chinese else ("no visible hair" if not r["selected"] else " and ".join(r["selected"]) + " hair") for r in roles]
    if chinese:
        prefix = ("从上到下" if axis else "从左到右") + "，人物的头发依次为：" if ordered else "不同人物的发色情况包括："
        return prefix + "；".join(descriptions) + "。"
    prefix = ("From top to bottom" if axis else "From left to right") + ", the people have: " if ordered else "The people include those with: "
    return prefix + "; ".join(descriptions) + "."


def _sync_part(part, english_color, zh_color, remove):
    kept = []
    for sentence in re.split(r"(?<=[。！？；;])|(?<=[.!?])(?=\s|$)", part):
        if not _HAIR.search(sentence):
            kept.append(sentence)
            continue
        # 无发、多人或间接色彩修辞只保留确认事实，避免残留旧色和错误角色对应。
        if remove or _INDIRECT.search(sentence) or _NEGATED.search(sentence):
            continue
        rest = _ZH.sub("", _AFTER.sub("", _BEFORE.sub("", sentence)))
        if _HAIR.search(rest):
            continue
        sentence = _BEFORE.sub(lambda m: english_color + m["tail"], sentence)
        sentence = _AFTER.sub(lambda m: m["head"] + english_color, sentence)
        kept.append(_ZH.sub(lambda m: zh_color + m["tail"], sentence))
    return "".join(kept)


def _undo(original, rendered, current):
    if current == rendered:
        return original
    # 只反转仍在当前正文中的同步片段，保留同步后的人工作文修改。
    blocks = SequenceMatcher(None, rendered, current, autojunk=False).get_matching_blocks()
    edits = []
    for op, a, b, c, d in SequenceMatcher(None, original, rendered, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        for match in blocks:
            if match.a <= c and d <= match.a + match.size:
                start = match.b + c - match.a
                edits.append((start, start + d - c, original[a:b]))
                break
    for start, end, value in reversed(edits):
        current = current[:start] + value + current[end:]
    return current


def rewrite(text, record, *, fact_only=False):
    colors = record.get("selected", [])
    no_hair = bool(record.get("no_hair") or record.get("status") == "skipped" and not colors)
    multiple = record.get("multiple_people") or len(colors) != 1
    chinese = len(re.findall(r"[\u4e00-\u9fff]", _QUOTES.sub("", text))) > len(text) * .15
    english_color = " and ".join(colors)
    zh_color = "、".join(_ZH_NAMES.get(color, color) + "色" for color in colors)
    parts = _QUOTES.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = _sync_part(parts[index], english_color, zh_color, no_hair or multiple)
    result = "".join(parts).strip()
    if no_hair:
        fact = "画面中没有可见的人类头发。" if chinese else "No human hair is visible."
    elif multiple:
        fact = (f"画面中可见的发色包括{zh_color}。" if chinese else
                f"The visible hair colors across the people are {english_color}." if record.get("multiple_people") else
                f"The person's hair has {english_color} colors.")
        fact = _role_fact(record, chinese) or fact
    else:
        fact = f"可见头发为{zh_color}。" if chinese else f"The visible hair is {english_color}."
    if fact_only:
        return fact
    if result and result[-1] not in ".!?。！？\"”」":
        result += "。" if chinese else "."
    return fact + ("" if chinese or not result else " ") + result


def patch_caption(payload, record, tags=()):
    if not isinstance(payload, dict):
        return payload
    info = dict(payload.get("hair_review") or {})
    originals, rendered = dict(info.get("original_captions") or {}), dict(info.get("synced_captions") or {})
    settled = record["status"] in ("accepted", "decided", "skipped") and bool(record.get("selected") or record.get("no_hair") or record["status"] == "skipped")
    for field in FIELDS:
        current = payload.get(field)
        if not isinstance(current, str) or not current.strip():
            continue
        base = _undo(originals[field], rendered[field], current) if field in originals and field in rendered else current
        originals[field] = base
        payload[field] = rewrite(base, record) if settled else base
        if settled:
            from ..text_encoding.tokenizer import TrainingText
            counter = TrainingText()
            # 确认事实位于首句；预算太小时明确报错，不能裁成失去颜色的半句话。
            prefix = rewrite(base, record, fact_only=True)
            payload[field] = counter.fit(payload[field], tags, required_prefix=prefix)
            if field == "caption":
                payload["training_text"] = counter.metadata(payload[field], tags)
        elif field == "caption" and "training_text" in payload:
            from ..text_encoding.tokenizer import TrainingText
            payload["training_text"] = TrainingText().metadata(payload[field], tags)
        rendered[field] = payload[field]
    if originals:
        info.update(id=record["id"], status=record["status"], selected=record["selected"],
                    original_captions=originals, synced_captions=rendered)
        payload["hair_review"] = info
        if "caption" in payload:
            payload["chars"] = len(payload["caption"])
    return payload
