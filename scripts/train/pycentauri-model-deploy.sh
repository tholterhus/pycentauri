#!/bin/bash
# Auto-deploy: wartet auf model-result-v2.zip (vom MacBook per scp nach
# /home/tim/ geschoben), kompiliert Edge-TPU, deployt das Modell und
# startet pycentauri neu. Idempotent (SHA-Zustand), laeuft per cron.
set -u
SRC=/home/tim/model-result-v2.zip
STATE=/var/lib/pycentauri-model-deploy.sha256
MODELS=/opt/pycentauri/data/models
PY=/opt/pycentauri/venv/bin/python

[ -f "$SRC" ] || exit 0
sha=$(sha256sum "$SRC" | cut -d' ' -f1)
[ -f "$STATE" ] && [ "$(cat "$STATE")" = "$sha" ] && exit 0

logger -t model-deploy "neues Modell-ZIP gefunden ($SRC), deploye ..."
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
$PY -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$SRC" "$WORK" || { logger -t model-deploy "ZIP kaputt"; exit 1; }
ls "$WORK"/spaghetti_int8.tflite >/dev/null 2>&1 || { logger -t model-deploy "ZIP ohne spaghetti_int8.tflite — Inhalt: $(ls "$WORK" | tr '\n' ' ')"; exit 1; }

# Edge-TPU-Compiler (self-contained x86-64 Bundle, laeuft ohne Installation).
# WICHTIG: der Wrapper im Bundle loest sein _bin-Verzeichnis relativ zu $0
# auf — daher das ganze Verzeichnis nach /opt/edgetpu-compiler und Direktpfad
# (kein Symlink nach /usr/local/bin, und das -o-Verzeichnis muss existieren).
ETC=/opt/edgetpu-compiler/edgetpu-compilers-main/16.0/edgetpu_compiler
if [ ! -x "$ETC" ]; then
    logger -t model-deploy "lade edgetpu_compiler Bundle ..."
    curl -fsSL https://github.com/mbrooksx/edgetpu-compilers/archive/refs/heads/main.tar.gz -o "$WORK/etc.tar.gz" \
    && { mkdir -p /opt/edgetpu-compiler && tar -xzf "$WORK/etc.tar.gz" -C /opt/edgetpu-compiler && chmod +x "$ETC"; } \
    || logger -t model-deploy "edgetpu_compiler-bundle install fehlgeschlagen"
fi
EDGETPU=""
if [ -x "$ETC" ]; then
    mkdir -p "$WORK/edgetpu"
    (cd "$WORK" && "$ETC" -o "$WORK/edgetpu" spaghetti_int8.tflite >/dev/null 2>&1) \
        && EDGETPU="$WORK/edgetpu/spaghetti_int8_edgetpu.tflite" \
        || logger -t model-deploy "edgetpu-compile fehlgeschlagen — deploye nur CPU-Modell"
else
    logger -t model-deploy "edgetpu_compiler nicht verfuegbar — deploye nur CPU-Modell"
fi

# Backup + Deployment
BK="$MODELS-v2-backup-$(date +%Y%m%d-%H%M)"
cp -r "$MODELS" "$BK"
cp "$WORK/spaghetti_int8.tflite" "$MODELS/spaghetti.tflite"
[ -f "$WORK/spaghetti.tflite" ] && cp "$WORK/spaghetti.tflite" "$MODELS/spaghetti-float32.tflite"
[ -f "$WORK/spaghetti_labels.txt" ] && cp "$WORK/spaghetti_labels.txt" "$MODELS/spaghetti.txt"
[ -n "$EDGETPU" ] && [ -f "$EDGETPU" ] && cp "$EDGETPU" "$MODELS/spaghetti_edgetpu.tflite"
chown pycentauri:pycentauri "$MODELS"/*

# Threshold 0.5 (neues Modell) — idempotent
grep -q '^DETECT_THRESHOLD=0.5$' /etc/pycentauri.conf || sed -i 's/^DETECT_THRESHOLD=.*/DETECT_THRESHOLD=0.5/' /etc/pycentauri.conf

CHECK=$(/opt/pycentauri/venv/bin/centauri detect check 2>&1 | tail -2)
systemctl restart pycentauri.service
sleep 4
ST=$(curl -s --max-time 8 http://127.0.0.1:8787/api/detect | head -c 120)
echo "$sha" > "$STATE"
mv "$SRC" "$SRC.deployed-$(date +%Y%m%d-%H%M)"
logger -t model-deploy "deploy fertig. check: $CHECK | api: $ST"
