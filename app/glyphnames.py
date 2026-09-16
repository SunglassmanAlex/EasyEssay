"""按 PDF 内嵌字体的**字形名**还原数学符号（不猜，查表）。

## 为什么需要这个模块

LaTeX 排版的论文里，大号括号、求和号、大 ⊕ 这些符号来自扩展字体 CMEX10。
这类 PDF 的 ToUnicode 表常常是坏的：PyMuPDF 抽出来的是控制字符（`\\x10`、`\\x11`）
或私有区码位（`\\uf8ee`），甚至干脆无声丢弃。

但**字体本身没坏** —— Type1 字体的 `/Encoding` 数组里写着每个码位的**字形名**，
名字是可读的 TeX 名：`parenleftBig`、`summationtext`、`circleplusdisplay`、
`bracketlefttp` 等等。也就是说：

    ⟦?⟧ 到底是什么，不需要模型猜，查一次字体就知道。

这正是本项目"不要 OCR 说什么就是什么"的落点：能把信息从 PDF 里确定性挖出来的，
就不要丢给模型推断。

## 实测样例（用户论文 Private and Verifiable Outsourcing…，CMEX10 子集）

    0x10 parenleftBig        大左括号      -> \\big(
    0x11 parenrightBig       大右括号      -> \\big)
    0x4D circleplusdisplay   大号 ⊕        -> \\bigoplus
    0x50 summationtext       求和号        -> \\sum
    0x32/0x34/0x36 bracketlefttp/bt/ex    大 [ 的拼接片段
    0x33/0x35/0x37 bracketrighttp/bt/ex   大 ] 的拼接片段

## 为什么"拼接片段"故意不映射

`bracketlefttp`/`bt`/`ex` 是一个大 `[` 被拆成的上/下/中三块，正常公式里它们
合起来才是一个 `[`。逐块映射会输出 `[[[`，比占位符更糟。这类片段保持 ⟦?⟧，
交给模型按上下文还原 —— 实测模型对"成对片段 = 方括号"的判断是可靠的。
"""
from __future__ import annotations

import re
from typing import Any

# 字形名 -> LaTeX。只收录**一个字形就是一个符号**、且不依赖配对的：
# 用 \big( / \big) 这类自包含命令，避免出现 \left 没有 \right 的渲染报错。
NAME_TO_LATEX: dict[str, str] = {
    # ---- 大型定界符
    "parenleftBig": r"\big(",
    "parenrightBig": r"\big)",
    "parenleftbigg": r"\big(",
    "parenrightbigg": r"\big)",
    "parenleftBigg": r"\Big(",
    "parenrightBigg": r"\Big)",
    "bracketleftBig": r"\big[",
    "bracketrightBig": r"\big]",
    "bracketleftbigg": r"\big[",
    "bracketrightbigg": r"\big]",
    "braceleftBig": r"\big\{",
    "bracerightBig": r"\big\}",
    "braceleftbigg": r"\big\{",
    "bracerightbigg": r"\big\}",
    "floorleft": r"\lfloor",
    "floorright": r"\rfloor",
    "ceilingleft": r"\lceil",
    "ceilingright": r"\rceil",
    "bar": "|",
    "bardbl": r"\|",
    # ---- 大型运算符
    "summationtext": r"\sum",
    "summationdisplay": r"\sum",
    "producttext": r"\prod",
    "productdisplay": r"\prod",
    "integraltext": r"\int",
    "integraldisplay": r"\int",
    "integraloptext": r"\int",
    "uniontext": r"\bigcup",
    "uniondisplay": r"\bigcup",
    "intersectiontext": r"\bigcap",
    "intersectiondisplay": r"\bigcap",
    "circledottext": r"\bigodot",
    "circledotdisplay": r"\bigodot",
    "circleplustext": r"\oplus",
    "circleplusdisplay": r"\bigoplus",
    "circlemultiplytext": r"\otimes",
    "circlemultiplydisplay": r"\bigotimes",
    "logicalandtext": r"\bigwedge",
    "logicalanddisplay": r"\bigwedge",
    "logicalortext": r"\bigvee",
    "logicalordisplay": r"\bigvee",
}

# 只对这些字体做名字解析：CMEX 系列就是大符号扩展字体。
# 其它字体（CMR/CMBX/CMMI…）的码位本来就是正常字符，不需要也不能替换。
_EXT_FONT_RE = re.compile(r"CMEX", re.I)
# 子集字体的名字带 6 位子集前缀（`VFYNJW+CMEX10`），而 span 里往往只有 `CMEX10`，
# 两边必须先规范化，否则查表永远落空（这个坑踩过一次）。
# 按 PDF 规范是 6 个大写字母，这里容忍数字，免得少数生成器写的名字对不上。
_SUBSET_PREFIX_RE = re.compile(r"^[A-Z0-9]{6}\+")


def _norm_font(name: str) -> str:
    """去掉子集前缀并统一大小写，用于字体名匹配。"""
    return _SUBSET_PREFIX_RE.sub("", (name or "").strip()).lower()


def _parse_encoding(pfa: bytes) -> dict[int, str]:
    """从 Type1 字体的 /Encoding 数组里解析 {码位: 字形名}。"""
    txt = pfa.decode("latin-1", "replace")
    i = txt.find("/Encoding")
    if i < 0:
        return {}
    seg = txt[i:i + 8000]
    j = seg.find("array")
    if j < 0:
        return {}
    body = seg[j + 5:]
    rows = re.findall(r"dup\s+(\d+)\s*/([A-Za-z0-9_.]+)\s+put", body)
    if rows:
        return {int(n): nm for n, nm in rows}
    # 另一种写法：256 array 后面紧跟 256 个 /name
    return {k: nm for k, nm in enumerate(re.findall(r"/([A-Za-z0-9_.]+)", body))}


def collect_encodings(doc: Any) -> dict[str, dict[int, str]]:
    """收集文档里所有 CMEX 系列字体的 {字体名(已规范化): {码位: 字形名}}。

    按 xref 缓存，同一子集在多页复用时只解析一次。
    """
    out: dict[str, dict[int, str]] = {}
    cache: dict[int, dict[int, str]] = {}
    try:
        pages = range(doc.page_count)
    except Exception:  # noqa: BLE001
        return out
    for pno in pages:
        try:
            fonts = doc[pno].get_fonts(full=True)
        except Exception:  # noqa: BLE001
            continue
        for f in fonts:
            xref, base = f[0], f[3]
            if not _EXT_FONT_RE.search(str(base)):
                continue
            key = _norm_font(str(base))
            if key in out:
                continue
            if xref not in cache:
                try:
                    _, _, _, buf = doc.extract_font(xref)
                    cache[xref] = _parse_encoding(buf) if buf else {}
                except Exception:  # noqa: BLE001
                    cache[xref] = {}
            if cache[xref]:
                out[key] = cache[xref]
    return out


def make_resolver(font: str, encodings: dict[str, dict[int, str]]):
    """为某个字体造一个 "字符 -> LaTeX" 解析器；认不出返回 None（交回原逻辑）。

    只在**码位就是原始码位**时才解析。CMEX 这类字体的 ToUnicode 对控制字符/ASCII
    区是"恒等映射"（`\\x10` -> U+0010、`M` -> U+004D），所以码位可以直接当索引；
    而私有区码位（U+F8EE 等）对应的真实码位已被 ToUnicode 抹掉，查不到名字，
    会安全地落回 None。
    """
    # 双保险：只有 CMEX 系列才解析。收集阶段已经过滤过，这里再拦一道 ——
    # 万一别处传进来一张普通字体的编码表，也不该把正文里的 'M' 换成 \bigoplus。
    if not _EXT_FONT_RE.search(font or ""):
        return None
    enc = encodings.get(_norm_font(font))
    if not enc:
        return None

    def resolve(ch: str) -> str | None:
        code = ord(ch)
        if code > 0xFF:
            return None
        name = enc.get(code)
        if not name:
            return None
        return NAME_TO_LATEX.get(name) or NAME_TO_LATEX.get(name.lower())

    return resolve
