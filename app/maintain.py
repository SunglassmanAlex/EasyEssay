"""文档维护：用当前抽取器对已有文档重新抽取，并按"内容锚点"保住已有译文。

为什么需要它：抽取器升级后（例如新增跨页续段合并、表格识别）段落会被合并或新增，
段落 id 随之改变。若直接重抽，已翻译好的文档就会张冠李戴。
这里的做法是：用旧段落的文本前缀作为锚点，在新抽取结果里找到对应段落，再把译文搬过去；
搬不动的（原文已被合并掉）单独列出，不静默丢弃。
"""
from __future__ import annotations

import re

from . import store
from .extract import extract_pdf


def _anchor(text: str, n: int = 48) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def _concat_translations(a: dict, b: dict) -> dict:
    """两个旧段落被合并成一个新段落时，把两份译文拼起来而不是丢掉一份。"""
    out = dict(a)
    za, zb = (a.get("zh") or "").strip(), (b.get("zh") or "").strip()
    if za and zb:
        out["zh"] = za + " " + zb
    else:
        out["zh"] = za or zb
    ea, eb = (a.get("en") or "").strip(), (b.get("en") or "").strip()
    if ea or eb:
        out["en"] = (ea + " " + eb).strip()
    terms, seen = [], set()
    for t in (a.get("terms") or []) + (b.get("terms") or []):
        key = (t.get("en") or "").lower()
        if key and key not in seen:
            seen.add(key)
            terms.append(t)
    out["terms"] = terms
    return out



# ---------------------------------------------------------------- 文本体检与修复

def repair_math_text(doc_id: str) -> dict:
    r"""把**已有数据**里的公式文本修干净（不花 API，幂等）。

    修三类问题（都是"看起来能用、渲染出来已坏"）：
    1. 缺参数的 LaTeX 命令：`$\sqrt$ N` → `$\sqrt{N}$`
       （MathJax 会报 `Missing argument for sqrt` 并**把错误文字渲染进正文**）；
    2. 公式里的裸 `<` / `>`：`$a<b$` → `$a&lt;b$`（浏览器会当标签）；
    3. 落单的货币 `$`（`$4.06` → `\$4.06`，让 `$` 成对）。

    为什么要这个入口：规则是逐步完善起来的，**老数据不会自动变好**；
    重译一遍既花钱又慢，而这三类问题都是确定性的、可以就地修。
    """
    from . import mathify

    def fix(v):
        if not isinstance(v, str):
            return v
        return mathify.escape_math_angles(
            mathify.repair_bare_commands(mathify.balance_dollars(v)))

    def walk(obj) -> int:
        n = 0
        if isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, str):
                    new = fix(v)
                    if new != v:
                        obj[i] = new
                        n += 1
                else:
                    n += walk(v)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    new = fix(v)
                    if new != v:
                        obj[k] = new
                        n += 1
                else:
                    n += walk(v)
        return n

    extracted = store.load_extracted(doc_id)
    translations = store.load_translations(doc_id)
    n_ex = walk(extracted.get("paragraphs", []))
    n_tr = walk(translations)
    if n_ex:
        store.save_extracted(doc_id, extracted)
    if n_tr:
        store.save_translations(doc_id, translations)
    return {"extracted": n_ex, "translated": n_tr, "total": n_ex + n_tr}

def reextract_document(doc_id: str, page_from: int | None = None,
                       page_to: int | None = None, use_tables: bool = True) -> dict:
    """重新抽取并迁移译文，返回迁移统计。"""
    meta = store.get_meta(doc_id)
    if not meta:
        raise RuntimeError(f"文档不存在：{doc_id}")
    src = store.source_path(doc_id)
    if not src:
        raise RuntimeError("源文件不存在，无法重新抽取")

    old = store.load_extracted(doc_id)
    old_tr = store.load_translations(doc_id)
    old_paras = old.get("paragraphs", [])
    old_by_id = {p["id"]: p for p in old_paras}
    old_order = {p["id"]: i for i, p in enumerate(old_paras)}

    p_from = page_from or meta.get("page_from")
    p_to = page_to or meta.get("page_to")
    data = extract_pdf(src, p_from, p_to, use_ocr=True, use_tables=use_tables)
    new_paras = data.get("paragraphs", [])
    if not new_paras:
        raise RuntimeError("重新抽取没有得到任何段落，已放弃（原数据未改动）")

    index: dict[str, list[str]] = {}
    for p in new_paras:
        index.setdefault(_anchor(p["text"]), []).append(p["id"])

    remapped: dict[str, dict] = {}
    moved = 0
    merged_slots = 0
    stale = 0            # 段落变长、旧译文只覆盖前半截 → 作废等待重译
    orphan: list[str] = []
    # 按旧段落顺序处理，保证"两段并一段"时拼接顺序正确
    for pid, entry in sorted(old_tr.items(), key=lambda kv: old_order.get(kv[0], 1 << 30)):
        old_p = old_by_id.get(pid)
        if not old_p:
            orphan.append(pid)
            continue
        key = _anchor(old_p["text"])
        cands = index.get(key)
        if not cands:
            pref = key[:32]
            cands = [p["id"] for p in new_paras if _anchor(p["text"]).startswith(pref)]
        if not cands:
            pref = key[:24]
            cands = [p["id"] for p in new_paras if pref and pref in p["text"]]
        if not cands:
            orphan.append(pid)
            continue
        target = cands[0]
        new_p = next((p for p in new_paras if p["id"] == target), None)
        # 段落"变长了"说明内容有新增（典型情况：整段只有 ⟦?⟧ 的公式碎片被并了进来），
        # 旧译文只覆盖前半截 —— 留着会让用户看到一条缺了半边的公式，不如作废重译。
        if new_p is not None:
            old_t = (old_p.get("text") or "").rstrip()
            if new_p["text"] != old_p.get("text") and old_t and new_p["text"].startswith(old_t):
                stale += 1
                continue
        if target in remapped:
            remapped[target] = _concat_translations(remapped[target], entry)
            merged_slots += 1
        else:
            remapped[target] = entry
            if target != pid:
                moved += 1

    store.save_extracted(doc_id, data)
    store.save_translations(doc_id, remapped)
    store.merge_translations(doc_id, {})   # 刷新 stats

    meta = store.get_meta(doc_id)
    store.update_meta(doc_id, {
        "message": f"已重新抽取：{len(new_paras)} 段（原 {len(old_paras)} 段），"
                   f"译文迁移 {len(remapped)} 段",
    })
    return {
        "paragraphs": len(new_paras),
        "previous_paragraphs": len(old_paras),
        "translations": len(remapped),
        "moved": moved,
        "merged_slots": merged_slots,
        "stale": stale,
        "orphan": orphan,
        "page_count": data.get("page_count"),
        "ocr_pages": data.get("ocr_pages", []),
        "stats": meta.get("stats", {}),
    }
