"""Overlay a 10% coordinate grid on images for manual box annotation."""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

for arg in sys.argv[1:]:
    p = Path(arg)
    im = Image.open(p).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    for i in range(1, 10):
        x, y = w * i // 10, h * i // 10
        d.line([(x, 0), (x, h)], fill=(255, 255, 0), width=1)
        d.line([(0, y), (w, y)], fill=(255, 255, 0), width=1)
        d.text((x + 2, 2), f"{i}", fill=(255, 255, 0))
        d.text((2, y + 2), f"{i}", fill=(255, 255, 0))
    out = Path("/tmp/detect_replay/grid") / p.name
    out.parent.mkdir(exist_ok=True)
    im.save(out)
    print(out, f"{w}x{h}")
