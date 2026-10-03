#!/bin/bash
# Spaghetti-model training — part 2: run AFTER training finished
# (training.log ends with 15000/15000). Produces model-result.zip.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/models/research:${PYTHONPATH:-}"
caffeinate -im ./venv/bin/python models/research/object_detection/export_tflite_graph_tf2.py \
    --pipeline_config_path=pipeline.config --trained_checkpoint_dir=training \
    --output_directory=export_tflite
./venv/bin/python - << 'PY2'
import tensorflow as tf
import glob
def rep():
    for p in sorted(glob.glob("dataset/train/images/*.jpg"))[:200]:
        img = tf.io.decode_jpeg(tf.io.read_file(p), channels=3)
        img = tf.image.resize(img, [320, 320])
        yield [tf.reshape(img, [1, 320, 320, 3])]
c = tf.lite.TFLiteConverter.from_saved_model("export_tflite/saved_model")
c.optimizations = [tf.lite.Optimize.DEFAULT]
c.representative_dataset = rep
c.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
open("spaghetti_int8.tflite", "wb").write(c.convert())
print("int8 export ok")
PY2
cp export_tflite/saved_model/spaghetti.tflite . 2>/dev/null || true
printf 'spaghetti\n' > spaghetti_labels.txt
zip -q model-result.zip spaghetti.tflite spaghetti_int8.tflite spaghetti_labels.txt
echo "FERTIG: model-result.zip → zurück nach spielwiese (/opt/pycentauri/data/models/)"
