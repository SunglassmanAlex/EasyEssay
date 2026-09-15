"""静态站点生成：把文档库里所有已导出的对照阅读页汇总成一个站点。

用途：托管到任意静态空间（原来的 Hexo 托管、GitHub Pages、对象存储…），
用域名直接访问「论文阅读库」。页面与导出的单篇 HTML 同一套排版标准。

用法：
    python -m app.cli site --out site
生成：
    site/index.html                       阅读库首页（目录）
    site/papers/<标题>-中英对照.html        每篇的对照阅读页
    site/vendor/mathjax/tex-svg.js         本地 MathJax（断网也能渲染公式）
"""
from __future__ import annotations

import hashlib
import html
import re
import shutil
from pathlib import Path

from . import render, store
from .config import WEB_DIR

SITE_CSS = """
:root{
  --bg:#f7f5f1;--fg:#1f1f1f;--muted:#8a837a;--line:#d9d3c7;--rowline:#e7e2d9;
  --accent:#7a3b1e;--eqbg:#efece5;--card:#fffdfa;--box:#fbfaf7;--boxbd:#cfc7b8;
  --font:"Times New Roman",Times,"Nimbus Roman","Liberation Serif","Microsoft YaHei","PingFang SC","Noto Sans CJK SC",serif;
}
body.dark{
  --bg:#1b1a18;--fg:#e9e6e1;--muted:#9a938a;--line:#3b3733;--rowline:#2b2926;
  --accent:#e6a97c;--eqbg:#26241f;--card:#211f1d;--box:#211f1d;--boxbd:#413c36;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--fg);line-height:1.8;font-family:var(--font);font-size:15.5px;transition:background .2s,color .2s;}
.wrap{max-width:1080px;margin:0 auto;padding:34px 26px 90px;}
header{border-bottom:2px solid var(--line);padding-bottom:16px;margin-bottom:22px;}
header h1{font-size:24px;margin:0 0 6px;font-weight:650;letter-spacing:.4px;}
header .sub{font-size:14.5px;color:var(--muted);margin:0 0 8px;}
header .hint{font-size:13.2px;color:var(--muted);margin:0;line-height:1.75;}
h2.sec{font-size:13px;letter-spacing:1.4px;text-transform:uppercase;color:var(--muted);margin:26px 0 10px;font-weight:700;}
.item{display:block;text-decoration:none;color:inherit;border:1px solid var(--boxbd);background:var(--card);
      border-radius:8px;padding:14px 16px;margin-bottom:12px;transition:border-color .15s,transform .15s;}
.item:hover{border-color:var(--accent);transform:translateY(-1px);}
.item .t{font-size:16.5px;font-weight:650;margin-bottom:4px;line-height:1.5;}
.item .z{font-size:14.5px;color:var(--accent);margin-bottom:6px;}
.item .m{font-size:12.8px;color:var(--muted);}
.item .m span{margin-right:12px;white-space:nowrap;}
.badge{display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid var(--boxbd);color:var(--muted);}
.badge.ok{border-color:#7ba05b;color:#5d7f42;}
.badge.part{border-color:#c9a227;color:#96790d;}
.empty{border:1px dashed var(--boxbd);border-radius:8px;padding:18px;color:var(--muted);font-size:14px;}
.tgl{position:fixed;right:18px;bottom:18px;border:1px solid var(--line);background:var(--eqbg);color:var(--fg);
     border-radius:20px;padding:7px 14px;font-size:13px;cursor:pointer;font-family:inherit;}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);font-size:12.8px;color:var(--muted);}
@media (max-width:700px){.wrap{padding:20px 16px 80px;}}
"""


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=True)


def _strip_math(s: str) -> str:
    return re.sub(r"\$[^$]*\$", "", str(s or "")).strip()


def _slug(text: str, limit: int = 56) -> str:
    """给公网用的 ASCII 文件名：中文标题会退化成 slug-<hash>（标题仍完整显示在页面里）。"""
    t = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    t = t[:limit].strip("-") or "paper"
    digest = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:6]
    return f"{t}-{digest}"


def build_site(out_dir: str | Path, only_ids: list[str] | None = None,
               site_title: str = "论文阅读库", site_sub: str = "",
               skip_empty: bool = True, domain: str = "",
               include_all: bool = False) -> dict:
    """把文档渲染成静态站点（公网页面）。

    ★ 隐私底线：默认**只导出站长自己的文档**（含账号体系之前的无主文档）。
      其他账号（朋友）的论文绝不会因为生成站点而被公开 —— 除非显式 `include_all=True`。
    """
    out = Path(out_dir)
    papers = out / "papers"
    papers.mkdir(parents=True, exist_ok=True)

    all_docs = store.list_docs()
    docs = all_docs
    others = 0
    if only_ids:
        docs = [d for d in docs if d["id"] in set(only_ids)]

    entries = []
    for meta in docs:
        doc_id = meta["id"]
        extracted = store.load_extracted(doc_id)
        paragraphs = extracted.get("paragraphs", [])
        if not paragraphs:
            continue
        translations = store.load_translations(doc_id)
        if not skip_empty and not any((v or {}).get("zh") for v in translations.values()):
            continue          # 一篇都没译的文档不进阅读库
        doc = {"id": doc_id, "title": meta.get("title"), "paragraphs": paragraphs,
               "translations": translations, "meta": meta}
        # 文件名用 ASCII（中文/空格在公网 URL 里既难看又容易出问题）
        fname = _slug(meta.get("title") or doc_id) + ".html"
        # 公网静态页：关闭「问 AI」入口（没有后端可连），MathJax 指向上级目录的本地副本
        render.export_doc_to_file(doc, papers / fname, enable_ask=False,
                                  mathjax_local="../vendor/mathjax/tex-svg.js")
        stats = meta.get("stats") or {}
        zh_title = _strip_math((translations.get("p0001") or {}).get("zh") or "")
        entries.append({
            "id": doc_id,
            "title": meta.get("title") or doc_id,
            "zh_title": zh_title,
            "file": "papers/" + fname,
            "pages": meta.get("page_count") or extracted.get("page_count"),
            "paragraphs": stats.get("paragraphs") or len(paragraphs),
            "translated": stats.get("translated") or len(translations),
            "rebuilt": stats.get("rebuilt") or 0,
            "created": meta.get("created_at") or "",
            "status": meta.get("status") or "",
        })

    # 本地 MathJax，保证断网也能渲染公式
    mj_src = WEB_DIR / "vendor" / "mathjax" / "tex-svg.js"
    if mj_src.exists():
        (out / "vendor" / "mathjax").mkdir(parents=True, exist_ok=True)
        shutil.copy2(mj_src, out / "vendor" / "mathjax" / "tex-svg.js")

    items = []
    for e in entries:
        done = e["translated"] >= e["paragraphs"] and e["paragraphs"] > 0
        badge = ('<span class="badge ok">全文已译</span>' if done
                 else f'<span class="badge part">已译 {e["translated"]}/{e["paragraphs"]} 段</span>')
        meta_bits = []
        if e["pages"]:
            meta_bits.append(f'<span>原文 {e["pages"]} 页</span>')
        meta_bits.append(f'<span>{e["paragraphs"]} 段</span>')
        if e["rebuilt"]:
            meta_bits.append(f'<span>公式重建 {e["rebuilt"]} 段</span>')
        if e["created"]:
            meta_bits.append(f'<span>{_esc(e["created"][:10])}</span>')
        items.append(f"""  <a class="item" href="{_esc(e['file'])}">
    <div class="t">{_esc(e['title'])}</div>
    {(f'<div class="z">{_esc(e["zh_title"])}</div>') if e['zh_title'] else ''}
    <div class="m">{badge} {''.join(meta_bits)}</div>
  </a>""")

    body = "\n".join(items) if items else '<div class="empty">文档库还是空的：先在 EasyEssay 里上传并翻译论文，再生成站点。</div>'
    total = len(entries)
    done_n = sum(1 for e in entries if e["translated"] >= e["paragraphs"] and e["paragraphs"])
    sub = site_sub or f"{total} 篇 · 其中 {done_n} 篇全文译完"
    index = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(site_title)}</title>
<style>{SITE_CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>{_esc(site_title)}</h1>
  <p class="sub">{_esc(sub)}</p>
  <p class="hint">每篇都是「左英文原文 / 右中文译文」逐段对齐的阅读页：左侧页栏标出原文页码，
  公式由 MathJax 渲染，选中任意句子可复制或提问（需在 EasyEssay 应用内打开时可用）。
  右下角按钮可切换深/浅色。</p>
</header>
<h2 class="sec">全部论文</h2>
{body}
<footer>由 EasyEssay 生成 · 论文版权归原作者所有，此处仅供个人学习阅读</footer>
</div>
<button class="tgl" onclick="document.body.classList.toggle('dark')">切换深/浅色</button>
</body>
</html>
"""
    (out / "index.html").write_text(index, encoding="utf-8")

    # GitHub Pages 需要的两份标记文件
    (out / ".nojekyll").write_text("", encoding="utf-8")
    if domain:
        (out / "CNAME").write_text(domain.strip() + "\n", encoding="utf-8")
    (out / "README.md").write_text(
        f"""# {site_title}

由 EasyEssay 生成的静态「论文阅读库」，直接部署到静态空间即可访问。

- `index.html` —— 阅读库首页（目录）
- `papers/*.html` —— 每篇的中英对照阅读页（自包含，公式由 `vendor/mathjax` 本地渲染）
- `vendor/mathjax/tex-svg.js` —— MathJax 本地副本，离线/被墙也能渲染公式
- `.nojekyll` —— GitHub Pages 专用：跳过 Jekyll 处理
- `CNAME` —— 自定义域名（GitHub Pages 用它绑定域名）

重新生成：`python -m app.cli site --out site`
""", encoding="utf-8")

    return {"out": str(out), "papers": total, "index": str(out / "index.html"),
            "domain": domain, "skipped_other_users": others,
            "files": [str(p.relative_to(out)) for p in sorted(out.rglob("*")) if p.is_file()]}
