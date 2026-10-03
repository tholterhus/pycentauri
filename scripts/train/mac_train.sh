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

if [ ! -f unified.zip ]; then
    echo "unified.zip fehlt — von spielwiese (/opt/pycentauri/data/train/) hierher kopieren."
    exit 1
fi

PY=""
for c in python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null && [ "$("$c" -c 'import sys; print(sys.version_info >= (3, 10))')" = "True" ]; then
        PY="$c"; break
    fi
done
[ -n "$PY" ] || { echo "kein python3.10+ — brew install python@3.12"; exit 1; }
echo "python: $($PY --version)  arch: $(uname -m)"

if [ ! -d venv ]; then
    "$PY" -m venv venv
    ./venv/bin/pip install --quiet --upgrade pip
    if [ "$(uname -m)" = "arm64" ]; then
        ./venv/bin/pip install --quiet "tensorflow>=2.16,<2.21"
    else
        ./venv/bin/pip install --quiet "tensorflow-cpu==2.15.1"
    fi
fi
./venv/bin/python -c "import tensorflow as tf; print('TF', tf.__version__)" \
    || { echo "TF-Import fehlgeschlagen — Mac ohne AVX? Dann Colab/Kaggle nutzen."; exit 1; }

if [ ! -d models/research ]; then
    git clone --quiet --depth 1 https://github.com/tensorflow/models.git
fi
command -v protoc >/dev/null || { echo "protoc fehlt — brew install protobuf"; exit 1; }
(cd models/research && protoc object_detection/protos/*.proto --python_out=.)
./venv/bin/pip install --quiet models/research || echo "OD-API install: siehe models/research — setup.py alternativ manuell"

if [ ! -d dataset ]; then
    mkdir dataset && unzip -q unified.zip -d dataset
fi
printf 'item {\n  id: 1\n  name: '"'"'spaghetti'"'"'\n}\n' > label_map.pbtxt

[ -f train.record ] || ./venv/bin/python models/research/object_detection/dataset_tools/create_pascal_tf_record.py \
    --label_map_path=label_map.pbtxt --data_dir=dataset/train \
    --annotations_dir=annotations --output_path=train.record --img_path=images
[ -f val.record ] || ./venv/bin/python models/research/object_detection/dataset_tools/create_pascal_tf_record.py \
    --label_map_path=label_map.pbtxt --data_dir=dataset/val \
    --annotations_dir=annotations --output_path=val.record --img_path=images

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
