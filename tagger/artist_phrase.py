"""画师名的占位符保护：送模型前替换，生成后回填。

为什么需要这一层
----------------
画师名是**绑定键**，不是描述文本，它有两个天生的危险：

1. **模型会写错**。只要原名进了模型可见的自由文本，就可能被翻译
   （`sakimichan` → `咲美酱`）、音译、改大小写或拆成两个词。名字错一个
   字符，训练侧就绑不上人。
2. **模型会复述**。名字一旦出现在 prompt 里，模型可能在输出里再写一遍，
   和正文里按模板注入的那次重复。

于是送模型前把名字换成 `__ARTIST_i__`：模型从头到尾没见过原名，也就无从
写错，只能原样搬运占位符；回填由本模块按映射完成，`@` 只在回填时补一次。
占位符的形态固定为双下划线包数字，普通英文里不会自然出现，正则也不会
误伤正文里的 `@name` 或 `artist_0` 这类词。

模板句式（正文里那句"使用 @名 的风格"）不在这里，见 `artist_templates`。
"""
from __future__ import annotations

import re
import sys
from typing import Iterable, Sequence

PLACEHOLDER_TMPL = "__ARTIST_{i}__"
# 回填时 `@` 是可选的，而且**允许连续多个**：模型有时照着 `@name` 的写法输出
# `@__ARTIST_0__`，被上游拼过前缀时还会变成 `@@__ARTIST_0__`。这些都要收敛成
# 单个 `@原名`；`@` 后面的可选空白只允许出现在 `@` 之后，否则会吞掉正常词距。
_RESTORE_RE = re.compile(r"(?:@+[ \t]?)?__ARTIST_(\d+)__")
# 词边界字符集：ASCII 字母/数字/下划线。名字紧挨着它们时不匹配，避免
# `rin` 命中 `ring`、`ARTIST` 命中 `ARTISTS`、`wlop` 命中 `wlop_fan`。
# 刻意**不含** CJK 与假名：中文/日文正文没有词间空格，把 CJK 也算作词字符
# 会让 `画师竹うら的作品` 里的名字整个漏掉——漏名比多替换一次危险得多。
_WORD_CHARS = r"A-Za-z0-9_"
# 与画师一致性校验相同：成对引号内的名字属于图中文字引用。
_QUOTED = re.compile(r'“[^”]*”|"[^"]*"|「[^」]*」|『[^』]*』|‘[^’]*’')


def _unique_names(names: Iterable[str]) -> list[str]:
    """去空白、去重、保序。

    入参统一归一成**裸名**（去掉前导 `@`）：名字可能来自 tag 串（`@wlop`），
    也可能来自元数据（`wlop`）。不归一的话 `protect("by wlop", ["@wlop"])`
    一个都匹配不上，等于保护静默失效。
    """
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            continue
        name = name.strip().lstrip("@").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def protect(text: str, names: Sequence[str]) -> tuple[str, dict[str, str]]:
    """把 `text` 里的画师名换成占位符，返回 `(替换后的文本, {占位符: 原名})`。

    四个必须成立的细节：

    - **长名优先**：`sakimi` 是 `sakimichan` 的前缀。若短名先替换，长名只剩
      后半截，就再也匹配不上。因此所有名字按长度倒序拼进一条正则的交替
      分支——同一位置上长度最长的分支总先命中，等价于"按长度倒序替换"，
      但没有逐个 `str.replace` 的中间态。
    - **单趟替换**：一次扫描完成，已插入的占位符不会再被后来者二次匹配，
      名字恰好是占位符片段时也不会被替换两次。
    - **吞掉名字前的 `@`**：正文常写成 `@wlop`。`@` 一并吃掉，回填时由
      `restore` 统一补回，否则会变成 `@@wlop`。
    - **编号按首次出现**：同一名字出现多次只占一个占位符，回填结果一致。
    - **词边界**：名字紧挨 ASCII 字母/数字/下划线时不替换。没有这一条，
      短名会在长词里造出假画师（`rin` 命中 `a ring of light`，`ARTIST` 命中
      `ARTISTS`）。括号、顿号/逗号、引号、空格、行首行尾都不是词字符，
      这些位置照常替换；CJK 与假名也不算词字符（正文没有词间空格）。

    映射的值是原样名字（不含 `@`），回填时才补 `@`，保证"正文里每个
    `@名` 与原名逐字相等"。
    """
    ordered = sorted(_unique_names(names), key=len, reverse=True)
    if not ordered:
        return text, {}

    # 交替分支必须整体分组：否则 `(?:@[ \t]?)?` 只作用于第一个分支。
    # 两侧 lookaround 是词边界；边界字符集见 `_WORD_CHARS` 的注释。
    pattern = re.compile(
        rf"(?<![{_WORD_CHARS}])(?:@[ \t]?)?(?:"
        + "|".join(re.escape(name) for name in ordered)
        + rf")(?![{_WORD_CHARS}])"
    )

    mapping: dict[str, str] = {}
    by_name: dict[str, str] = {}

    def _sub(m: re.Match[str]) -> str:
        raw = m.group(0)
        name = raw[1:].lstrip(" \t") if raw.startswith("@") else raw
        ph = by_name.get(name)
        if ph is None:
            ph = PLACEHOLDER_TMPL.format(i=len(by_name))
            by_name[name] = ph
            mapping[ph] = name
        return ph

    return pattern.sub(_sub, text), mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    """把已知占位符回填成 `@原名`；未知占位符原样保留。

    `@` 只在这一步补一次，所以三种输入收敛到同一结果：裸占位符
    `__ARTIST_0__`、模型模仿 `@` 写法输出的 `@__ARTIST_0__`、以及被上游
    拼过前缀的 `@@__ARTIST_0__` 都变成 `@原名`，不会出现 `@@`。未知下标
    （模型编造，或映射丢失、只拿到半个 mapping）一律不改写——把错的字符串
    当名字写进正文，比留一个显眼的占位符更难排查。
    """
    if not mapping:
        return text

    def _sub(m: re.Match[str]) -> str:
        name = mapping.get(PLACEHOLDER_TMPL.format(i=m.group(1)))
        if name is None:
            return m.group(0)
        # 容忍调用方把映射值写成 `@名字`，避免二次补 @。
        return name if name.startswith("@") else "@" + name

    return _RESTORE_RE.sub(_sub, text)


def leaked_names(text: str, names: Iterable[str], *,
                 ignore_quoted: bool = False) -> list[str]:
    """`text` 里仍然出现、本不该出现的画师原名。

    入参与 `protect` / `restore` 同口径：接受 `@名` 写法并归一成裸名，返回值
    也是裸名。**边界也与 `protect` 一致**——名字被粘在更长的词里（`rin` 之于
    `ring`、`wlop` 之于 `wlop_fan`）不算泄漏。两边口径必须一致，否则
    `protect` 之后这个函数会永远报"泄漏"，它就再也指不出真正的漏名了。

    `protect` 之后结果必须是空列表：非空即说明保护漏了名字，真名已经
    进入过模型上下文，那一段输出不能再用。

    检查模型正文时可用 ignore_quoted 排除图内文字引文；prompt 保护检查
    默认仍检查全部文本。没有闭合的引号不会排除。
    """
    if not text:
        return []
    if ignore_quoted:
        text = _QUOTED.sub(" ", text)
    out: list[str] = []
    for name in _unique_names(names):
        pattern = re.compile(
            rf"(?<![{_WORD_CHARS}]){re.escape(name)}(?![{_WORD_CHARS}])")
        if pattern.search(text):
            out.append(name)
    return out


# --------------------------------------------------------------- 自检

def _selftest() -> int:
    """占位符保护的回归自检；任一条不成立，名字就可能漏进模型上下文。

    只覆盖占位符层（替换/回填/泄漏检测），模板句式由 `artist_templates`
    自己验证。全部通过返回 0，否则打印 got/want 并返回非 0。
    """
    failures: list[str] = []
    passed = 0

    def check(label: str, got: object, want: object) -> None:
        nonlocal passed
        if got == want:
            passed += 1
        else:
            failures.append(f"{label}\n      got : {got!r}\n      want: {want!r}")

    # 1) 单名：替换成形、映射可回填、保护后无泄漏
    p, m = protect("a portrait, by wlop, detailed", ["wlop"])
    check("1 单名 protect", p, "a portrait, by __ARTIST_0__, detailed")
    check("1 单名 mapping", m, {"__ARTIST_0__": "wlop"})
    check("1 单名 restore", restore(p, m), "a portrait, by @wlop, detailed")
    check("1 单名 无泄漏", leaked_names(p, ["wlop"]), [])

    # 2) 多名：各自独立占位符，回填顺序与正文顺序一致
    p, m = protect("by wlop and sakimichan, detailed", ["wlop", "sakimichan"])
    check("2 多名 protect", p, "by __ARTIST_0__ and __ARTIST_1__, detailed")
    check("2 多名 mapping", m,
          {"__ARTIST_0__": "wlop", "__ARTIST_1__": "sakimichan"})
    check("2 多名 restore", restore(p, m), "by @wlop and @sakimichan, detailed")
    check("2 多名 无泄漏", leaked_names(p, ["wlop", "sakimichan"]), [])

    # 3) 名字互为前缀：长名先命中；入参顺序不影响结果
    text = "painted by sakimichan, not sakimi"
    long_first, m = protect(text, ["sakimichan", "sakimi"])
    short_first, _ = protect(text, ["sakimi", "sakimichan"])
    check("3 前缀 protect", long_first, "painted by __ARTIST_0__, not __ARTIST_1__")
    check("3 前缀 顺序无关", short_first, long_first)
    check("3 前缀 restore", restore(long_first, m),
          "painted by @sakimichan, not @sakimi")
    check("3 前缀 无残留片段", "sakimi" in long_first, False)

    # 4) 正文已含 @name：@ 一并吞掉，回填后仍只有一个 @
    p, m = protect("see @wlop for style, and wlop again", ["wlop"])
    check("4 含@ protect", p, "see __ARTIST_0__ for style, and __ARTIST_0__ again")
    restored = restore(p, m)
    check("4 含@ restore", restored, "see @wlop for style, and @wlop again")
    check("4 含@ 无@@", "@@" in restored, False)

    # 5) 占位符重复出现：模型复述多次，每处都要回填
    model_out = "art, __ARTIST_0__ style, then __ARTIST_0__ again, __ARTIST_0__"
    restored = restore(model_out, {"__ARTIST_0__": "wlop"})
    check("5 重复 restore", restored, "art, @wlop style, then @wlop again, @wlop")
    check("5 重复 次数", restored.count("@wlop"), 3)

    # 6) 未知占位符原样保留（含模型自带 @ 的写法）
    known = {"__ARTIST_0__": "wlop"}
    check("6 未知 裸", restore("x __ARTIST_7__ y", known), "x __ARTIST_7__ y")
    check("6 未知 带@", restore("x @__ARTIST_7__ y", known), "x @__ARTIST_7__ y")
    check("6 未知 空映射", restore("__ARTIST_3__", {}), "__ARTIST_3__")
    check("6 混合", restore("__ARTIST_0__ __ARTIST_9__", known),
          "@wlop __ARTIST_9__")

    # 7) 模型自加 @ 的已知占位符：只补一个 @
    check("7 模型带@", restore("@__ARTIST_0__", known), "@wlop")
    check("7 模型带@空格", restore("@ __ARTIST_0__", known), "@wlop")

    # 8) 边界：空文本、无名字、名字未出现、名字含正则元字符、名字带空白
    check("8 空文本", protect("", ["wlop"]), ("", {}))
    check("8 无名字", protect("no names here", []), ("no names here", {}))
    check("8 名字未出现", protect("nothing here", ["wlop"]), ("nothing here", {}))
    p, m = protect("style of andy.warhol? no, andyXwarhol", ["andy.warhol"])
    check("8 元字符 protect", p, "style of __ARTIST_0__? no, andyXwarhol")
    check("8 元字符 restore", restore(p, m),
          "style of @andy.warhol? no, andyXwarhol")
    check("8 名字去空白", protect("by wlop", ["  wlop  "])[0], "by __ARTIST_0__")
    check("8 泄漏检测", leaked_names("a wlop piece", ["wlop"]), ["wlop"])

    # 9) 词边界：不在长词内部造出假画师
    check("9 词边界 rin 不进 ring",
          protect("a ring of light", ["rin"]), ("a ring of light", {}))
    check("9 词边界 ARTIST 不进 ARTISTS",
          protect("ARTISTS", ["ARTIST"])[0], "ARTISTS")
    check("9 词边界 数字紧邻不匹配", protect("img0012", ["0012"])[0], "img0012")
    check("9 词边界 下划线紧邻不匹配", protect("wlop_fan", ["wlop"])[0], "wlop_fan")
    check("9 词边界 空格包围匹配",
          protect("by wlop here", ["wlop"])[0], "by __ARTIST_0__ here")
    check("9 词边界 括号包围匹配", protect("(wlop)", ["wlop"])[0], "(__ARTIST_0__)")
    check("9 词边界 顿号逗号包围匹配",
          protect("wlop、askzy, x", ["wlop", "askzy"])[0],
          "__ARTIST_0__、__ARTIST_1__, x")
    check("9 词边界 行首行尾匹配", protect("wlop", ["wlop"])[0], "__ARTIST_0__")
    check("9 词边界 CJK 紧邻仍匹配（否则整段漏名）",
          protect("画师竹うら的作品", ["竹うら"]), ("画师__ARTIST_0__的作品",
                                                   {"__ARTIST_0__": "竹うら"}))
    check("9 词边界 替换后无泄漏",
          leaked_names(protect("a ring of light", ["rin"])[0], ["rin"]), [])
    check("9 词边界 真名字仍被抓（不漏报）",
          leaked_names("by wlop here", ["wlop"]), ["wlop"])
    check("9 词边界 词内不算泄漏", leaked_names("wlop_fan", ["wlop"]), [])

    # 10) 入参归一：`@名` 与裸名同一口径（否则保护会静默失效）
    p, m = protect("by wlop", ["@wlop"])
    check("10 @形态入参 protect", p, "by __ARTIST_0__")
    check("10 @形态入参 mapping", m, {"__ARTIST_0__": "wlop"})
    check("10 @形态入参 restore", restore(p, m), "by @wlop")
    check("10 泄漏检测接受 @形态", leaked_names("a wlop piece", ["@wlop"]), ["wlop"])
    check("10 裸名与 @名混入去重",
          protect("by wlop", ["wlop", "@wlop"])[1], {"__ARTIST_0__": "wlop"})

    # 11) `@@` 收敛：模型/上游多写一个 @ 也要回到单个 `@`，不自愈失败
    check("11 @@收敛", restore("@@__ARTIST_0__", known), "@wlop")
    check("11 @@@收敛", restore("@@@__ARTIST_0__", known), "@wlop")
    check("11 @@带空格收敛", restore("@@ __ARTIST_0__", known), "@wlop")
    p2, m2 = protect("@@wlop", ["wlop"])
    check("11 protect 双@再回填", restore(p2, m2), "@wlop")
    check("11 未知下标 @@ 不改写",
          restore("@@__ARTIST_9__", known), "@@__ARTIST_9__")

    # 12) 不留无人使用的定义：占位符只由回填那一条正则负责
    module = sys.modules[__name__]
    check("12 无死定义 PLACEHOLDER_RE", hasattr(module, "PLACEHOLDER_RE"), False)

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
    print("usage: python -m tagger.artist_phrase --selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
