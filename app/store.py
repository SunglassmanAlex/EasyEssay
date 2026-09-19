"""文档存储：data/docs/<doc_id>/ 下按 meta / extracted / translated / qa 分文件保存。

目录结构（便于直接手改、便于出问题排查）：
    data/docs/<doc_id>/
        meta.json        文档元信息、翻译设置与进度
        extracted.json   抽取出的段落（原文，含 $...$ LaTeX）
        translated.json  {"p0001": {"zh": "...", "terms": [...]}}
        qa.jsonl         提问记录（每行一条）
        source.pdf       原始 PDF（或上传的图片）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .config import DOCS_DIR


def _slug(text: str, limit: int = 40) -> str:
    """生成目录名用的 slug：只用 ASCII，避免中文/空格在 URL 与文件系统里出问题。

    中文（或其他非 ASCII）标题会得到 `doc-<hash>` 形式，完整标题仍保存在 meta.json 里。
    """
    t = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    t = t[:limit].strip("-") or "doc"
    digest = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:4]
    return f"{t}-{digest}"


def new_doc_id(title: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(title)}"


def doc_dir(doc_id: str) -> Path:
    return DOCS_DIR / doc_id


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ meta

def create_doc(title: str, source_path: Path, source_name: str, kind: str = "pdf",
               settings: dict | None = None, owner: str = "") -> str:
    """新建文档。owner 用于账号隔离：不同账号互相看不到对方的文档。"""
    doc_id = new_doc_id(title)
    d = doc_dir(doc_id)
    d.mkdir(parents=True, exist_ok=True)
    suffix = Path(source_name).suffix or (".pdf" if kind == "pdf" else ".png")
    dest = d / f"source{suffix}"
    shutil.copy2(source_path, dest)
    meta = {
        "id": doc_id,
        "title": title,
        "owner": owner,
        "source_name": source_name,
        "source_file": dest.name,
        "source_kind": kind,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "extracted",       # extracted | translating | ready | partial | error
        "progress": 0.0,
        "message": "",
        "error": "",
        "stats": {"paragraphs": 0, "translated": 0},
        "settings": settings or {},
    }
    _write_json(d / "meta.json", meta)
    return doc_id


def get_meta(doc_id: str) -> dict:
    return _read_json(doc_dir(doc_id) / "meta.json", {})


def update_meta(doc_id: str, patch: dict) -> dict:
    meta = get_meta(doc_id)
    meta.update(patch)
    meta["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(doc_dir(doc_id) / "meta.json", meta)
    return meta


def list_docs(owner: str | None = None, include_unowned: bool = False) -> list[dict]:
    """列出文档。

    owner=None            → 全部（CLI/站点生成用）
    owner="x"             → 只返回 x 的文档
    owner="x" + include_unowned=True → 额外包含"账号体系出现之前"创建的无主文档
                                        （只有站长会用这个参数来接手历史数据）
    """
    out = []
    if not DOCS_DIR.exists():
        return out
    for d in sorted(DOCS_DIR.iterdir(), reverse=True):
        meta = _read_json(d / "meta.json", None)
        if not meta:
            continue
        doc_o = meta.get("owner", "")
        if owner is not None and doc_o != owner:
            if not (include_unowned and doc_o == ""):
                continue
        out.append(meta)
    return out


def claim_orphan_docs(owner: str) -> int:
    """把没有归属的历史文档认领给指定账号（首次创建站长账号时调用一次）。"""
    n = 0
    if not DOCS_DIR.exists():
        return 0
    for d in DOCS_DIR.iterdir():
        meta = _read_json(d / "meta.json", None)
        if meta and meta.get("owner", "") == "":
            update_meta(meta["id"], {"owner": owner})
            n += 1
    return n


def doc_owner(doc_id: str) -> str:
    return (get_meta(doc_id) or {}).get("owner", "")


def owned_by(doc_id: str, owner: str) -> bool:
    """文档是否属于该账号（历史文档 owner 为空视为站长所有）。"""
    meta = get_meta(doc_id)
    if not meta:
        return False
    doc_o = meta.get("owner", "")
    return doc_o == owner or (doc_o == "" and owner == "")


def delete_doc(doc_id: str) -> bool:
    d = doc_dir(doc_id)
    if d.exists() and d.is_dir() and str(d.resolve()).startswith(str(DOCS_DIR.resolve())):
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


# ------------------------------------------------------- extracted / translated

def save_extracted(doc_id: str, data: dict) -> None:
    _write_json(doc_dir(doc_id) / "extracted.json", data)
    update_meta(doc_id, {"stats": {
        "paragraphs": len(data.get("paragraphs", [])),
        "translated": len(load_translations(doc_id)),
    }})


def load_extracted(doc_id: str) -> dict:
    return _read_json(doc_dir(doc_id) / "extracted.json", {"paragraphs": []})


def save_glossary(doc_id: str, glossary: list[list[str]]) -> None:
    """把术语表落盘（文档级）。用途：① 重译/重抽时沿用既有译法，减少不一致；
    ② 术语统一（第三轮）的输入。"""
    _write_json(doc_dir(doc_id) / "glossary.json", glossary)


def load_glossary(doc_id: str) -> list[list[str]]:
    data = _read_json(doc_dir(doc_id) / "glossary.json", [])
    return [list(x) for x in data if isinstance(x, (list, tuple)) and len(x) >= 2]


def load_translations(doc_id: str) -> dict:
    return _read_json(doc_dir(doc_id) / "translated.json", {})


def save_translations(doc_id: str, mapping: dict) -> None:
    _write_json(doc_dir(doc_id) / "translated.json", mapping)


def merge_translations(doc_id: str, patch: dict) -> dict:
    cur = load_translations(doc_id)
    cur.update(patch)
    save_translations(doc_id, cur)
    # 顺带刷新统计（translated / rebuilt），供前端展示与"是否需要重建左栏"判断
    meta = get_meta(doc_id)
    stats = dict(meta.get("stats") or {})
    if not stats.get("paragraphs"):
        stats["paragraphs"] = len(load_extracted(doc_id).get("paragraphs", []))
    stats["translated"] = sum(1 for v in cur.values() if (v or {}).get("zh"))
    stats["rebuilt"] = sum(1 for v in cur.values() if (v or {}).get("en"))
    update_meta(doc_id, {"stats": stats})
    return cur


def append_qa(doc_id: str, record: dict) -> None:
    path = doc_dir(doc_id) / "qa.jsonl"
    record = dict(record)
    record.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_qa(doc_id: str) -> list[dict]:
    path = doc_dir(doc_id) / "qa.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def source_path(doc_id: str) -> Path | None:
    meta = get_meta(doc_id)
    if not meta:
        return None
    p = doc_dir(doc_id) / meta.get("source_file", "")
    return p if p.exists() else None


# ---------------------------------------------------------------- 全局清单（v2）

def outline_path(doc_id: str) -> Path:
    return doc_dir(doc_id) / "outline.json"


def load_outline(doc_id: str) -> dict | None:
    """读全局清单（v2 的"两段式"第一段产物）。没有就返回 None。"""
    p = outline_path(doc_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def save_outline(doc_id: str, outline: dict) -> None:
    p = outline_path(doc_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
