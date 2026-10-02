# Spaghetti-model training plan (v0.13.0 target)

Step-by-step plan to produce a real failed-print model for pycentauri:
4 classes (**spaghetti, blobs, cracks, warping**), trained on community
datasets + this printer's own frames, exported fully int8, compiled for
the Edge TPU, deployed as a model pair in `data/models/`.

> This document is the handoff: any fresh session (or the human alone)
> can resume from any checkbox. Collect-mode frames accumulate in
> `data/collect/` while printing — the DETECT panel shows the count.

## Status checklist

- [x] Collect mode live in dashboard (`POST /api/detect/collect`)
- [x] `scripts/fetch-smoke-model.sh` (pipeline smoke test)
- [x] `centauri detect test` (model-vs-image validation tool)
- [ ] **User: print with Collect enabled** — target 600+ own frames,
      incl. 50–100 real failure frames (deliberate filament pulls)
- [ ] **User: free Roboflow account + API key** →
      `install -m 600 /dev/null ~/.roboflow-key` (paste key)
- [ ] **User: browse candidates below in the browser**, note license +
      image count in `scripts/roboflow-candidates.txt`
- [ ] Run `scripts/roboflow-harvest.py check` (needs the key)
- [ ] Run `scripts/roboflow-harvest.py download` (YOLO-format zips →
      `data/train/roboflow/`)
- [ ] Label own frames (Label Studio, see below) — classes must match
- [ ] `scripts/prepare-dataset.py` (assembly: Roboflow + own frames →
      unified Pascal VOC + train/val split) — write when data exists
- [ ] Colab training run (steps below)
- [ ] int8 TFLite export + `edgetpu_compiler`
- [ ] Deploy model pair + labels to `data/models/`, validate with
      `centauri detect test` over labeled samples
- [ ] Tune threshold/window; arm via dashboard (Pause first, Stop later)
- [ ] Release as v0.13.0 (CHANGELOG, docs, tag)

## Data sources

| Source | Content | License | Status |
|---|---|---|---|
| Own frames (`data/collect/`) | this printer, this camera, this lighting | n/a (ours) | accumulating |
| Roboflow Universe candidates (see `scripts/roboflow-candidates.txt`) | labeled YOLO boxes, several hundred to 1000+ images each | **verify per dataset** (Universe: usually CC BY 4.0 / CC0, some private) | to check in browser |
| Deliberate failure prints | real spaghetti on this camera | n/a | 2–3 print sessions |

Class set (from the verified community model design — see
"Reference model"): `spaghetti`, `blobs`, `cracks`, `warping`.
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
