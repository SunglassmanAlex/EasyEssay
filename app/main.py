"""EasyEssay 后端服务（FastAPI）。

启动：python -m app.main     或     run.bat / run.sh
打开：http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import maintain, mathify, ocr, render, store, translate
from .config import (DEFAULT_ASK_PROMPT, DEFAULT_SYSTEM_PROMPT, UPLOAD_DIR, WEB_DIR,
                     load_settings, public_settings, save_settings)
from .deepseek import DeepSeekError, make_client
from .extract import extract_pdf, extract_with_pdfplumber

app = FastAPI(title="EasyEssay —— 论文翻译助手", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    # 只放行两类来源：导出的离线 HTML（file:// 的 Origin 是字符串 "null"，它用 Bearer 令牌访问）
    # 与本机页面。**不再用通配符** —— 通配符会让任意网站都能发请求过来。
    allow_origins=["null", "http://127.0.0.1:8765", "http://localhost:8765"],
    allow_credentials=False,      # 不靠跨站 Cookie 认证，令牌走 Authorization 头
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# 运行中的翻译任务：doc_id -> {"stop": Event, "started": ts}
TASKS: dict[str, dict[str, Any]] = {}
TASK_LOCK = threading.Lock()

SESSION_COOKIE = "ee_session"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """基础安全响应头：防嗅探、防点击劫持、限制资源来源。"""
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")        # 防点击劫持（also via frame-ancestors）
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    # 页面只允许加载本站资源；脚本需要内联（页面里有内联脚本），故保留 'unsafe-inline'。
    # 导出的离线文件是 file://，不受这里约束（它自带本地 MathJax）。
    if not request.url.path.startswith("/api/"):
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data: blob:; "
                     "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                     "font-src 'self' data:; connect-src 'self'; "
                     "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    return resp


# ------------------------------------------------------------------ 页面

@app.get("/", response_class=HTMLResponse)
def index() -> Any:
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/reader", response_class=HTMLResponse)
def reader_page() -> Any:
    return FileResponse(str(WEB_DIR / "reader.html"))


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "engines": ocr.available_engines()}


# ------------------------------------------------------------------ 设置

@app.get("/api/settings")
def get_settings() -> dict:
    s = public_settings()
    s["default_prompts"] = {"translate": DEFAULT_SYSTEM_PROMPT, "ask": DEFAULT_ASK_PROMPT}
    s["ocr_engines"] = ocr.available_engines()
    return s


@app.post("/api/settings")
def post_settings(payload: dict) -> dict:
    patch = {k: v for k, v in (payload or {}).items() if v is not None}
    if patch.get("api_key") == "":
        patch.pop("api_key")
    save_settings(patch)
    return {"ok": True, "settings": public_settings()}


@app.post("/api/settings/test")
def test_settings(payload: dict | None = None) -> dict:
    """用一条极短请求验证 Key / Base URL / 模型是否可用。"""
    st = load_settings()
    st.update({k: v for k, v in (payload or {}).items() if v})
    # 只试不存：测试用的 Key 不写进设置文件
    try:
        client = make_client(st)
    except DeepSeekError as e:
        return {"ok": False, "error": str(e)}
    try:
        models = client.list_models()
        out = client.chat([{"role": "user", "content": "回复两个字：可用"}], max_tokens=16)
        return {"ok": True, "reply": out.strip()[:50], "models": models}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}
    finally:
        client.close()


# ------------------------------------------------------------------ 文档

@app.get("/api/docs")
def api_list_docs() -> list[dict]:
    return store.list_docs()


@app.get("/api/docs/{doc_id}")
def api_get_doc(doc_id: str, with_text: bool = True) -> dict:
    meta = store.get_meta(doc_id)
    if not meta:
        raise HTTPException(404, "文档不存在")
    out: dict[str, Any] = {"meta": meta}
    if with_text:
        extracted = store.load_extracted(doc_id)
        paragraphs = extracted.get("paragraphs", [])
        translations = store.load_translations(doc_id)
        # 实时判断"哪些段还没修好"，而不是只信落盘的 `unrepaired` 字段：
        # 检测规则是逐步完善起来的（先有占位符残留，后来才补上"用 ? 顶替"、
        # "用空括号顶替"），所以**旧数据**里可能藏着按当时的规则查不出的坏段。
        # 每次读取都重算一遍，这些段才会浮出来给「修复公式段」按钮。
        #
        # 只看**译文**，不看抽取文本：抽取里留着 ⟦?⟧ 并不等于没修好 —— 模型完全可以
        # 正确还原它。若把"抽取含占位符"也算进来，那些已经修好的段会被永久选中，
        # 按钮点一次重译一次、数字永不归零。只认译文里残留的问题，按钮才会收敛。
        broken: list[str] = []
        for p in paragraphs:
            rec = translations.get(p.get("id")) or {}
            if not rec:
                continue          # 还没翻译的段归「继续翻译」，不归「修复」
            if mathify.find_unrepaired(
                    (rec.get("en") or "") + (rec.get("zh") or ""),
                    # 源里没有占位符的段，译文里的 `?` 是原文自带的，不算"模型藏了问题"
                    had_placeholder=mathify.MISSING_GLYPH in (p.get("text") or "")):
                broken.append(p.get("id"))
        out["paragraphs"] = paragraphs
        out["translations"] = translations
        # 全局术语表：应用内阅读的术语高亮也用它（与导出同一份真源）
        out["glossary"] = store.load_glossary(doc_id)
        out["needs_repair"] = broken
        out["page_count"] = extracted.get("page_count")
        out["ocr_pages"] = extracted.get("ocr_pages", [])
    out["running"] = doc_id in TASKS
    return out


@app.delete("/api/docs/{doc_id}")
def api_delete_doc(doc_id: str) -> dict:
    stop_task(doc_id)
    return {"ok": store.delete_doc(doc_id)}


@app.get("/api/docs/{doc_id}/source")
def api_source(doc_id: str) -> Any:
    p = store.source_path(doc_id)
    if not p:
        raise HTTPException(404, "源文件不存在")
    return FileResponse(str(p))


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...),
                     title: str = Form(""),
                     page_from: int = Form(0),
                     page_to: int = Form(0),
                     use_ocr: bool = Form(True)) -> dict:
    name = file.filename or "upload.pdf"
    suffix = Path(name).suffix.lower()
    tmp = UPLOAD_DIR / f"{int(time.time() * 1000)}-{Path(name).name}"
    with tmp.open("wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)

    is_pdf = suffix in (".pdf", ".PDF".lower()) or tmp.read_bytes()[:4] == b"%PDF"
    doc_title = title.strip() or Path(name).stem
    doc_id = store.create_doc(doc_title, tmp, name,
                              kind="pdf" if is_pdf else "image",
                              settings={"model": load_settings().get("model")})
    # ★ 立刻置为 extracting：create_doc 的初始状态是 extracted，
    #   若不当场改掉，前端轮询可能在这段窗口里读到"已完成、0 段"。
    store.update_meta(doc_id, {"status": "extracting",
                               "page_from": page_from or None, "page_to": page_to or None})
    try:
        tmp.unlink()
    except Exception:
        pass

    def work() -> None:
        try:
            store.update_meta(doc_id, {"status": "extracting", "message": "正在解析文档…"})
            if is_pdf:
                data = extract_pdf(tmp if tmp.exists() else store.source_path(doc_id),
                                   page_from or None, page_to or None, use_ocr=use_ocr)
                if not data.get("paragraphs"):
                    try:
                        data = extract_with_pdfplumber(store.source_path(doc_id))
                    except Exception:
                        pass
            else:
                from .ocr import ocr_images_to_paragraphs

                src = store.source_path(doc_id)
                paras = ocr_images_to_paragraphs([src], engine=load_settings().get("ocr_engine", "auto"))
                data = {"title": doc_title, "paragraphs": paras, "page_count": 1,
                        "body_size": 10.0, "ocr_pages": [], "images": True}
            store.save_extracted(doc_id, data)
            patch = {"status": "extracted", "message": "", "title": title.strip() or data.get("title") or doc_title,
                     "page_count": data.get("page_count"), "ocr_pages": data.get("ocr_pages", []),
                     "body_size": data.get("body_size")}
            # 有认不出的字形就明确告诉用户（它们被标为 ⟦?⟧，翻译时由模型按上下文还原）
            n_glyph = int(data.get("glyph_issues") or 0)
            if n_glyph:
                patch["message"] = (
                    f"解析完成：{len(data.get('paragraphs') or [])} 段，其中 {n_glyph} 处字形无法辨认"
                    f"（已标为 ⟦?⟧，翻译时会按上下文还原）")
                patch["glyph_issues"] = n_glyph
            store.update_meta(doc_id, patch)
        except Exception as e:  # noqa: BLE001
            store.update_meta(doc_id, {"status": "error", "error": str(e)[:500],
                                       "message": f"解析失败：{str(e)[:150]}"})

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "id": doc_id, "status": "extracting"}


# ------------------------------------------------------------------ 翻译

def stop_task(doc_id: str) -> bool:
    with TASK_LOCK:
        t = TASKS.get(doc_id)
    if not t:
        return False
    t["stop"].set()
    return True


@app.post("/api/docs/{doc_id}/translate")
def api_translate(doc_id: str, payload: dict | None = None) -> dict:
    if not store.get_meta(doc_id):
        raise HTTPException(404, "文档不存在")
    if doc_id in TASKS:
        return {"ok": False, "error": "该文档正在翻译中", "running": True}
    p = payload or {}
    settings = {}
    for key in ("api_key", "base_url", "model", "temperature", "system_prompt",
                "target_lang", "translate_batch_size", "max_chars_per_batch",
                "fix_formula", "restore_original", "ask_model", "provider"):
        if p.get(key) is not None:
            settings[key] = p[key]
    force = bool(p.get("force"))
    only_ids = p.get("only_ids") or None
    if settings:
        # ★ 安全要点：请求里带的 api_key 只用于本次调用，**不写入服务器设置**
        #   否则用户自己的密钥会被持久化到磁盘，站长就能读到。
        save_settings({k: v for k, v in settings.items() if k != "api_key"})

    ev = threading.Event()
    with TASK_LOCK:
        TASKS[doc_id] = {"stop": ev, "started": time.time()}

    def work() -> None:
        try:
            translate.translate_document(doc_id, settings, only_ids=only_ids, force=force,
                                        should_stop=ev.is_set)
        except Exception as e:  # noqa: BLE001
            store.update_meta(doc_id, {"status": "error", "error": str(e)[:500],
                                       "message": f"翻译失败：{str(e)[:150]}"})
        finally:
            with TASK_LOCK:
                TASKS.pop(doc_id, None)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "running": True}


@app.post("/api/docs/{doc_id}/stop")
def api_stop(doc_id: str) -> dict:
    return {"ok": stop_task(doc_id)}


@app.post("/api/docs/{doc_id}/reextract")
def api_reextract(doc_id: str, payload: dict | None = None) -> dict:
    """用当前抽取器重新抽取（新增跨页续段 / 表格识别），按内容锚点保留已有译文。"""
    if not store.get_meta(doc_id):
        raise HTTPException(404, "文档不存在")
    if doc_id in TASKS:
        return {"ok": False, "error": "该文档有任务正在运行", "running": True}
    p = payload or {}
    ev = threading.Event()
    with TASK_LOCK:
        TASKS[doc_id] = {"stop": ev, "started": time.time(), "kind": "reextract"}

    def work() -> None:
        try:
            store.update_meta(doc_id, {"status": "extracting", "message": "正在重新抽取…"})
            r = maintain.reextract_document(doc_id, p.get("page_from"), p.get("page_to"))
            tr = r["stats"].get("translated", 0)
            store.update_meta(doc_id, {
                "status": "ready" if tr >= r["paragraphs"] else ("partial" if tr else "extracted"),
                "progress": tr / max(1, r["paragraphs"]),
                "message": f"重新抽取完成：{r['paragraphs']} 段，译文保留 {r['translations']} 段"
                           + (f"（{r['merged_slots']} 处合并段已拼接）" if r.get("merged_slots") else "")
                           + (f"，{len(r['orphan'])} 段未匹配" if r["orphan"] else ""),
            })
        except Exception as e:  # noqa: BLE001
            store.update_meta(doc_id, {"status": "error", "error": str(e)[:500],
                                       "message": f"重新抽取失败：{str(e)[:150]}"})
        finally:
            with TASK_LOCK:
                TASKS.pop(doc_id, None)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "running": True}


@app.post("/api/docs/{doc_id}/restore")
def api_restore(doc_id: str, payload: dict | None = None) -> dict:
    """只重建左栏原文（公式），保留已有译文——用于"译文已翻好、只想修公式"。"""
    if not store.get_meta(doc_id):
        raise HTTPException(404, "文档不存在")
    if doc_id in TASKS:
        return {"ok": False, "error": "该文档有任务正在运行", "running": True}
    p = payload or {}
    settings = {k: v for k, v in p.items()
                if k in ("api_key", "base_url", "model", "system_prompt",
                         "restore_original", "temperature", "translate_batch_size",
                         "max_chars_per_batch", "provider") and v is not None}
    if settings:
        # 同上：密钥不落盘
        save_settings({k: v for k, v in settings.items() if k != "api_key"})
    ev = threading.Event()
    with TASK_LOCK:
        TASKS[doc_id] = {"stop": ev, "started": time.time(), "kind": "restore"}

    def work() -> None:
        try:
            translate.restore_document(doc_id, settings, force=bool(p.get("force")),
                                       should_stop=ev.is_set)
        except Exception as e:  # noqa: BLE001
            store.update_meta(doc_id, {"status": "error", "error": str(e)[:500],
                                       "message": f"重建失败：{str(e)[:150]}"})
        finally:
            with TASK_LOCK:
                TASKS.pop(doc_id, None)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "running": True}


@app.post("/api/docs/{doc_id}/retranslate")
def api_retranslate(doc_id: str, payload: dict) -> dict:
    para_id = (payload or {}).get("id")
    if not para_id:
        raise HTTPException(400, "缺少段落 id")
    try:
        return {"ok": True, "item": translate.retranslate_paragraph(
            doc_id, para_id, (payload or {}).get("instruction", ""),
            {k: v for k, v in (payload or {}).items()
             if k in ("model", "temperature", "system_prompt") and v is not None})}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)[:400]) from e


# ------------------------------------------------------------------ 问答（SSE）

@app.post("/api/docs/{doc_id}/ask")
def api_ask(doc_id: str, payload: dict) -> Any:
    if not store.get_meta(doc_id):
        raise HTTPException(404, "文档不存在")
    question = (payload or {}).get("question", "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    para_id = (payload or {}).get("para_id", "")
    selection = (payload or {}).get("selection", "")
    history = (payload or {}).get("history") or []

    def gen():
        try:
            for piece in translate.ask_stream(doc_id, question, para_id, selection,
                                              history=history):
                yield "data: " + json.dumps({"delta": piece}, ensure_ascii=False) + "\n\n"
            record = {"para_id": para_id, "selection": selection, "question": question}
            try:
                store.append_qa(doc_id, record)
            except Exception:
                pass
            yield "data: " + json.dumps({"done": True}, ensure_ascii=False) + "\n\n"
        except Exception as e:  # noqa: BLE001
            yield "data: " + json.dumps({"error": str(e)[:400]}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/docs/{doc_id}/qa")
def api_qa(doc_id: str) -> list[dict]:
    return store.load_qa(doc_id)


# ------------------------------------------------------------------ 导出

@app.get("/api/docs/{doc_id}/export")
def api_export(doc_id: str, download: bool = True) -> Any:
    meta = store.get_meta(doc_id)
    if not meta:
        raise HTTPException(404, "文档不存在")
    extracted = store.load_extracted(doc_id)
    doc = {
        "id": doc_id,
        "title": meta.get("title"),
        "paragraphs": extracted.get("paragraphs", []),
        "translations": store.load_translations(doc_id),
        # 全局术语表：术语高亮的唯一真源（没有它前端会退回逐段 terms，标得满页都是）
        "glossary": store.load_glossary(doc_id),
        "meta": meta,
    }
    # 内联 MathJax：用户"导出后只拿到一个 HTML"是常态，而默认写法要 ./vendor/mathjax/
    # 这个目录（下载时并不存在），只能回退 jsdelivr —— 国内常被阻断，公式就不渲染了。
    # 内联后 2.3 MB，但双击必然能看（见 render.build_standalone_html 的说明）。
    html = render.build_standalone_html(doc, inline_mathjax=True)
    fname = render.safe_filename(f"{meta.get('title', doc_id)}-中英对照") + ".html"
    headers: dict[str, str] = {}
    if download:
        # ⚠️ 响应头必须是 **latin-1**，而文件名里有中文（`…-中英对照.html`）——
        # 直接把中文塞进 filename 会让 uvicorn 抛
        # `UnicodeEncodeError: 'latin-1' codec can't encode characters`
        # → 界面点「导出 HTML」就是 **500 Internal Server Error**（真实报障，已复现）。
        # 按 RFC 6266 给两个名字：filename 走 ASCII 回退（保底），
        # filename* 用 UTF-8 百分号编码（现代浏览器都用它，用户看到的还是中文名）。
        ascii_name = re.sub(r"[^\x20-\x7e]", "_", fname).replace('"', "_") or "easyessay.html"
        headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(fname, safe='')}"
        )
    return HTMLResponse(html, headers=headers)


@app.post("/api/docs/{doc_id}/export-file")
def api_export_file(doc_id: str, payload: dict | None = None) -> dict:
    """把离线 HTML 落到 samples/ 或指定目录（用于生成示例）。"""
    meta = store.get_meta(doc_id)
    if not meta:
        raise HTTPException(404, "文档不存在")
    extracted = store.load_extracted(doc_id)
    doc = {"id": doc_id, "title": meta.get("title"),
           "paragraphs": extracted.get("paragraphs", []),
           "translations": store.load_translations(doc_id), "meta": meta}
    out_dir = Path((payload or {}).get("out_dir") or (Path.cwd() / "samples"))
    fname = render.safe_filename(f"{meta.get('title', doc_id)}-中英对照") + ".html"
    p = render.export_doc_to_file(doc, out_dir / fname)
    return {"ok": True, "path": str(p)}


def _pick_port(preferred: int) -> int:
    """端口被占用就换一个空闲端口（双击启动时不希望因为端口冲突直接失败）。"""
    import socket

    for candidate in (preferred, 8899, 9000, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", candidate))
                return s.getsockname()[1]
        except OSError:
            continue
    return preferred


def _open_browser_later(url: str, delay: float = 1.2) -> None:
    """等服务器起来后自动打开浏览器（打包成 app 后双击即用）。"""
    import threading
    import webbrowser

    def run() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def _lan_urls(port: int) -> list[str]:
    """列出本机在局域网里的可访问地址，方便把链接发给朋友。"""
    import socket

    urls: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip not in [u.split("//")[1].split(":")[0] for u in urls]:
                urls.append(f"http://{ip}:{port}")
    except Exception:
        pass
    if not urls:
        # 退而求其次：用 UDP 探测出网卡地址（不会真的发包）
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            urls.append(f"http://{s.getsockname()[0]}:{port}")
            s.close()
        except Exception:
            pass
    return urls


def _webview_available() -> bool:
    """能不能开原生桌面窗口（装了 pywebview 且平台支持）。

    故意做成"能就用、不能用就退回浏览器"：pywebview 在 Linux 上依赖 GTK/Qt，
    缺了会 import 失败；这种情况下不能让程序起不来。
    """
    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _run_native_window(url: str, server: Any, title: str = "EasyEssay · 论文翻译助手") -> bool:
    """开一个原生窗口指向本地服务；窗口关掉就结束。失败返回 False（交给浏览器模式）。

    服务器必须在**后台线程**跑：Windows 上 pywebview 用 WinForms，要求主线程归它。
    """
    import os

    try:
        import webview
    except Exception:  # noqa: BLE001
        return False
    try:
        # 默认是关的 —— 不打开的话，应用里的「导出 HTML」在窗口模式下会被静默拦掉
        webview.settings["ALLOW_DOWNLOADS"] = True
        # 外链（例如"去申请 Key"）交给系统浏览器，别在应用窗口里打开
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:  # noqa: BLE001
        pass
    try:
        win = webview.create_window(title, url, width=1280, height=880,
                                    min_size=(940, 620))
        _wait_ready(url, timeout=25.0)
        # 测试钩子：设了 EE_WINDOW_AUTOCLOSE=<秒> 就自动关窗并退出。
        # 给自动化冒烟测试用（GUI 程序没法在 CI 里手点关闭），不影响正常使用。
        auto = os.getenv("EE_WINDOW_AUTOCLOSE")

        def _auto_close() -> None:
            time.sleep(float(auto or 0))
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

        if auto:
            threading.Thread(target=_auto_close, daemon=True).start()
        webview.start()
        # 走到这里说明窗口被关了 —— 顺手停掉服务，进程干净退出
        try:
            server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  （原生窗口打开失败，改用浏览器：{exc}）")
        return False


def _wait_ready(url: str, timeout: float = 20.0) -> None:
    """等服务真的能响应了再开窗口，否则用户会先看到一个错误页。"""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1.0):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)


def main() -> None:
    import argparse
    import os

    import uvicorn

    ap = argparse.ArgumentParser(description="EasyEssay 论文翻译助手")
    # 默认只绑本机；要让局域网的朋友访问，用 --host 0.0.0.0（或设 HOST/PORT 环境变量）
    ap.add_argument("--host", default=os.getenv("EE_HOST") or os.getenv("HOST", "127.0.0.1"),
                    help="监听地址，默认 127.0.0.1（仅本机）；朋友要用就填 0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    ap.add_argument("--open", dest="open_lan", action="store_true",
                    help="等价于 --host 0.0.0.0，方便直接把链接发给朋友")
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--no-browser", action="store_true",
                    help="不自动打开浏览器（默认会自动打开）")
    ap.add_argument("--window", dest="force_window", action="store_true",
                    help="强制用原生桌面窗口（装了 pywebview 时默认就是它）")
    ap.add_argument("--browser", action="store_true",
                    help="强制用浏览器打开，不开桌面窗口")
    args = ap.parse_args()
    if args.open_lan:
        args.host = "0.0.0.0"

    # 端口占用时自动换端口，避免双击启动直接报错
    if os.getenv("PORT") is None:
        picked = _pick_port(args.port)
        if picked != args.port:
            print(f"  （端口 {args.port} 被占用，改用 {picked}）")
        args.port = picked

    print()
    settings = load_settings()
    print("  EasyEssay 已启动")
    if not (settings.get("api_key") or "").strip() and not os.getenv("DEEPSEEK_API_KEY"):
        print("  （第一次使用：打开页面后会让你填 DeepSeek API Key，填一次即可记住）")
    print(f"    本机：   http://127.0.0.1:{args.port}")
    expose = args.host not in ("127.0.0.1", "localhost")
    if expose:
        for u in _lan_urls(args.port) or ["（未能自动识别网卡地址）"]:
            print(f"    局域网： {u}")
        print()
        print("  ⚠ 已对外监听。两点提醒：")
        print("    1) 这个模式**没有密码保护**，同一网络里的人都能打开并消耗你的 API 额度；")
        print("       只在可信网络使用（要给别人分享，请让对方下载一份在自己机器上跑）。")
        print("    2) Windows 防火墙可能拦截：首次运行会弹窗，选“允许访问”；")
        print("       没弹窗就手动放行： netsh advfirewall firewall add rule "
              "name=\"EasyEssay\" dir=in action=allow protocol=TCP localport=%d" % args.port)
    else:
        print("    （当前仅本机可访问；要让朋友连上用 --host 0.0.0.0 或 --open）")

    # 原生窗口模式（默认）：把服务放到后台线程，主线程交给窗口。
    # 注意 `--reload` 不能在窗口模式下用（reload 要求主线程跑服务器）。
    want_window = (args.force_window or _webview_available()) and not args.browser and not args.reload
    if want_window:
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        print("    窗口：   正在打开桌面窗口…（关掉窗口即退出）")
        print()
        if _run_native_window(f"http://127.0.0.1:{args.port}", server):
            return
        # 窗口没起来 → 退回浏览器模式，不能让用户对着黑屏干等
        _open_browser_later(f"http://127.0.0.1:{args.port}")
        while not getattr(server, "should_exit", False):
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                break
        return

    print()
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
