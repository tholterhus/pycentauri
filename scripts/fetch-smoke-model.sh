#!/bin/sh
# Download the pycentauri smoke-test detection model: a COCO SSD MobileNet
# V2 in BOTH variants (Edge TPU-compiled and CPU) plus the label sidecar,
# from Google's public google-coral/test_data repository.
#
# IMPORTANT: this model does NOT detect spaghetti — it knows everyday
# objects (persons, cups, bottles, ...). Its only purpose is to verify
# the complete detection pipeline end to end. A real spaghetti model is
# trained separately (see docs/CORAL_SPAGHETTI_DETECTION.md).
#
# Usage: scripts/fetch-smoke-model.sh [target-dir]   (default: data/models)
set -eu

BASE="https://raw.githubusercontent.com/google-coral/test_data/master"
DIR="${1:-data/models}"

mkdir -p "$DIR"

fetch() {
    # fetch <remote-name> <local-name>
    if [ -f "$DIR/$2" ]; then
        echo "exists: $DIR/$2"
        return 0
    fi
    echo "downloading $1 ..."
    curl -fsSL --retry 3 -o "$DIR/$2" "$BASE/$1"
    echo "ok: $DIR/$2"
}

fetch ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite ssd_mobilenet_v2_coco_edgetpu.tflite
fetch ssd_mobilenet_v2_coco_quant_postprocess.tflite        ssd_mobilenet_v2_coco.tflite
fetch coco_labels.txt                                       ssd_mobilenet_v2_coco_edgetpu.txt

echo
echo "Smoke model ready in $DIR/ — try: centauri detect check"
