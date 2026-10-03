#!/bin/bash
# Spaghetti-model training on a Mac — part 1: setup + training start.
#
# Usage (on the Mac, in a folder of your choice):
#   cp /path/to/unified.zip .      # dataset from spielwiese
#   bash mac_train.sh              # one-time setup + detached training
#   tail -f training.log           # monitor (step/loss every 500 steps)
#
# When training.log shows the final step (15000/15000), run:
#   bash mac_export.sh             # → model-result.zip
#
# Apple Silicon: ~3-6 h. Intel Mac: ~8-15 h (needs AVX — anything 2013+).

set -euo pipefail
cd "$(dirname "$0")"
echo "mac_train_v5.sh v5 (2026-10-03)"

if [ ! -f unified.zip ]; then
    echo "unified.zip fehlt — von spielwiese (/opt/pycentauri/data/train/) hierher kopieren."
    exit 1
fi

# Nimm den ersten Python, mit dem sich TensorFlow installieren lässt —
# Pythons ohne TF-Wheels (z.B. 3.14) werden automatisch übersprungen.
if [ ! -d venv ] || ! ./venv/bin/python -c "import tensorflow" 2>/dev/null; then
    rm -rf venv
    PY_OK=""
    # OD-API braucht tf.compat.v1.estimator — entfernt ab TF 2.16.
    # Daher: TF 2.15.x (letzte Estimator-Version) → Python 3.11 Pflicht.
    for c in python3.11 python3; do
        command -v "$c" >/dev/null || continue
        "$c" -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)' || continue
        echo "versuche venv mit $c ($($c --version 2>&1)) — TF 2.15 braucht python<=3.11!"
        "$c" -m venv venv
        ./venv/bin/pip install --quiet --upgrade pip
        if [ "$(uname -m)" = "arm64" ]; then
            TF_SPEC="tensorflow==2.15.1"   # arm64-wheel vorhanden
        else
            TF_SPEC="tensorflow-cpu==2.15.1"
        fi
        if ./venv/bin/pip install --quiet "$TF_SPEC"; then
            PY_OK="$c"; break
        fi
        echo "  tensorflow nicht verfügbar für $c — nächster python …"
        rm -rf venv
    done
    [ -n "$PY_OK" ] || { echo "kein python mit tensorflow-support gefunden — brew install python@3.12"; exit 1; }
fi
./venv/bin/python -c "import tensorflow as tf; print('TF', tf.__version__)" \
    || { echo "TF-Import fehlgeschlagen — Mac ohne AVX? Dann Colab/Kaggle nutzen."; exit 1; }
if [ ! -d models/research ]; then
    git clone --quiet --depth 1 https://github.com/tensorflow/models.git
fi

# Alle REQUIRED_PACKAGES des OD-API aus dem geklonten setup.py lesen und
# ZUSAMMEN mit den TF-2.15-Pins installieren (ein Resolver-Aufruf = eine
# kohärente Versionskette: numpy<2, protobuf 4.25, grpcio-tools 1.62).
./venv/bin/python - << 'PY2'
import ast
src = open("models/research/object_detection/packages/tf2/setup.py").read()
tree = ast.parse(src)
pkgs = []
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if getattr(t, "id", None) == "REQUIRED_PACKAGES":
                pkgs = [ast.literal_eval(e) for e in node.value.elts]
keep = [p for p in pkgs if not p.startswith(("tensorflow==", "tf-models-official", "keras"))]
open("/tmp/od-deps.txt", "w").write("\n".join(keep))
print("od-deps:", keep)
PY2
./venv/bin/pip install --quiet "numpy==1.26.4" "protobuf==4.25.8" "grpcio-tools==1.62.3" \
    tensorflow_io lvis matplotlib pycocotools tf-slim lxml scipy \
    opencv-python-headless $(tr '\n' ' ' < /tmp/od-deps.txt) \
    || echo "dep-install hatte konflikte —_training.log zeigt den grund"
# protos werden mit grpcio-tools generiert (passend zur protobuf-runtime)
./venv/bin/pip install --quiet "grpcio-tools==1.62.3" "protobuf==4.25.8" "numpy==1.26.4" lxml matplotlib pycocotools tf-slim scipy opencv-python-headless lvis
# gencode der pb2-dateien muss zur protobuf-runtime passen (TF 2.20 → 6.33):
./venv/bin/pip install --quiet --no-deps "tf-models-official==2.15.0"  # official.* fuer efficientnet-import, ohne tensorflow-text
# deshalb grpc_tools.protoc statt des brew-protoc
./venv/bin/python -m grpc_tools.protoc -Imodels/research --python_out=models/research models/research/object_detection/protos/*.proto
# OD-API: setup.py liegt in object_detection/packages/tf2/ (liefert auch lxml)
./venv/bin/pip install --quiet lxml || true
# Garantiert importierbar: research-Root auf den PYTHONPATH (klappt immer)
export PYTHONPATH="$PWD/models/research:${PYTHONPATH:-}"
./venv/bin/python -c "import object_detection, lvis, tf_slim, pycocotools, matplotlib" || { echo "trainings-imports fehlen — fix: ./venv/bin/pip install lvis tf-slim pycocotools matplotlib lxml"; ./venv/bin/pip show object-detection | grep Location || true; exit 1; }
echo "alle trainings-imports OK"

if [ ! -d dataset ]; then
    mkdir dataset && unzip -q unified.zip -d dataset
fi
# TFRecords mit eigenem builder (keine VOC2007-annahmen des OD-API-helpers)
if [ ! -f build_tfrecords.py ]; then
    curl -fsSL -o build_tfrecords.py "https://raw.githubusercontent.com/tholterhus/pycentauri/main/scripts/train/build_tfrecords.py"
fi
./venv/bin/python build_tfrecords.py --dataset-dir dataset \
    --classes-file dataset/classes.txt \
    --train-out train.record --val-out val.record

if [ ! -f pipeline.config ]; then
    MODEL=ssd_mobilenet_v2_320x320_coco17_tpu-8
    curl -fsSL -o m.tar.gz "http://download.tensorflow.org/models/object_detection/tf2/20200711/${MODEL}.tar.gz"
    tar -xzf m.tar.gz && rm m.tar.gz
    ./venv/bin/python - << 'PY2'
import re
cfg = open("ssd_mobilenet_v2_320x320_coco17_tpu-8/pipeline.config").read()
cfg = re.sub(r"num_classes: \d+", "num_classes: 1", cfg)
cfg = re.sub(r"batch_size: \d+", "batch_size: 8", cfg)
cfg = re.sub(r"num_steps: \d+", "num_steps: 15000", cfg)
cfg = re.sub(r"fine_tune_checkpoint: \"[^\"]*\"",
             'fine_tune_checkpoint: "ssd_mobilenet_v2_320x320_coco17_tpu-8/checkpoint/ckpt-0"', cfg)
cfg = cfg.replace("PATH_TO_BE_CONFIGURED/train.record", "train.record")
cfg = cfg.replace("PATH_TO_BE_CONFIGURED/val.record", "val.record")
cfg = cfg.replace("PATH_TO_BE_CONFIGURED/label_map.pbtxt", "label_map.pbtxt")
cfg = re.sub(r"keep_checkpoint_max: \d+", "keep_checkpoint_max: 2", cfg)
open("pipeline.config", "w").write(cfg)
print("config ok:", "num_classes: 1" in cfg)
PY2
fi

if pgrep -f "model_main_tf2" > /dev/null; then
    echo "training läuft bereits — monitor: tail -f training.log"
    exit 0
fi
nohup caffeinate -ims ./venv/bin/python models/research/object_detection/model_main_tf2.py \
    --model_dir=training --pipeline_config_path=pipeline.config \
    --num_train_steps=15000 > training.log 2>&1 &
disown
echo "training gestartet (PID $!) — caffeinate hält den Mac wach"
echo "monitor: tail -f training.log   |   netzteil anschließen!"
echo "nach dem letzten Schritt (15000/15000): bash mac_export.sh"
