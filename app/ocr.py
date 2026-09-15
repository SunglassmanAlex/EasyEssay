"""可选 OCR：处理扫描版 PDF 页面与图片上传。

引擎优先级（auto）：rapidocr（pip 可装、免系统依赖） > tesseract > 不可用。
未安装任何引擎时给出明确提示，而不是静默失败。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

_rapid = None
_rapid_tried = False


def _get_rapid():
    global _rapid, _rapid_tried
    if _rapid_tried:
        return _rapid
    _rapid_tried = True
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        _rapid = RapidOCR()
    except Exception:
        _rapid = None
    return _rapid


def has_tesseract() -> bool:
    try:
        import pytesseract  # type: ignore

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def available_engines() -> list[str]:
    engines = []
    if _get_rapid() is not None:
        engines.append("rapidocr")
    if has_tesseract():
        engines.append("tesseract")
    return engines


def ocr_image(path: str | Path, engine: str = "auto", lang: str = "eng") -> str:
    """对图片文件做 OCR，返回纯文本。"""
    path = str(path)
    order = ["rapidocr", "tesseract"] if engine in ("auto", "") else [engine]
    last_err = ""
    for eng in order:
        try:
            if eng == "rapidocr":
                r = _get_rapid()
                if r is None:
                    last_err = "rapidocr 未安装"
                    continue
                result, _ = r(path)
                if not result:
                    return ""
                return "\n".join(line[1] for line in result if len(line) > 1)
            if eng == "tesseract":
                import pytesseract  # type: ignore
                from PIL import Image  # type: ignore

                return pytesseract.image_to_string(Image.open(path), lang=lang)
        except Exception as e:  # noqa: BLE001
            last_err = f"{eng}: {e}"
            continue
    raise RuntimeError(
        "未找到可用的 OCR 引擎，无法识别图片/扫描页。请执行：\n"
        "  pip install -r requirements-ocr.txt\n"
        f"（最后一次错误：{last_err}）"
    )


def ocr_page(page, dpi: int = 200, engine: str = "auto", lang: str = "eng") -> str:
    """对 PDF 页面渲染后 OCR（用于扫描版 PDF）。"""
    pix = page.get_pixmap(dpi=dpi)
    tmp = Path(tempfile.gettempdir()) / f"easyessay_ocr_{page.number}.png"
    pix.save(str(tmp))
    try:
        return ocr_image(tmp, engine=engine, lang=lang)
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def ocr_images_to_paragraphs(paths: list[str | Path], engine: str = "auto",
                             lang: str = "eng") -> list[dict]:
    """把若干图片 OCR 成段落列表（供上传图片时使用）。"""
    import re

    out: list[dict] = []
    idx = 0
    for p in paths:
        text = ocr_image(p, engine=engine, lang=lang)
        for chunk in [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]:
            idx += 1
            out.append({
                "id": f"p{idx:04d}", "page": idx, "kind": "text",
                "text": re.sub(r"\s+", " ", chunk), "math_ratio": 0.0,
            })
    return out
