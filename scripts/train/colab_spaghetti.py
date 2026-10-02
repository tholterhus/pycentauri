"""Spaghetti-model training on Google Colab — run top to bottom.

Prepared for the 4-class SSD MobileNet V2 320 model of
docs/TRAINING_PLAN.md (classes: spaghetti, blobs, cracks, warping).
Works in a fresh Colab VM (Runtime → GPU). Pinned versions matter.

Before running: upload the assembled dataset zip
(data/train/unified → unified.zip, built by scripts/prepare-dataset.py)
to the Colab session, or set DATASET_URL to a hosted copy.

Cell markers (# %%) allow running via jupytext/colab cell-by-cell.
If a step breaks, note the cell number — OD API details drift; the
pinned versions below are the safety net.
"""

# %% [1] Versions pinnen (OD API ist versions-sensitiv)
# %%capture
%env TF_CPP_MIN_LOG_LEVEL=3
!pip install -q tensorflow==2.15.0
!pip install -q keras==2.15.0
import os

if not os.path.exists("models"):
    !git clone --quiet --depth 1 https://github.com/tensorflow/models.git

# %% [2] Object Detection API installieren (protobuf kompilieren)
# %%capture
%cd /content/models/research
!protoc object_detection/protos/*.proto --python_out=.
!cp object_detection/packages/tf2/setup.py .
!pip -q install .
import object_detection  # noqa: F401  (registers the ops)

# %% [3] Datensatz entpacken
# unified.zip: train/{images,annotations}, val/{images,annotations}, classes.txt
!test -f unified.zip || echo "unified.zip hochladen (Colab-Datei-Panel)!"
!unzip -q -o unified.zip -d /content/dataset
!head -4 /content/dataset/classes.txt

# %% [4] TFRecords bauen (Pascal VOC → TFRecord)
%cd /content/models/research
TRAIN_RECORD = "/content/train.record"
VAL_RECORD = "/content/val.record"
LABEL_MAP = "/content/label_map.pbtxt"

with open(LABEL_MAP, "w") as f:
    for i, c in enumerate(open("/content/dataset/classes.txt").read().split()):
        f.write(f"item {{\n  id: {i + 1}\n  name: '{c}'\n}}\n")
print(open(LABEL_MAP).read())

!python object_detection/dataset_tools/create_pascal_tf_record.py \
    --label_map_path={LABEL_MAP} \
    --data_dir=/content/dataset/train \
    --annotations_dir=annotations \
    --output_path={TRAIN_RECORD} \
    --img_path=images
!python object_detection/dataset_tools/create_pascal_tf_record.py \
    --label_map_path={LABEL_MAP} \
    --data_dir=/content/dataset/val \
    --annotations_dir=annotations \
    --output_path={VAL_RECORD} \
    --img_path=images

# %% [5] Pipeline-Config herunterladen und anpassen
import re

MODEL = "ssd_mobilenet_v2_320x320_coco17_tpu-8"
BASE = "http://download.tensorflow.org/models/object_detection/tf2/20200711"
!curl -LO {BASE}/{MODEL}.tar.gz && tar -xzf {MODEL}.tar.gz
!cp {MODEL}/pipeline.config /content/pipeline.config

cfg = open("/content/pipeline.config").read()
N_CLASSES = 4
BATCH = 12
STEPS = 30000
cfg = re.sub(r"num_classes: \d+", f"num_classes: {N_CLASSES}", cfg)
cfg = re.sub(r"batch_size: \d+", f"batch_size: {BATCH}", cfg)
cfg = re.sub(r"num_steps: \d+", f"num_steps: {STEPS}", cfg)
cfg = cfg.replace("PATH_TO_BE_CONFIGURED/train.record", TRAIN_RECORD)
cfg = cfg.replace("PATH_TO_BE_CONFIGURED/val.record", VAL_RECORD)
cfg = cfg.replace("PATH_TO_BE_CONFIGURED/label_map.pbtxt", LABEL_MAP)
cfg = cfg.replace("PATH_TO_BE_CONFIGURED/", f"/content/{MODEL}/checkpoint/")
# fein-tuning: COCO-Checkpoint laden, Kopf neu initialisieren
cfg = re.sub(r"fine_tune_checkpoint: \"[^\"]*\"", 'fine_tune_checkpoint: "/content/' + MODEL + '/checkpoint/ckpt-0"', cfg)
open("/content/pipeline.config", "w").write(cfg)
print("config geschrieben — Klasse-Check:", "num_classes: 4" in cfg)

# %% [6] Training (~2-4 h GPU; Colab-Fenster im Blick behalten)
%cd /content/models/research
!python object_detection/model_main_tf2.py \
    --model_dir=/content/training \
    --pipeline_config_path=/content/pipeline.config \
    --num_train_steps={STEPS}

# %% [7] TFLite-Export (SSD-Postprocess-Tensoren!) + int8-Quantisierung
%cd /content/models/research
!python object_detection/export_tflite_graph_tf2.py \
    --pipeline_config_path=/content/pipeline.config \
    --trained_checkpoint_dir=/content/training \
    --output_directory=/content/export_tflite

import tensorflow as tf  # noqa: E402


def representative_dataset():
    # int8-Kalibrierung braucht echte Trainingsbilder (aus dem Record)
    import glob

    for path in sorted(glob.glob("/content/dataset/train/images/*.jpg"))[:200]:
        img = tf.io.decode_jpeg(tf.io.read_file(path), channels=3)
        img = tf.image.resize(img, [320, 320])
        yield [tf.reshape(img, [1, 320, 320, 3])]


converter = tf.lite.TFLiteConverter.from_saved_model("/content/export_tflite/saved_model")
converter.optimizations = [tf.lite.Optimize.DEFAULT]
converter.representative_dataset = representative_dataset
converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
converter.inference_input_type = tf.uint8
converter.inference_output_type = tf.float32
tflite = converter.convert()
open("/content/spaghetti.tflite", "wb").write(tflite)
print("int8-TFLite:", len(tflite), "bytes")

# %% [8] Edge-TPU kompilieren (läuft IN Colab — das lokale Netz blockt
# packages.cloud.google.com, die Colab-VM nicht)
!curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key add -
!echo "deb https://packages.cloud.google.com/apt coral-edgetpu-stable main" > /etc/apt/sources.list.d/coral-edgetpu.list
!apt-get -qq update && apt-get -qq install edgetpu-compiler
%cd /content
!edgetpu_compiler spaghetti.tflite
# → spaghetti_edgetpu.tflite (+ ggf. _1, _2 … wenn Ops geteilt werden mussten)

# %% [9] Ergebnis prüfen: die 4 SSD-Postprocess-Tensoren sind Pflicht
from ai_edge_litert.interpreter import Interpreter  # noqa: E402

i = Interpreter(model_path="/content/spaghetti_edgetpu.tflite")
i.allocate_tensors()
for d in i.get_output_details():
    print("output:", d["shape"], d["dtype"])
# erwartet: [1,N,4] + [1,N] + [1,N] + skalar (TFLite_Detection_PostProcess)

# %% [10] Labels + beide Varianten herunterladen (zippen, Colab-Panel)
!cp /content/dataset/classes.txt /content/spaghetti_edgetpu.txt
!zip /content/spaghetti-model.zip spaghetti.tflite spaghetti_edgetpu.tflite spaghetti_edgetpu.txt
# → data/models/ im pycentauri-Klon entpacken, dann:
#    centauri detect check   &&   centauri detect test <gelabelte frames>
