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
two_d = [k for k, d in enumerate(outs) if len(d["shape"]) == 2 and d["shape"][0] == 1]
rng = np.random.default_rng(7)
probes = {
    "noise": rng.random(shape[1:], dtype=np.float32),
    "black": np.zeros(shape[1:], dtype=np.float32),
    "midgray": np.full(shape[1:], 0.5, dtype=np.float32),
}
# int8/uint8-Modelle brauchen ihr eigenes Input-Dtype/Range-Format
def prep(img):
    if inp["dtype"] == np.uint8:
        return (img * 255.0).clip(0, 255).astype(np.uint8)
    return img.astype(inp["dtype"])
# Robust: von jedem [1,N]-Output die Spreizung ueber die drei Proben
# messen — der Score-Tensor eines gesunden Modells variiert mit dem
# Input (Klassen-IDs bleiben konstant). Konstant = input-blind.
probe_max = {k: [] for k in probes}
for k, img in probes.items():
    i.set_tensor(inp["index"], prep(img)[np.newaxis])
    i.invoke()
    for j in two_d:
        probe_max[k].append(float(np.max(i.get_tensor(outs[j]["index"]))))
spread = 0.0
for j in range(len(two_d)):
    vals = [probe_max[k][j] for k in probes]
    spread = max(spread, max(vals) - min(vals))
print(f"probe_max={probe_max} spread={spread:.4f}")
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

# GATE 3: Acceptance-Separation — der Kandidat muss auf den bekannten
# Positiv-Frames hoeher scoren als auf den Negativ-Frames (min pos > max
# neg). Runde 2b waere hier durchgefallen (konstant 0.23/0.285 ueber alles).
SEP_LOG=$("$PY" - "$WORK" <<'PY10'
import io, sys, numpy as np
from pathlib import Path
from ai_edge_litert.interpreter import Interpreter
from PIL import Image

work = Path(sys.argv[1])
root = Path("/opt/pycentauri")
i = Interpreter(model_path=str(work / "spaghetti_int8.tflite"), num_threads=2)
i.allocate_tensors()
inp = i.get_input_details()[0]
outs = i.get_output_details()
two_d = [k for k, d in enumerate(outs) if len(d["shape"]) == 2 and d["shape"][0] == 1]

def run(jpeg):
    img = np.array(Image.open(io.BytesIO(jpeg)).convert("RGB").resize(tuple(inp["shape"][1:3])), dtype=np.float32)
    if inp["dtype"] == np.uint8:
        t = img.astype(np.uint8)
    else:
        t = img.astype(inp["dtype"])
    i.set_tensor(inp["index"], t[np.newaxis])
    i.invoke()
    return max(float(np.max(i.get_tensor(outs[j]["index"]))) for j in two_d)

pos = [run(p.read_bytes()) for p in sorted((root / "data/spaghetti-positive").glob("*.jpg"))]
neg = [run(p.read_bytes()) for p in sorted((root / "data/false-positives").glob("*.jpg"))]
if not pos or not neg:
    print(f"referenz-frames fehlen (pos={len(pos)} neg={len(neg)}) — Gate unmoeglich")
    sys.exit(0)  # keine Referenz -> nicht blockieren
print(f"acceptance: min(pos)={min(pos):.3f} max(neg)={max(neg):.3f}")
if min(pos) <= max(neg):
    print("KEINE SEPARATION — Kandidat ist schlechter als die Referenz verlangt")
    sys.exit(4)
PY10
)
SEP_RC=$?
logger -t model-deploy "GATE 3 (rc=$SEP_RC): $SEP_LOG"
if [ "$SEP_RC" -ne 0 ]; then
    logger -t model-deploy "Deploy ABGEBROCHEN — Acceptance-Fail (keine Separation). ZIP bleibt liegen."
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
# OPTION A (Tim/Mac 4.11.): int8-CPU servieren — der Edge-TPU-Compile
# degradet das Modell (POS 0.383-0.555 vs int8 0.648-0.715). Das stale
# _edgetpu-File MUSS aus dem Load-Pfad, sonst lädt der Dienst via
# Coral-Pfad das ALLE Modell (der Sibling-Fallback greift nur bei
# Delegate-Fail — der Coral läuft aber!).
mv "$MODELS/spaghetti_edgetpu.tflite" "$MODELS/spaghetti_edgetpu.tflite.disabled" 2>/dev/null || true
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
