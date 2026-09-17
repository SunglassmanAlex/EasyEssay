"""扫描：全库里有多少"裸 LaTeX 命令"（缺参数，会让 MathJax 直接报错）。用完即删。"""
import json
import re
from collections import Counter
from pathlib import Path

D = Path('dist/data/docs/20260916-141631-compass-encrypted-semantic-search-with-h-99cd')
tr = json.loads((D / 'translated.json').read_text(encoding='utf-8'))
ex = json.loads((D / 'extracted.json').read_text(encoding='utf-8'))

r = tr.get('p0148') or {}
en = r.get('en') or ''
i = en.find('Pacmann requires')
print("模型重建的 en:", re.sub(r'\s+', ' ', en[i:i + 130]) if i >= 0 else re.sub(r'\s+', ' ', en[:160]))
zh = r.get('zh') or ''
j = zh.find('Pacmann')
print("模型产出的 zh:", re.sub(r'\s+', ' ', zh[j:j + 130]) if j >= 0 else '')

# 缺参数的 LaTeX 命令（后面不是 { 或 [ 或字母）
CMD = r'\\(sqrt|frac|vec|hat|bar|overline|underline|text|mathrm|mathbb|mathcal|left|right|binom|stackrel|not|sum|prod|int|log|arg)\s*(?=[^\{a-zA-Z\[\(])'
BAD = re.compile(CMD)
hits = []
for p in ex['paragraphs']:
    rec = tr.get(p['id']) or {}
    for side, txt in (('en抽取', p.get('text') or ''), ('zh', rec.get('zh') or ''),
                      ('en重建', rec.get('en') or '')):
        for m in BAD.finditer(txt):
            hits.append((p['id'], side, m.group(1)))
print(f"\n全库『缺参数的命令』共 {len(hits)} 处")
if hits:
    print("  按命令统计:", Counter(h[2] for h in hits).most_common(10))
    print("  按侧统计:", Counter(h[1] for h in hits).most_common())
    for h in hits[:8]:
        print(f"    {h[0]:6} [{h[1]:6}] \\{h[2]}")
