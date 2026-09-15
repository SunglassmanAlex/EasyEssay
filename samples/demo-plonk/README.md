# 离线效果样例

`PLONK-论文前两页-中英对照示例.html` —— **双击即可打开**，不需要启动服务、不需要 API Key。

- 内容：PLONK（IACR ePrint 2019/953）前两页共 24 段，左侧为英文原文，右侧为中文译文
  （本样例的中文为人工翻译，用于展示目标效果；正式使用时由 DeepSeek 逐段生成）。
- **左栏是「重建原文」**：24 段中有 12 段带蓝标「公式已重建」，即由模型把 PDF 抽取出的碎公式
  还原成规范 LaTeX。最典型的对比是第 21 段（正文倒数第 4 段）：
  - PDF 直抽（点顶栏「原始抽取」可见）：`$L _{x}$($X ) = c^{x}$($X^{n}-$ 1) ($X - x$ )$,$`
  - 重建后：`$$L_x(X) = c_x \cdot \frac{X^{n} - 1}{X - x}$$`
  两条一对比就能看出：分式横线、上下标在 PDF 文本层是不存在的，必须由模型按数学含义重建。
  其余像是 `$H \subset F$`、`$x\in H$`、`G$_{1}$`、`$\times G$` 这些也是同样道理。
- 术语：译文中的关键术语带虚线下划线，鼠标悬停显示英文原词。
- 交互：选中任意句子会浮出「问 AI / 解释公式 / 译得更准」；顶栏可切换对照/仅原文/仅译文、
  原始抽取、参考文献、字号、明暗。快捷键 `j`/`k`、`[`/`]`、`t`。

## 关于「问 AI」

导出文件里的「问 AI」会连回本地服务：

1. 先用 `run.bat`（或 `python -m app.main`）启动 EasyEssay；
2. 再打开本 HTML，选中句子提问即可。

本样例的文档 id 是生成时对应文档的 id，因此只要本地服务里还留着这篇文档，回答就能带上上下文。
若服务未启动，界面会提示「连不上本地 EasyEssay 服务」，其余阅读功能不受影响。

## 自己再生成一份

```bash
# 用文档库里的某篇文档生成（会带上可用的文档 id）
python scripts/make_demo.py --doc <doc_id> --first 30 --filename my-demo

# 或直接用现有的抽取结果与译文
python scripts/make_demo.py \
  --extracted extracted.json \
  --translations translations.json \
  --out samples/my-demo
```

`extracted.json` 是抽取结果快照，`translations.json` 是 `{段落id: {zh, terms}}` 形式的译文。
两者都可以手改（例如做人工校对后再导出）。

## 目录内容

| 文件 | 说明 |
|---|---|
| `PLONK-论文前两页-中英对照示例.html` | 自包含的双栏对照页面（CSS/JS 已内联） |
| `extracted.json` | 该文档的段落抽取快照（含 `$...$` 公式） |
| `translations.json` | 逐段中文译文与术语表 |
| `vendor/mathjax/tex-svg.js` | MathJax 本地副本，断网也能渲染公式 |

> 论文版权归原作者所有，此处仅用于功能演示。
