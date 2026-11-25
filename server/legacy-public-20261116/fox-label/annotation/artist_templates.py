"""画师注入的句式层：固定开头 + 中英各一套模板。

为什么要句式而不是裸名前缀
--------------------------
把 `@name` 直接顶在正文最前面，等于让"画师"变成 caption 的第一个 token：
模型很快会把"开头必是画师名"当成分布规律学下去，推理时前几个 token 就被
画师占满。更糟的是裸名没有语义，读起来不像描述，人也难以判断这句话到底
在讲画面还是讲出处。所以改成一句完整的自然描述，位置固定在最前。

为什么位置固定而不是随机
------------------------
随机位置让同一张图重跑得到不同句子，审计和 A/B 时对不上；而且"随机"并没有
额外收益——真正要避免的是"裸名当首 token"，用句式就已经解决了。

为什么名字必须逐字原样
----------------------
画师名是绑定键，不是描述文本：`sakimichan` 与 `Sakimichan` 在训练侧是两个
不同的键，假名、下划线、空格同理。模板只负责加 `@` 和连接词，**绝不改写
名字本身**——不做大小写归一、不做空格转下划线、不做任何 transliteration。

没有可信画师时整句省略：`render([])` 返回空串，调用方保持正文原样，
不造名字、也不写"疑似某画师"这类没有证据的说法。
"""
from __future__ import annotations

import sys
from typing import Iterable, Sequence

# 每种语言只有一套模板。`{a}` 是一个或多个 `@名字`，`{rest}` 是正文。
# 句式本身不含任何 tag 类型标注（没有 `artist:` 之类的前缀）。
TEMPLATES: dict[str, str] = {
    "zh": "使用 {a} 的风格，{rest}",
    "en": "In the style of {a}, {rest}",
}

# 一次最多列几个画师。超过就按原 tag 顺序截断：名字越多，句式越长，
# 而"画风"是共享属性，列到第三个之后边际信息已经很低。
MAX_ARTISTS = 3

# 语言归一化：配置与产物里出现过 zh/zh-CN/cn/中文 等写法，认不出按英文。
_ZH_LANGS = frozenset({"zh", "zh-cn", "zh_cn", "zh-hans", "zh-hant",
                       "cn", "chinese", "中文"})

# 句式开头标记：`insert`/幂等判断要认出"这句已经是画师句"。
# 与 TEMPLATES 同源（selftest 有断言），改了模板要一起改。
_HEADINGS = {"zh": "使用 @", "en": "In the style of @"}


def route_lang(lang: str | None) -> str:
    """把各种语言写法收敛成 `zh` / `en`；认不出按 `en`。"""
    low = str(lang or "").strip().lower()
    return "zh" if low in _ZH_LANGS else "en"


def tag_of(name: str) -> str:
    """名字 → 注入形态 `@名字`。逐字原样，只在最前面补**一个** `@`。

    入参可能已经被上游加过 `@`（`@wlop`），甚至加了两遍（`@@wlop`，拼接层
    重复加前缀就会这样）。这里统一收敛成单个 `@`：正文里出现 `@@wlop`，
    训练侧的绑定键就找不回这个名字了。只有 `@` 没有名字的一律当空。
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    # 一次剥掉开头所有 `@` 与空白：`@ @wlop` 这种"带空格的重复前缀"也要收敛，
    # 否则剥一个 @ 再接一个，等于原样留下 `@@`。
    core = raw.lstrip("@ \t").strip()
    return "@" + core if core else ""


def pick(artists: Iterable[str] | str, *, limit: int = MAX_ARTISTS) -> list[str]:
    """取前 `limit` 个非空名字，保序去重。

    去重是为了"同一个画师被表和目录同时命中"这种情形不重复列名；
    截断按**原 tag 顺序**，不排序——顺序本身是上游给的稳定性依据。

    裸 `str` 必须当成"一个名字"：按字符迭代会产出 `@w、@l、@o` 这种残句，
    而它一眼看去"有 @ 有名字"，最容易被当成正常产物放过去。
    """
    if isinstance(artists, str):
        artists = [artists]
    out: list[str] = []
    seen: set[str] = set()
    for artist in artists or ():
        tag = tag_of(artist)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= max(1, limit):
            break
    return out


def _join(tags: Sequence[str], lang: str) -> str:
    """多画师连接：中文顿号，英文用 and（3 个时前面用逗号分隔）。"""
    if len(tags) <= 1:
        return tags[0] if tags else ""
    if lang == "zh":
        return "、".join(tags)
    if len(tags) == 2:
        return f"{tags[0]} and {tags[1]}"
    return ", ".join(tags[:-1]) + f" and {tags[-1]}"


def _prefix_part(lang: str) -> str:
    """模板去掉 `{rest}` 之后的部分，例如 `使用 {a} 的风格`。

    用 `str.replace` 而不是 `str.format`：正文里可能含 `{` `}`，
    format 会把它当占位符并抛异常——正文必须逐字进、逐字出。
    """
    head = TEMPLATES[route_lang(lang)].split("{rest}")[0]
    return head.rstrip("，, ").rstrip()


def prefix(artists: Sequence[str], lang: str) -> str:
    """只要画师那半句（不含正文），没有画师时返回空串。"""
    lang_key = route_lang(lang)
    tags = pick(artists)
    if not tags:
        return ""
    return _prefix_part(lang_key).replace("{a}", _join(tags, lang_key)).strip()


def render(artists: Sequence[str], lang: str, rest: str | None = None) -> str:
    """按语言生成画师句。

    - `rest is None`：只返回画师那半句（`使用 @名 的风格` / `In the style of @名`），
      供 `insert` 拼到已有正文前面；
    - 给了 `rest`：直接返回整句，正文原样接在后面。

    没有画师（空名单、全是空名字）时返回空串：宁可没有画师句，
    也不要造一个 `使用 @ 的风格` 这样的残句。
    """
    lang_key = route_lang(lang)
    tags = pick(artists)
    if not tags:
        return ""
    joined = _join(tags, lang_key)
    if rest is None:
        return _prefix_part(lang_key).replace("{a}", joined).strip()
    body = str(rest).lstrip()
    if not body:
        return _prefix_part(lang_key).replace("{a}", joined).strip()
    return TEMPLATES[lang_key].replace("{a}", joined).replace("{rest}", body).strip()


def _lang_of_sentence(sentence: str) -> str | None:
    """从画师句的开头标记反推它出自哪套模板；认不出返回 None。

    分隔符必须由**模板语言**决定，不能看句子里有没有汉字：英文模板配一个
    CJK 画师名（`@竹うら`，真实数据里就有）时，按"含不含汉字"判断会给出全角
    逗号，`In the style of @竹うら，a girl` 在英文句子里就是明确的错字。
    """
    body = (sentence or "").lstrip()
    for lang, head in _HEADINGS.items():
        if body.startswith(head):
            return lang
    return None


def insert(text: str, sentence: str, *, lang: str | None = None) -> str:
    """把画师句放到正文**最前面**（位置恒为开头，不做随机）。

    分隔符只由模板语言决定：中文全角逗号，英文逗号加空格。语言优先取显式
    `lang`，否则按句式开头标记反推；两者都认不出时按英文半角——半角逗号在
    中文里只是不美观，全角逗号出现在英文句子里是明确的错字。
    句式自身已带尾随逗号时不再补，避免 `，，` 或 `, ,`。
    正文里已经有同一句时不重复加，保证重跑幂等。
    """
    body = (text or "").strip()
    sent = (sentence or "").strip()
    if not sent:
        return body
    if not body:
        return sent
    if body.startswith(sent):
        return body
    if sent.endswith("，"):
        return f"{sent}{body}"
    if sent.endswith(","):
        return f"{sent} {body}"
    sep = "，" if route_lang(lang or _lang_of_sentence(sent)) == "zh" else ", "
    return f"{sent}{sep}{body}"


def starts_with_artist_sentence(text: str, tag: str) -> bool:
    """正文是否已经以该画师的句式开头。

    跨层幂等的判据：打标侧已按模板写过一次画师句，封包侧如果还按
    "裸名前缀"再拼一次，同一个名字就会在正文里出现两遍。
    """
    body = (text or "").lstrip()
    if not body or not tag:
        return False
    if not any(body.startswith(head) for head in _HEADINGS.values()):
        return False
    return tag in body


# --------------------------------------------------------------- 自检

def _selftest() -> int:
    """句式层回归：中英各 4 例 + 多画师 + 空名 + 含空格名字逐字保真。

    任一条不成立都会直接影响训练侧的画师绑定，所以这里要求全过。
    """
    failures: list[str] = []
    passed = 0

    def check(label: str, got: object, want: object) -> None:
        nonlocal passed
        if got == want:
            passed += 1
        else:
            failures.append(f"{label}\n      got : {got!r}\n      want: {want!r}")

    def check_true(label: str, cond: bool) -> None:
        check(label, bool(cond), True)

    # ---------- 中文 4 例 ----------
    # 1) 直接给正文：整句成形
    check("zh1 整句", render(["wlop"], "zh", "一位少女站在雨中的车站"),
          "使用 @wlop 的风格，一位少女站在雨中的车站")
    # 2) 只给前缀 + insert：位置在开头
    check("zh2 前缀+insert", insert("画面里有猫", render(["sakimichan"], "zh")),
          "使用 @sakimichan 的风格，画面里有猫")
    # 3) 语言归一化 zh-CN / cn → 中文
    check("zh3 zh-CN 路由", route_lang("zh-CN"), "zh")
    check("zh3 cn 路由", route_lang("cn"), "zh")
    check("zh3 大写入参", render(["wlop"], "ZH", "室外"), "使用 @wlop 的风格，室外")
    # 4) 只取前缀时不含正文、也不留尾随逗号
    check("zh4 纯前缀", render(["wlop"], "zh"), "使用 @wlop 的风格")

    # ---------- 英文 4 例 ----------
    # 1) 整句
    check("en1 整句", render(["wlop"], "en", "a dark forest at dusk"),
          "In the style of @wlop, a dark forest at dusk")
    # 2) 前缀 + insert
    check("en2 前缀+insert", insert("a dark forest", render(["wlop"], "en")),
          "In the style of @wlop, a dark forest")
    # 3) 认不出的语言按英文
    check("en3 未知语言", route_lang("ja"), "en")
    check("en3 空语言", route_lang(None), "en")
    # 4) 纯前缀
    check("en4 纯前缀", render(["wlop"], "en"), "In the style of @wlop")

    # ---------- 多画师 ----------
    check("multi2 zh", render(["a", "b"], "zh", "室外"),
          "使用 @a、@b 的风格，室外")
    check("multi2 en", render(["a", "b"], "en", "outdoor"),
          "In the style of @a and @b, outdoor")
    check("multi3 zh", render(["a", "b", "c"], "zh", "室内"),
          "使用 @a、@b、@c 的风格，室内")
    check("multi3 en", render(["a", "b", "c"], "en", "indoor"),
          "In the style of @a, @b and @c, indoor")
    # 超过 3 个：按原 tag 顺序截断，不排序
    check("multi4 截断", render(["z", "y", "x", "w"], "en", "art"),
          "In the style of @z, @y and @x, art")
    check("multi 去重", render(["a", "a", "b"], "zh", "室外"),
          "使用 @a、@b 的风格，室外")

    # ---------- 空名 ----------
    check("empty 名单", render([], "zh", "室外"), "")
    check("empty 全空白", render(["", "   "], "en", "art"), "")
    check("empty insert", insert("正文", ""), "正文")
    check("empty 正文", insert("", render(["a"], "en")), "In the style of @a")

    # ---------- 名字含空格：逐字保真，不转下划线 ----------
    check("space zh", render(["sakimi chan"], "zh", "室外"),
          "使用 @sakimi chan 的风格，室外")
    check("space en", render(["sakimi chan"], "en", "art"),
          "In the style of @sakimi chan, art")
    check("space 不转下划线", "_" in render(["sakimi chan"], "en", "art"), False)
    check("space 多画师", render(["sakimi chan", "kani-bi"], "en", "art"),
          "In the style of @sakimi chan and @kani-bi, art")

    # ---------- 名字逐字原样：大小写 / 假名 / 已有 @ ----------
    check("verbatim 大小写", render(["Sakimichan"], "en", "art"),
          "In the style of @Sakimichan, art")
    check("verbatim 假名", render(["竹うら"], "zh", "室外"),
          "使用 @竹うら 的风格，室外")
    check("verbatim 已带@", render(["@wlop"], "en", "art"),
          "In the style of @wlop, art")
    check("verbatim 不出现@@", "@@" in render(["@wlop"], "en", "art"), False)

    # ---------- 句式里不夹带 tag 类型标注 ----------
    for lang in ("zh", "en"):
        sentence = render(["wlop"], lang, "x")
        check(f"no-tag-type {lang}", ":" in sentence or "artist" in sentence.lower()
              or "画师" in sentence or "tag" in sentence.lower(), False)

    # ---------- 开头标记与模板同源（防止改了模板忘了改幂等判据） ----------
    for lang in ("zh", "en"):
        rendered = _prefix_part(lang).replace("{a}", "@x")
        check(f"heading {lang}", rendered.startswith(_HEADINGS[lang]), True)

    # ---------- 跨层幂等：已有画师句时不再补裸名 ----------
    zh_sentence = "使用 @wlop 的风格，一位少女"
    en_sentence = "In the style of @wlop, a girl"
    check("幂等 zh", starts_with_artist_sentence(zh_sentence, "@wlop"), True)
    check("幂等 en", starts_with_artist_sentence(en_sentence, "@wlop"), True)
    check("幂等 多画师", starts_with_artist_sentence("使用 @a、@b 的风格，x", "@b"), True)
    check("幂等 别家名字", starts_with_artist_sentence(en_sentence, "@other"), False)
    check("幂等 普通正文", starts_with_artist_sentence("a girl, outdoor", "@wlop"), False)
    check("幂等 名字在句尾不算开头",
          starts_with_artist_sentence("outdoor art @wlop", "@wlop"), False)

    # ---------- 分隔符只由模板语言决定（不看句式里有没有汉字） ----------
    # 英文模板 + CJK 名字：真实数据里确实有 `竹うら` 这种名字
    check("f1 en+CJK 名字 半角",
          render(["竹うら"], "en", "a girl stands"),
          "In the style of @竹うら, a girl stands")
    check("f1 en+CJK insert 半角",
          insert("a girl stands", render(["竹うら"], "en")),
          "In the style of @竹うら, a girl stands")
    check("f1 英文句里不得有全角逗号",
          "\uff0c" in insert("a girl stands", render(["竹うら"], "en")), False)
    # 反向：中文模板 + 拉丁名字，仍然是全角逗号
    check("f1 zh+拉丁 全角",
          insert("一位少女站在雨中", render(["wlop"], "zh")),
          "使用 @wlop 的风格，一位少女站在雨中")
    check("f1 中文句里必须是全角逗号",
          "\uff0c" in insert("x", render(["wlop"], "zh")), True)
    # 显式 lang 优先于开头标记的推断
    check("f1 显式 lang 优先",
          insert("a girl", "使用 @wlop 的风格", lang="en"),
          "使用 @wlop 的风格, a girl")

    # ---------- 裸 str 入参：当"一个名字"，不按字符拆 ----------
    check("f2 str 中文整句", render("wlop", "zh", "正文"),
          "使用 @wlop 的风格，正文")
    check("f2 str 英文整句", render("wlop", "en", "art"),
          "In the style of @wlop, art")
    check("f2 str pick", pick("wlop"), ["@wlop"])
    check("f2 str 不出现逐字画师",
          "、".join(["@w", "@l"]) in render("wlop", "zh", "x"), False)

    # ---------- 多余的 `@` 归并成一个 ----------
    check("f3 @@ 收敛", render(["@@wlop"], "en", "a girl stands"),
          "In the style of @wlop, a girl stands")
    check("f3 @@ 无残留", "@@" in render(["@@wlop"], "zh", "室外"), False)
    check("f3 @@@ 收敛", tag_of("@@@wlop"), "@wlop")
    check("f3 @@ 与裸名去重", render(["@@wlop", "wlop"], "en", "art"),
          "In the style of @wlop, art")
    check("f3 只有 @ 视作空", tag_of("@"), "")

    if failures:
        print(f"FAIL: {len(failures)} 项未通过，{passed} 项通过")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"OK: {passed} 项全部通过")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--selftest"]:
        return _selftest()
    print("usage: python -m tagger.artist_templates --selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
