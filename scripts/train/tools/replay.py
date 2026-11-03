"""Replay the spaghetti detector over collected frames and print raw scores."""
import sys
from pathlib import Path

sys.path.insert(0, "/opt/pycentauri/src")

from pycentauri.detect.backend import Detector

model = Path(sys.argv[1])
pattern = sys.argv[2]
force_cpu = "--cpu" in sys.argv

det = Detector.from_model(model, force_cpu=force_cpu)
print(f"backend={det.backend_name} model={model.name}", flush=True)

files = (
    sorted(Path("/").glob(pattern.lstrip("/")))
    if pattern.startswith("/")
    else sorted(Path.cwd().glob(pattern))  # relative to the working directory
)
print(f"frames={len(files)}", flush=True)
for f in files:
    jpeg = f.read_bytes()
    try:
        dets = det.detect(jpeg, threshold=0.01)
    except Exception as err:
        print(f"{f.name} ERROR {err!r}", flush=True)
        continue
    top = dets[0] if dets else None
    score = f"{top.score:.4f}" if top else "-"
    label = top.label if top else "-"
    boxes = " ".join(
        f"[{d.label} {d.score:.3f} x={d.box[0]:.2f} y={d.box[1]:.2f}]" for d in dets[:3]
    ) if dets else ""
    print(f"{f.name} top={score} {label} {boxes}", flush=True)
