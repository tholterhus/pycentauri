"""Contact sheet of collected frames with timestamps + detector scores."""

import sys
from pathlib import Path

sys.path.insert(0, "/opt/pycentauri/src")

from PIL import Image, ImageDraw

from pycentauri.detect.backend import Detector

pattern, out = sys.argv[1], sys.argv[2]
force_cpu = "--cpu" in sys.argv
files = sorted(Path("/").glob(pattern.lstrip("/")))
det = Detector.from_model(
    Path("/opt/pycentauri/data/models/spaghetti_edgetpu.tflite"), force_cpu=force_cpu
)

COLS, TW = 9, 200
th = 120
rows = (len(files) + COLS - 1) // COLS
sheet = Image.new("RGB", (COLS * TW, rows * (th + 14)), (20, 20, 20))
draw = ImageDraw.Draw(sheet)
for i, f in enumerate(files):
    im = Image.open(f).resize((TW, th))
    x, y = (i % COLS) * TW, (i // COLS) * (th + 14)
    sheet.paste(im, (x, y))
    dets = det.detect(f.read_bytes(), threshold=0.01)
    score = dets[0].score if dets else 0.0
    ts = f.name[9:13] + "s" + f.name[13:15] if f.name[6:8] == "10" else f.name
    color = (
        (255, 80, 80) if score >= 0.65 else ((255, 200, 60) if score >= 0.5 else (180, 180, 180))
    )
    draw.text(
        (x + 4, y + th + 1),
        f"{f.name[6:8]}. {f.name[9:11]}:{f.name[11:13]}:{f.name[13:15]} {score:.2f}",
        fill=color,
    )
sheet.save(out)
print(f"saved {out} ({len(files)} frames)")
