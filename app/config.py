"""全局配置与路径定义。

所有可调参数集中在 data/settings.json（运行时可在网页「设置」里改），
环境变量可以覆盖敏感项（也可以在网页里直接填 API Key）。

同时兼容两种运行方式：
  * 源码运行（python -m app.main）：数据放在仓库的 data/
  * 打包成可执行文件（PyInstaller）：**只读资源**（web/）从解包目录读，
    **可写数据**放在 exe 旁边，而不是临时解包目录 —— 否则一退出就丢。
"""
from __future__ import annotations

import hashlib
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
从 PDF 抽取出的数学内容往往是碎的或错的，**你要负责把它修对**，而不是照抄：
- 上下标丢失或粘连（如 "xQ"、"c x"）；
- 分式被拉平（如 "c_x(X^n-1)(X-x)" 实为分数）；
- \vec/\mathrm/\langle 等语义命令彻底消失（PDF 根本不存这些语义）；
- **`⟦?⟧` 是"无法辨认的字形"占位符**：PDF 字体表坏了，抽出的是垃圾码位。
  据实测，`⟦?⟧` 来自两类字形，都出自 LaTeX 的大符号扩展字体（CMEX10）：
  · **大型运算符**：`\sum`（最常见）、`\prod`、`\int` 以及它们的上下限；
  · **大型定界符**：矩阵的 [ ]、向量的 ( )、集合的 { }、范数 ‖ ‖、内积 ⟨ ⟩、
    分式横线；常成对出现在同一行两侧，或被拆到上下两行（大括号断成上下半截）。
  **必须结合上下文推断并补齐**。特别地，**"整段只有 ⟦?⟧、几乎看不到别的字符"的
  段落，几乎都是上一条公式被切断的碎片**（抽取器把一条公式拆成了好几段），
  单看它无从判断，要连着相邻段落当成同一条公式去读；
  同一行里成对出现的 `⟦?⟧…⟦?⟧`，中间夹着竖排元素时基本就是矩阵方括号：
  · `⟦?⟧ Y ··· ⟦?⟧ Y`（Y 竖排）→ `\left[\begin{matrix} Y \\ \vdots \\ Y \end{matrix}\right]`。
  **绝不允许在输出里保留 `⟦?⟧`；也不允许用 `?` 或一对空括号顶替** ——
  那只是把问题藏起来。请给出你最可能的具体推断。
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

【公式补全（重要）】
PDF 抽取时根号这类符号常会**丢掉参数**，你会看到 `$\sqrt$ N` 这种写法
—— `\sqrt` 后面没有花括号，表示被开方的内容没被圈进公式里。
**请根据上下文补全它**：`$O(\log N \sqrt$ N)` 应写成 `$O(\log N \sqrt{N})$`
（被开方的是紧随其后的那个 N）。
`\frac`、`\vec`、`\hat`、`\overline`、`\text` 等**必须带参数**的命令同理。
缺参数的公式在浏览器里会**直接报错并把错误文字渲染进正文**
（例如把 "Missing argument for sqrt" 当成正文显示），必须避免。

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


# 历代 DEFAULT_SYSTEM_PROMPT 的 sha1 指纹（按 strip 后计算）。
# 用途见 _is_legacy_default_prompt：识别"设置里存的是旧版默认值"，好自动升级。
#   8c63d3c8… 初版（775 字，没有任何 ⟦?⟧ 说明 —— 用户库里躺着的那份）
#   c2f24ecd… 加了 ⟦?⟧ 说明，但例子有误导（把竖排矩阵写成横排）
_LEGACY_PROMPT_SHA1 = frozenset({
    "8c63d3c8dfd9fcae8d2f1c1c3fae18c7956061f3",
    "c2f24ecdf345b1e9e2de23006dab41785b63df81",
    # 2026-09-17：加入【公式补全】段（修 `$\sqrt$ N` 这类缺参数命令）之前的版本。
    # ⚠️ 维护约定：每次改 DEFAULT_SYSTEM_PROMPT，把**上一版**的 sha1 加到这里，
    # 否则老用户的 settings.json 里存着旧提示词，永远收不到新规则。
    "87a3a2806783bd860b95c00dae1d89ea76b90958",
})


def _is_legacy_default_prompt(p: str) -> bool:
    """判断保存下来的提示词是否只是"旧版默认值"。

    只有确认是历史默认值才替换成新版；用户自己改过的提示词一律不动。

    为什么用 sha1 而不是"找某个关键词"：上一版就是这么写的，判据是
    `"你是一位专业的学术论文翻译专家" in p`，而真正的旧默认写的是
    "…翻译**与排版**专家" —— 子串对不上，于是这份旧提示词一直没被升级。
    结果用户的设置里躺着初版提示词（**完全没有 ⟦?⟧ 的说明**），模型压根不知道
    ⟦?⟧ 是什么，只能把它改写成 `?` 或空括号。指纹比对没有这个毛病。

    维护约定：**每次改动 DEFAULT_SYSTEM_PROMPT，都把它上一版的 sha1 加进来**。
    这样老用户升级后会自动换成新默认值，而不是卡在旧提示词上。
    """
    if not p:
        return False
    return hashlib.sha1(p.strip().encode("utf-8")).hexdigest() in _LEGACY_PROMPT_SHA1


_prompt_migration_done = False


def _persist_prompt_migration(system_prompt: str, ask_prompt: str) -> None:
    """把升级后的提示词写回 settings.json（每个进程最多一次）。

    只动提示词字段，其余原样保留 —— 尤其 `api_key`，绝不能碰。
    用"写临时文件 + 原子替换"避免写坏；失败就算了（内存里已是新提示词，
    不影响使用，下次启动再试），绝不因为回写失败而影响正常读取。
    """
    global _prompt_migration_done
    _prompt_migration_done = True
    try:
        disk: dict[str, Any] = {}
        if SETTINGS_FILE.exists():
            disk = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        disk["system_prompt"] = system_prompt
        if not (disk.get("ask_system_prompt") or "").strip():
            disk["ask_system_prompt"] = ask_prompt
        tmp = SETTINGS_FILE.parent / (SETTINGS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except Exception:  # noqa: BLE001
        pass


def load_settings() -> dict[str, Any]:
    """读取设置：默认值 <- settings.json <- 环境变量。

    顺带做提示词迁移：空提示词、或仍是旧版默认值时，升级为当前默认值并回写，
    以免用户一直卡在一份过时的提示词上（历史教训见 _is_legacy_default_prompt）。
    """
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass

    prompt = (data.get("system_prompt") or "").strip()
    stale = (not prompt) or _is_legacy_default_prompt(prompt)
    if stale:
        data["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    if not (data.get("ask_system_prompt") or "").strip():
        data["ask_system_prompt"] = DEFAULT_ASK_PROMPT
    if stale and not _prompt_migration_done and SETTINGS_FILE.exists():
        _persist_prompt_migration(data["system_prompt"], data["ask_system_prompt"])

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
