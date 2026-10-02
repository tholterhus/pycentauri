# Coral USB spaghetti-detection — integration plan

Status: proposal, not yet implemented. This doc is the agreed plan for
adding failed-print ("spaghetti") detection to pycentauri using a Google
Coral USB Accelerator (Edge TPU).

## What we build on (current state)

* `camera.py` — single-shot MJPEG snapshot (first complete JPEG from
  `:3031/video` / CC2 `:8080/video`).
* `mjpeg_broadcast.py` — `CameraBroadcaster`: one shared upstream MJPEG
  connection fanned out to many subscribers, whole-frame reassembly,
  slow-consumer frame drop, idle close 30 s after the last subscriber.
  **The printer's camera server starves under connection churn**, so any
  new frame consumer must go through this broadcaster, never open its
  own MJPEG connection.
* `server.py` — `PrinterManager` (one long-lived printer connection),
  control gated behind `--enable-control`, `printer.watch()` for live
  status pushes. `print_status == 13` = printing.
* `client.py` / `cc2.py` — `pause()`, `resume()`, `stop()` with the same
  semantics on both models.
* `data/` — empty; reserved for models and detection evidence.
* Runtime: Debian 13 (trixie) LXC on Proxmox, x86_64, Python 3.13 venv.

## Phase 0 — system prerequisites (Coral not yet plugged in)

1. **Proxmox USB passthrough.** The Coral presents as
   `1a6e:089a` (Global Unichip / Apex). On the Proxmox host add to
   `/etc/pve/lxc/<vmid>.conf`:
   `usb0: host=1a6e:089a,usb3=1` (Web UI: VM → Resources → Add → USB
   Device → Vendor/Device). Hot-plug works on recent Proxmox.
2. **Edge TPU runtime.** Inside the container install Google's
   `libedgetpu1-std` from `packages.google.com/on-device-ai` (bookworm
   repo). The C library + udev rules install fine on trixie; only the
   *Python* bindings are version-tied (we don't use them — see Phase 1).
   This provides `libedgetpu.so.1` and the `/dev/apex/0` udev rule.
3. **Permissions.** If udev inside the unprivileged LXC doesn't create
   `/dev/apex/0` automatically: `SUBSYSTEM=="apex" MODE="0660"` rule +
   add the service user to the `apex`/`plugdev` group, or `mknod` with
   a `lxc.cgroup2.devices.allow` entry as last resort.
4. **Verify:** plug in, `ls /dev/apex/0`, then the
   `centauri detect check` command from Phase 1.

## Phase 1 — inference backend (runs on today's Python 3.13)

`pycoral` is dead-ended on old Python (≤3.10 wheels) and Google's
`python3-tflite-runtime` stops at 3.11 — **but** `ai-edge-litert`
(Google's LiteRT successor) ships `cp313 manylinux x86_64` wheels
(verified: 2.2.0 on PyPI). So no sidecar venv, no Docker: the detector
runs in-process against the same 3.13 venv, loading the Edge TPU
delegate from the C library installed in Phase 0:

```python
from ai_edge_litert.interpreter import Interpreter, load_delegate

def make_interpreter(model_path: str, *, use_edgetpu: bool) -> Interpreter:
    delegates = [load_delegate("libedgetpu.so.1")] if use_edgetpu else []
    return Interpreter(model_path=model_path, experimental_delegates=delegates)
```

New optional extra in `pyproject.toml`:

```toml
detect = ["ai-edge-litert>=2.2", "numpy>=1.26", "pillow>=10"]
```

New module `src/pycentauri/detect/`:

* `backend.py` — interpreter factory: Edge TPU if `/dev/apex/0` exists
  **and** the model is `_edgetpu.tflite`-compiled, else CPU TFLite
  fallback (same model file, just slower). Logs which backend is live.
  JPEG bytes → Pillow decode → resize to model input → quantized int8
  tensor. Inference calls are blocking (~5–15 ms on TPU) → run in an
  executor, never on the event loop.
* `pipeline.py` — the detection loop (Phase 2).
* `cli_check()` — `centauri detect check`: reports device presence,
  backend selection, model load, and one warm inference latency. This
  is the Phase 0 smoke test.

Type/tooling notes: `mypy` override `ignore_missing_imports` for
`ai_edge_litert.*` (no stubs); hardware-dependent tests get
`@pytest.mark.skipif(not Path("/dev/apex/0").exists())`. CI stays
hardware-free with a fake interpreter.

## Phase 2 — detection pipeline & server integration

### Frame source (the important constraint)

The detector subscribes to the existing `CameraBroadcaster` — it is
just another subscriber, exactly like a browser tab:

* While a print is active the detector's subscription keeps the single
  upstream connection alive (even with zero humans watching).
* When the print ends the detector unsubscribes → the broadcaster's
  30 s idle close fires → the printer's scarce camera slots are freed,
  same as today.
* Slow-consumer drops already protect the shared reader; the detector
  simply processes every Nth frame (1 fps is plenty).
* Subscriber payloads are whole multipart parts (headers included) —
  extract the JPEG with the same SOI/EOI scan `camera.py` uses.

### Lifecycle

`DetectionController` (new, held on `app.state` next to
`PrinterManager`, started with `--detect`):

* Watches `printer.watch()`. Processing runs only while
  `print_status == 13` (Printing). On CC2 also skip 27/28/29 (Canvas
  filament switch — head parked at the purge chute is a known
  false-positive source).
* Grace period after print start (~90 s or first N layers): priming,
  purging and brim-laying all look like spaghetti to any model.
* Trigger rule: positive detection on ≥ K of the last M frames
  (default 4 of 6) above confidence threshold (default 0.5). One alert
  per print; cooldown after firing.

### Actions (safety model)

* **Always:** save evidence (triggering frame + a few pre/post frames)
  to `data/evidence/<timestamp>/`, log it, and POST a JSON webhook
  (user-configurable URL — ntfy / Home Assistant / Telegram bridge).
  Add `data/evidence/` to `.gitignore`.
* **Opt-in only:** `--detect-action pause|stop` calls
  `manager.printer.pause()` / `.stop()`. Refused at startup unless the
  server already runs with `--enable-control` (same gate as the HTTP
  control endpoints). Default is notify-only — stopping a print is
  destructive and a false positive must never destroy a good print
  uninvited.

### Wiring

* `server.py`: `--detect [MODEL]`, `--detect-action`, `--detect-webhook`,
  `--detect-threshold` flags threaded through `run()` → `create_app()`
  (same pattern as `rtsp_config`).
* Endpoints (registered only with `--detect`):
  * `GET /api/detect` — state: `{enabled, backend, model, processing,
    last_result, detections}`.
  * `GET /api/detect/evidence/{name}` — saved evidence JPEGs.
* Standalone mode (no HTTP server):
  `centauri detect watch --model ... --host ... --webhook ...` polls
  `camera.snapshot()` on an interval instead of a persistent MJPEG
  connection — one short HTTP GET per frame is the gentlest possible
  touch on the printer's camera server.
* Web UI panel (optional, Phase 4): badge on the dashboard fed by
  `/api/detect` + evidence thumbnails.

## Phase 3 — the model (the long pole)

1. **Smoke model (day one).** Any pre-compiled Edge TPU model from
   Coral's model zoo (e.g. SSD MobileNet V2 int8, 300×300) validates
   plumbing, delegate loading and latency — its classes are COCO, so it
   detects nothing useful on a print, but proves the TPU path.
2. **Custom 2-class model.** Classes: `print_ok`, `spaghetti`
   (optionally `bed_empty`, `finished`).
   * Data: collect frames from this printer's own camera — cron
     `centauri snapshot` during real prints into `data/collect/`
     (lighting/angle/PTZ parity with inference time is what makes it
     accurate). Supplement with public failed-print datasets.
   * Label with any bounding-box tool; train EfficientDet-Lite0 or SSD
     MobileNet V2 320 via the TF Object Detection API (Colab is fine —
     training never needs the TPU), export **fully int8-quantized**
     TFLite, compile with `edgetpu_compiler` (runs on this x86_64
     container) → `model_edgetpu.tflite`.
   * The same `.tflite` runs on the CPU fallback unchanged (just
     slower, ~100–300 ms/frame — still fine at 1 fps).
   * Ship models under `data/models/`; `.gitignore` the binaries,
     document the expected file name in the flag help.

## Testing (house style: no hardware in CI)

* `tests/test_detect.py` — fake interpreter with canned boxes;
  lifecycle transitions (13 → processing, idle → unsubscribe);
  debounce/K-of-M logic; action gating (notify-only without
  `--enable-control`); multipart-frame → JPEG extraction reusing the
  broadcaster's frame format.
* Hardware tests behind the `/dev/apex/0` skipif; live smoke = run
  `centauri detect check` then a print with notify-only mode.

## Risks / open questions

* `libedgetpu1-std` is a bookworm .deb — installs on trixie in
  practice, but verify in Phase 0 before writing code (worst case:
  inference subprocess in a bookworm chroot/container).
* udev in the unprivileged LXC — fallbacks listed in Phase 0.
* False positives are the product risk, not the tech: ship
  notify-only, tune thresholds against evidence snapshots for a week,
  then consider `--detect-action`.
* Edge TPU only accelerates fully-int8 ops; any unsupported op silently
  falls back to CPU inside the delegate — fine, just slower.
* CC1 camera slots: never open a second MJPEG connection; the
  broadcaster tap is the whole point of this design.

## Suggested order

Phase 0 (system, ~1 h once the Coral is on the desk) → Phase 1
(backend + `detect check`, small) → Phase 2 (pipeline + server, the
bulk) → smoke model → Phase 3 (data collection can start immediately,
in parallel) → Phase 4 (UI polish). Ship as 0.12.0 with README,
ARCHITECTURE and CHANGELOG updates.
