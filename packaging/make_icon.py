#!/usr/bin/env python
"""生成应用图标（用代码画，不依赖外部素材，可随时重生成）。

画的是 EasyEssay 的语义：**左英右中两栏 + 一条分隔线**，
配色与阅读页一致（米白底 #f7f5f1、赭石色 #7a3b1e），小尺寸下也能认出来。

用法：
    python packaging/make_icon.py            # 生成 packaging/icon.ico + icon.png
然后在打包时用：python packaging/build.py --zip（会自动带上图标）
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path(__file__).resolve().parent
BG = (247, 245, 241, 255)      # #f7f5f1 纸色
ACCENT = (122, 59, 30, 255)    # #7a3b1e 赭石
INK = (58, 58, 58, 255)        # #3a3a3a 正文色
LINE = (217, 211, 199, 255)    # #d9d3c7 分隔线


def draw(size: int = 256):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = round(size * 0.055)
    radius = round(size * 0.22)
    # 圆角底板
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius, fill=BG,
                        outline=LINE, width=max(1, round(size * 0.012)))

    # 两栏内容区
    left = pad + size * 0.10
    right = size - pad - size * 0.10
    mid = size / 2
    top = pad + size * 0.16
    row_h = size * 0.072

    # 左栏（英文：长短交替的线条，代表不等长的句子）
    widths = [0.86, 0.62, 0.78, 0.54]
    for i, w in enumerate(widths):
        y = top + i * row_h * 1.32
        d.rounded_rectangle([left, y, left + (mid - left) * w * 0.94, y + row_h * 0.52],
                            radius=row_h * 0.26, fill=INK)
    # 右栏（中文：用主题色方块，暗示"译文"）
    for i in range(len(widths)):
        y = top + i * row_h * 1.32
        w = 0.9 if i % 2 == 0 else 0.66
        d.rounded_rectangle([mid + size * 0.035, y, mid + size * 0.035 + (right - mid) * w * 0.92,
                             y + row_h * 0.52], radius=row_h * 0.26, fill=ACCENT)

    # 中间分隔线（与阅读页一致的那条竖线）
    d.rounded_rectangle([mid - size * 0.006, top - size * 0.03,
                         mid + size * 0.006, top + size * 0.42],
                        radius=size * 0.006, fill=LINE if size > 64 else ACCENT)

    # 底部一道下划线，作为聚焦点
    d.rounded_rectangle([left, size - pad - size * 0.16,
                         right, size - pad - size * 0.16 + size * 0.035],
                        radius=size * 0.018, fill=ACCENT)
    return img


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("需要 Pillow：pip install pillow")
        return 2

    png = draw(512)
    png_path = OUT_DIR / "icon.png"
    png.save(png_path)

    ico_path = OUT_DIR / "icon.ico"
    png.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                              (128, 128), (256, 256)])
    print(f"已生成：{png_path}")
    print(f"已生成：{ico_path}（含 16–256 多尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
