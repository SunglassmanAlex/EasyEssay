# EasyEssay 使用说明与排错

## 1. 安装

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows
.venv/bin/python    -m pip install -r requirements.txt      # macOS / Linux

# 可选：处理扫描件与图片（首次会自动下载 OCR 模型，约 15MB）
.venv/Scripts/python -m pip install -r requirements-ocr.txt
```

依赖说明：`PyMuPDF`（版面与字体信息，公式还原的关键）、`pdfplumber`（兜底抽取）、
`pylatexenc`（Unicode→LaTeX 兜底映射）、`fastapi/uvicorn`（服务）、`httpx`（调用 DeepSeek）。

## 2. 无需注册：填一次 API Key 就能用

这是**本机单用户**应用，没有账号体系。打开页面后：

- 第一次会弹出「开始使用」，粘贴你自己的 **DeepSeek API Key**（也可以是任何 OpenAI 兼容服务的 Key）；
- 密钥只保存在本机 `data/settings.json`，不会上传到任何地方，接口也**从不回显明文**（只回打码形式）；
- 之后随时可在右上角「设置」里改（模型、Base URL、提示词等）。

不想让 Key 落在文件里，就用环境变量：

```bash
DEEPSEEK_API_KEY=sk-xxx python easyessay.py
```

想让**同一局域网的别人**也能打开你的这份服务：`python easyessay.py --open`。
注意这个模式**没有密码保护**，同一网络里的人都能打开并消耗你的额度 —— 只在可信网络用；
要分享给别人长期使用，正确做法是让对方下载一份在自己的机器上跑。

## 3. 配置模型

三种方式，优先级从高到低：

1. 环境变量：`DEEPSEEK_API_KEY`、`EASYESSay_BASE_URL`、`EASYESSay_MODEL`
2. 网页右上角「设置」（写入 `data/settings.json`）
3. 项目根目录 `.env`（复制 `.env.example`）

只要接口是 **OpenAI 兼容** 的就能用，例如：

| 服务 | Base URL | 模型 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` / `deepseek-reasoner` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | 任意已开通模型 |
| Ollama 本地 | `http://127.0.0.1:11434/v1` | `qwen2.5:14b` 等 |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` 等 |

设置里有两条系统提示词，都可以改：

- **翻译提示词**：控制译文风格与术语策略（默认要求「一段一译、公式原样、术语标注」）。
- **提问提示词**：控制问答风格（默认「先直觉后推导、不确定要说明」）。

「翻译模型」建议用 `deepseek-chat`；「提问模型」可以填 `deepseek-reasoner` 让追问更有推理深度。
「每批段落数」默认 8 —— 调大更快但更容易触发长度上限，调小更稳但请求更多。

## 4. 日常流程

- **只翻一部分先看看**：上传时填页码范围（比如 1–5）。
- **中途停止**：阅读页或文档库点「停止」；已完成的段落已落盘，之后点「继续翻译」只补缺失段落。
- **重新翻译**：会覆盖已有译文（按钮上会二次确认）。
- **单段不满意**：选中该段译文 →「译得更准」，或直接在调 `POST /api/docs/{id}/retranslate`
  传 `{"id":"p0012","instruction":"术语 commitment 统一译作「承诺」"}`。
- **导出**：`/api/docs/{id}/export` 下载自包含 HTML。导出文件会自动尝试连回
  `http://127.0.0.1:8765`，因此本地服务在跑时依然可以「问 AI」；换机器阅读则退化为纯阅读器。

## 5. 公式相关（两步法）

PDF **只存字形，不存数学语义**——`\vec`、`\mathrm`、`\langle`、分式横线在文本层都不存在。
所以 EasyEssay 用两步：

1. **确定性抽取**（不经过模型）：字体名 + 基线偏移重建上下标 + Unicode→LaTeX 映射。
   产物随时可在阅读页顶栏点 **「原始抽取」** 查看。
2. **模型重建**（默认开启）：把①的结果交回模型，只规范化数学排版、**不改文字**，与翻译在同一次请求里返回。
   阅读页默认显示这一版，带左侧蓝标「公式已重建」。

- 设置里的 **「左栏原文重建」** 可关闭（关掉后左栏就是 PDF 直抽结果，公式会比较碎）。
- **译文已经翻好、只想修左栏公式**：点阅读页顶栏的 **「重建左栏」**（或 `--restore-only` /
  `POST /api/docs/{id}/restore`）。它只让模型返回重建后的原文、不重译，花费远低于重新翻译，
  且已有译文一字不动；重复运行不会重复处理（幂等）。
- 若某段重建结果可疑：点「原始抽取」对照，或选中该段用「问 AI → 译得更准」让它单段重来。
- 想把 EasyEssay 当"公式清理器"用：`--restore-only`，或把翻译提示词改成"只输出重建后的原文"。

## 6. 把翻译好的论文发布成网页

```bash
python -m app.cli site --out site --title "我的论文阅读库"
```

产出 `site/index.html`（阅读库目录）+ `site/papers/*.html`（每篇对照页）+ 本地 MathJax。
整个目录丢到静态空间即可；静态页不含任何密钥，可以放心公开。
想放到自己的域名/静态空间（GitHub Pages、对象存储等）的步骤见 [`DEPLOY.md`](DEPLOY.md)。

## 7. 重新抽取（升级抽取器后）

抽取器升级会让段落被合并/新增，**段落编号随之变化**。直接重抽会让已有译文张冠李戴，
所以用：

```bash
# 网页：文档库卡片上的「重新抽取」；接口：POST /api/docs/{id}/reextract
```

它按「内容锚点」把旧译文搬到新段落，两段并一段时自动**拼接**两份译文，
搬不动的会明确列出（不静默丢弃）。跑完若提示有几段待重译，点「继续翻译」即可补上。

## 8. 打包给自己/别人下载

```bash
pip install -r requirements-build.txt        # 只需 PyInstaller
python packaging/build.py --zip              # 产出 dist/EasyEssay(.exe) 与 zip
python packaging/build.py --onedir --console # 目录版 + 保留控制台（排错用）
python packaging/make_icon.py                # 重新生成应用图标（代码生成，无需素材）
```

把 zip 发给别人，解压双击即用；或者打 tag 推到 GitHub，自动为三平台构建并挂到 Release
（见 `.github/workflows/release.yml`）。

## 9. 命令行批量转换

不想开浏览器时（例如批量处理一堆 PDF）：

```bash
python -m app.cli paper.pdf --pages 1-10 -o out/          # 抽前 10 页并翻译
python -m app.cli paper.pdf --no-translate                # 只抽取，先看版面/公式
python -m app.cli paper.pdf --mock --temp -o out/x.html   # 离线跑通流程（占位译文）
python -m app.cli --help                                  # 全部参数
```

默认会把文档留在文档库里，导出后仍可在网页里继续阅读、追问；加 `--temp` 则用完即删。
`--system-prompt` / `--model` / `--batch` / `--no-formula-fix` 可覆盖设置里的默认值。

## 10. 自测（不需要 API Key）

改动代码后建议跑一遍，两条都绿再交付：

```bash
# 接口层端到端（上传→抽取→翻译→导出→问答，含 404/错误码；不需要 API Key）
python scripts/http_test.py

# 翻译流水线（分批 / 术语表 / 漏返补漏 / 断点续译 / 停止 / 单段重译 / 流式问答 / 导出）
python scripts/pipeline_test.py

# 首页「填 Key 即用」流程（jsdom 驱动真实页面）
EE_JSDOM=<jsdom 路径> node scripts/ui_smoke.js

# 前端无头渲染（jsdom 真实执行导出的 HTML：双栏对齐、术语高亮、公式、视图切换）
EE_JSDOM="<你的 node_modules>/jsdom" \
  node scripts/render_test.js "samples/demo-plonk/PLONK-论文前两页-中英对照示例.html"
```

原理：`app/mock.py` 实现了与 `DeepSeekClient` 完全相同的接口，且**故意**返回
```` ```json ```` 包裹的文本、并故意漏返某个段落 id，因此解析容错与补漏逻辑都是被真实路径验证的。
`provider: "mock"` 只是自测开关，正常使用不受影响。

## 11. 常见问题

**Q: 页面能打开但没有样式/公式是源码。**
静态资源被缓存或 MathJax CDN 不通。强制刷新（Ctrl+F5）；项目自带
`web/vendor/mathjax/tex-svg.js`，加载失败时会自动回退到本地副本。

**Q: 上传后一直「解析中」。**
超大 PDF（数百页）或勾选了 OCR 会慢。可先只传前 10 页；`data/docs/<id>/meta.json`
里有 `status` 与 `error` 字段，可直接查看失败原因。

**Q: 翻译报 401 / 402。**
`/api/settings/test` 会显示真实错误。401 是 Key 不对，402 是余额不足。

**Q: 想让某几段不翻译。**
在 `data/docs/<id>/translated.json` 里给它们写入任意译文即可跳过（或直接改该文件做人工润色，
刷新页面即时生效）。

**Q: 端口被占用。**
`python -m app.main --port 8899`，然后访问 <http://127.0.0.1:8899>。

## 12. API 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` | 上传 PDF/图片；`title`、`page_from`、`page_to`、`use_ocr` |
| GET | `/api/docs` | 文档库列表 |
| GET | `/api/docs/{id}` | 段落 + 译文 + 进度 |
| POST | `/api/docs/{id}/translate` | 开始翻译（`force=false` 只补缺失段落） |
| POST | `/api/docs/{id}/restore` | **只重建左栏公式**（不重译，保留已有译文） |
| POST | `/api/docs/{id}/stop` | 停止当前任务 |
| POST | `/api/docs/{id}/retranslate` | 按自定义要求重译某一段 |
| POST | `/api/docs/{id}/ask` | 提问，SSE 流式返回 |
| GET | `/api/docs/{id}/export` | 导出离线双栏 HTML |
| GET | `/api/docs/{id}/source` | 原始文件 |
| GET/POST | `/api/settings` | 读写设置（Key、模型、提示词…） |
| POST | `/api/settings/test` | 测试 Key / Base URL / 模型连通性 |

## 13. 数据与重置

```
data/settings.json          你的设置（含 API Key）
data/docs/<doc_id>/
    meta.json               状态、进度、统计
    source.pdf              原始文件
    extracted.json          抽取出的段落（可手改）
    translated.json          译文（可手改、可导出）
    qa.jsonl                提问记录
```

删除某篇文档：文档库卡片上的「删除」，或直接删 `data/docs/<doc_id>` 目录。
想全量重置：删除 `data/docs` 与 `data/settings.json` 即可。
