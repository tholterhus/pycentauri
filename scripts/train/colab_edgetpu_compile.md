# Edge-TPU compilation in Colab — compile-only (for the pre-trained model)

The Mac training already produced the int8 model:
`data/models/spaghetti.tflite` (SSD MobileNet V2 320×320, uint8 input,
the 4 SSD postprocess outputs — probe-verified via `centauri detect
check`). The only missing step is the `edgetpu_compiler` pass: the
compiler is x86-64-only and its apt repo (packages.cloud.google.com) is
blocked from this network, so it runs on a free **Colab CPU runtime**
(no GPU needed, ~2 minutes).

Open https://colab.research.google.com → new notebook → paste the cells
one by one.

**Cell 1 — get the model** (public release asset; alternatively upload
`data/models/spaghetti.tflite` via the Files panel on the left):

```python
!curl -fsSLO https://github.com/tholterhus/pycentauri/releases/download/v0.13.0/spaghetti.tflite
```

**Cell 2 — install the compiler**

```python
!curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key add -
!echo "deb https://packages.cloud.google.com/apt coral-edgetpu-stable main" > /etc/apt/sources.list.d/coral-edgetpu.list
!apt-get -qq update && apt-get -qq install edgetpu-compiler
```

**Cell 3 — compile and verify**

```python
!edgetpu_compiler spaghetti.tflite
from ai_edge_litert.interpreter import Interpreter
i = Interpreter(model_path="spaghetti_edgetpu.tflite")
i.allocate_tensors()
for d in i.get_output_details():
    print("output:", d["shape"], d["dtype"])
# expects the 4 SSD postprocess tensors:
# [1,N,4] boxes + [1,N] classes + [1,N] scores + scalar count
```

**Cell 4 — bring it back**

Download `spaghetti_edgetpu.tflite` from the Colab Files panel, copy it
to spielwiese, then:

```sh
cp spaghetti_edgetpu.tflite /opt/pycentauri/data/models/
cd /opt/pycentauri
venv/bin/centauri detect check --model data/models/spaghetti_edgetpu.tflite
# → "edge tpu: present", backend edgetpu, steady-state ~5–15 ms
```

## Switch the service to the trained model

The label sidecar is found automatically
(`spaghetti_edgetpu.tflite` → `spaghetti.txt`, see
`src/pycentauri/detect/backend.py`) — no extra copy needed. Keep the
uncompiled int8 sibling: when an Edge-TPU model cannot execute on the
CPU, the fallback loads the sibling (`<name>.tflite`).

```sh
sudo sed -i 's|ssd_mobilenet_v2_coco_edgetpu|spaghetti_edgetpu|' \
    /etc/systemd/system/pycentauri.service
sudo systemctl daemon-reload && sudo systemctl restart pycentauri
```

Arming resets to **notify** on restart. Run one print in notify mode
(zero false alerts expected — see the acceptance criteria in
`docs/TRAINING_PLAN.md`), then arm Pause in the DETECT panel.
