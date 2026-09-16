"""导出：把一篇文档渲染成**可独立打开**的双栏 HTML（内联 CSS/JS，公式走 MathJax）。

生成的 HTML 既可直接双击打开当离线文档读，若 EasyEssay 服务在运行，
文件里的「问 AI」面板还会自动连回本地服务（默认 127.0.0.1:8765）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import WEB_DIR

# 本地 MathJax 优先：国内访问 jsdelivr 常被阻断，且被阻断时 onerror 不一定触发，
# 所以默认直接用随文件分发的本地副本，CDN 只作为本地缺失时的兜底。
MATHJAX_CDN = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"
MATHJAX_LOCAL = "./vendor/mathjax/tex-svg.js"

_JSON_SAFE = [("</", "<\\/"), ("<!--", "<\\!--")]


def _read(rel: str, default: str = "") -> str:
    p = WEB_DIR / rel
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return default


def _embed_json(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    for a, b in _JSON_SAFE:
        s = s.replace(a, b)
    return s


def build_standalone_html(doc: dict, api_base: str = "http://127.0.0.1:8765",
                          author_note: str = "", enable_ask: bool = True,
                          mathjax_local: str | None = None,
                          inline_mathjax: bool = False) -> str:
    """doc: {id, title, paragraphs, translations, meta?}

    enable_ask=False 用于发布到公网的静态页：隐藏「问 AI」入口，避免出现"连不上本地服务"。

    inline_mathjax=True 时把 MathJax **整个内联**进 HTML（约 +2 MB）。
    用于"下载一个文件就能双击看"的场景：默认走 `./vendor/mathjax/tex-svg.js`，
    但用户单独下载 HTML 时并不存在这个目录，只能回退到 jsdelivr CDN ——
    而国内访问 jsdelivr 常被阻断，公式就不渲染了。内联后无网也能看。
    （站点/示例那种能一并分发 vendor 目录的场景，保持外链即可，省体积。）
    """
    css = _read("css/reader.css")
    md_js = _read("js/md.js")
    bil_js = _read("js/bilingual.js")
    ask_js = _read("js/askpanel.js")

    meta = doc.get("meta") or {}
    title = doc.get("title") or meta.get("title") or "EasyEssay"
    body_data = {
        "id": doc.get("id", ""),
        "title": title,
        "paragraphs": doc.get("paragraphs", []),
        "translations": doc.get("translations", {}),
        "meta": {
            "source_name": meta.get("source_name", ""),
            "page_count": meta.get("page_count", ""),
            "model": (meta.get("settings") or {}).get("model", ""),
            "created_at": meta.get("created_at", ""),
            "stats": meta.get("stats", {}),
        },
    }
    stats = body_data["meta"]["stats"] or {}
    enable_ask_js = "true" if enable_ask else "false"
    mj_local = mathjax_local if mathjax_local is not None else MATHJAX_LOCAL
    # 默认：本地副本优先，CDN 仅作回退（拼字符串而不是 f-string，免得引号嵌套出错）
    _fallback = ("(function(){var s=document.createElement('script');s.src='"
                 + MATHJAX_CDN + "';document.head.appendChild(s);})()")
    mj_tag = '<script src="' + mj_local + '" onerror="' + _fallback + '"></script>'
    if inline_mathjax:
        src_js = WEB_DIR / "vendor" / "mathjax" / "tex-svg.js"
        if src_js.exists():
            # 内联时不需要回退：本地就是最可靠的来源
            mj_tag = "<script>" + src_js.read_text(encoding="utf-8") + "</script>"
        else:
            mj_tag = '<script src="' + MATHJAX_CDN + '"></script>'
    # 导出文件用 file:// 打开时带不上 Cookie，所以把「限定到本文档」的令牌嵌进来，
    # 前端以 Authorization: Bearer 发送（可在「账号」页撤销）。
    ask_btn = ('<button class="ee-btn" data-act="toggle-ask">问 AI</button>'
               if enable_ask else '')
    ask_panel = ('<div class="ee-ask" id="ee-ask" hidden></div>' if enable_ask
                 else '<div class="ee-ask" id="ee-ask" hidden></div>')
    note = author_note or (
        f"共 {stats.get('paragraphs', len(body_data['paragraphs']))} 段，"
        f"已译 {stats.get('translated', len(body_data['translations']))} 段"
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)} — EasyEssay 中英对照</title>
<style>
{css}
</style>
</head>
<body class="ee-standalone" data-api-base="{api_base}">
<header class="ee-topbar">
  <div class="ee-topbar-left">
    <span class="ee-logo">EasyEssay</span>
    <span class="ee-doc-title" title="{_html_escape(title)}">{_html_escape(title)}</span>
    <span class="ee-note">{_html_escape(note)}</span>
  </div>
  <div class="ee-topbar-actions">
    <button class="ee-btn" data-act="view-all" title="显示原文+译文">对照</button>
    <button class="ee-btn" data-act="view-en" title="只显示英文原文">仅原文</button>
    <button class="ee-btn" data-act="view-zh" title="只显示中文译文">仅译文</button>
    <button class="ee-btn" data-act="toggle-raw" title="在「重建原文」与「PDF 直抽原文」之间切换">原始抽取</button>
    <button class="ee-btn" data-act="toggle-ref" title="显示/隐藏参考文献">参考文献</button>
    <button class="ee-btn" data-act="font-minus">A-</button>
    <button class="ee-btn" data-act="font-plus">A+</button>
    <button class="ee-btn" data-act="toggle-font" title="在衬线（Times）与无衬线字体之间切换">字体</button>
    <button class="ee-btn" data-act="toggle-theme">明/暗</button>
    {ask_btn}
  </div>
</header>
<div class="ee-progress"><div class="ee-progress-bar" style="width:100%"></div></div>
<main class="ee-reader" id="ee-reader"></main>
{ask_panel}
<script>
window.MathJax = {{
  tex: {{
    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
    processEscapes: true,
    tags: 'none'
  }},
  svg: {{ fontCache: 'global' }},
  options: {{ skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'] }}
}};
window.EASYESSay_API = {json.dumps(api_base)};
window.EASYESSay_STANDALONE = true;
</script>
{mj_tag}
<script>
{md_js}
</script>
<script>
{bil_js}
</script>
<script>
{ask_js}
</script>
<script id="ee-doc-data" type="application/json">{_embed_json(body_data)}</script>
<script>
(function () {{
  var doc = JSON.parse(document.getElementById('ee-doc-data').textContent);
  var reader = document.getElementById('ee-reader');
  var ENABLE_ASK = {enable_ask_js};
  if (ENABLE_ASK) {{
    EasyEssay.askPanel.mount(document.getElementById('ee-ask'), {{
      docId: doc.id,
      apiBase: window.EASYESSay_API
    }});
  }}
  EasyEssay.renderBilingual(reader, doc, {{
    viewMode: 'all',
    onAsk: ENABLE_ASK ? function (payload) {{ EasyEssay.askPanel.open(payload); }} : null
  }});
  EasyEssay.bindToolbar(document.querySelector('.ee-topbar'), reader);
}})();
</script>
</body>
</html>
"""


def _html_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def export_doc_to_file(doc: dict, out_path: str | Path, **kw) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_standalone_html(doc, **kw), encoding="utf-8")
    return out


def safe_filename(name: str, limit: int = 60) -> str:
    n = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip() or "easyessay"
    return n[:limit]
