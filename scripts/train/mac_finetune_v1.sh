#!/bin/bash
# LIGHT-RUNDE — schnelle Aufnahme neuer positives/negatives.
#
# Startet vom Checkpoint der letzten Runde (nur Backbone wird übernommen,
# der Head lernt aus dem GESAMTEN Dataset neu — eingebautes Replay),
# 3000 Steps statt 15000, LR 0.02 statt 0.08. Laufzeit ~25 min.
#
# Usage (auf dem Mac, im Ordner mit venv/models/dataset):
#   STEPS=3000 LR=0.02 bash mac_finetune_v1.sh
#
# Voraussetzung: unified.zip liegt hier frisch (von spielwiese gezogen).

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "kein venv — erst die Voll-Einrichtung: bash mac_train_v5.sh"; exit 1
fi
if [ ! -f unified.zip ]; then
    echo "unified.zip fehlt — von spielwiese /opt/pycentauri/data/train/ holen"; exit 1
fi

# Warm-Start vom letzten Checkpoint der vorherigen Runde, falls vorhanden
LAST_CKPT=$(ls -1d training/ckpt-* 2>/dev/null | sort -V | tail -1 || true)
export CKPT="${LAST_CKPT:-}"
export MODEL_OUT="training-light"
export STEPS="${STEPS:-3000}"
export LR="${LR:-0.02}"

if [ -n "$CKPT" ]; then
    echo "Light-Runde: warm start von $CKPT"
else
    echo "Light-Runde: kein vorheriger Checkpoint — Start von der COCO-Basis"
fi

bash mac_train_v5.sh

echo
echo "Nach ${STEPS}/${STEPS}: bash mac_export_v4.sh && scp model-result.zip spielwiese:/home/tim/model-result-v2.zip"
