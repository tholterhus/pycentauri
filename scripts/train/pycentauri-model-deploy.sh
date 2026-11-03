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

# HARD-GATE: input-sensitivity probe. Ein kollabiertes (input-blindes) Modell
# darf NIE live gehen — Runde 1+2 des v2-Retrains wären beide hier hängen
# geblieben (konstante Scores für Foto/Schwarz/Noise). Reihenfolge:
#  1) int8-Kandidat muss mit dem CPU-Interpreter ladbar sein (XNNPACK-Fail
#     =Abort), 2) Scores für 3 differente Inputs muessen streuen.
GATE_LOG=$("$PY" - "$WORK" <<'PY9'
import io, sys, numpy as np
from pathlib import Path
from ai_edge_litert.interpreter import Interpreter

work = Path(sys.argv[1])
model = work / "spaghetti_int8.tflite"
try:
    i = Interpreter(model_path=str(model), num_threads=2)
    i.allocate_tensors()
except Exception as err:
    print(f"int8 load failed: {str(err)[:120]}")
    sys.exit(3)
inp = i.get_input_details()[0]
shape = [1] + list(inp["shape"][1:])
outs = i.get_output_details()
scores_idx = [k for k, d in enumerate(outs) if len(d["shape"]) == 2 and d["shape"][-1] == 1]
rng = np.random.default_rng(7)
probes = {
    "noise": rng.random(shape[1:], dtype=np.float32),
    "black": np.zeros(shape[1:], dtype=np.float32),
    "midgray": np.full(shape[1:], 0.5, dtype=np.float32),
}
scores = {}
for k, img in probes.items():
    i.set_tensor(inp["index"], img)
    i.invoke()
    vals = [float(np.max(i.get_tensor(outs[j]["index"]))) for j in scores_idx]
    scores[k] = max([v for v in vals if 0.0 <= v <= 1.0] or [0.0])
spread = max(scores.values()) - min(scores.values())
print(f"scores={scores} spread={spread:.4f}")
if spread < 0.05:
    print("INPUT-BLIND (kollabiert)")
    sys.exit(4)
PY9
)
GATE_RC=$?
logger -t model-deploy "HARD-GATE (rc=$GATE_RC): $GATE_LOG"
if [ "$GATE_RC" -ne 0 ]; then
    logger -t model-deploy "Deploy ABGEBROCHEN — Kandidat ist input-blind oder unladbar. ZIP bleibt liegen."
    exit 1
fi

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
