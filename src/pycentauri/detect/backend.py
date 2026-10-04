"""TFLite inference backend for failed-print ("spaghetti") detection.

Loads a quantized SSD-style TFLite detector on the Edge TPU (Coral USB
Accelerator, via the ``libedgetpu`` delegate) when one is present, and
falls back to the plain CPU interpreter otherwise. Only imported when the
``detect`` extra is installed (``ai-edge-litert``, ``numpy``, ``pillow``);
:mod:`pycentauri.detect.pipeline` pulls it in lazily, so a base
``pycentauri`` install never touches any of this.

Model conventions (Coral model zoo and our trained models):

* SSD detector with ``TFLite_Detection_PostProcess`` outputs — located by
  shape, not index: boxes ``[1, N, 4]`` (normalized y0, x0, y1, x1), a
  scalar count, and two remaining ``[1, N]`` float tensors read in the
  op's fixed wire order (class ids first, then scores).
* Labels: ``.txt`` sidecar next to the model (same stem, or
  ``labels.txt`` in the same directory), one label per line.
* Edge TPU execution requires a *fully int8* model compiled with
  ``edgetpu_compiler`` (conventionally named ``*_edgetpu.tflite``); the
  same file without compilation runs unchanged on the CPU fallback,
  just two orders of magnitude slower — still fine at ~1 fps.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image

log = logging.getLogger(__name__)

#: udev (from ``libedgetpu1-std``) creates one node per Edge TPU; PCIe
#: devices show up as ``/dev/apex/0``. The USB Accelerator never gets an
#: apex node — libedgetpu talks to it directly through libusb.
EDGE_TPU_DEVICE = Path("/dev/apex/0")
#: Delegate shared library shipped by the ``libedgetpu1-std`` package.
EDGE_TPU_LIB = "libedgetpu.so.1"
#: USB IDs the Coral USB Accelerator presents (Global Unichip 1a6e:089a,
#: Google 18d1:9302 on some firmware).
USB_CORAL_IDS = {("1a6e", "089a"), ("18d1", "9302")}

__all__ = [
    "EDGE_TPU_DEVICE",
    "DetectBackendError",
    "Detection",
    "Detector",
    "discover_model",
    "edge_tpu_available",
]


class DetectBackendError(RuntimeError):
    """The model could not be loaded, or inference failed irrecoverably."""


@dataclass(frozen=True)
class Detection:
    """One detection above threshold. Box coordinates are normalized 0..1."""

    label: str
    score: float
    box: tuple[float, float, float, float]  # x0, y0, x1, y1

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "score": round(self.score, 4),
            "box": [round(v, 4) for v in self.box],
        }


def edge_tpu_available() -> bool:
    """True when an Edge TPU is present.

    PCIe devices expose ``/dev/apex/0``; the USB Accelerator is detected
    by its USB IDs on the bus (there is no apex node for USB — libedgetpu
    enumerates it via libusb).
    """
    if EDGE_TPU_DEVICE.exists():
        return True
    try:
        for vendor_path in Path("/sys/bus/usb/devices").glob("*/idVendor"):
            vendor = vendor_path.read_text().strip()
            product_path = vendor_path.with_name("idProduct")
            product = product_path.read_text().strip()
            if (vendor, product) in USB_CORAL_IDS:
                return True
    except OSError:
        pass
    return False


def _is_edgetpu_compiled(model_path: Path) -> bool:
    """The ``edgetpu_compiler`` output conventionally ends in ``_edgetpu``."""
    return "_edgetpu" in model_path.name


def _load_labels(*model_paths: Path) -> list[str]:
    """Read the label sidecar (Coral convention) if one exists.

    Tries ``<stem>.txt`` for each given model path (the CPU variant *and*
    the original requested one — the sidecar usually ships next to the
    ``*_edgetpu`` file), then ``labels.txt`` in the same directory.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()
    for model_path in model_paths:
        for candidate in (
            model_path.with_suffix(".txt"),
            model_path.with_name(model_path.stem.replace("_edgetpu", "") + ".txt"),
        ):
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    candidates.append(model_paths[0].parent / "labels.txt")
    for candidate in candidates:
        if candidate.is_file():
            lines = [
                line.strip()
                for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            ]
            return [line for line in lines if line]
    return []


def discover_model(search_dir: Path) -> Path | None:
    """Pick a model to run: prefer Edge TPU-compiled ones, newest first."""
    if not search_dir.is_dir():
        return None
    candidates = sorted(
        list(search_dir.glob("*_edgetpu.tflite")) + list(search_dir.glob("*.tflite")),
        key=lambda p: (not _is_edgetpu_compiled(p), -p.stat().st_mtime),
    )
    return candidates[0] if candidates else None


def _build_interpreter(model_path: Path, delegate: Any) -> Any:
    """Create a LiteRT interpreter, optionally with the Edge TPU delegate.

    Split out so tests can substitute a fake interpreter without loading
    a real model.
    """
    from ai_edge_litert.interpreter import Interpreter

    kwargs: dict[str, Any] = {"model_path": str(model_path)}
    if delegate is not None:
        kwargs["experimental_delegates"] = [delegate]
    else:
        kwargs["num_threads"] = 2
    interpreter = Interpreter(**kwargs)
    interpreter.allocate_tensors()
    return interpreter


def _load_edge_tpu_delegate() -> Any:
    from ai_edge_litert.interpreter import load_delegate

    return load_delegate(EDGE_TPU_LIB)


def _locate_outputs(output_details: list[dict[str, Any]]) -> dict[str, int | None]:
    """Map the SSD post-processing output tensors by shape.

    Returns ``{"boxes": idx, "classes": idx, "scores": idx, "count": idx}``.
    The count tensor is the scalar output (ndim ≤ 1, or a degenerate
    ``[1, 1]``); boxes are the only 3-D output; of the two remaining
    ``[1, N]`` float tensors, class ids precede scores (fixed wire order
    of ``TFLite_Detection_PostProcess``).
    """
    found: dict[str, int | None] = {"boxes": None, "classes": None, "scores": None, "count": None}
    rest: list[int] = []
    for d in output_details:
        shape = tuple(int(v) for v in d["shape"])
        idx = int(d["index"])
        if len(shape) == 3 and shape[-1] == 4 and found["boxes"] is None:
            found["boxes"] = idx
        elif (len(shape) <= 1 or (len(shape) == 2 and shape[-1] == 1)) and found["count"] is None:
            found["count"] = idx
        else:
            rest.append(idx)
    if len(rest) >= 1:
        found["classes"] = rest[0]
    if len(rest) >= 2:
        found["scores"] = rest[1]
    return found


def _quantize(arr: NDArray[Any], dtype: Any, quantization: tuple[float, float]) -> NDArray[Any]:
    """Map a 0..255 uint8 image onto the input tensor's quantization."""
    scale, zero_point = quantization
    if dtype == np.int8:
        if scale > 0.0:
            return np.rint(arr / 255.0 / scale + zero_point).clip(-128, 127).astype(np.int8)
        return (arr.astype(np.float32) - 128.0).astype(np.int8)
    if dtype == np.uint8:
        return arr.astype(np.uint8)
    return (arr / 255.0).astype(dtype)  # float models expect 0..1


class Detector:
    """A loaded SSD detector: preprocessing, one-call inference, postprocessing.

    Inference is synchronous and blocking — callers on the event loop must
    run it in an executor (~5-15 ms per frame on an Edge TPU, ~100-300 ms
    on CPU).
    """

    def __init__(
        self,
        interpreter: Any,
        labels: list[str],
        model_path: Path,
        backend_name: str,
    ) -> None:
        self._interpreter = interpreter
        self.labels = labels
        self.model_path = model_path
        self.backend_name = backend_name
        self.last_inference_s: float | None = None
        interpreter.allocate_tensors()  # idempotent — shapes vor dem lesen festschreiben

        detail = interpreter.get_input_details()[0]
        self._input_index = int(detail["index"])
        shape = tuple(int(v) for v in detail["shape"])
        self._input_shape = shape
        # (height, width, …) on the wire; we expose (width, height) like PIL.
        self.input_size = (shape[2], shape[1])
        self._dtype = detail["dtype"]
        quant = detail["quantization"]
        self._quantization = (float(quant[0]), float(quant[1]))
        self._outputs = _locate_outputs(interpreter.get_output_details())
        if self._outputs["boxes"] is None or self._outputs["scores"] is None:
            raise DetectBackendError(
                f"model {model_path.name} does not expose SSD detection outputs; "
                "expected a TFLite_Detection_PostProcess detector"
            )
        self._resolve_class_score_order()

    def _resolve_class_score_order(self) -> None:
        """Die beiden [1, N]-Outputs (klassen/scores) sind je nach export in
        unterschiedlicher reihenfolge — disambiguierung per probe-inferenz:
        klassen-ids sind integral, scores fraktional."""
        c_idx, s_idx = self._outputs["classes"], self._outputs["scores"]
        if c_idx is None or s_idx is None:
            return
        rng = np.random.default_rng(42)
        integral = {c_idx: True, s_idx: True}
        try:
            for _ in range(3):
                probe = rng.integers(
                    0, 256, size=(self.input_size[1], self.input_size[0], 3), dtype=np.uint8
                )
                self._interpreter.set_tensor(
                    self._input_index, _quantize(probe, self._dtype, self._quantization)[np.newaxis]
                )
                self._interpreter.invoke()
                for idx in (c_idx, s_idx):
                    vals = np.asarray(self._interpreter.get_tensor(idx)).flatten()
                    if np.any(vals != np.rint(vals)):
                        integral[idx] = False
        except Exception:
            return  # probe fehlgeschlagen → wire-order fallback bleibt
        if integral[c_idx] and not integral[s_idx]:
            return  # wire-order stimmt
        if integral[s_idx] and not integral[c_idx]:
            self._outputs["classes"], self._outputs["scores"] = s_idx, c_idx
            log.info("detection: swapped classes/scores outputs (probe)")

    @classmethod
    def from_model(cls, model_path: Path, *, force_cpu: bool = False) -> Detector:
        """Load a model, choosing the Edge TPU when possible.

        Falls back to CPU — with a logged reason — when the device is
        absent, the model isn't TPU-compiled, or the delegate library
        can't be loaded. An Edge TPU-compiled model contains a custom op
        the CPU interpreter cannot execute, so on CPU we automatically
        switch to the uncompiled sibling (``<stem>_edgetpu.tflite`` →
        ``<stem>.tflite``) when it exists. Ship both variants.
        """
        if not model_path.is_file():
            raise DetectBackendError(f"model not found: {model_path}")

        use_tpu = not force_cpu and edge_tpu_available() and _is_edgetpu_compiled(model_path)
        delegate: Any = None
        if use_tpu:
            try:
                delegate = _load_edge_tpu_delegate()
            except Exception as err:
                log.warning("Edge TPU delegate unavailable (%r) — falling back to CPU", err)
                use_tpu = False

        model_file = model_path
        if not use_tpu and _is_edgetpu_compiled(model_path):
            sibling = Path(str(model_path).replace("_edgetpu", "", 1))
            if sibling.is_file():
                log.info("Edge TPU unavailable — using CPU variant %s", sibling.name)
                model_file = sibling

        try:
            interpreter = _build_interpreter(model_file, delegate)
        except ImportError as err:
            raise DetectBackendError(
                f"detection runtime not installed ({err}). "
                "Install with: pip install 'pycentauri[detect]'"
            ) from err
        except Exception as err:
            raise DetectBackendError(f"failed to load model {model_file}: {err}") from err
        backend_name = "edgetpu" if use_tpu else "cpu"
        detector = cls(interpreter, _load_labels(model_file, model_path), model_file, backend_name)
        log.info(
            "detection backend: %s (model=%s, input=%dx%d, labels=%d)",
            backend_name,
            model_file.name,
            *detector.input_size,
            len(detector.labels),
        )
        return detector

    def detect(self, jpeg: bytes, *, threshold: float = 0.5) -> list[Detection]:
        """Run one frame (JPEG bytes) through the model.

        Returns detections with score ≥ ``threshold``, highest first.
        """
        with Image.open(io.BytesIO(jpeg)) as img:
            resized = img.convert("RGB").resize(self.input_size, Image.Resampling.BILINEAR)
        tensor = _quantize(np.asarray(resized), self._dtype, self._quantization)
        if tuple(tensor.shape) != self._input_shape and tensor.ndim + 1 == len(self._input_shape):
            tensor = np.expand_dims(tensor, axis=0)  # unbatched image → [1, H, W, C]
        self._interpreter.set_tensor(self._input_index, tensor)

        started = time.perf_counter()
        self._interpreter.invoke()
        self.last_inference_s = time.perf_counter() - started

        assert self._outputs["boxes"] is not None and self._outputs["scores"] is not None
        boxes = self._interpreter.get_tensor(self._outputs["boxes"])[0]
        class_ids = (
            self._interpreter.get_tensor(self._outputs["classes"])[0]
            if self._outputs["classes"] is not None
            else np.zeros(len(boxes))
        )
        scores = self._interpreter.get_tensor(self._outputs["scores"])[0]
        count = len(scores)
        if self._outputs["count"] is not None:
            count = int(np.asarray(self._interpreter.get_tensor(self._outputs["count"])).item())

        out: list[Detection] = []
        for i in range(min(count, len(scores))):
            score = float(scores[i])
            if score < threshold:
                continue
            y0, x0, y1, x1 = (float(v) for v in boxes[i])
            class_id = int(class_ids[i]) if i < len(class_ids) else 0
            label = self.labels[class_id] if 0 <= class_id < len(self.labels) else str(class_id)
            out.append(Detection(label=label, score=score, box=(x0, y0, x1, y1)))
        out.sort(key=lambda d: d.score, reverse=True)
        return out

    def warmup(self) -> float:
        """Run one blank inference and return its wall time in seconds.

        The first ``invoke()`` on an Edge TPU also compiles the model onto
        the device, so this is the honest "ready" latency to report.
        """
        img = Image.new("RGB", self.input_size, (128, 128, 128))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        started = time.perf_counter()
        self.detect(buf.getvalue(), threshold=2.0)
        return time.perf_counter() - started
