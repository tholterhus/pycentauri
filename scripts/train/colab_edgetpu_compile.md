# Edge-TPU compilation — how it was done (and how to redo it)

**Status 2026-10-04: `spaghetti_edgetpu.tflite` is compiled and
deployed** — the service runs it on the Coral. This doc is the recipe
for any future recompilation (e.g. a retrained model).

The official install route is dead: the Google apt repo
(packages.cloud.google.com/apt, `coral-edgetpu-stable`) returns **403
even from Colab** — so the compiler comes from the self-contained
16.0 bundle at github.com/mbrooksx/edgetpu-compilers (x86-64 binaries
extracted from the Debian package, ships its own glibc + loader, runs
on any x86-64 Linux — including spielwiese itself, no install).

## Route A — compile directly on spielwiese (what actually happened)

```sh
cd /tmp
curl -fsSLO https://github.com/mbrooksx/edgetpu-compilers/archive/refs/heads/main.tar.gz
tar xzf main.tar.gz && chmod +x edgetpu-compilers-main/16.0/edgetpu_compiler
cp /opt/pycentauri/data/models/spaghetti.tflite .
edgetpu-compilers-main/16.0/edgetpu_compiler spaghetti.tflite
cp spaghetti_edgetpu.tflite /opt/pycentauri/data/models/
```

Expect: ~99/102 ops on Edge TPU (the rest = standard SSD postprocess).

## Route B — Colab fallback (one cell)

```python
!pip install -q ai-edge-litert
!curl -fsSLO https://github.com/tholterhus/pycentauri/releases/download/v0.13.0/spaghetti.tflite
!git clone --depth 1 https://github.com/mbrooksx/edgetpu-compilers
!chmod +x edgetpu-compilers/main/16.0/edgetpu_compiler
!edgetpu-compilers/main/16.0/edgetpu_compiler spaghetti.tflite
from ai_edge_litert.interpreter import Interpreter
i = Interpreter(model_path="spaghetti_edgetpu.tflite"); i.allocate_tensors()
for d in i.get_output_details(): print("output:", d["shape"], d["dtype"])
from google.colab import files
files.download("spaghetti_edgetpu.tflite")
```

## Verify + switch the service

```sh
venv/bin/centauri detect check --model data/models/spaghetti_edgetpu.tflite
sudo sed -i 's|ssd_mobilenet_v2_coco_edgetpu|spaghetti_edgetpu|' /etc/systemd/system/pycentauri.service
sudo systemctl daemon-reload && sudo systemctl restart pycentauri
```

Gotchas learned the hard way:

- The label sidecar resolves automatically
  (`spaghetti_edgetpu.tflite` → `spaghetti.txt`, see
  `src/pycentauri/detect/backend.py`).
- Keep the uncompiled int8 sibling (`spaghetti.tflite`) — the CPU
  fallback loads it when the TPU variant cannot execute.
- **Don't run `centauri detect check/test` against the TPU while the
  service is watching it** — concurrent device access made the service
  abort (libedgetpu re-enumerates the USB device). For frame tests use
  the uncompiled sibling (runs on CPU): `centauri detect test --model
  data/models/spaghetti.tflite <frames>`.
- Arming resets to **notify** on restart. Acceptance: one full print in
  notify mode with zero false alerts, then arm Pause in the DETECT panel
  (criteria in `docs/TRAINING_PLAN.md`).
