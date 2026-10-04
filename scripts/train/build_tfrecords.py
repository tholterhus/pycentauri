#!/usr/bin/env python3
"""Build TFRecords for the TensorFlow Object Detection API from the
assembled unified dataset (data/train/unified — built by
scripts/prepare-dataset.py: images/ + annotations/*.xml + classes.txt).

Deliberately simple: no PASCAL VOC2007 assumptions (the OD API helper
create_pascal_tf_record.py expects that exact structure, which our
assembler does not produce).

Usage (in the training venv with tensorflow installed):
  python3 build_tfrecords.py --dataset-dir dataset \
      --classes-file dataset/classes.txt \
      --train-out train.record --val-out val.record
"""

from __future__ import annotations

import argparse
import glob
import os
import xml.etree.ElementTree as ET

import tensorflow as tf


def int64_feature(v: int) -> tf.train.Feature:
    return tf.train.Feature(int64_list=tf.train.Int64List(value=[v]))


def int64_list_feature(v: list[int]) -> tf.train.Feature:
    return tf.train.Feature(int64_list=tf.train.Int64List(value=v))


def bytes_feature(v: bytes) -> tf.train.Feature:
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[v]))


def bytes_list_feature(v: list[bytes]) -> tf.train.Feature:
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=v))


def float_list_feature(v: list[float]) -> tf.train.Feature:
    return tf.train.Feature(float_list=tf.train.FloatList(value=v))


def build_example(
    image_path: str, xml_path: str, label_to_id: dict[str, int]
) -> tuple[tf.train.Example, int] | None:
    with tf.io.gfile.GFile(image_path, "rb") as f:
        encoded = f.read()
    root = ET.parse(xml_path).getroot()
    filename = root.findtext("filename") or os.path.basename(image_path)
    width = int(root.findtext("size/width") or 0)
    height = int(root.findtext("size/height") or 0)
    if not width or not height:
        return None

    xmins: list[float] = []
    ymins: list[float] = []
    xmaxs: list[float] = []
    ymaxs: list[float] = []
    classes_text: list[bytes] = []
    classes: list[int] = []
    for obj in root.findall("object"):
        name = obj.findtext("name") or ""
        if name not in label_to_id:
            continue
        box = obj.find("bndbox")
        if box is None:
            continue
        xmins.append(float(box.findtext("xmin", "0")) / width)
        ymins.append(float(box.findtext("ymin", "0")) / height)
        xmaxs.append(float(box.findtext("xmax", "0")) / width)
        ymaxs.append(float(box.findtext("ymax", "0")) / height)
        classes_text.append(name.encode("utf-8"))
        classes.append(label_to_id[name])
    if not classes:
        return None

    feature = {
        "image/encoded": bytes_feature(encoded),
        "image/format": bytes_feature(b"jpeg"),
        "image/filename": bytes_feature(filename.encode("utf-8")),
        "image/source_id": bytes_feature(filename.encode("utf-8")),
        "image/height": int64_feature(height),
        "image/width": int64_feature(width),
        "image/object/bbox/xmin": float_list_feature(xmins),
        "image/object/bbox/ymin": float_list_feature(ymins),
        "image/object/bbox/xmax": float_list_feature(xmaxs),
        "image/object/bbox/ymax": float_list_feature(ymaxs),
        "image/object/class/text": bytes_list_feature(classes_text),
        "image/object/class/label": int64_list_feature(classes),
        "image/object/difficult": int64_list_feature([0] * len(classes)),
    }
    return tf.train.Example(features=tf.train.Features(feature=feature)), len(classes)


def write_record(
    images_dir: str, ann_dir: str, label_to_id: dict[str, int], out_path: str
) -> tuple[int, int]:
    xmls = sorted(glob.glob(os.path.join(ann_dir, "*.xml")))
    writer = tf.io.TFRecordWriter(out_path)
    n_images = 0
    n_boxes = 0
    for xml_path in xmls:
        stem = os.path.splitext(os.path.basename(xml_path))[0]
        image_path = None
        for ext in (".jpg", ".jpeg", ".png"):
            candidate = os.path.join(images_dir, stem + ext)
            if os.path.isfile(candidate):
                image_path = candidate
                break
        if image_path is None:
            continue
        result = build_example(image_path, xml_path, label_to_id)
        if result is None:
            continue
        example, boxes = result
        writer.write(example.SerializeToString())
        n_images += 1
        n_boxes += boxes
    writer.close()
    return n_images, n_boxes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset-dir", required=True, help="unified dataset dir (train/ val/ classes.txt)")
    ap.add_argument("--classes-file", required=True, help="classes.txt (one class per line)")
    ap.add_argument("--train-out", default="train.record")
    ap.add_argument("--val-out", default="val.record")
    args = ap.parse_args()

    with open(args.classes_file, encoding="utf-8") as classes_fh:
        classes = [c.strip() for c in classes_fh.read().splitlines() if c.strip()]
    label_to_id = {name: i + 1 for i, name in enumerate(classes)}  # 1-based
    with tf.io.gfile.GFile(args.classes_file.replace(".txt", "_label_map.pbtxt"), "w") as f:
        for name, cid in label_to_id.items():
            f.write(f"item {{\n  id: {cid}\n  name: '{name}'\n}}\n")
    print(f"label map: {label_to_id}")

    for split, out in (("train", args.train_out), ("val", args.val_out)):
        n_images, n_boxes = write_record(
            os.path.join(args.dataset_dir, split, "images"),
            os.path.join(args.dataset_dir, split, "annotations"),
            label_to_id,
            out,
        )
        print(f"{split}: {n_images} bilder, {n_boxes} boxen → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
