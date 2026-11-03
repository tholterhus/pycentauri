# HANDOVER — Training auf dem Mac (für neue KI-Session)

Du laufst jetzt AUF dem Mac und kannst direkt debuggen. Ziel: SSD
MobileNet v2 320 (1 Klasse: spaghetti) mit dem TensorFlow Object
Detection API trainieren → int8-TFLite → Edge-TPU-kompilieren.

## Stand (alles unter ~/train/pycentauri/scripts/train)

* `venv/` — Python 3.11 + TF 2.15.1 (über `mac_train_v5.sh` gebaut)
* `dataset/` — 7295 Bilder (train 5836 / val 1459) + `classes_label_map.pbtxt`
* `train.record` / `val.record` — TFRecords, fertig (5836/1459 Bilder, 13791 Boxen)
* `models/` — OD-API-Klon (research/object_detection), protos generiert
* `unified.zip` — Quelldatensatz
* Ablauf bisher scheiterte IMMER im `model_main_tf2.py`-Import bei
  `control_flow_ops.case` — **Ursache gefunden:** der `pip install` der
  OD-Abhängigkeiten hat **still TensorFlow auf 2.20 hochgezogen**
  (unpinned `tensorflow_io` zieht TF ≥ 2.16 nach sich), und TF 2.20 hat
  den Estimator entfernt, den das OD-API braucht.

## Erster Fix-Versuch (vermutlich Lösung)

1. `./venv/bin/python -c "import tensorflow as tf; print(tf.__version__)"`
   → zeigt vermutlich **2.20** (zu neu).
2. TF 2.15.1 wiederherstellen UND tensorflow_io auf die TF-2.15-kompatible
   Version pinnen:
   ```sh
   ./venv/bin/pip install "tensorflow-cpu==2.15.1" "tensorflow_io==0.37.0" "numpy==1.26.4" "protobuf==4.25.8"
   ./venv/bin/python -c "import tensorflow as tf; import tensorflow_io; print('OK', tf.__version__)"
   ```
   (Wenn `tensorflow_io` import noch klemmt: Version 0.36.0 statt 0.37.0
   versuchen — die Release-Matrix: 0.36→TF 2.15, 0.37→TF 2.16.)
3. `bash mac_train_v5.sh` — das Skript überspringt erledigte Schritte,
   baut die pipeline.config neu (Platzhalter-Fix ist drin) und startet
   detached mit `caffeinate`. `tail -f training.log` → `Step 100/15000`.

## Falls der Import-Kette immer noch etwas fehlt

Die fehlenden Module der Reihe nach ergänzen (`pip install X`), jede
ist ein Einzeiler-Fix. Bisher waren nötig: lxml, matplotlib,
pycocotools, tf-slim, lvis, tensorflow_io, scipy, opencv-python-headless,
tf-models-official (installiert mit `--no-deps`, siehe Skript).

## Nach 15000/15000

`bash mac_export_v4.sh` → `model-result.zip` → auf spielwiese nach
`/opt/pycentauri/data/models/` entpacken → `centauri detect check`
→ im Dashboard DETECT-Panel auf „Pause" armieren.

## Fallback (falls der Mac-Kram klemmt)

`colab_spaghetti.py` liegt im selben Ordner — der Colab-Weg (Browser,
GPU, 2–4 h) ist vollständig vorbereitet.
