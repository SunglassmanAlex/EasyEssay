"""HTTP 接口层端到端自测（不需要真实 API Key，也不碰真实数据目录）。

这是"下载即用"版本：**没有账号、没有登录**，所有接口直接可用。
本脚本用 FastAPI TestClient 把整条链路跑一遍，回归时能立刻发现接口层被改坏。

覆盖：
  1. 无登录墙：页面与接口直接 200
  2. 安全响应头 / CORS 不放行任意站点
  3. 设置：能保存 API Key，但**接口永远不回显明文**（只回打码形式）
  4. 上传 PDF → 抽取完成（状态 + 段数）
  5. HTTP 触发翻译（provider=mock）→ ready，译文段数等于原文段数
  6. 导出 HTML：自包含、本地 MathJax、含双栏结构
  7. 问答接口（SSE 流式）
  8. 单段重译 / 重建左栏 / 重新抽取 三个维护接口可用
  9. 404 与错误码
 10. 删除文档

用法：python scripts/http_test.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.console import force_utf8  # noqa: E402
force_utf8()   # Windows 控制台默认不是 UTF-8，不切的话打印中文/✅ 会直接崩


PASS, FAIL = [], []


def sample_pdf() -> Path:
    """测试用的 PDF：优先真实论文；仓库里没有（版权原因不分发）就用合成样张。

    合成样张由 scripts/make_sample_pdf.py 现场生成，包含标题/摘要/章节/公式/表格/参考文献，
    足以覆盖抽取器的各条路径，因此在新 clone 的仓库或 CI 上也能跑通自测。
    """
    real = ROOT / "samples" / "plonk.pdf"
    if real.exists():
        return real
    synthetic = ROOT / "samples" / "sample-paper.pdf"
    if not synthetic.exists():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_pdf.py")],
                       check=True, cwd=str(ROOT))
    return synthetic



def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  —— {detail}" if detail else ""))


def wait_idle(client, doc_id: str, tries: int = 120) -> None:
    """等后台任务结束（TASKS 里还挂着时会拒绝新任务）。"""
    for _ in range(tries):
        if not client.get(f"/api/docs/{doc_id}?with_text=false").json().get("running"):
            return
        time.sleep(0.35)


def wait_status(client, doc_id: str, wanted: tuple[str, ...], tries: int = 200,
                need_paragraphs: bool = False) -> dict:
    meta: dict = {}
    for _ in range(tries):
        meta = client.get(f"/api/docs/{doc_id}?with_text=false").json()["meta"]
        if meta.get("status") in wanted:
            if not need_paragraphs or meta.get("stats", {}).get("paragraphs"):
                return meta
        time.sleep(0.35)
    return meta


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="easyessay-http-"))

    import app.config as C
    C.DATA_DIR = tmp
    C.DOCS_DIR = tmp / "docs"
    C.UPLOAD_DIR = tmp / "uploads"
    C.SETTINGS_FILE = tmp / "settings.json"
    for d in (C.DOCS_DIR, C.UPLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)

    import app.store as S
    S.DOCS_DIR = C.DOCS_DIR

    import app.main as M
    M.UPLOAD_DIR = C.UPLOAD_DIR

    from fastapi.testclient import TestClient
    client = TestClient(M.app, follow_redirects=False)
    pdf = sample_pdf()
    print(f"\n临时数据目录：{tmp}\n")

    try:
        # ------------------------------------------------------------ 1
        print("== 1. 没有登录墙 ==")
        check("GET / 直接 200（不跳登录）", client.get("/").status_code == 200)
        check("GET /reader 直接 200", client.get("/reader").status_code == 200)
        check("GET /api/docs 直接 200", client.get("/api/docs").status_code == 200)
        check("已无 /login 与 /account 页面",
              client.get("/login").status_code == 404
              and client.get("/account").status_code == 404)
        check("已无 /api/auth 接口", client.get("/api/auth/me").status_code == 404)
        # 图标：桌面窗口模式下 WebView 会请求它，缺了会在日志里刷 404
        check("favicon 可访问（窗口/标签页有图标）",
              client.get("/static/favicon.ico").status_code == 200)
        check("页面已声明 favicon 引用",
              'rel="icon"' in client.get("/").text)

        # ------------------------------------------------------------ 2
        print("\n== 2. 安全响应头 ==")
        h = client.get("/").headers
        check("x-content-type-options: nosniff", h.get("x-content-type-options") == "nosniff")
        check("x-frame-options: DENY", h.get("x-frame-options") == "DENY")
        check("页面有 CSP", "default-src 'self'" in (h.get("content-security-policy") or ""))
        check("CORS 不放行任意站点",
              client.get("/api/health", headers={"Origin": "https://evil.example"})
              .headers.get("access-control-allow-origin") is None)

        # ------------------------------------------------------------ 3
        print("\n== 3. 设置与 API Key ==")
        s0 = client.get("/api/settings").json()
        check("初始未配置 Key", s0["api_key_set"] is False, json.dumps(s0)[:90])
        secret = "sk-LOCAL-TEST-KEY-abcdefghijklmnop"
        check("保存 Key 成功",
              client.post("/api/settings",
                          json={"api_key": secret, "model": "deepseek-chat"}).status_code == 200)
        s1 = client.get("/api/settings").json()
        check("接口确认已配置", s1["api_key_set"] is True)
        check("**接口不回显明文密钥**",
              secret not in json.dumps(s1, ensure_ascii=False), s1.get("api_key_masked", ""))
        check("只返回打码形式", "*" in (s1.get("api_key_masked") or ""),
              s1.get("api_key_masked", ""))
        check("不再有 readonly 字段（单用户）", "readonly" not in s1)

        # ------------------------------------------------------------ 4
        print("\n== 4. 上传 + 抽取 ==")
        with pdf.open("rb") as fh:
            r = client.post("/api/upload",
                            files={"file": ("plonk.pdf", fh, "application/pdf")},
                            data={"page_from": "1", "page_to": "2", "use_ocr": "false",
                                  "title": "PLONK 前两页"})
        check("上传返回 doc id", r.status_code == 200 and r.json().get("id"), r.text[:100])
        doc_id = r.json()["id"]
        meta = wait_status(client, doc_id, ("extracted", "error"), need_paragraphs=True)
        check("抽取完成且有段落",
              meta["status"] == "extracted" and meta["stats"]["paragraphs"] > 10,
              json.dumps(meta["stats"]))

        # ------------------------------------------------------------ 5
        print("\n== 5. HTTP 触发翻译（mock）==")
        r = client.post(f"/api/docs/{doc_id}/translate",
                        json={"force": False, "provider": "mock", "model": "mock-translate",
                              "translate_batch_size": 8})
        check("接受翻译请求", r.status_code == 200 and r.json().get("ok"), r.text[:100])
        final = wait_status(client, doc_id, ("ready", "partial", "error"))
        check("翻译完成", final["status"] == "ready", str(final["status"]))
        check("译文段数 == 原文段数",
              final["stats"]["translated"] == final["stats"]["paragraphs"],
              json.dumps(final["stats"]))
        detail = client.get(f"/api/docs/{doc_id}").json()
        check("返回段落与译文",
              len(detail["paragraphs"]) > 10 and len(detail["translations"]) > 10)
        one = next(iter(detail["translations"].values()))
        check("译文结构含 zh", "zh" in one, json.dumps(one, ensure_ascii=False)[:80])

        # ------------------------------------------------------------ 6
        print("\n== 6. 导出 HTML ==")
        exp = client.get(f"/api/docs/{doc_id}/export?download=false")
        check("导出成功", exp.status_code == 200 and len(exp.text) > 20000, str(len(exp.text)))
        check("自包含（内联样式 + 渲染脚本）",
              "<style>" in exp.text and "renderBilingual" in exp.text and ".colhead" in exp.text)
        check("优先用本地 MathJax（离线可渲染）",
              "./vendor/mathjax/tex-svg.js" in exp.text)
        check("不含账号/令牌痕迹",
              "EASYESSay_TOKEN" not in exp.text and "/account" not in exp.text)

        # ------------------------------------------------------------ 7
        print("\n== 7. 问答（SSE）==")
        r = client.post(f"/api/docs/{doc_id}/ask",
                        json={"question": "这段在讲什么？", "para_id": "p0005"})
        check("问答返回 200", r.status_code == 200, str(r.status_code))
        check("流式返回内容", "data:" in r.text and len(r.text) > 50,
              r.text[:70].replace("\n", " "))
        check("问答记录接口可用", client.get(f"/api/docs/{doc_id}/qa").status_code == 200)

        # ------------------------------------------------------------ 8
        print("\n== 8. 维护接口 ==")
        r = client.post(f"/api/docs/{doc_id}/retranslate",
                        json={"id": "p0001", "instruction": "术语统一"})
        check("单段重译可用",
              r.status_code == 200 and (r.json().get("item") or {}).get("zh"), r.text[:90])
        r = client.post(f"/api/docs/{doc_id}/restore", json={})
        check("重建左栏可用", r.status_code == 200 and r.json().get("ok"), r.text[:80])
        wait_status(client, doc_id, ("ready", "partial", "error"))
        wait_idle(client, doc_id)          # 等上一个任务从 TASKS 里摘掉
        r = client.post(f"/api/docs/{doc_id}/reextract", json={})
        check("重新抽取可用", r.status_code == 200 and r.json().get("ok"), r.text[:80])
        wait_status(client, doc_id, ("extracted", "ready", "partial", "error"),
                    need_paragraphs=True)
        after = client.get(f"/api/docs/{doc_id}?with_text=false").json()["meta"]
        check("重抽后仍有段落", after["stats"]["paragraphs"] > 10, json.dumps(after["stats"]))
        check("重抽保留了译文", after["stats"].get("translated", 0) > 0,
              json.dumps(after["stats"]))

        # ------------------------------------------------------------ 9
        print("\n== 9. 错误码 ==")
        check("未知文档 → 404", client.get("/api/docs/not-a-doc").status_code == 404)
        check("未知文档导出 → 404",
              client.get("/api/docs/not-a-doc/export").status_code == 404)
        r = client.post("/api/settings/test", json={"api_key": ""})
        check("空 Key 测试连接 → 结构化结果（不抛 500）",
              r.status_code == 200 and "ok" in r.json(), r.text[:100])

        # ------------------------------------------------------------ 10
        print("\n== 10. 删除文档 ==")
        check("删除成功", client.delete(f"/api/docs/{doc_id}").status_code == 200)
        check("删除后 404", client.get(f"/api/docs/{doc_id}").status_code == 404)
        check("列表已空", client.get("/api/docs").json() == [])

        print(f"\n== 结果：{len(PASS)} 项通过，{len(FAIL)} 项失败 ==")
        if FAIL:
            print("失败项：" + "、".join(FAIL))
            sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
