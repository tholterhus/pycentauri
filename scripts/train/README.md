# Spaghetti detection model — how it was made

The deployed model ships as release assets of
[v0.13.0](https://github.com/tholterhus/pycentauri/releases/tag/v0.13.0):
`spaghetti_edgetpu.tflite` (Coral), `spaghetti.tflite` (int8 CPU
sibling), `spaghetti-float32.tflite`, `spaghetti.txt` (labels) and
`model-result.zip` (the original export bundle). Drop them into
`data/models/` — the label sidecar resolves automatically.

## Model

SSD MobileNet V2 320×320, one class (`spaghetti`), fully int8
quantized (uint8 input), 4 SSD `TFLite_Detection_PostProcess` output
tensors. Edge-TPU build: 99/102 ops on the chip, ~5–23 ms inference;
the 3 remaining ops are the standard SSD postprocessing (CPU).

## Training data

7,295 images / 13,791 boxes, assembled by `scripts/prepare-dataset.py`
from Roboflow Universe exports plus frames collected from the printer
itself (`POST /api/detect/collect`):

- "3D Printer Spaghetti Detection" dataset by training-l9tjj
  (Roboflow Universe, **Public Domain**)
- "Spaghetti" dataset by aiot-innowork (Roboflow Universe, **CC BY 4.0**) —
  modified: only the spaghetti class subset used
- Base architecture: SSD MobileNet V2 320×320, TensorFlow Object
  Detection API (**Apache-2.0**)

## Method (reproduce it)

1. **Dataset** — assemble a unified YOLO/Pascal-VOC dataset with
   `scripts/prepare-dataset.py`, then build TFRecords with
   `scripts/train/build_tfrecords.py`.
2. **Train** — SSD MobileNet V2 320 fine-tuned from the COCO checkpoint
   with the TensorFlow Object Detection API (TF 2.15.1 — the Estimator
   it needs was removed in TF 2.16+, so pin it), 15,000 steps.
   `scripts/train/colab_spaghetti.py` contains the complete cell-by-cell
   Colab recipe (OD-API install, training, int8 export with a
   representative dataset, verification of the 4 postprocess tensors).
   The deployed model was trained exactly this way on local hardware.
3. **Compile for the Edge TPU** — the Google apt repo
   (`packages.cloud.google.com/apt`, `coral-edgetpu-stable`) returns
   403 even from Colab, so the compiler comes from the self-contained
   16.0 bundle at github.com/mbrooksx/edgetpu-compilers (x86-64
   binaries extracted from the Debian package; runs anywhere without
   installation):

   ```sh
   curl -fsSLO https://github.com/mbrooksx/edgetpu-compilers/archive/refs/heads/main.tar.gz
   tar xzf main.tar.gz && chmod +x edgetpu-compilers-main/16.0/edgetpu_compiler
   edgetpu-compilers-main/16.0/edgetpu_compiler spaghetti.tflite
   ```

4. **Verify + deploy** — `centauri detect check --model
   data/models/spaghetti_edgetpu.tflite` (expect `backend: edgetpu`),
   then point the service at it. Don't run `detect check/test` against
   the TPU while the service is watching it — concurrent device access
   makes libedgetpu re-enumerate the USB device.

## Road to v2 — the 4-class model

The current model sees only `spaghetti`. The planned v2 model adds
`blobs`, `cracks` and `warping`. The tooling is already class-agnostic
(`prepare-dataset.py` targets exactly those four classes); what v2 needs
is **labeled examples of each failure mode**. From scratch:

1. **Collect frames** — run prints with frame collection on
   (`POST /api/detect/collect {"enabled": true}`; frames land in
   `data/collect/`, FIFO-capped at 2,000). For real failure examples,
   deliberately pull the filament mid-print or remove supports early —
   50–100 failure frames are worth more than thousands of healthy ones.
2. **Label own frames** in Label Studio (self-hosted, no cloud):

   ```sh
   docker run -d -p 8080:8080 -v ~/label-studio:/label-studio/data heartexlabs/label-studio
   ```

   Project type: *Object detection with bounding boxes*; labels exactly
   `spaghetti, blobs, cracks, warping` (this order = class order).
   Import a sample (200–400 frames, healthy prints included — they get
   **no** boxes and are what keeps false alarms down). Export as
   **Pascal VOC XML** — that is the format `prepare-dataset.py` reads.
3. **Fetch the Roboflow datasets again** — the Universe website blocks
   scripted downloads (Cloudflare), so use a browser: open the dataset
   page → *Download Dataset* → format **YOLOv8** → unzip into
   `data/train/roboflow/<project>-v<version>/`. Verify licenses
   (CC0/CC BY 4.0 only) on the dataset page.
4. **Assemble + train** — the exact chain above: `prepare-dataset.py`
   (merges Roboflow exports + Label Studio XML) → `build_tfrecords.py`
   → the Colab recipe in `colab_spaghetti.py` (set the class list to 4)
   → Edge-TPU compile → deploy both variants. Acceptance: per-class
   recall eyeballed with `centauri detect test`, then the same
   criteria below.

## Acceptance criteria used

- Recall ≥ 90 % on spaghetti in the validation split at the shipped
  threshold.
- ≤ 1 false alarm per print-hour on healthy-print validation frames.
- One full print in notify mode with zero false alerts before arming
  pause/stop.
