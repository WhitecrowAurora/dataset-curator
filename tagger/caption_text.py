"""caption 里引文的去留策略。

实测结论（Qwen3-VL 8B Q6_K）：文字不清晰时，模型**不会**标注 `text partially
illegible`，也**判断不了自己是否真的看清**——它会用语言先验把残缺的字补全
（"欢迎光临本店" 被撕掉一角，照样完整抄出）。因此引文是否可信，不能靠模型
自省，只能在 caption 出来之后按策略过滤。

三种策略：

- `keep`         原样保留（默认，与旧行为一致）
- `strip`        剥掉所有引文及其引导短语，只留画面描述
- `conservative` 逐条判断：可信的引文原地保留，不可信的连同引导短语剥掉。
                 信任判据见 `_looks_reliable`——短词、大写标识符算可信；
                 长句、小写散文、多行条款、孤立韩文算不可信，这些正是
                 手写体和小字上错得最多的地方。

为什么剥离要删引导短语：只删引号会留下 "the sign reads" 这种断句，样本
直接变废。所以剥引文时把 `reads`/`写着` 这类引导一起削掉；但并列列举
（`the first line is "A", the second is "B"`）里被剥的那条不啃引导语，
免得把旁边可信的那条也毁掉。
"""

from __future__ import annotations

import re

# 中英文引号都认：模型两种都会用。不跨行——多行条款会被当成一条超长引文。
_QUOTED = re.compile(r'["“]([^"”\n]{1,120})["”]')

# 引文前面那串"谁在读它"的引导语。剥引文时一并削掉，避免断句。
_LEAD_IN = re.compile(
    r"(?:"
    r"\b(?:the\s+)?(?:text|words?|sign|label|lettering|characters?|caption|"
    r"title|heading|banner|inscription|line)\b[^.;,]{0,60}?"
    r"\b(?:reads?|says?|shows?|displays?|spells?\s+out|is\s+written\s+as|"
    r"is\s+printed\s+as|is\s+labell?ed|labell?ed|marked|titled|reading)\b"
    r"|(?:上面|上|中)?(?:写|印|标注|标着|显示)(?:着|有)?(?:文字|的字|字样)?"
    r")"
    r"[\s:：,]*$",
    re.I,
)

# 引文后面跟着的解释性从句，例如 `, partially obscured by a sticker`。
_TRAILING = re.compile(r"^\s*[,;]\s*(?:in\s+)?[a-z][^.;]{0,90}", re.I)


def _looks_reliable(text: str, max_len: int) -> bool:
    """conservative 策略的判据：这条引文值不值得留。

    判据来自实测：失分最重的是"长句 + 小写散文"（手写便签、多行条款）和
    孤立小语种（韩文那位整词编造）。反过来，全大写标识几乎不出错。

    ★大写标识放在长度检查之前：`KEEP OUT`(8) / `WELCOME`(7) 都是可靠的，
      但它们比默认 max_len 长，先判长度会把它们误杀。
    """
    s = text.strip()
    if not s or "\n" in s:
        return False

    # 1. 全大写字母数字标识：KEEP OUT / WELCOME / NO SMOKING。强信号，先放行。
    if re.fullmatch(r"[A-Z0-9][A-Z0-9 .'&/!?:-]{0,40}", s):
        return True

    # 2. 拉丁词组（首字母大写或全小写单词），短且不含句读：Sample Text 之类。
    if re.fullmatch(r"[A-Za-z][A-Za-z' -]{0,24}", s) and len(s) <= max_len + 6:
        return True

    # 3. 孤立韩文一律不留。实测 안녕하세요→한글 这种整词编造正好落在
    #    "两个字"的短词区间里，长度判据挡不住，只能按字符集挡。
    if re.search(r"[\uac00-\ud7af]", s):
        return False

    # 4. CJK 短词：中文招牌、日文标牌。要求是单个词，不是一句话。
    if len(s) <= max_len and not re.search(r"[\s,.!?;:]", s):
        return True

    return False


def _strip_span(sentence: str, start: int, end: int) -> str:
    """删掉 `sentence[start:end]`，顺带处理它前后的引导语/尾随从句。

    引导语只在它**紧邻被删引文**时才削：并列列举里 `the first line is "A",`
    的引导语离得太远，削了会把结构毁掉。
    """
    head = sentence[:start]
    tail = sentence[end:]

    lead = _LEAD_IN.search(head)
    if lead:
        head = head[: lead.start()]

    trap = _TRAILING.match(tail)
    if trap:
        tail = tail[trap.end():]

    out = (head.rstrip(" ,;:") + " " + tail.lstrip(" ,;:")).strip()
    return re.sub(r"\s{2,}", " ", out)


def _strip_quotes(sentence: str, drop: set[str] | None = None) -> str:
    """剥引文。`drop` 为 None 时剥全部，否则只剥其中的。"""
    while True:
        # 每轮重新扫：剥掉一条之后位置全变，按 span 一次性处理会错位。
        matches = list(_QUOTED.finditer(sentence))
        if not matches:
            break

        target = None
        for m in matches:
            if drop is None or m.group(1) in drop:
                target = m
                break
        if target is None:
            break

        new = _strip_span(sentence, target.start(), target.end())
        if new == sentence:
            # 兜底：剥不动就强行删掉这段，避免死循环。
            new = (sentence[: target.start()] + sentence[target.end():]).strip()
        sentence = new

    return sentence


def _drop_orphan_quotes(text: str, keep: set[str]) -> str:
    """清掉剥离后残留的、不属于 `keep` 的引号。

    一次剥多条时，被删的引文没了、两侧的引号却各自残留；剩下的引号会
    重新配对，围出一大片原本不属于同一处文字的正文（实测：5 条条款剥掉
    4 条后，残留引号把第 2~5 条正文连成了一条假引文）。

    判据按语义而非计数：引号里的内容不在 `keep` 里，就是残留，只去掉引号
    本身，里面的文字留着（它本来是 caption 正常描述的一部分）。

    ★必须先按内容清一遍再查奇偶：只剩一个落单引号时，它前面已经没有配对
      的引号可依，正则 `"..."` 根本匹配不上，只能靠计数补掉。
    """
    def repl(m: re.Match) -> str:
        return m.group(0) if m.group(1).strip() in keep else m.group(1)

    out = re.sub(r'["“]([^"“”\n]{1,400})["”]', repl, text)

    # 落单的引号（奇偶不成对）直接去掉。
    marks = list(re.finditer(r'["“”]', out))
    while marks and len(marks) % 2 == 1:
        last = marks[-1]
        out = out[: last.start()] + out[last.end():]
        out = re.sub(r"\s{2,}", " ", out)
        marks = list(re.finditer(r'["“”]', out))
    return out.strip()


def apply_policy(caption: str, policy: str, *, max_len: int = 6) -> str:
    """按策略处理 caption 里的引文。

    - `keep`：不碰
    - `strip`：剥掉全部引文
    - `conservative`：只剥不可信的，可信的原样保留

    剥完如果句子只剩标点就整句删；整段空了原样返回——空 caption 会被上游
    判废，那是正确的信号，不该在这里悄悄塞一句假话。
    """
    if not caption or policy in ("", "keep"):
        return caption
    if policy not in ("strip", "conservative"):
        raise ValueError(f"未知的引文策略: {policy}")

    # ★整段处理，不按句切。切句会在引文没闭合的地方切一刀，跨句的引号
    #   就成了孤儿；实测条款那批正是这样漏出一个收尾引号的。
    found = _QUOTED.findall(caption)

    if policy == "strip":
        out = _drop_orphan_quotes(_strip_quotes(caption), set())
    else:
        bad = {q for q in found if not _looks_reliable(q, max_len)}
        good = set(found) - bad
        out = _strip_quotes(caption, bad) if bad else caption
        out = _drop_orphan_quotes(out, good)

    # 收拾剥完留下的碎渣：空括号、连续空句、句首多余逗号。
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,;:])\s*(?=[.;!?])", "", out)
    out = re.sub(r"\s{2,}", " ", out)

    # 只剩标点的句子删掉（剥光引文后常见的残句）。
    parts = [p for p in re.split(r"(?<=[.!?])\s+", out)
             if p.strip(" .,;:!?-\"'“”")]
    return " ".join(parts).strip()


def dedupe_quotes(caption: str) -> str:
    """同一段文字只保留第一次引用，后面的重复引用就地去掉引号。

    为什么在代码里做而不写进 prompt：实测给中文规则加"同一段文字只引用
    一次"之后，模型反而抄得更多（引文重复 7→27 条，长度 290→423）——
    规则里一提到"重复引用"这四个字，它就把文字当成要展开的重点了。
    去重是确定性的字符串操作，交给代码比让模型自己控制可靠得多。

    只脱引号不删正文：重复出现的那句话通常还带着位置信息（"右侧那包同样
    印着 XX"），删掉会丢内容，留着文字、去掉引号正好。
    """
    if not caption:
        return caption

    seen: set[str] = set()

    def repl(m: re.Match) -> str:
        body = m.group(1)
        key = re.sub(r"\s+", "", body).lower()
        if key in seen:
            return body          # 重复：脱掉引号，文字留着
        seen.add(key)
        return m.group(0)        # 首次：原样保留

    out = _QUOTED.sub(repl, caption)
    return re.sub(r"\s{2,}", " ", out).strip()


# 末尾复述段的起始标志。模型描述完画面后常追加一句"画面中可见文字：XXX 为
# 某某字体"，把前文已经写过的文字内容连同字体再罗列一遍，属于纯粹的重复。
_RESTATE = re.compile(
    r"(?:画面中|画面里|画面主体上|图中)?(?:显著)?可见(?:的)?文字"
    # 分隔词可有可无：h3 是「可见文字：KADOKAWA为…」，h1/h4 是
    # 「可见文字盐焗腰果」「可见文字Calbee…」——后者直接贴内容，没有冒号。
    r"(?:\s*(?:为|是|包括|有|：|:)\s*)?"
)


def drop_restatement(caption: str) -> str:
    """砍掉末尾那段复述文字。

    实测（五张真实商品照）有三种形态，但都从同一个短语起头：
      - h1：整段一句，尾巴是「整体为摄影风格，画面中可见文字盐焗腰果。」
      - h4：整段一句，尾巴是「…画面中可见文字Calbee…コンソメパンチ。」
      - h3：复述横跨末尾三句，「画面中可见文字：KADOKAWA为深蓝色…」
    所以在标志处**截到段尾**，而不是只删那一句——h3 那种会漏。

    ★只在标志出现在全段后 1/3 时才砍：模型偶尔会用"可见文字"描述第一次
      出现的关键信息（比如整段就是在讲招牌），从中间砍会毁掉正文。
    """
    if not caption:
        return caption

    # 取最后一个命中，且要求标志后面确实还跟着内容：`可见文字` 单独收尾
    # 不构成复述，砍了反而会丢正文最后一句。
    cut_at = -1
    for m in _RESTATE.finditer(caption):
        rest = caption[m.end():].strip(" ，、；,;:：")
        if len(rest) >= 2:
            cut_at = m.start()
    if cut_at < 0 or cut_at < len(caption) * 0.6:
        return caption
    m_start = cut_at

    # 往回吞掉紧挨着的连接成分（"、" "，以及"最后"之类的引导）。
    cut = m_start
    while cut > 0 and caption[cut - 1] in "，、；,;:： ":
        cut -= 1
    head = caption[:cut].rstrip("，、；,;:： ")

    # 砍完补齐句末标点：原文若以句号结尾，这里也补上。
    if head and head[-1] not in "。.！？!?":
        head += "。"
    return head


def tidy_caption(caption: str) -> str:
    """caption 入库前的清洗总入口。

    顺序有讲究：先去重再砍复述段。反过来的话，复述段里那批重复引文会把
    "首次出现"占掉，正文里真正该保留的那条反而被当成重复脱了引号。

    两步都是纯字符串操作，不改画面描述本身，只在引文层面收敛。
    """
    if not caption:
        return caption
    return drop_restatement(dedupe_quotes(caption))
