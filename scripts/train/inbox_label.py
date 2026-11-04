#!/usr/bin/env python3
"""Label the training inbox automatically.

For every class folder in data/train/inbox/<class>/ each JPG without a
sibling XML gets an auto-generated VOC annotation:

* positives: the deployed detector (CPU) proposes a box when it scores
  >= --min-score on the frame; the class comes from the FOLDER name,
  never from the detector label. Frames the detector cannot box stay in
  place UNLABELED and are reported — box them by hand with the grid
  tool (scripts/train/tools/grid.py) and re-run.
* ``negative/``: empty XML (hard negatives).

Existing XMLs are never touched. Idempotent.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, "/opt/pycentauri/src")

INBOX = Path("/opt/pycentauri/data/train/inbox")
CLASSES = ("spaghetti", "shift", "warping", "blobs")
MIN_SCORE = 0.30
MODEL = Path("/opt/pycentauri/data/models/spaghetti_edgetpu.tflite")


def voc_xml(stem: str, size: tuple[int, int], cls: str, box: tuple | None, note: str) -> str:
    w, h = size
    root = ET.Element("annotation")
    ET.SubElement(root, "filename").text = stem + ".jpg"
    src = ET.SubElement(root, "source")
    ET.SubElement(src, "annotation").text = note
    size_el = ET.SubElement(root, "size")
    ET.SubElement(size_el, "width").text = str(w)
    ET.SubElement(size_el, "height").text = str(h)
    ET.SubElement(size_el, "depth").text = "3"
    if box is not None:
        obj = ET.SubElement(root, "object")
        ET.SubElement(obj, "name").text = cls
        ET.SubElement(obj, "difficult").text = "0"
        bb = ET.SubElement(obj, "bndbox")
        ET.SubElement(bb, "xmin").text = str(round(box[0] * w))
        ET.SubElement(bb, "ymin").text = str(round(box[1] * h))
        ET.SubElement(bb, "xmax").text = str(round(box[2] * w))
        ET.SubElement(bb, "ymax").text = str(round(box[3] * h))
    return ET.tostring(root, encoding="unicode")


def main() -> int:
    from pycentauri.detect.backend import Detector

    det = Detector.from_model(MODEL, force_cpu=True)
    stats: dict[str, int] = {}
    unboxed: list[str] = []
    for cls in (*CLASSES, "negative"):
        folder = INBOX / cls
        if not folder.is_dir():
            continue
        for jpg in sorted(folder.glob("*.jpg")):
            xml = jpg.with_suffix(".xml")
            if xml.exists():
                continue
            stem = jpg.stem
            if cls == "negative":
                from PIL import Image

                w, h = Image.open(jpg).size
                xml.write_text(voc_xml(stem, (w, h), cls, None, "inbox: negative"))
                stats["negative"] = stats.get("negative", 0) + 1
                continue
            dets = det.detect(jpg.read_bytes(), threshold=MIN_SCORE)
            if dets:
                top = dets[0]
                note = f"inbox: auto-boxed by {det.backend_name} detector (score {top.score:.2f})"
                xml.write_text(voc_xml(stem, jpg.size, cls, tuple(top.box), note))
                stats[cls] = stats.get(cls, 0) + 1
            else:
                unboxed.append(str(jpg))
    print(json.dumps(stats, indent=1, ensure_ascii=False))
    for path in unboxed:
        print(f"BRAUCHT BOX VON HAND: {path}")
    if unboxed:
        print(
            "Box von Hand setzen: venv/bin/python scripts/train/tools/grid.py <datei> "
            "→ XML wie in scripts/train/tools/grid.py-Ausgabe, dann inbox_label.py erneut."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
