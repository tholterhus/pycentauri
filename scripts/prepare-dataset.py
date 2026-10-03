#!/usr/bin/env python3
"""Assemble the spaghetti-model training dataset (plan: docs/TRAINING_PLAN.md).

Unifies three sources into one Pascal-VOC dataset with a train/val split:

1. Roboflow downloads — data/train/roboflow/<project>-v<ver>/ in YOLO
   format (images/ + labels/ + data.yaml), produced by
   scripts/roboflow-harvest.py download.
2. Own labeled frames — a Label Studio **Pascal VOC export** directory
   (Annotations/*.xml + JPEGImages/*.jpg), produced from
   data/collect/ frames.
3. Everything already unified (re-runs are idempotent).

Usage:
  python3 scripts/prepare-dataset.py \\
      --roboflow data/train/roboflow/<project>-v<ver> \\
      --own data/train/own-labelstudio-export \\
      --out data/train/unified [--val-split 0.2]

Class names from all sources are mapped onto the target set
(spaghetti, blobs, cracks, warping). YOLO class ids are translated via
the class list in each data.yaml; unknown class names are reported and
skipped (never guessed). Output layout:

  data/train/unified/
    train/{images,annotations}/   val/{images,annotations}/
    classes.txt                   (target order)
    report.txt                    (per-class counts, split sizes)

No ML dependencies — pure stdlib + PIL if available (images are copied,
never re-encoded).
"""

from __future__ import annotations

import argparse
import random
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

TARGET_CLASSES = ["spaghetti", "blobs", "cracks", "warping"]
ALIASES = {
    # community datasets use varying spellings — extend as encountered
    "spaghetti": "spaghetti",
    "spagetti": "spaghetti",
    "blob": "blobs",
    "blobs": "blobs",
    "crack": "cracks",
    "cracks": "cracks",
    "warping": "warping",
    "warp": "warping",
}


def read_data_yaml(directory: Path) -> list[str]:
    names: list[str] = []
    yaml = directory / "data.yaml"
    if not yaml.is_file():
        return names
    in_names = False
    for line in yaml.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("names:"):
            rest = stripped.split(":", 1)[1].strip()
            if rest.startswith("["):  # inline list: names: ['a', 'b']
                names = [n.strip().strip("\"'") for n in rest.strip("[]").split(",") if n.strip()]
                break
            in_names = True  # block list: "names:" followed by "- x" lines
            continue
        if in_names:
            if stripped.startswith("-"):
                names.append(stripped[1:].strip().strip("\"'"))
            elif stripped:
                break
    return names


def map_class(name: str) -> str | None:
    return ALIASES.get(name.strip().lower())


def collect_yolo(source: Path) -> list[tuple[Path, list[tuple[str, float, float, float, float]]]]:
    """Return (image_path, [(class, cx, cy, w, h) …]) in normalized coords."""
    out: list[tuple[Path, list[tuple[str, float, float, float, float]]]] = []
    names = read_data_yaml(source)
    splits = [s for s in ("train", "valid", "test") if (source / s).is_dir()]
    corrupt = 0
    for split in splits:
        labels = source / split / "labels"
        for label_file in sorted(labels.glob("*.txt")):
            # Roboflow zips sometimes carry macOS AppleDouble metadata
            # (._label.txt) — skip those and any file we cannot parse.
            if label_file.name.startswith("._"):
                corrupt += 1
                continue
            for ext in (".jpg", ".jpeg", ".png"):
                image = source / split / "images" / (label_file.stem + ext)
                if image.is_file():
                    break
            else:
                continue
            boxes: list[tuple[str, float, float, float, float]] = []
            try:
                lines = label_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                corrupt += 1
                continue
            for line in lines:
                parts = line.split()
                if len(parts) != 5:
                    continue
                try:
                    idx = int(parts[0])
                    cx, cy, w, h = (float(v) for v in parts[1:5])
                except ValueError:
                    corrupt += 1
                    continue
                if idx >= len(names) or max(w, h) > 1.5:
                    continue
                target = map_class(names[idx])
                if target is None:
                    continue
                boxes.append((target, cx, cy, w, h))
            if boxes:
                out.append((image, boxes))
            else:
                corrupt += 1
    if corrupt:
        print(
            f"  {source.name}: {corrupt} label-dateien/zeilen übersprungen (leer, korrupt oder fremdklasse)"
        )
    return out


def collect_voc(source: Path) -> list[tuple[Path, list[tuple[str, float, float, float, float]]]]:
    """Label Studio Pascal VOC export: Annotations/*.xml + JPEGImages/."""
    out: list[tuple[Path, list[tuple[str, float, float, float, float]]]] = []
    ann_dir = source / "Annotations"
    img_dir = source / "JPEGImages"
    for xml in sorted(ann_dir.glob("*.xml")):
        try:
            tree = ET.parse(xml)
        except ET.ParseError:
            continue
        root = tree.getroot()
        size = root.find("size")
        width = float((size.find("width").text if size is not None else 0) or 0)
        height = float((size.find("height").text if size is not None else 0) or 0)
        if not width or not height:
            continue
        image_name = root.findtext("filename") or ""
        image = img_dir / image_name
        if not image.is_file():
            continue
        boxes: list[tuple[str, float, float, float, float]] = []
        for obj in root.findall("object"):
            target = map_class(obj.findtext("name") or "")
            box = obj.find("bndbox")
            if target is None or box is None:
                continue
            x0 = float(box.findtext("xmin", "0")) / width
            y0 = float(box.findtext("ymin", "0")) / height
            x1 = float(box.findtext("xmax", "0")) / width
            y1 = float(box.findtext("ymax", "0")) / height
            boxes.append((target, (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0))
        out.append((image, boxes))
    return out


def write_voc(
    image: Path,
    boxes: list[tuple[str, float, float, float, float]],
    out_xml: Path,
    stem: str,
    width_px: int,
    height_px: int,
) -> None:
    """Boxes arrive normalized (0..1) and are scaled with the REAL image
    dimensions (x by width, y by height — a 640x360 camera frame has a
    different height scale than a square roboflow image)."""
    ann = ET.Element("annotation")
    ET.SubElement(ann, "filename").text = stem + image.suffix
    size = ET.SubElement(ann, "size")
    ET.SubElement(size, "width").text = str(width_px)
    ET.SubElement(size, "height").text = str(height_px)
    ET.SubElement(size, "depth").text = "3"
    for target, cx, cy, w, h in boxes:
        obj = ET.SubElement(ann, "object")
        ET.SubElement(obj, "name").text = target
        bnd = ET.SubElement(obj, "bndbox")
        x0 = max(0.0, (cx - w / 2)) * width_px
        y0 = max(0.0, (cy - h / 2)) * height_px
        x1 = min(1.0, cx + w / 2) * width_px
        y1 = min(1.0, cy + h / 2) * height_px
        ET.SubElement(bnd, "xmin").text = str(round(x0, 1))
        ET.SubElement(bnd, "ymin").text = str(round(y0, 1))
        ET.SubElement(bnd, "xmax").text = str(round(x1, 1))
        ET.SubElement(bnd, "ymax").text = str(round(y1, 1))
    ET.ElementTree(ann).write(out_xml, encoding="utf-8", xml_declaration=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--roboflow", action="append", default=[], help="roboflow YOLO source dir (repeatable)"
    )
    ap.add_argument(
        "--own", action="append", default=[], help="own Label Studio VOC export dir (repeatable)"
    )
    ap.add_argument("--out", default="data/train/unified", help="output directory")
    ap.add_argument("--val-split", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    entries: list[tuple[Path, list[tuple[str, float, float, float, float]], str]] = []
    for src in args.roboflow:
        src_path = Path(src)
        for image, boxes in collect_yolo(src_path):
            entries.append((image, boxes, src_path.name))
    for src in args.own:
        src_path = Path(src)
        for image, boxes in collect_voc(src_path):
            entries.append((image, boxes, "own"))
    if not entries:
        print("nichts gefunden — prüfe die Quellpfade (--roboflow/--own)")
        return 1

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    for sub in ("train/images", "train/annotations", "val/images", "val/annotations"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    random.seed(args.seed)
    random.shuffle(entries)
    n_val = round(len(entries) * args.val_split)

    counts = dict.fromkeys(TARGET_CLASSES, 0)
    skipped_unknown = 0
    for i, (image, boxes, _source) in enumerate(entries):
        known = [b for b in boxes if b[0] in TARGET_CLASSES]
        skipped_unknown += len(boxes) - len(known)
        if not known:
            continue  # frames without a known class are dropped entirely
        split = "val" if i < n_val else "train"
        stem = f"{i:05d}_{image.parent.name[:20].replace(' ', '_')}"
        shutil.copy2(image, out / split / "images" / (stem + image.suffix))
        from PIL import Image

        with Image.open(image) as im:
            width_px, height_px = im.size
        write_voc(
            image, known, out / split / "annotations" / (stem + ".xml"), stem, width_px, height_px
        )
        for target, *_ in known:
            counts[target] += 1

    (out / "classes.txt").write_text("\n".join(TARGET_CLASSES) + "\n", encoding="utf-8")
    report = [
        f"bilder gesamt: {len(entries)} (train: {len(entries) - n_val}, val: {n_val})",
        f"boxes: {counts}",
        f"übersprungene unbekannte klassen: {skipped_unknown}",
    ]
    print("\n".join(report))
    (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(
        f"→ {out}\nNächster Schritt: dataset-zip nach Colab (docs/TRAINING_PLAN.md, Training-Schritt)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
