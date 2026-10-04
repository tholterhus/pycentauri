# Spaghetti-model training plan (v0.13.0 target)

Step-by-step plan to produce a real failed-print model for pycentauri:
4 classes (**spaghetti, blobs, cracks, warping**), trained on community
datasets + this printer's own frames, exported fully int8, compiled for
the Edge TPU, deployed as a model pair in `data/models/`.

> This document is the handoff: any fresh session (or the human alone)
> can resume from any checkbox. Collect-mode frames accumulate in
> `data/collect/` while printing — the count is visible via
> `GET /api/detect` (field `collect`).

## Status checklist

Status 2026-10-04 (evening): **v1 model compiled and deployed** — the
service runs `spaghetti_edgetpu.tflite` on the Coral (backend edgetpu,
~23 ms probe, 0/4 false hits on healthy frames at 0.65). Open: one
notify-mode print as acceptance run, then arming; own-frame labeling
for the 4-class v2 model.

- [x] Collect mode live in dashboard (`POST /api/detect/collect`)
- [x] `scripts/fetch-smoke-model.sh` (pipeline smoke test)
- [x] `centauri detect test` (model-vs-image validation tool)
- [x] **User: print with Collect enabled** — 1,011 own frames collected
      (200-per-print cap hit); no deliberate failure frames yet
- [x] **User: free Roboflow account + API key** → `~/.roboflow-key`
- [x] **User: browse candidates below** — licenses verified: Public
      Domain (training-l9tjj) + CC BY 4.0 (aiot-innowork)
- [x] `scripts/roboflow-harvest.py check` (3,177 images in v3)
- [x] Roboflow download — via browser (the script route is
      Cloudflare-blocked); unified dataset: 7,295 images / 13,791 boxes
- [ ] Label own frames (Label Studio, see below) — classes must match;
      feeds the 4-class v2 model (v1 = Roboflow data only)
- [x] `scripts/prepare-dataset.py` + TFRecords (built on the Mac:
      5,836 train / 1,459 val)
- [x] Training run — done **on the Mac** (`mac_train_v5.sh`, TF 2.15.1),
      not Colab; int8 TFLite exported (`mac_export_v4.sh`)
- [x] `edgetpu_compiler` → `spaghetti_edgetpu.tflite` — **compiled
      locally on spielwiese** via the self-contained 16.0 bundle from
      github.com/mbrooksx/edgetpu-compilers (the Google apt repo 403s
      even from Colab; 99/102 ops on TPU). See
      `scripts/train/colab_edgetpu_compile.md` for the fallback.
- [x] Deploy model pair + labels to `data/models/`, validate — service
      switched to `spaghetti_edgetpu.tflite` (unit sed), `detect check`:
      backend edgetpu, 23 ms; 4 healthy frames × 0.65: 0 detections
- [ ] Tune threshold/window; arm via dashboard (Pause first, Stop later)
      — acceptance run: one full print in notify mode, zero false alerts
- [x] Release as v0.13.0 (CHANGELOG, docs, tag) — model artifacts
      attached to the release; deploy/acceptance checkboxes stay open

## Data sources

| Source | Content | License | Status |
|---|---|---|---|
| Own frames (`data/collect/`) | this printer, this camera, this lighting | n/a (ours) | accumulating |
| Roboflow Universe candidates (see `scripts/roboflow-candidates.txt`) | labeled YOLO boxes, several hundred to 1000+ images each | **verify per dataset** (Universe: usually CC BY 4.0 / CC0, some private) | to check in browser |
| Deliberate failure prints | real spaghetti on this camera | n/a | 2–3 print sessions |

Class set (from the verified community model design — see
"Reference model"): `spaghetti`, `blobs`, `cracks`, `warping`.

### Collect mechanics & retention (how the frames are made)

* While the printer reports `print_status == 13`, frames are written
  to `data/collect/`, named by UTC timestamp. Layer-aware schedule
  (2026-10-05): the **first `collect_first_layers` layers (3)** collect
  densely every `collect_interval_s` (**5 s**) — the bed-adhesion zone —
  then one frame per `total_layers // collect_max_per_print` **layer
  changes** (e.g. 2,000-layer print / 200 budget → every 10th layer),
  floor-limited by `collect_min_gap_s` (**20 s**). Result: the budget
  spreads over the WHOLE print instead of the first 17 minutes. Without
  layer info the old time-interval mode applies as fallback.
* Measured reality (first collection run, ~65 min print): 811 frames,
  ~20 MB total (~25 KB per 640×360 JPEG).
* Retention caps (both shown in the DETECT panel):
  * `collect_max_per_print` (**200**): a print stops collecting after
    200 frames (~the first 17 minutes) — near-duplicate frames add
    nothing to training.
  * `collect_max_files` (**2000**): global FIFO — when the directory
    exceeds this, the oldest frames are deleted automatically after
    every save. Worst-case disk usage ≈ 50 MB.
* Without the caps a 12 h print would produce ~8,600 frames ≈ 215 MB.
* Both limits live in `DetectConfig` (`src/pycentauri/detect/pipeline.py`);
  collection is toggled via `POST /api/detect/collect` and **persists
  across service restarts** (dotfile `data/collect/.collecting`).
Frames showing a healthy print get **no boxes** (negative examples —
do not skip them; they are what keeps false alarms down).

## Labeling (own frames)

1. Label Studio, self-hosted, no cloud:
   `docker run -d -p 8080:8080 -v ~/label-studio:/label-studio/data heartexlabs/label-studio`
   (or `pip install label-studio && label-studio`).
2. Project type: Object detection with bounding boxes; labels =
   `spaghetti, blobs, cracks, warping`.
3. Import a *sample* of `data/collect/` frames (200–400 first pass) —
   Roboflow data comes pre-labeled, own frames are the domain-specific
   gold. Export as **Pascal VOC XML** (matches
   `scripts/prepare-dataset.py`).

## Training (Google Colab, free tier)

Model: **SSD MobileNet V2 320×320** (Coral-proven) fine-tuned from the
COCO checkpoint, then EfficientDet-Lite0 as variant B if accuracy
needs it.

1. Colab notebook: TensorFlow 2 Object Detection API installation
   (github.com/tensorflow/models → research/object_detection, the
   official Colab tutorial works).
2. Config: `ssd_mobilenet_v2_320x320_coco17_tpu-8.config` — set
   `num_classes: 4`, paths to the assembled dataset, batch 12–16,
   ~25k–40k steps.
3. Export: `export_tflite_graph_tf2.py` → then
   `tf.lite.TFLiteConverter` with
   `optimizations=[tf.lite.Optimize.DEFAULT]`, int8 inference,
   **representative dataset** (a few hundred preprocessed training
   images — required for full int8), and
   `supported_ops=[tf.lite.OpsSet.TFLITE_BUILTINS_INT8]` — verify the
   output tensors are the 4 SSD postprocess tensors (boxes [1,N,4],
   classes, scores, count) before compiling.
4. Local evaluation: `centauri detect test` over the val split —
   eyeball per-class hits/misses.

## Edge TPU compilation

The `edgetpu_compiler` binary is the one part without a PyPI route.
Sources to try (in order):

1. `https://storage.googleapis.com/…` — coral.ai download links point
   into GCS buckets (reachable from most networks; test
   `https://coral.ai/docs/edgetpu/compiler/` for the current link).
2. If the network blocks it: run the compiler on any other machine
   (5-minute step) or inside a container that has the .deb.
3. Input: the int8 TFLite from step 3. Output:
   `spaghetti_edgetpu.tflite` (+ report which ops fell back to CPU).

Deployment pair (both must ship — see detect/backend.py sibling
fallback): `spaghetti_edgetpu.tflite` + `spaghetti.tflite` +
`spaghetti_edgetpu.txt` (4 lines, class order = training order).
Decision to make at deploy time: commit the model pair into the
repository (`models/` tracked, ~10 MB) so strangers get a turnkey
setup, or keep them in `data/models/` and document the download.

## Model training data (attribution — required for shipping)

* "3D Printer Spaghetti Detection" dataset by training-l9tjj
  (Roboflow Universe, Public Domain)
* "Spaghetti" dataset by aiot-innowork (Roboflow Universe, **CC BY 4.0**)
  — modified: only the spaghetti class subset used
* Base architecture: SSD MobileNet V2 320×320, TensorFlow Object
  Detection API (Apache-2.0)

→ Copy this block into the README when the model ships. The int8 model
file itself may be distributed in this repository.

## Mac training option (no cloud, no GPU rental)

`scripts/train/mac_train.sh` + `mac_export.sh` run the whole training
locally on a Mac (M-series ~3-6 h, Intel ~8-15 h): one-time setup,
then a detached `caffeinate`-wrapped training that survives idle
timers. The Mac must stay connected to power; lid closed works only in
clamshell mode with an external display.

## Acceptance criteria before arming

* Recall on spaghetti in the validation split: ≥ 90 % at the shipped
  threshold (0.5–0.65).
* False alarms: ≤ 1 per print-hour on healthy-print validation frames.
* `centauri detect check` reports `backend: edgetpu` and ≤ 20 ms
  steady-state inference.
* One full print with action=notify producing zero false alerts before
  switching to `pause`.

## Reference model (verified 2026-10-02)

github.com/CookiezRGood/klipper-print-failure-detection ships a
YOLOv8 640×640 float32 TFLite (output [1,8,8400], 4 classes: spaghetti,
blobs, cracks, warping) — 928 ms/frame on x86 CPU, not Edge-TPU
compiled, **no license file** (cannot be redistributed or shipped
here). Value taken from it: the 4-class design and the confirmation
that ~6 MB TFLite detectors work at this task. A YOLO backend mode for
pycentauri (decode + NMS) remains a possible future extension.
