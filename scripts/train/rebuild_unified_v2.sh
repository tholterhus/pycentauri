#!/usr/bin/env bash
# Rebuild the unified spaghetti training dataset — one shot.
#
# Merges:
#   1. Roboflow browser downloads (YOLOv8 zips) — put BOTH into
#      data/train/roboflow/  (see RETRAIN-RUNBOOK.md step 1)
#   2. The v2 increment (data/train/spaghetti-v2/ — 38 positives from the
#      shifted-part print + 423 negatives incl. the false-alarm material)
#
# Output: data/train/unified-rebuilt.zip + a count report.
# Nothing is deleted; re-runs are idempotent.
#
# Usage (on spielwiese):  bash scripts/train/rebuild_unified_v2.sh

set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root /opt/pycentauri

PY=venv/bin/python
RB=data/train/roboflow
WORK=data/train/rebuild
OUT=data/train/unified-rebuilt
V2=data/train/spaghetti-v2

mkdir -p "$RB" "$WORK"

echo "=== 1/4 Roboflow-Daten prüfen"
# Akzeptiert beides: entpackte Datensatz-Verzeichnisse (data.yaml) direkt in
# $RB, oder Zips, die hier nach $WORK entpackt werden.
shopt -s nullglob
zips=("$RB"/*.zip)
shopt -u nullglob
for z in "${zips[@]}"; do
    name=$(basename "$z" .zip)
    dest="$WORK/$name"
    echo "== entpacke $z"
    rm -rf "$dest"
    mkdir -p "$dest"
    "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$z" "$dest"
done
mapfile -t dirs < <(find "$RB" "$WORK" -maxdepth 2 -mindepth 1 -name data.yaml -printf '%h\n' | sort -u)
if [ "${#dirs[@]}" -ne 2 ]; then
    echo "FEHLER: ${#dirs[@]} statt 2 YOLO-Datensätze (data.yaml) in $RB[/$WORK]" >&2
    echo "  erwartet (Browser-Download, YOLOv8, zips ODER entpackt):" >&2
    echo "    - 3d-printer-spaghetti-detection (training-l9tjj, ~3177 Bilder)" >&2
    echo "    - spaghetti (aiot-innowork, 114 Bilder)" >&2
    ls -la "$RB" >&2
    exit 1
fi
# Fremdklassen-Namen normieren: der aiot-Export nennt die (einzige) Klasse
# 'detection' — prepare-dataset.py würde alle Boxen überspringen. Der
# Datensatz ist verifiziert 100% Spaghetti (aiot-innowork/Spaghetti, CC BY 4.0).
for d in "${dirs[@]}"; do
    "$PY" - "$d/data.yaml" <<'EOF'
import re, sys
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text()
m = re.search(r"names:\s*\[([^\]]*)\]", text)
if m and [n.strip().strip("\"'") for n in m.group(1).split(",")] == ["detection"]:
    print(f"  {p.parent.name}: Klasse 'detection' -> 'spaghetti' (verifiziert: aiot-innowork/Spaghetti, CC BY 4.0)")
    p.write_text(re.sub(r"names:\s*\[[^\]]*\]", "names: ['spaghetti']", text, count=1))
EOF
done
echo "== Datensätze: ${dirs[*]}"

echo
echo "=== 2/4 prepare-dataset.py (YOLO -> VOC, train/val-Split)"
rm -rf "$OUT"
"$PY" scripts/prepare-dataset.py \
    --roboflow "${dirs[0]}" \
    --roboflow "${dirs[1]}" \
    --out "$OUT" 2>&1 | tail -20

echo
echo "=== 3/4 v2-Increment dazumergen (Positives 1./2.11. + Negatives)"
merged=0; skipped=0
for split in train val; do
    mkdir -p "$OUT/$split/images" "$OUT/$split/annotations"
    for img in "$V2/$split/images/"*; do
        base=$(basename "$img")
        if [ -e "$OUT/$split/images/$base" ]; then
            skipped=$((skipped+1)); continue
        fi
        cp "$img" "$OUT/$split/images/$base"
        cp "$V2/$split/annotations/${base%.*}.xml" "$OUT/$split/annotations/"
        merged=$((merged+1))
    done
done
echo "== $merged v2-Dateien gemergt, $skipped übersprungen (Namenskollision)"

echo
echo "=== 4/4 ZIP bauen + Report"
"$PY" - "$OUT" <<'EOF'
import sys, zipfile, shutil
from pathlib import Path
from collections import Counter
import xml.etree.ElementTree as ET

out = Path(sys.argv[1])
zpath = Path(str(out) + ".zip")
shutil.make_archive(str(out), "zip", root_dir=out)

report = []
for split in ("train", "val"):
    imgs = sorted((out / split / "images").glob("*"))
    pos = neg = 0
    for x in sorted((out / split / "annotations").glob("*.xml")):
        root = ET.parse(x).getroot()
        (pos := pos + 1) if root.findall("object") else (neg := neg + 1)
    report.append(f"  {split}: {len(imgs)} Bilder | {pos} mit Boxen | {neg} Negative")

classes = (out / "classes.txt").read_text().split()
print("CLASSES:", classes)
print("\n".join(report))
print(f"ZIP: {zpath} ({zpath.stat().st_size/1e6:.1f} MB)")
print("ERWARTUNG (2026-11-03-Rebuild): ~3750 Bilder gesamt (3026 train / 723 val)")
EOF
echo
echo "FERTIG. Nächster Schritt laut RETRAIN-RUNBOOK.md (/home/tim/RETRAIN-RUNBOOK.md):"
echo "  unified-rebuilt.zip auf den Mac kopieren ODER in Colab hochladen."
