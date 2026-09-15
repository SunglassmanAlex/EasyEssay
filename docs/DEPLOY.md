# 把翻译好的论文发布成网页

EasyEssay 本身是**本机应用**（下载即用，不需要服务器）。
但如果你想把某些论文的对照阅读页放到网上给别人看，可以生成一个**纯静态**的阅读库。

> 注意：阅读页是"死文件"，任何拿到链接的人都能看。**里面不含你的 API Key**，
> 但包含论文全文与译文 —— 公开前请确认没有版权/隐私顾虑。

## 1. 生成静态站点

```bash
python -m app.cli site --out site --title "我的论文阅读库" --sub "一句话副标题"
```

产出：

```
site/
├── index.html                 阅读库首页（目录，与论文页同一套排版）
├── papers/<slug>.html         每篇的对照阅读页（自包含，公式走本地 MathJax）
├── vendor/mathjax/tex-svg.js  本地 MathJax（断网/被墙也能渲染公式）
├── .nojekyll                  GitHub Pages 专用：跳过 Jekyll 处理
└── README.md
```

只导出某几篇：`--only "<文档id>,<文档id>"`。
一篇都没翻译的文档会自动跳过。

## 2. 上传到静态空间

**GitHub Pages**：把 `site/` 里的内容放到站点仓库（或其 `docs/` 目录 / `gh-pages` 分支），
在仓库 Settings → Pages 里选好来源即可。若是给仓库配自定义域名，记得保留根目录的 `CNAME` 文件。

**对象存储 / 虚拟主机**：把 `site/` 内容传到站点根目录，默认首页设为 `index.html`。

**只想发给一个朋友**：直接把 `papers/xxx.html` 和 `vendor/` 一起打包发过去，
对方双击就能看（公式照样渲染，因为 MathJax 是本地副本）。

## 3. 以后更新

重新翻译完论文后，重新跑一次 `python -m app.cli site --out site`，再把内容同步上去即可。

## 4. 想顺便改点样式

阅读页的样式由 `web/css/reader.css` 统一提供（应用内与导出文件共用）。
改完重新生成站点/导出即可。
