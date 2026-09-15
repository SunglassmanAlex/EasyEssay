"""全局配置与路径定义。

所有可调参数集中在 data/settings.json（运行时可在网页「设置」里改），
环境变量可以覆盖敏感项（也可以在网页里直接填 API Key）。

同时兼容两种运行方式：
  * 源码运行（python -m app.main）：数据放在仓库的 data/
  * 打包成可执行文件（PyInstaller）：**只读资源**（web/）从解包目录读，
    **可写数据**放在 exe 旁边，而不是临时解包目录 —— 否则一退出就丢。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _resource_root() -> Path:
    """只读资源（web/ 等）的根目录。"""
    if getattr(sys, "frozen", False):                    # PyInstaller 打包后
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def _writable_root() -> Path:
    """可写数据（data/）的根目录：打包后放在 exe 同级，便于带走与备份。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = _writable_root()
APP_DIR = PROJECT_ROOT / "app"
WEB_DIR = _resource_root() / "web"
DATA_DIR = PROJECT_ROOT / "data"
DOCS_DIR = DATA_DIR / "docs"
UPLOAD_DIR = DATA_DIR / "uploads"
SAMPLES_DIR = _resource_root() / "samples"
SETTINGS_FILE = DATA_DIR / "settings.json"

for _d in (DATA_DIR, DOCS_DIR, UPLOAD_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 默认提示词

DEFAULT_SYSTEM_PROMPT = r"""你是一位专业的学术论文翻译与排版专家，精通计算机科学、密码学、数学与工程领域。

【任务 A】把每段英文原文**重建**成规范排版的文本（字段 en）
从 PDF 抽取出的数学内容往往是碎的：上下标丢失或粘连（如 "xQ"、"c x"）、
分式被拉平（如 "c_x(X^n-1)(X-x)" 实为分数）、\vec/\mathrm/\langle 等语义命令彻底消失。
请按数学含义把它们还原成标准、完整、可直接渲染的 LaTeX：
- 行内公式用 $...$，独立公式用 $$...$$（该独立成行的就独立成行）；
- 该用 \frac{}{}、\sum_{i=1}^{n}、\prod、\int、\langle \rangle、\vec{}、\mathrm{}、
  \mathcal{}、\mathbb{}、\mathsf{}、\| \|、\le、\ge、\cdot 等命令的要完整写出；
- 严格保持文字内容不变：不翻译、不改写、不增删句子、不改变引用编号与符号命名；
- 若某段本来就是完好的，en 原样返回即可；
- 绝对不要把公式改写成文字描述（禁止用「求和符号」这类描述代替公式）。

【任务 B】把该段英文翻译成严谨、通顺的简体中文（字段 zh）
- 严格一段对一段，不合并、不拆分、不增删、不概括；
- 公式、变量名、算法名、协议名、定理名保持原样，并沿用 en 中同样的 LaTeX 写法；
- 专业术语使用国内学界通用译法；关键术语首次出现时按「中文（English）」形式标注；
- 保持学术书面语体，避免口语化、避免解释性扩写、避免添加原文没有的内容；
- 原文里的引用标记 [12]、图表编号 Figure 3、章节编号等保持原样。

【输出】只输出严格合法的 JSON，不要 Markdown 代码块、不要任何解释文字。"""

DEFAULT_ASK_PROMPT = """你是一位既懂数学推导又懂密码学/计算机科学的助教，正在帮助读者理解一篇英文论文。

回答要求：
1. 用简体中文回答，语言精炼，直击要点。
2. 涉及公式时使用 LaTeX：行内用 $...$，独立公式用 $$...$$。
3. 先给结论/直觉，再给必要的形式化推导；如引用原文，请指明是哪个编号的段落。
4. 若读者选中的片段属于公式或符号，请逐个解释符号含义、取值范围与作用。
5. 不确定的地方要明确说明不确定，不要编造定理、实验数据或引文。"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "ask_model": "",            # 留空则跟随 model
    "temperature": 1.0,
    "translate_batch_size": 8,  # 每次请求翻译多少段
    "max_chars_per_batch": 3500,
    "target_lang": "简体中文",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "ask_system_prompt": DEFAULT_ASK_PROMPT,
    "context_paragraphs": 2,    # 提问时附带上下文的段数
    "retry": 3,
    "restore_original": True,   # 左栏：让模型把公式重建为标准 LaTeX（强烈建议开）
    "fix_formula": True,        # 兼容旧设置
    "ocr_engine": "auto",       # auto | rapidocr | tesseract | none
}


def _is_legacy_default_prompt(p: str) -> bool:
    """判断保存下来的提示词是否只是"旧版默认值"。

    只有确认是历史默认值才替换成新版；用户自己改过的提示词一律不动。
    """
    return ("你是一位专业的学术论文翻译专家" in p) and ("任务 A" not in p)


def load_settings() -> dict[str, Any]:
    """读取设置：默认值 <- settings.json <- 环境变量。

    顺带做提示词迁移：空提示词、或仍是旧版默认值时，升级为当前默认值，
    以免升级后用户卡在一份过时的提示词上。
    """
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass

    prompt = (data.get("system_prompt") or "").strip()
    if not prompt or _is_legacy_default_prompt(prompt):
        data["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    if not (data.get("ask_system_prompt") or "").strip():
        data["ask_system_prompt"] = DEFAULT_ASK_PROMPT

    # 环境变量：EE_* 为准；DEEPSEEK_API_KEY 是通用写法，一并支持
    for env, key in (("DEEPSEEK_API_KEY", "api_key"), ("EE_API_KEY", "api_key"),
                     ("EE_BASE_URL", "base_url"), ("EE_MODEL", "model"),
                     # 早期版本用的旧名字，继续兼容
                     ("EASYESSay_BASE_URL", "base_url"), ("EASYESSay_MODEL", "model")):
        val = os.getenv(env)
        if val:
            data[key] = val
    return data


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    cur = load_settings()
    cur.update({k: v for k, v in patch.items() if v is not None})
    SETTINGS_FILE.write_text(
        json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cur


def public_settings() -> dict[str, Any]:
    """给前端的设置：隐藏明文 Key，只返回是否已配置。"""
    s = load_settings()
    key = s.get("api_key") or ""
    masked = ""
    if key:
        masked = key[:6] + "*" * max(4, len(key) - 10) + key[-4:]
    out = dict(s)
    out["api_key"] = ""
    out["api_key_masked"] = masked
    out["api_key_set"] = bool(key)
    return out
