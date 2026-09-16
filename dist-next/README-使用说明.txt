EasyEssay · 论文翻译助手
================================

怎么用（三步）
--------------
1. 把本目录整个解压到一个**可写**的位置（例如 D:\\EasyEssay），不要放在压缩包里直接运行。
2. 双击 EasyEssay（Windows 下是 EasyEssay.exe）。首次运行系统防火墙可能弹窗，选“允许访问”
   （只是让应用连上它自己启动的本地服务，不允许也不影响使用）。
3. 会弹出一个应用窗口 —— 第一次让你填自己的 DeepSeek API Key
   （只保存在本机 data/settings.json，不会上传到任何地方）。

之后就能：上传 PDF → 自动逐段翻译 → 左右对照阅读 → 选中任意句子追问。
顶栏「导出 HTML」可得到可独立打开、可分享的对照阅读页。

窗口还是浏览器？
----------------
默认弹**应用窗口**。想要浏览器标签页（或窗口起不来时兜底），加参数：
  EasyEssay.exe --browser        用浏览器打开
  EasyEssay.exe --no-browser     不开浏览器，只启动服务（自己访问提示的地址）
  EasyEssay.exe --open           让同一局域网的别人也能访问（无密码，慎用）

需要什么
--------
* Windows 10/11、macOS 12+ 或 Linux（x86_64）
* 一个 DeepSeek API Key（https://platform.deepseek.com/api_keys）
* 桌面窗口在 Windows 上依赖系统自带的 WebView2（Win10/11 一般都有）；
  万一没有，程序会自己改用浏览器打开，不影响使用。
* 扫描件需要 OCR，可用 pip 装 requirements-ocr.txt 后改用源码方式运行

数据在哪
--------
程序同级目录的 data/ ：
  data/settings.json        你的设置与 API Key
  data/docs/<文档>/          原文、抽取结果、译文、问答记录
删掉 data/ 就等于恢复出厂设置；换电脑时把这个目录一起拷走即可。

常见问题
--------
* 双击没反应 / 窗口一闪而过：在终端里运行 （Windows: 在地址栏输 cmd 回车，然后输 EasyEssay.exe）
  可以看到具体报错。
* 端口被占用：程序会自动换一个端口，以浏览器实际打开的地址为准；也可 --port 9000 指定。
* 想给同一局域网的别人用：命令行加 --open（注意：这个模式没有密码保护，只在可信网络使用）。
* 想完全离线/自建模型：设置里把 Base URL 换成 https://api.siliconflow.cn/v1 或本地 Ollama 的
  http://127.0.0.1:11434/v1 即可。

本程序按 MIT 协议开源，论文版权归原作者所有。
