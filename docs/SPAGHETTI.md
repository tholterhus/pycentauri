# Failed-print detection ("spaghetti")

> Moved out of the README - the overview and quick start live there;
> this document holds the details. The deep design record lives in
> [CORAL_SPAGHETTI_DETECTION.md](CORAL_SPAGHETTI_DETECTION.md).

## Spaghetti detection (Coral USB / Edge TPU)

While a print is running, pycentauri watches the printer's own webcam
through an object-detection model and raises an alert when the print has
failed into a stringy mess ("spaghetti"). There are two equally
supported ways to run it: **without a Coral** (the default for most
people) the analysis runs on your computer's CPU at ~200 ms per look —
**your camera frames never leave your network**; the only thing that
ever travels outside is the optional Telegram message (to *your own*
bot — strictly one-way, nothing is ever received), and the optional
update check is off by default.
**with a Google Coral USB Accelerator** its AI chip does the same in
~5–15 ms — the stick is purely an accelerator, never a requirement.
⚠️ **Measure before you trust the compiled model.** The Edge-TPU compiler
requantizes the TPU segment and can degrade detection quality — with our
v2 retrain the compiled variant lost the positive/negative separation the
int8 model kept (0.383–0.555 vs 0.648–0.715 on the reference frames).
Score both variants on your own reference frames and serve the better
one (the loader picks `*_edgetpu.tflite` when a Coral is present — see
the "Ship models in pairs" notes below). QAT (quantization-aware
training) is the path back to TPU parity.
The detector taps the same
shared camera stream as the dashboard (the printer never sees a second
camera connection), skips the first `--detect-grace` seconds of a print,
and fires at most one alert per print when ≥ 4 of the last 6 frames show a
detection. The dashboard gains a **DETECT** panel with live state and
evidence snapshots.

### Quick start

```sh
# 1. install with the detection extra (see INSTALL.md for a full walkthrough)
pip install 'pycentauri[detect,server]'

# 2. fetch a model — the repository ships none
./scripts/fetch-smoke-model.sh

# 3. optional: install the Edge TPU runtime for a Coral
#    (see INSTALL.md, "Failed-print detection (Coral) — step by step")
#    No Coral? Skip this entirely — plain CPU is the normal path
#    (~200 ms per look instead of ~5-15 ms). Everything else behaves
#    identically, just slower.
#    The CPU path works on x86-64, ARM-64 (Raspberry Pi) and macOS.
#    The Coral runs on Linux x86-64 AND ARM-64 (e.g. Raspberry Pi);
#    only the model *compiler* is x86-64-only (see scripts/train/README.md).

# 4. verify and run
centauri detect check
centauri server --host printer.example --enable-control \
    --detect --detect-action notify
```

Notes:

- **A trained model is bundled** — `DETECT_MODEL=auto` (the default)
  finds it, whether you installed via pip or cloned the repo. Deployed
  your own model into `data/models/`? That one wins. The optional smoke
  model (`scripts/fetch-smoke-model.sh`) detects everyday objects, not
  spaghetti, and exists purely to test the pipeline. How the bundled
  model was made: [`scripts/train/README.md`](scripts/train/README.md).
- **Network load & cadence**: detection analyses one frame per second
  by default — raise it via `--detect-interval` / `DETECT_INTERVAL`
  (service config) if you want it lazier. Note what does and does not
  cost network: the camera stream itself is the only real traffic (the
  printer sends one continuous JPEG flow ("MJPEG"), a few Mbit/s, to
  the server; every UI
  viewer pulls its own copy from the server), while the detection adds
  nothing — it just picks frames from that shared stream. A Telegram
  alert costs one ~25 KB image.
- **Default action is notify only**: evidence frames land in
  `data/evidence/` (and optionally a webhook). Arming `pause|stop`
  additionally requires `--enable-control`. The armed action **persists
  across service restarts**; `DETECT_ACTION` in the config is only the
  initial default.
- **Ship models in pairs**: an Edge-TPU-compiled `*_edgetpu.tflite` cannot
  execute on the CPU — on a machine without a Coral the software
  automatically loads the uncompiled CPU variant (`<name>.tflite`) instead.
  Note the compiled variant can be *worse*: the compiler requantizes the
  TPU segment and may degrade separation (our v2 retrain lost it — see the
  Coral section above). Score both and keep the better one live.
- **Optional Telegram push**: with `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`
  (in `/etc/pycentauri.conf`, or `--telegram-token` / `--telegram-chat-id`
  on the CLI) every alert goes to your chat — a one-liner with the camera
  frame. This covers detections **and** print-state changes (paused,
  filament switch, aborted, finished, error). Setup: create a bot via
  @BotFather, message it once, read the chat id from
  `https://api.telegram.org/bot<token>/getUpdates` — in the returned
  JSON look for `"chat":{"id":123456789,...}`; that number is your chat
  id). Empty values disable it. Telegram is strictly **one-way**:
  pycentauri never receives anything from Telegram — no polling, no
  webhook, no listening of any kind.
- **Optional bot password**: if you secure your bot with a password in
  @BotFather, put it in `TELEGRAM_PASSWORD` (or `--telegram-password`);
  pycentauri attaches it to every outgoing Telegram request as the
  `X-Telegram-Bot-Api-Secret-Token` header — the documented place for a
  bot secret. Without a bot password nothing needs to be configured:
  token + chat id are enough to post.
- Standalone without the HTTP server: `centauri detect watch --host …`.
  Model debugging against arbitrary JPEGs: `centauri detect test img.jpg`.
- Any SSD TFLite detector with `TFLite_Detection_PostProcess` outputs and a
  `.txt` label sidecar works.

### What the model detects — and what it doesn't

The shipped model knows exactly **one failure mode: spaghetti** (a print
that has detached and turned into a stringy mess). It does **not**
detect warping, zits/scars, layer shifts, or other defect types — those
require a retrained multi-class model (spaghetti, blobs, cracks,
warping), which in turn needs labeled examples of each failure mode on
your own camera. Until then: treat the detector as a runaway-print
alarm, not a general print-quality inspector.

### Trained model & training data attribution

A trained 1-class spaghetti model (SSD MobileNet V2 320×320) ships as
assets of the [v0.13.0 release](https://github.com/tholterhus/pycentauri/releases/tag/v0.13.0):
`spaghetti_edgetpu.tflite` (the Coral variant),
`spaghetti.tflite` (int8, the CPU sibling),
`spaghetti-float32.tflite`, `spaghetti.txt` (labels) and
`model-result.zip` (the original export bundle).

**One manual step is required**: the pip package does not bundle any
model — download the files above into `data/models/` (the installer
only auto-fetches the COCO *smoke* model as a pipeline test) and point
`DETECT_MODEL` at `data/models/spaghetti_edgetpu.tflite`. Then run
`centauri detect check`; details in [INSTALL.md](INSTALL.md).

The model was trained on 7,295 images / 13,791 boxes assembled from:

- "3D Printer Spaghetti Detection" dataset by training-l9tjj (Roboflow
  Universe, Public Domain)
- "Spaghetti" dataset by aiot-innowork (Roboflow Universe, CC BY 4.0) —
  modified: only the spaghetti class subset used
- Base architecture: SSD MobileNet V2 320×320, TensorFlow Object
  Detection API (Apache-2.0)
