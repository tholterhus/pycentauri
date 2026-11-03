"""Assemble the spaghetti-v2 fine-tune dataset (unified VOC layout).

Positives: 6 frames of the shifted part (2026-11-01, data/spaghetti-positive)
with hand-estimated boxes, augmented ~10x (flip / brightness / contrast /
crop / JPEG jitter). Negatives: intact frames incl. the false-positive
material + a spread of normal-print collect frames (empty VOC XMLs).

Layout (matches scripts/train/colab_spaghetti.py expectations):
  out_dir/train/images/*.jpg + annotations/*.xml
  out_dir/val/images/*.jpg   + annotations/*.xml
  out_dir/classes.txt  (one class: spaghetti)
"""
import json
import random
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageEnhance

ROOT = Path("/opt/pycentauri/data")
OUT = ROOT / "train" / "spaghetti-v2"
NEG_TARGET = 400
VAL_FRACTION = 0.15
SEED = 42

# Hand-estimated boxes (normalized x0, y0, x1, y1) — failed top edge of the
# shifted part (Layer-Shift ab 21:22 Ortszeit, Spaghetti ab ~21:25).
POSITIVES = {
    "20261101-202247.jpg": [(0.33, 0.40, 0.68, 0.54)],
    "20261101-202409.jpg": [(0.22, 0.40, 0.68, 0.55)],
    "20261101-202500.jpg": [(0.22, 0.41, 0.68, 0.56)],
    "20261101-202553.jpg": [(0.14, 0.40, 0.67, 0.57)],
    "20261101-202646.jpg": [(0.13, 0.39, 0.66, 0.58)],
    "20261101-202739.jpg": [(0.13, 0.40, 0.67, 0.60)],
}
# Two base frames go to val as untouched originals.
VAL_POSITIVES = {"20261101-202646.jpg", "20261101-202247.jpg"}

rng = random.Random(SEED)


def voc_xml(stem: str, size: tuple[int, int], boxes: list[tuple]) -> str:
    w, h = size
    root = ET.Element("annotation")
    ET.SubElement(root, "filename").text = stem + ".jpg"
    size_el = ET.SubElement(root, "size")
    ET.SubElement(size_el, "width").text = str(w)
    ET.SubElement(size_el, "height").text = str(h)
    ET.SubElement(size_el, "depth").text = "3"
    for (x0, y0, x1, y1) in boxes:
        obj = ET.SubElement(root, "object")
        ET.SubElement(obj, "name").text = "spaghetti"
        ET.SubElement(obj, "difficult").text = "0"
        bb = ET.SubElement(obj, "bndbox")
        ET.SubElement(bb, "xmin").text = str(round(x0 * w))
        ET.SubElement(bb, "ymin").text = str(round(y0 * h))
        ET.SubElement(bb, "xmax").text = str(round(x1 * w))
        ET.SubElement(bb, "ymax").text = str(round(y1 * h))
    return ET.tostring(root, encoding="unicode")


def augments(im: Image.Image, boxes: list[tuple]) -> list[tuple[str, Image.Image, list[tuple]]]:
    """Return [(suffix, image, boxes), ...] — 9 augmented variants."""
    out = []
    w, h = im.size

    def flip():
        return im.transpose(Image.FLIP_LEFT_RIGHT), [
            (1 - x1, y0, 1 - x0, y1) for (x0, y0, x1, y1) in boxes
        ]

    out.append(("flip", *flip()))
    out.append(("br70", ImageEnhance.Brightness(im).enhance(0.7), boxes))
    out.append(("br130", ImageEnhance.Brightness(im).enhance(1.3), boxes))
    out.append(("ct80", ImageEnhance.Contrast(im).enhance(0.8), boxes))
    out.append(("ct120", ImageEnhance.Contrast(im).enhance(1.2), boxes))
    # two random crops that fully contain every box (with margin)
    for k in range(2):
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        mx, my = rng.uniform(0.05, 0.18), rng.uniform(0.05, 0.18)
        cx0 = max(0.0, x0 - mx)
        cy0 = max(0.0, y0 - my)
        cx1 = min(1.0, x1 + mx)
        cy1 = min(1.0, y1 + rng.uniform(0.15, 0.35))  # keep context below
        box_px = (
            int(cx0 * w),
            int(cy0 * h),
            int(cx1 * w),
            int(cy1 * h),
        )
        crop = im.crop(box_px)
        nb = [
            (
                (b[0] - cx0) / (cx1 - cx0),
                (b[1] - cy0) / (cy1 - cy0),
                (b[2] - cx0) / (cx1 - cx0),
                (b[3] - cy0) / (cy1 - cy0),
            )
            for b in boxes
        ]
        out.append((f"crop{k}", crop, nb))
    out.append(("jq60", im, boxes))  # saved at lower quality later
    return out


def save_jpg(im: Image.Image, path: Path, quality: int = 92) -> None:
    im.convert("RGB").save(path, quality=quality)


def main() -> None:
    for split in ("train", "val"):
        (OUT / split / "images").mkdir(parents=True, exist_ok=True)
        (OUT / split / "annotations").mkdir(parents=True, exist_ok=True)
    (OUT / "classes.txt").write_text("spaghetti\n")

    n_pos = 0
    for name, boxes in POSITIVES.items():
        src = ROOT / "spaghetti-positive" / name
        im = Image.open(src)
        size = im.size
        stem = Path(name).stem
        split = "val" if name in VAL_POSITIVES else "train"
        save_jpg(im, OUT / split / "images" / f"{stem}.jpg")
        (OUT / split / "annotations" / f"{stem}.xml").write_text(
            voc_xml(stem, size, boxes)
        )
        n_pos += 1
        if split == "val":
            continue
        for suffix, aimg, aboxes in augments(im, boxes):
            q = 60 if suffix == "jq60" else 92
            astem = f"{stem}_a{suffix}"
            save_jpg(aimg, OUT / "train" / "images" / f"{astem}.jpg", quality=q)
            (OUT / "train" / "annotations" / f"{astem}.xml").write_text(
                voc_xml(astem, aimg.size, aboxes)
            )
            n_pos += 1

    # --- negatives -----------------------------------------------------
    # Intact frames + false-positive evidence (curated already):
    neg_files = sorted((ROOT / "false-positives").glob("*.jpg"))
    # Spread of normal prints from collect, excluding the shift phase
    # (20261101-202247..202739) and the 20:00-20:21 intact frames (already
    # in the set via false-positives).
    collect = sorted((ROOT / "collect").glob("*.jpg"))
    per_day: dict[str, list[Path]] = {}
    for f in collect:
        day = f.name[6:8]
        if f.name.startswith("20261101-20"):  # shift + intact duplicates
            continue
        per_day.setdefault(day, []).append(f)
    print("negatives per day available:", {d: len(v) for d, v in per_day.items()})
    quota = max(1, NEG_TARGET // max(1, len(per_day)))
    for day, files in per_day.items():
        step = max(1, len(files) // quota)
        neg_files.extend(files[::step][:quota])

    rng.shuffle(neg_files)
    n_val_neg = round(len(neg_files) * VAL_FRACTION)
    for i, f in enumerate(neg_files):
        im = Image.open(f)
        size = im.size
        stem = "neg_" + f.stem
        split = "val" if i < n_val_neg else "train"
        save_jpg(im, OUT / split / "images" / f"{stem}.jpg")
        (OUT / split / "annotations" / f"{stem}.xml").write_text(
            voc_xml(stem, size, [])
        )

    manifest = {
        "positives_train": n_pos - len(VAL_POSITIVES),
        "positives_val": len(VAL_POSITIVES),
        "negatives_total": len(neg_files),
        "note": "boxes hand-estimated on 10% grid; filenames UTC",
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    for split in ("train", "val"):
        imgs = len(list((OUT / split / "images").glob("*.jpg")))
        xmls = len(list((OUT / split / "annotations").glob("*.xml")))
        print(f"{split}: {imgs} images, {xmls} annotations")


if __name__ == "__main__":
    main()
