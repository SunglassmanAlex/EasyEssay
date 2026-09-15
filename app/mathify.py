"""Unicode 数学文本 -> LaTeX 还原，以及上下标还原。

设计原则（对应需求「公式必须真正渲染、禁止裸露 LaTeX / 手写 Unicode 符号」）：
1. **不猜测语义**：只做确定性的"字形 -> LaTeX"映射，不引入模型幻觉，保证左栏原文可信。
2. **来源可靠**：优先用 PDF 内部字体信息判定数学片段（CMMI/CMSY/EUFM/MSBM… 是 LaTeX 数学
   字体；CMR/CMBX/CMTI/CMSS 是正文字体，不能误判），字体信息不足时退化为 Unicode 区段判定。
3. **上下文校准**：单字母是否属于公式，取决于同一行里有没有真正含数学符号的片段，
   这样既能还原 "y = f(x)"，又不会把标题 "PlonK" 拆成 $P$$lon$$K$。
4. **上下标还原**：利用 PyMuPDF 给出的字号与基线（origin.y）差异，把 PDF 里被拆散的
   上下标重新组装成 `x^{2}` / `x_{i}`——这是公式能否正确渲染的关键。
5. 若仍需更强恢复能力（扫描件 / 矢量图形公式），可打开「公式修复」让模型参与重写。
"""
from __future__ import annotations

import re
import unicodedata

# ------------------------------------------------------------------ 字体判定

# LaTeX / OpenType 数学字体（真·数学字体）
MATH_FONT_RE = re.compile(
    r"(cmmi|cmsy|cmex|cmext|msam|msbm|eufm|eusm|eufb|rsfs|stmary|symbol|mtmi|mtsy|"
    r"mt-?extra|math|stix|xits|lmmi|lmsy|lmex|euler|wasy|txmi|txsy|pxmi|pxsy|"
    r"asana|fourier|cambria)",
    re.I,
)
# 明确的正文字体：出现这些名字就不再当数学处理
TEXT_FONT_RE = re.compile(
    r"(\bcmr|\bcmsl|\bcmti|\bcmbx|\bcmss|\bcmtt|\bcmcsc|times|arial|helvet|nimbusrom|"
    r"nimbussans|liberation|dejavu\s*sans|garamond|palatino|bookman|georgia|calibri|"
    r"minion|charter|constantia|candara|segoe|roboto|lato|source\s*sans|noto\s*sans)",
    re.I,
)

# Unicode 数学区段
MATH_RANGES = (
    (0x00B0, 0x00BE),   # ° ± ² ³ ´ µ · ¹ º ¼ ½ ¾
    (0x00D7, 0x00D7),   # ×
    (0x00F7, 0x00F7),   # ÷
    (0x00AC, 0x00AC),   # ¬
    (0x0370, 0x03FF),   # Greek
    (0x2032, 0x2037),   # ′ ″ ‴（撇号 / 双撇 / 三撇）
    (0x2044, 0x2044),   # ⁄ 分数斜线
    (0x2061, 0x2064),   # 不可见函数应用符
    (0x2070, 0x209F),   # 上下标
    (0x2100, 0x214F),   # Letterlike Symbols: ℤ ℝ ℓ ℘
    (0x2150, 0x218F),   # Number Forms
    (0x2190, 0x21FF),   # Arrows
    (0x2200, 0x22FF),   # Mathematical Operators
    (0x2300, 0x23FF),   # Misc Technical
    (0x25A0, 0x25FF),   # Geometric Shapes
    (0x2600, 0x26FF),   # Misc Symbols（★ ✓ 等）
    (0x27C0, 0x27EF),   # Misc Math Symbols-A
    (0x27F0, 0x27FF),   # Supplemental Arrows-A
    (0x2900, 0x297F),   # Supplemental Arrows-B
    (0x2980, 0x29FF),   # Misc Math Symbols-B
    (0x2A00, 0x2AFF),   # Supplemental Math Operators
    (0x2B00, 0x2BFF),   # Misc Symbols and Arrows
    (0x3008, 0x3011),   # 〈〉〖〗
    (0x1D400, 0x1D7FF),  # Mathematical Alphanumeric Symbols
    (0x1EE00, 0x1EEFF),  # Arabic Mathematical Alphabetic Symbols
)
# 正文里几乎不出现的"强数学"ASCII 字符：一旦出现即可确认这是公式
STRONG_ASCII = set("=+")

# 无法识别的字形占位符。
#
# 为什么需要它：PDF 只存字形码位，字体的 ToUnicode 表经常是坏的。实测某论文里
# CMEX10（LaTeX 的大型括号/矩阵扩展字体）的字形被映射成控制字符 U+0010/U+0011
# 和私有区码位 U+F8EE…，它们其实是**大括号的上/下半截、矩阵的方括号**。
# 早期实现把这些字符"静默丢弃"——公式就少了括号，模型拿到残缺公式自然翻错。
# 现在统一替换成显式占位符，把"这里缺了什么"明确交给下游（模型）去按上下文还原。
MISSING_GLYPH = "⟦?⟧"


def is_unrenderable(ch: str) -> bool:
    """是否是"渲染不出来、也没有语义"的垃圾码位（控制字符 / 私有区 / 替换符）。"""
    cp = ord(ch)
    if ch in "\n\t\r":
        return False
    if cp == 0xFFFD:                      # 替换符
        return True
    if cp < 0x20 or 0x7F <= cp <= 0x9F:   # C0 / C1 控制字符
        return True
    if 0xE000 <= cp <= 0xF8FF:            # 私有区（各字体自定义，含义不明）
        return True
    return False


def count_missing_glyphs(text: str) -> int:
    """统计文本里未还原的字形占位符个数（用于向用户提示"这段有几处要修"）。"""
    return text.count(MISSING_GLYPH)

MATH_SPAN_RE = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)


def is_math_char(ch: str) -> bool:
    if ch in STRONG_ASCII:
        return True
    cp = ord(ch)
    for lo, hi in MATH_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def has_math_chars(text: str) -> bool:
    """文本里是否含有明确的数学符号（非 ASCII 字母数字之外的东西）。"""
    return any(is_math_char(c) for c in text)


def math_char_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if is_math_char(c)) / len(chars)


def math_coverage(markdown: str) -> float:
    """段落中"落在 $...$ 内"的字符占比——判断是否该按独立公式排版的可靠指标。"""
    compact = re.sub(r"\s+", "", markdown)
    if not compact:
        return 0.0
    inside = 0
    for m in MATH_SPAN_RE.finditer(markdown):
        inside += len(re.sub(r"\s+", "", m.group(0)))
    return min(1.0, inside / len(compact))


def is_math_font(fontname: str) -> bool:
    if not fontname:
        return False
    if TEXT_FONT_RE.search(fontname):
        return False
    return bool(MATH_FONT_RE.search(fontname))


# ------------------------------------------------------- 字符 -> LaTeX 映射表

_MAP: dict[str, str] = {
    # 希腊小写
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\varepsilon", "ϵ": r"\epsilon", "ζ": r"\zeta", "η": r"\eta",
    "θ": r"\theta", "ϑ": r"\vartheta", "ι": r"\iota", "κ": r"\kappa",
    "ϰ": r"\varkappa", "λ": r"\lambda", "μ": r"\mu", "µ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "ο": "o", "π": r"\pi", "ϖ": r"\varpi",
    "ρ": r"\rho", "ϱ": r"\varrho", "σ": r"\sigma", "ς": r"\varsigma",
    "τ": r"\tau", "υ": r"\upsilon", "φ": r"\varphi", "ϕ": r"\phi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega", "∂": r"\partial",
    # 希腊大写
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Υ": r"\Upsilon",
    "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",
    # 二元运算 / 大运算符
    "×": r"\times", "÷": r"\div", "±": r"\pm", "∓": r"\mp",
    "⋅": r"\cdot", "·": r"\cdot", "∙": r"\cdot", "∗": r"\ast", "⋆": r"\star",
    "∘": r"\circ", "∙": r"\bullet", "⊕": r"\oplus", "⊖": r"\ominus",
    "⊗": r"\otimes", "⊙": r"\odot", "⊘": r"\oslash", "⊚": r"\circledcirc",
    "†": r"\dagger", "‡": r"\ddagger", "∑": r"\sum", "∏": r"\prod",
    "∫": r"\int", "∮": r"\oint", "∬": r"\iint", "∭": r"\iiint",
    "⋃": r"\bigcup", "⋂": r"\bigcap", "⨁": r"\bigoplus", "⨂": r"\bigotimes",
    "⨀": r"\bigodot", "⋁": r"\bigvee", "⋀": r"\bigwedge", "⨄": r"\biguplus",
    "∪": r"\cup", "∩": r"\cap", "∨": r"\vee", "∧": r"\wedge",
    "√": r"\sqrt", "∛": r"\sqrt[3]", "∜": r"\sqrt[4]", "∞": r"\infty",
    "∅": r"\varnothing", "⌀": r"\varnothing", "∖": r"\setminus",
    # 关系
    "≤": r"\leq", "⩽": r"\leq", "≥": r"\geq", "⩾": r"\geq", "≠": r"\neq",
    "≈": r"\approx", "≃": r"\simeq", "∼": r"\sim", "≅": r"\cong",
    "≡": r"\equiv", "≜": r"\triangleq", "∝": r"\propto", "≐": r"\doteq",
    "≪": r"\ll", "≫": r"\gg", "≺": r"\prec",
    "≻": r"\succ", "⪯": r"\preceq", "⪰": r"\succeq", "⊂": r"\subset",
    "⊆": r"\subseteq", "⊊": r"\subsetneq", "⊃": r"\supset", "⊇": r"\supseteq",
    "∈": r"\in", "∉": r"\notin", "∋": r"\ni", "∌": r"\not\ni",
    "⊢": r"\vdash", "⊣": r"\dashv", "⊨": r"\models", "⊑": r"\sqsubseteq",
    "⊒": r"\sqsupseteq", "⊥": r"\perp", "⊤": r"\top", "∥": r"\parallel",
    "∦": r"\nparallel", "∣": r"|", "∤": r"\nmid",
    # 箭头
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "↑": r"\uparrow", "↓": r"\downarrow", "↕": r"\updownarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "⇑": r"\Uparrow", "⇓": r"\Downarrow", "⇕": r"\Updownarrow",
    "↦": r"\mapsto", "↪": r"\hookrightarrow", "↩": r"\hookleftarrow",
    "⇀": r"\rightharpoonup", "↼": r"\leftharpoonup", "⇌": r"\rightleftharpoons",
    "⟶": r"\longrightarrow", "⟵": r"\longleftarrow",
    "⟹": r"\Longrightarrow", "⟸": r"\Longleftarrow", "⟺": r"\iff",
    "↗": r"\nearrow", "↘": r"\searrow", "↙": r"\swarrow", "↖": r"\nwarrow",
    "⇝": r"\rightsquigarrow", "⇢": r"\dashrightarrow",
    # 分隔符 / 括号
    "⟨": r"\langle", "⟩": r"\rangle", "⌈": r"\lceil", "⌉": r"\rceil",
    "⌊": r"\lfloor", "⌋": r"\rfloor", "‖": r"\|",
    "【": "[", "】": "]", "〔": "(", "〕": ")",
    # 逻辑 / 几何
    "∀": r"\forall", "∃": r"\exists", "∄": r"\nexists", "¬": r"\neg",
    "∠": r"\angle", "△": r"\triangle", "□": r"\square", "◇": r"\diamond",
    "⋄": r"\diamond", "♢": r"\diamond", "★": r"\star", "✓": r"\checkmark",
    "✔": r"\checkmark", "✗": r"\times",
    # 省略号 / 装饰
    "…": r"\ldots", "⋯": r"\cdots", "⋮": r"\vdots", "⋱": r"\ddots",
    "′": r"'", "″": r"''", "‴": r"'''", "°": r"^\circ", "‰": r"\permil",
    # 连字与不可见字符（LaTeX PDF 的 ToUnicode 常见映射）
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "ft", "\ufb06": "st",
    "\u00a0": " ", "\u200b": "", "\u00ad": "", "\u20dd": "",
    # Letterlike
    "ℓ": r"\ell", "ℏ": r"\hbar", "℘": r"\wp", "ℑ": r"\Im", "ℜ": r"\Re",
    "ℵ": r"\aleph", "ℶ": r"\beth",
}

_SUPSUB = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6",
    "⁷": "7", "⁸": "8", "⁹": "9", "⁺": "+", "⁻": "-", "⁼": "=", "⁽": "(",
    "⁾": ")", "ⁿ": "n", "ⁱ": "i", "₀": "0", "₁": "1", "₂": "2", "₃": "3",
    "₄": "4", "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9", "₊": "+",
    "₋": "-", "₌": "=", "₍": "(", "₎": ")", "ₐ": "a", "ₑ": "e", "ₒ": "o",
    "ₓ": "x", "ₕ": "h", "ₖ": "k", "ₗ": "l", "ₘ": "m", "ₙ": "n", "ₚ": "p",
    "ₛ": "s", "ₜ": "t", "ᵢ": "i", "ⱼ": "j", "ᵣ": "r", "ᵤ": "u", "ᵥ": "v",
}

_ALPHA_STYLES = (
    (0x1D400, 0x1D419, "A", r"\mathbf"),
    (0x1D41A, 0x1D433, "a", r"\mathbf"),
    (0x1D434, 0x1D44D, "A", ""),
    (0x1D44E, 0x1D467, "a", ""),
    (0x1D468, 0x1D481, "A", r"\boldsymbol"),
    (0x1D482, 0x1D49B, "a", r"\boldsymbol"),
    (0x1D49C, 0x1D4B5, "A", r"\mathcal"),
    (0x1D4B6, 0x1D4CF, "a", r"\mathcal"),
    (0x1D4D0, 0x1D4E9, "A", r"\mathcal"),
    (0x1D504, 0x1D51D, "A", r"\mathfrak"),
    (0x1D51E, 0x1D537, "a", r"\mathfrak"),
    (0x1D538, 0x1D551, "A", r"\mathbb"),
    (0x1D552, 0x1D56B, "a", r"\mathbb"),
    (0x1D56C, 0x1D585, "A", r"\mathfrak"),
    (0x1D5A0, 0x1D5B9, "A", r"\mathsf"),
    (0x1D5BA, 0x1D5D3, "a", r"\mathsf"),
    (0x1D5D4, 0x1D5ED, "A", r"\mathsf"),
    (0x1D608, 0x1D621, "A", r"\mathsf"),
    (0x1D670, 0x1D689, "A", r"\mathtt"),
    (0x1D7CE, 0x1D7D7, "0", r"\mathbf"),
    (0x1D7D8, 0x1D7E1, "0", r"\mathbb"),
    (0x1D7E2, 0x1D7EB, "0", r"\mathsf"),
)

_ASCII_ESCAPE = {
    "\\": r"\backslash",
    "#": r"\#", "&": r"\&", "%": r"\%", "$": r"\$",
    "{": r"\{", "}": r"\}",
}

_LETTERLIKE_EXTRA = {
    "ℛ": r"\mathcal{R}", "ℬ": r"\mathcal{B}", "ℰ": r"\mathcal{E}",
    "ℱ": r"\mathcal{F}", "ℋ": r"\mathcal{H}", "ℐ": r"\mathcal{I}",
    "ℒ": r"\mathcal{L}", "ℳ": r"\mathcal{M}", "ℯ": "e", "ℊ": "g", "ℴ": "o",
}


def _unicode_style_char(ch: str) -> str | None:
    cp = ord(ch)
    for lo, hi, base, cmd in _ALPHA_STYLES:
        if lo <= cp <= hi:
            letter = chr(ord(base) + (cp - lo))
            return f"{cmd}{{{letter}}}" if cmd else letter
    return _LETTERLIKE_EXTRA.get(ch)


def char_to_latex(ch: str) -> str:
    """单个字符 -> LaTeX 片段。

    字母型命令（\\alpha / \\times …）后接一个哨兵字符 \\x00，最终统一换成空格，
    这样即使后续 strip() 也不会把 `\\times G` 粘成 `\\timesG`。
    """
    cmd = _MAP.get(ch)
    if cmd is None:
        cmd = _unicode_style_char(ch)
    if cmd is None:
        cmd = _SUPSUB.get(ch)
    if cmd is None:
        cmd = _ASCII_ESCAPE.get(ch)
    if cmd is None:
        if is_unrenderable(ch):       # 控制字符 / 私有区：留占位符，绝不静默丢弃
            return MISSING_GLYPH
        if ch.isascii():
            return ch
        if unicodedata.combining(ch):  # 组合用变音符：直接丢弃，避免输出乱码
            return ""
        try:
            import contextlib
            import io as _io

            from pylatexenc.latexencode import unicode_to_latex  # type: ignore

            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                out = unicode_to_latex(ch)
            out = re.sub(r"\\ensuremath\{(.*)\}", r"\1", out)
            out = re.sub(r"\\text\{(.*)\}", r"\1", out)
            cmd = out
        except Exception:
            return ch
    if re.fullmatch(r"\\[A-Za-z]+", cmd):
        return cmd + "\x00"
    return cmd


# 组合用斜线（U+0338）等"覆盖型"字符：直接合成等价字符
_COMBINING_MAP = {
    "∈": "∉", "=": "≠", "⊂": "⊄", "⊃": "⊅", "∣": "∤", "<": "≮", ">": "≯",
    "≃": "≄", "≅": "≇", "≈": "≉", "≤": "≰", "≥": "≱", "∼": "≁", "∋": "∌",
    "≡": "≢", "⊆": "⊈", "⊇": "⊉", "∃": "∄", "⊥": "⟂",
}


def normalize_combining(text: str) -> str:
    """把 base + U+0338 之类的组合序列替换成等价字符（∉ -> ∉）。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if nxt and unicodedata.combining(nxt):
            if "\u0338" in nxt and ch in _COMBINING_MAP:
                out.append(_COMBINING_MAP[ch])
                i += 2
                continue
            i += 2  # 其它组合符直接丢掉
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def cleanup_latex(latex: str) -> str:
    """合并完成后的整段 LaTeX 清理：把"斜杠覆盖"写法还原成标准命令。"""
    s = re.sub(r"/\s*\\in\b", r"\\notin", latex)
    s = re.sub(r"/\s*\\subset\b", r"\\not\\subset", s)
    s = re.sub(r"/\s*\\equiv\b", r"\\not\\equiv", s)
    s = re.sub(r"/\s*=", r"\\neq ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s


def text_to_latex(text: str) -> str:
    """一段已知为数学的文本 -> LaTeX 片段（保留必要空格，其余交给 MathJax）。"""
    text = normalize_combining(text)
    out: list[str] = []
    for ch in text:
        if ch in "\r\n\t":
            ch = " "
        if ch == " ":
            if out and not out[-1].endswith((" ", "\x00")):
                out.append(" ")
            continue
        out.append(char_to_latex(ch))
    res = "".join(out)
    res = re.sub(r"[ \t]{2,}", " ", res)
    return res.strip()   # 结尾的 \\x00 哨兵保留，等整段合并完成后再换空格


def plain_text_escape(text: str) -> str:
    """非数学文本：转义会干扰 Markdown / MathJax 的字符。

    注意这里也处理不可渲染码位 —— 早期版本只处理了数学片段，正文里的
    控制字符/私有区字符会原样漏进最终结果，表现就是"论文里出现乱码"。
    """
    out = []
    for i, ch in enumerate(text):
        if is_unrenderable(ch):
            out.append(MISSING_GLYPH)
            continue
        if ch == "\\":
            out.append("∕")
        elif ch in "*_`$":
            out.append("\\" + ch)
        elif ch in "#>" and (i == 0 or text[i - 1] in "\n "):
            out.append("\\" + ch)
        elif ch == "<":
            out.append("&lt;")
        elif ch == "&":
            out.append("&amp;")
        else:
            out.append(ch)
    return "".join(out)


# ------------------------------------------------------------------ 行内渲染

def _span_kind(font: str, text: str, strong: bool, promote: bool) -> str:
    """判定单个 span 是 math 还是 text。"""
    stripped = text.strip()
    if not stripped:
        return "text"
    if strong:
        return "math"
    if is_math_font(font):
        letters = re.sub(r"[^A-Za-z]", "", stripped)
        # 纯英文单词（≥2 字母）通常是被排版成数学字体的正文，例如花体标题
        if len(letters) >= 2 and len(letters) == len(stripped):
            return "text"
        return "math" if promote else "text"
    # 非数学字体：只有含明确数学符号才当公式
    return "math" if math_char_ratio(stripped) >= 0.5 else "text"


def spans_to_markdown(spans: list[dict], base_size: float | None = None) -> str:
    """把一行的 span 列表拼成带 $...$ 的文本，自动还原上下标。"""
    spans = [s for s in spans if s.get("text")]
    if not spans:
        return ""

    sizes = [float(s.get("size", 10.0)) for s in spans]
    dom = max(sizes) or 10.0

    # ---- 行级数学强度：决定单字母/短片段是否按公式处理
    strong_flags = [has_math_chars(s["text"]) for s in spans]
    strong_count = sum(1 for f in strong_flags if f)
    total_chars = sum(len(s["text"]) for s in spans) or 1
    math_chars = sum(1 for s in spans for c in s["text"] if is_math_char(c))
    promote = strong_count >= 1 or (math_chars / total_chars) >= 0.12

    # 基线：取本行主字号的 span
    bases = [s["origin"][1] for s in spans if abs(float(s.get("size", dom)) - dom) < 0.35]
    baseline = sum(bases) / len(bases) if bases else spans[0]["origin"][1]

    items: list[dict] = []
    prev_x1: float | None = None
    prev_kind: str | None = None
    prev_normal: bool = False

    for s, strong in zip(spans, strong_flags):
        raw = s["text"]
        if not raw or not raw.strip():
            if items and prev_x1 is not None:
                items[-1]["prefix_space"] = True
            continue
        size = float(s.get("size", dom))
        x0, _y0, x1, _y1 = s.get("bbox", (0.0, 0.0, 0.0, 0.0))
        oy = s.get("origin", (x0, 0.0))[1]
        dy = baseline - oy

        kind = _span_kind(s.get("font", ""), raw, strong, promote)

        # 上下标：字号明显更小 + 基线偏移，且必须紧贴前一个正常片段
        gap = (x0 - prev_x1) if prev_x1 is not None else 0.0
        small = size <= dom * 0.93
        adjacent = prev_normal and prev_x1 is not None and gap < 0.5 * dom
        sup = small and dy >= dom * 0.15 and adjacent
        sub = small and dy <= -dom * 0.09 and adjacent

        prefix_space = False
        if prev_x1 is not None and gap > 0.13 * dom:
            prefix_space = True

        if sup or sub:
            frag = text_to_latex(raw)
            if not frag:
                continue
            kind = "math"
        elif kind == "math":
            frag = text_to_latex(raw)
            if not frag:
                continue
        else:
            frag = raw

        items.append({"kind": kind, "text": frag, "sup": sup, "sub": sub,
                      "prefix_space": prefix_space})
        prev_x1, prev_kind = x1, kind
        prev_normal = not (sup or sub) and bool(raw.strip())

    # ---- 合并同类片段（数学模式内空格无副作用，可安全拼接）
    merged: list[list] = []
    for it in items:
        latex = it["text"]
        if it["sup"]:
            latex = "^{" + latex + "}"
        elif it["sub"]:
            latex = "_{" + latex + "}"
        sp = " " if it["prefix_space"] else ""
        if merged and merged[-1][0] == it["kind"]:
            merged[-1][1] += sp + latex
        else:
            merged.append([it["kind"], sp + latex])

    parts: list[str] = []
    for kind, txt in merged:
        if kind == "math":
            body = cleanup_latex(txt.replace("\x00", " ")).strip()
            parts.append("$" + body + "$" if body else "")
        else:
            parts.append(plain_text_escape(txt))

    line = "".join(parts)
    line = re.sub(r"\$\s*\$", "", line)
    line = re.sub(r"\$\$+", "$", line)
    return line.strip()


def is_display_equation(markdown: str, coverage: float) -> bool:
    """判断某段是否应作为独立公式（$$...$$）渲染。"""
    stripped = markdown.strip()
    if not stripped or len(stripped) > 500:
        return False
    if coverage >= 0.6:
        return True
    # 形如 (3)  $x = y + z$  或  $x = y$  (3)
    if re.fullmatch(r"\(\d+[a-z]?\)\s*\$[^$]+\$", stripped):
        return True
    if re.fullmatch(r"\$[^$]+\$\s*\(\d+[a-z]?\)", stripped):
        return True
    return False
