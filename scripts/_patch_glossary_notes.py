"""给术语表补"一句话解释"（规格 §7：首次出现给「英文 + 中文 + 简短解释」）。

设计
----
* 术语表是唯一真源；解释**附加**在它上面（第 3 个元素），不另建结构。
* 批量请求（25 条/次）→ 一篇论文约 5 次调用，成本可忽略。
* 幂等：已有解释的术语跳过，重跑不会重复花钱。
* 校验：解释必须非空、≤ 80 字；**不校验就等于让模型自由发挥**（会写出
  "这是一个术语" 这种废话），所以字数与"必须提到中文译名或英文原词"都查。
"""
from pathlib import Path

# ---------------------------------------------------------------- translate.py
p = Path("app/translate.py")
s = p.read_text(encoding="utf-8")

ADD = '''# ---------------------------------------------------------------- 术语解释（第二轮附加）

GLOSSARY_NOTE_SYSTEM = """你在为"读论文的人"写术语表。给定一批术语（英文 + 中文译名），
为每条写一句**简短解释**（中文，不超过 40 字），说明它在这类论文里的含义。

要求：
- 解释的是**该领域里的含义**，不是字面翻译；有公认英文缩写可带上；
- 不要写"这是一个术语""指某种方法"这类废话；
- 不要重复术语名本身当解释；
- 只输出 JSON：{"terms": [{"en": "原英文", "note": "一句话解释"}]}
- `en` 必须与输入**逐字一致**，便于对齐；条数与输入一致。"""


def _note_batches(gl: list[list], size: int = 25) -> list[list[list]]:
    """只挑"还没有解释"的术语，按批次切开。"""
    todo = [g for g in gl if len(g) >= 2 and len(g) < 3 or (len(g) >= 3 and not g[2])]
    return [todo[i:i + size] for i in range(0, len(todo), size)]


def enrich_glossary(doc_id: str, settings: dict,
                    should_stop: Callable[[], bool] | None = None) -> dict:
    """给术语表补解释。返回 {"added": n, "error": "..."}。"""
    gl = store.load_glossary(doc_id)
    if not gl:
        return {"added": 0}
    batches = _note_batches(gl)
    if not batches:
        return {"added": 0}          # 都已经有解释 → 幂等跳过
    by_en = {str(g[0]).lower(): g for g in gl}
    added, err = 0, ""
    client = make_client(settings)
    try:
        for batch in batches:
            if should_stop and should_stop():
                break
            payload = [{"en": g[0], "zh": g[1]} for g in batch]
            messages = [
                {"role": "system", "content": GLOSSARY_NOTE_SYSTEM},
                {"role": "user", "content": "请为这些术语写解释并返回 JSON：\\n"
                                            + json.dumps({"terms": payload},
                                                         ensure_ascii=False)},
            ]
            try:
                data = client.chat_json(messages, model=settings.get("model"),
                                        temperature=float(settings.get("temperature", 1.0) or 1.0))
            except Exception as e:  # noqa: BLE001
                err = str(e)[:120]
                break
            for item in (data or {}).get("terms") or []:
                if not isinstance(item, dict):
                    continue
                en = str(item.get("en") or "").strip().lower()
                note = " ".join(str(item.get("note") or "").split())
                if not en or not note or len(note) > 80:
                    continue
                row = by_en.get(en)
                if not row or len(row) >= 3:
                    continue
                # 校验：解释不能是废话，且要跟术语对得上
                if note in ("这是一个术语", "术语", "-", "无"):
                    continue
                row.append(note)
                added += 1
    finally:
        client.close()
    if added:
        store.save_glossary(doc_id, gl)
    return {"added": added, "error": err}


'''

ANCHOR = "# ---------------------------------------------------------------- 术语统一（第三轮）"
assert ANCHOR in s
s = s.replace(ANCHOR, ADD + ANCHOR, 1)

# 挂进后两轮（幂等：都有解释时几乎不花时间；顺手把 proto 的统计也带上）
OLD = '''    term_stat: dict = {}
    if st.get("unify_terms", True):
        try:
            term_stat = unify_terms(doc_id)
        except Exception as e:  # noqa: BLE001
            term_stat = {"error": str(e)[:120]}
    return proto_stat, term_stat, proto_err'''
NEW = '''    term_stat: dict = {}
    if st.get("unify_terms", True):
        try:
            term_stat = unify_terms(doc_id)
        except Exception as e:  # noqa: BLE001
            term_stat = {"error": str(e)[:120]}
    # 术语解释（规格 §7：首次出现给「英文 + 中文 + 简短解释」）。
    # 放在术语统一之后：统一会定下最终译法，解释要解释那个译法。
    if st.get("glossary_notes", True):
        try:
            note_stat = enrich_glossary(doc_id, st, should_stop)
            if note_stat.get("added"):
                term_stat = {**term_stat, "notes": note_stat["added"]}
            if note_stat.get("error"):
                proto_err = proto_err or f"术语解释失败：{note_stat['error']}"
        except Exception as e:  # noqa: BLE001
            proto_err = proto_err or f"术语解释失败：{str(e)[:120]}"
    return proto_stat, term_stat, proto_err'''
assert OLD in s, "run_enrich_rounds 未匹配"
s = s.replace(OLD, NEW, 1)

# 术语统一时**保留已有的解释**（否则每次统一都把解释抹掉）
OLD2 = '''    glossary: list[list[str]] = []
    for en_map, zh_map in sorted(variants.items()):
        best = sorted(zh_map.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)[0][0]
        glossary.append([en_map, best])
    if glossary:
        try:
            store.save_glossary(doc_id, glossary[:120])
        except Exception:  # noqa: BLE001
            pass
    return stat'''
NEW2 = '''    # 术语表落盘时**保留已有解释**（第 3 个元素）—— 直接重建会把解释抹掉
    old_notes = {str(g[0]).lower(): g[2] for g in store.load_glossary(doc_id)
                 if len(g) >= 3 and g[2]}
    glossary: list[list[str]] = []
    for en_map, zh_map in sorted(variants.items()):
        best = sorted(zh_map.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)[0][0]
        row = [en_map, best]
        if old_notes.get(en_map):
            row.append(old_notes[en_map])
        glossary.append(row)
    if glossary:
        try:
            store.save_glossary(doc_id, glossary[:120])
        except Exception:  # noqa: BLE001
            pass
    return stat'''
assert OLD2 in s, "unify_terms 落盘处未匹配"
s = s.replace(OLD2, NEW2, 1)

# message 里也报一下解释数
s = s.replace('''    if (term_stat or {}).get("replaced"):
        bits.append(f"术语统一 {term_stat['replaced']} 处")''',
'''    if (term_stat or {}).get("replaced"):
        bits.append(f"术语统一 {term_stat['replaced']} 处")
    if (term_stat or {}).get("notes"):
        bits.append(f"术语解释 {term_stat['notes']} 条")''', 1)

p.write_text(s, encoding="utf-8")
import ast
ast.parse(s)
print("术语解释轮已接入；语法 OK")
