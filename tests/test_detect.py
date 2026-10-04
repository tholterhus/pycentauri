"""Tests for failed-print detection: backend, pipeline, and endpoints.

No hardware and no real model files — the LiteRT interpreter is faked, and
the pipeline is driven with a fake camera broadcaster and a fake printer
(the same in-process fake pattern as test_server.py). Backend tests need
the ``detect`` extra (numpy / pillow) and skip cleanly without it.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")

from pycentauri.detect import backend as detect_backend
from pycentauri.detect.backend import (
    DetectBackendError,
    Detection,
    Detector,
    _locate_outputs,
    _quantize,
)
from pycentauri.detect.pipeline import (
    PRINTING,
    DetectConfig,
    DetectionController,
    DetectionEvent,
    jpeg_from_part,
    save_evidence,
)
from pycentauri.models import Status

# --- fakes -------------------------------------------------------------------


class FakeInterpreter:
    """Minimal LiteRT interpreter shape with SSD postprocess tensors."""

    def __init__(
        self,
        *,
        n: int = 10,
        input_dtype: Any = np.int8,
        quant: tuple[float, float] = (0.007843137, -1.0),
    ) -> None:
        self._input_details = [
            {
                "index": 9,
                "shape": np.array([1, 300, 300, 3]),
                "dtype": input_dtype,
                "quantization": quant,
            }
        ]
        self._output_details = [
            {"index": 0, "shape": np.array([1, n, 4]), "dtype": np.float32},
            {"index": 1, "shape": np.array([1, n]), "dtype": np.float32},
            {"index": 2, "shape": np.array([1, n]), "dtype": np.float32},
            {"index": 3, "shape": np.array([1]), "dtype": np.float32},
        ]
        self.last_input: Any = None
        self.invoke_count = 0
        self.boxes = np.zeros((1, n, 4), dtype=np.float32)
        self.class_ids = np.zeros((1, n), dtype=np.float32)
        self.scores = np.zeros((1, n), dtype=np.float32)
        self.count = np.array([0], dtype=np.float32)

    def allocate_tensors(self) -> None:
        pass

    def get_input_details(self) -> list[dict[str, Any]]:
        return self._input_details

    def get_output_details(self) -> list[dict[str, Any]]:
        return self._output_details

    def set_tensor(self, index: int, value: Any) -> None:
        assert index == 9
        self.last_input = value

    def invoke(self) -> None:
        self.invoke_count += 1

    def get_tensor(self, index: int) -> Any:
        return {0: self.boxes, 1: self.class_ids, 2: self.scores, 3: self.count}[index]


class FakeDetector:
    """Stands in for the loaded model inside pipeline tests."""

    def __init__(self) -> None:
        self.backend_name = "cpu"
        self.calls = 0

    def detect(self, jpeg: bytes, *, threshold: float = 0.5) -> list[Detection]:
        self.calls += 1
        return [Detection(label="spaghetti", score=0.9, box=(0.1, 0.1, 0.5, 0.5))]


class FakeCamera:
    """Stands in for CameraBroadcaster: one subscriber, finite frames."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.subscribe_count = 0

    async def subscribe(self) -> tuple[str, AsyncIterator[bytes]]:
        self.subscribe_count += 1

        async def gen() -> AsyncIterator[bytes]:
            for frame in self._frames:
                await asyncio.sleep(0.01)
                yield frame

        return "multipart/x-mixed-replace; boundary=frame", gen()


class FakePrinter:
    """Yields one Status per configured print_status code."""

    def __init__(self, statuses: list[int]) -> None:
        self._statuses = statuses

    async def watch(self) -> AsyncIterator[Status]:
        for code in self._statuses:
            await asyncio.sleep(0.01)
            yield Status.from_payload({"PrintInfo": {"Status": code, "Filename": "job.gcode"}})


def _jpeg(color: tuple[int, int, int] = (120, 120, 120)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _wait_for(predicate: Any, timeout_s: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


# --- backend ------------------------------------------------------------------


def test_quantize_int8_uses_scale_and_zero_point() -> None:
    scale = 1.0 / 127.0
    out = _quantize(np.array([0.0, 128.0, 255.0]), np.int8, (scale, -1.0))
    assert out.dtype == np.int8
    assert out.tolist() == [-1, 63, 126]


def test_quantize_uint8_and_float_paths() -> None:
    arr = np.array([0.0, 255.0])
    assert _quantize(arr, np.uint8, (0.0, 0)).tolist() == [0, 255]
    out = _quantize(arr, np.float32, (0.0, 0))
    assert out.dtype == np.float32 and out.tolist() == pytest.approx([0.0, 1.0])


def test_locate_outputs_ssd_order() -> None:
    interp = FakeInterpreter()
    assert _locate_outputs(interp.get_output_details()) == {
        "boxes": 0,
        "classes": 1,
        "scores": 2,
        "count": 3,
    }


def test_detect_filters_threshold_and_maps_labels() -> None:
    interp = FakeInterpreter(n=3)
    interp.boxes = np.array(
        [[[0.1, 0.2, 0.5, 0.6], [0.0, 0.0, 0.9, 0.9], [0.3, 0.3, 0.4, 0.4]]], np.float32
    )
    interp.class_ids = np.array([[1, 0, 7]], np.float32)
    interp.scores = np.array([[0.9, 0.2, 0.8]], np.float32)
    interp.count = np.array([3], np.float32)
    detector = Detector(interp, ["print_ok", "spaghetti"], Path("fake.tflite"), "cpu")

    out = detector.detect(_jpeg(), threshold=0.5)
    # 0.2 filtered; 0.9 (class 1) before 0.8 (unknown class → its id).
    assert [d.label for d in out] == ["spaghetti", "7"]
    assert out[0].score == pytest.approx(0.9)
    # Wire order is y0,x0,y1,x1 — exposed as x0,y0,x1,y1.
    assert out[0].box == pytest.approx((0.2, 0.1, 0.6, 0.5))


def test_from_model_prefers_edgetpu(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = tmp_path / "m_edgetpu.tflite"
    model.write_bytes(b"fake")
    (tmp_path / "apex0").touch()
    monkeypatch.setattr(detect_backend, "EDGE_TPU_DEVICE", tmp_path / "apex0")
    monkeypatch.setattr(detect_backend, "_build_interpreter", lambda p, d: FakeInterpreter())
    used_delegate = []
    monkeypatch.setattr(
        detect_backend, "_load_edge_tpu_delegate", lambda: used_delegate.append(1) or object()
    )
    detector = Detector.from_model(model)
    assert detector.backend_name == "edgetpu"
    assert used_delegate


def test_from_model_falls_back_when_device_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "m_edgetpu.tflite"
    model.write_bytes(b"fake")
    monkeypatch.setattr(detect_backend, "EDGE_TPU_DEVICE", tmp_path / "missing")
    monkeypatch.setattr(detect_backend, "edge_tpu_available", lambda: False)
    monkeypatch.setattr(detect_backend, "_build_interpreter", lambda p, d: FakeInterpreter())
    detector = Detector.from_model(model)
    assert detector.backend_name == "cpu"


def test_from_model_falls_back_when_delegate_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "m_edgetpu.tflite"
    model.write_bytes(b"fake")
    (tmp_path / "apex0").touch()
    monkeypatch.setattr(detect_backend, "EDGE_TPU_DEVICE", tmp_path / "apex0")
    monkeypatch.setattr(detect_backend, "_build_interpreter", lambda p, d: FakeInterpreter())

    def boom() -> object:
        raise RuntimeError("no delegate")

    monkeypatch.setattr(detect_backend, "_load_edge_tpu_delegate", boom)
    detector = Detector.from_model(model)
    assert detector.backend_name == "cpu"


def test_from_model_uncompiled_model_uses_cpu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "plain.tflite"  # no _edgetpu marker
    model.write_bytes(b"fake")
    (tmp_path / "apex0").touch()
    monkeypatch.setattr(detect_backend, "EDGE_TPU_DEVICE", tmp_path / "apex0")
    monkeypatch.setattr(detect_backend, "_build_interpreter", lambda p, d: FakeInterpreter())
    detector = Detector.from_model(model)
    assert detector.backend_name == "cpu"


def test_from_model_swaps_to_uncompiled_sibling_on_cpu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An _edgetpu.tflite contains a custom op the CPU cannot run — the
    uncompiled sibling must be loaded instead when the TPU is absent."""
    tpu_model = tmp_path / "m_edgetpu.tflite"
    tpu_model.write_bytes(b"fake")
    sibling = tmp_path / "m.tflite"
    sibling.write_bytes(b"fake")
    loaded: list[Path] = []
    monkeypatch.setattr(detect_backend, "EDGE_TPU_DEVICE", tmp_path / "missing")
    monkeypatch.setattr(detect_backend, "edge_tpu_available", lambda: False)
    monkeypatch.setattr(
        detect_backend,
        "_build_interpreter",
        lambda p, d: loaded.append(Path(p)) or FakeInterpreter(),
    )
    detector = Detector.from_model(tpu_model)
    assert detector.backend_name == "cpu"
    assert loaded == [sibling]
    assert detector.model_path == sibling


def test_from_model_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DetectBackendError):
        Detector.from_model(tmp_path / "nope.tflite")


def test_discover_model_prefers_edgetpu_then_newest(tmp_path: Path) -> None:
    import os

    old_tpu = tmp_path / "a_edgetpu.tflite"
    new_tpu = tmp_path / "b_edgetpu.tflite"
    plain = tmp_path / "c.tflite"
    for p in (old_tpu, new_tpu, plain):
        p.write_bytes(b"x")
    os.utime(old_tpu, (1, 1))
    os.utime(new_tpu, (2, 2))
    os.utime(plain, (3, 3))
    # Newest Edge TPU model wins over an older TPU model and a newer plain one.
    assert detect_backend.discover_model(tmp_path) == new_tpu
    new_tpu.unlink()
    # An older TPU-compiled model still beats a newer uncompiled one.
    assert detect_backend.discover_model(tmp_path) == old_tpu


# --- pipeline pieces ------------------------------------------------------------


def test_jpeg_from_part_extracts_payload() -> None:
    jpeg = _jpeg()
    part = (
        b"--frame\r\nContent-Type: image/jpeg\r\n"
        + b"Content-Length: "
        + str(len(jpeg)).encode()
        + b"\r\n\r\n"
        + jpeg
        + b"\r\n"
    )
    assert jpeg_from_part(part) == jpeg
    assert jpeg_from_part(b"no image here") is None


def test_save_evidence_writes_frame_and_sidecar(tmp_path: Path) -> None:
    event = DetectionEvent(
        when="t",
        score=0.9,
        label="spaghetti",
        backend="cpu",
        model="m.tflite",
        evidence="",
        detections=[{"label": "spaghetti", "score": 0.9, "box": [0.1, 0.1, 0.5, 0.5]}],
    )
    path = save_evidence(tmp_path, event, _jpeg())
    assert path.is_file()
    assert path.read_bytes().startswith(b"\xff\xd8")  # SOI
    side = json.loads(path.with_suffix(".json").read_text())
    assert side["label"] == "spaghetti" and side["score"] == 0.9


def test_detect_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DetectConfig(model_path=tmp_path / "m.tflite", action="destroy")
    with pytest.raises(ValueError):
        DetectConfig(model_path=tmp_path / "m.tflite", threshold=1.5)
    with pytest.raises(ValueError):
        DetectConfig(model_path=tmp_path / "m.tflite", window_size=3, window_needed=5)


# --- controller lifecycle -------------------------------------------------------


async def test_controller_fires_once_per_print(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        detect_backend.Detector,
        "from_model",
        staticmethod(lambda path, force_cpu=False: FakeDetector()),
    )
    cfg = DetectConfig(
        model_path=tmp_path / "m_edgetpu.tflite",
        grace_s=0.0,
        min_frame_interval_s=0.0,
        window_size=4,
        window_needed=3,
        action="stop",
        webhook_url="http://hook.example/x",
        evidence_dir=tmp_path / "ev",
    )
    camera = FakeCamera([_jpeg()] * 12)
    actions: list[str] = []
    hooks: list[dict[str, Any]] = []

    async def action_runner(verb: str) -> None:
        actions.append(verb)

    async def poster(payload: dict[str, Any]) -> None:
        hooks.append(payload)

    controller = DetectionController(
        cfg,
        camera=camera,
        get_printer=lambda: FakePrinter([0] + [PRINTING] * 20 + [9]),
        control_allowed=True,
        action_runner=action_runner,
        webhook_poster=poster,
    )
    await controller.start()
    fired = await _wait_for(lambda: controller._last_event is not None)
    assert fired, f"no event fired; state={controller.state()}"
    await _wait_for(lambda: bool(actions) or controller.state()["processing"] is False)
    await controller.stop()

    assert actions == ["stop"]  # exactly once, despite 12 positive frames
    assert len(hooks) == 1
    assert hooks[0]["type"] == "spaghetti_detected"
    evidence = Path(controller._last_event.evidence)  # type: ignore[union-attr]
    assert evidence.is_file() and evidence.parent == (tmp_path / "ev")
    assert camera.subscribe_count == 1


async def test_controller_not_firing_below_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        detect_backend.Detector,
        "from_model",
        staticmethod(lambda path, force_cpu=False: FakeDetector()),
    )
    cfg = DetectConfig(
        model_path=tmp_path / "m_edgetpu.tflite",
        grace_s=0.0,
        min_frame_interval_s=0.0,
        window_size=4,
        window_needed=3,
        evidence_dir=tmp_path / "ev",
    )
    camera = FakeCamera([_jpeg()] * 2)  # only 2 positives — under the window
    actions: list[str] = []

    async def action_runner(verb: str) -> None:
        actions.append(verb)

    controller = DetectionController(
        cfg,
        camera=camera,
        get_printer=lambda: FakePrinter([PRINTING] * 20 + [9]),
        control_allowed=True,
        action_runner=action_runner,
    )
    await controller.start()
    await _wait_for(lambda: controller.state()["processing"] is False, timeout_s=5.0)
    await controller.stop()
    assert actions == []
    assert controller._last_event is None


async def test_controller_holds_subscription_through_filament_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CC2 Canvas switch (27): keep the camera, don't infer, then resume."""
    cfg = DetectConfig(
        model_path=tmp_path / "m_edgetpu.tflite",
        grace_s=0.0,
        min_frame_interval_s=0.0,
        window_size=4,
        window_needed=3,
        evidence_dir=tmp_path / "ev",
    )
    camera = FakeCamera([_jpeg()] * 12)
    detector = FakeDetector()
    monkeypatch.setattr(
        detect_backend.Detector, "from_model", staticmethod(lambda p, force_cpu=False: detector)
    )
    controller = DetectionController(
        cfg,
        camera=camera,
        get_printer=lambda: FakePrinter([PRINTING] * 3 + [27] * 3 + [PRINTING] * 15 + [9]),
        control_allowed=True,
    )
    await controller.start()
    fired = await _wait_for(lambda: controller._last_event is not None)
    await _wait_for(lambda: controller.state()["processing"] is False)
    await controller.stop()
    assert fired
    assert camera.subscribe_count == 1  # never resubscribed across the switch
    assert detector.calls > 0


async def test_controller_notify_only_without_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pause/stop are refused (notify-only) when control is not enabled."""
    monkeypatch.setattr(
        detect_backend.Detector,
        "from_model",
        staticmethod(lambda path, force_cpu=False: FakeDetector()),
    )
    cfg = DetectConfig(
        model_path=tmp_path / "m_edgetpu.tflite",
        grace_s=0.0,
        min_frame_interval_s=0.0,
        window_size=2,
        window_needed=1,
        action="stop",
        evidence_dir=tmp_path / "ev",
    )
    actions: list[str] = []

    async def action_runner(verb: str) -> None:
        actions.append(verb)

    controller = DetectionController(
        cfg,
        camera=FakeCamera([_jpeg()] * 6),
        get_printer=lambda: FakePrinter([PRINTING] * 20 + [9]),
        control_allowed=False,  # server without --enable-control
        action_runner=action_runner,
    )
    await controller.start()
    fired = await _wait_for(lambda: controller._last_event is not None)
    await _wait_for(lambda: controller.state()["processing"] is False)
    await controller.stop()
    assert fired
    assert actions == []  # refused — evidence + webhook still happened


async def test_controller_surfaces_model_load_error(tmp_path: Path) -> None:
    """Model load failure: server keeps running, but no camera is held."""
    cfg = DetectConfig(model_path=tmp_path / "missing_edgetpu.tflite")
    camera = FakeCamera([_jpeg()] * 4)
    controller = DetectionController(
        cfg, camera=camera, get_printer=lambda: FakePrinter([PRINTING] * 5 + [9])
    )
    await controller.start()
    await _wait_for(lambda: controller.state()["processing"] is False, timeout_s=5.0)
    state = controller.state()
    await controller.stop()
    assert state["enabled"] is True
    assert state["backend"] is None
    assert "model load failed" in (state["error"] or "")
    # Without a backend the session must not subscribe to the camera —
    # the printer's scarce camera slots are not wasted on nothing.
    assert camera.subscribe_count == 0


async def test_collect_writes_frames_while_printing_without_model(tmp_path: Path) -> None:
    """Collect mode saves frames while printing — no model needed at all."""
    cfg = DetectConfig(
        model_path=tmp_path / "missing_edgetpu.tflite",
        collect_dir=tmp_path / "collect",
        collect_interval_s=0.0,
    )
    camera = FakeCamera([_jpeg()] * 6)
    controller = DetectionController(
        cfg, camera=camera, get_printer=lambda: FakePrinter([PRINTING] * 20 + [9])
    )
    controller.set_collecting(True)
    await controller.start()
    await _wait_for(lambda: controller._collect_count() >= 6, timeout_s=5.0)
    await _wait_for(lambda: controller.state()["processing"] is False, timeout_s=5.0)
    await controller.stop()
    assert controller._collect_count() == 6
    assert camera.subscribe_count == 1  # collection holds the camera without a model
    assert controller.state()["collect"]["enabled"] is True
    assert controller.state()["collect"]["target"] == cfg.collect_target


async def test_collect_disabled_by_default(tmp_path: Path) -> None:
    cfg = DetectConfig(model_path=tmp_path / "m_edgetpu.tflite", collect_dir=tmp_path / "c")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            detect_backend.Detector,
            "from_model",
            staticmethod(lambda path, force_cpu=False: FakeDetector()),
        )
        camera = FakeCamera([_jpeg()] * 4)
        controller = DetectionController(
            cfg, camera=camera, get_printer=lambda: FakePrinter([PRINTING] * 10 + [9])
        )
        await controller.start()
        await _wait_for(lambda: controller.state()["processing"] is False, timeout_s=5.0)
        await controller.stop()
    finally:
        monkeypatch.undo()
    assert controller._collect_count() == 0
    assert controller.state()["collect"]["enabled"] is False


async def test_collect_per_print_cap_and_fifo(tmp_path: Path) -> None:
    """Retention: max frames per print + global FIFO deletes the oldest."""
    cfg = DetectConfig(
        model_path=tmp_path / "missing_edgetpu.tflite",
        collect_dir=tmp_path / "collect",
        collect_interval_s=0.0,
        collect_max_per_print=3,
        collect_max_files=4,
    )
    # Zwei alte Frames vorab — beim 4. gesammelten Frame fliegt der älteste raus.
    for name in ("20250101-000001.jpg", "20250101-000002.jpg"):
        (tmp_path / "collect").mkdir(parents=True, exist_ok=True)
        (tmp_path / "collect" / name).write_bytes(_jpeg())
    camera = FakeCamera([_jpeg()] * 8)
    controller = DetectionController(
        cfg, camera=camera, get_printer=lambda: FakePrinter([PRINTING] * 20 + [9])
    )
    controller.set_collecting(True)
    await controller.start()
    await _wait_for(lambda: controller._collect_count() >= 4, timeout_s=5.0)
    await _wait_for(lambda: controller.state()["processing"] is False, timeout_s=5.0)
    await controller.stop()
    assert controller._collect_count() == 4  # cap global: 2 alt + 3 neu - 1 fifo
    remaining = sorted(p.name for p in (tmp_path / "collect").glob("*.jpg"))
    assert "20250101-000001.jpg" not in remaining  # ältestes per FIFO entfernt
    assert len([p for p in remaining if p.startswith("2")]) >= 2  # neue frames da


def test_set_action_runtime_gating(tmp_path: Path) -> None:
    cfg = DetectConfig(model_path=tmp_path / "m.tflite")
    controller = DetectionController(
        cfg, camera=FakeCamera([]), get_printer=lambda: FakePrinter([0]), control_allowed=False
    )
    controller.set_action("notify")
    assert controller.cfg.action == "notify"
    with pytest.raises(PermissionError):
        controller.set_action("pause")
    with pytest.raises(PermissionError):
        controller.set_action("stop")
    with pytest.raises(ValueError):
        controller.set_action("destroy")
    controller2 = DetectionController(
        DetectConfig(model_path=tmp_path / "m.tflite"),
        camera=FakeCamera([]),
        get_printer=lambda: FakePrinter([0]),
        control_allowed=True,
    )
    controller2.set_action("stop")
    assert controller2.cfg.action == "stop"


# --- server endpoints ----------------------------------------------------------


async def test_detect_endpoints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GET /api/detect reports state (including model-load errors)."""
    pytest.importorskip("fastapi")
    import httpx

    from pycentauri import server as server_module
    from tests.test_client import MAINBOARD, _FakePrinter

    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app(
        "127.0.0.1",
        mainboard_id=MAINBOARD,
        detect_config=DetectConfig(
            model_path=tmp_path / "missing_edgetpu.tflite", evidence_dir=tmp_path / "ev"
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        r = await client.get("/api/detect")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["backend"] is None
        assert "model load failed" in (body["error"] or "")

        r = await client.get("/api/detect/evidence/nope.jpg")
        assert r.status_code == 404
        # Path traversal is defused: Path(name).name strips directories.
        r = await client.get("/api/detect/evidence/..%2Fsecret.jpg")
        assert r.status_code in (400, 404)
        r = await client.get("/api/detect/evidence/nope.txt")
        assert r.status_code == 400

    await server.stop()


async def test_detect_endpoints_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --detect the endpoints 404 (route never registered)."""
    pytest.importorskip("fastapi")
    import httpx

    from pycentauri import server as server_module
    from tests.test_client import MAINBOARD, _FakePrinter

    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", mainboard_id=MAINBOARD)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        r = await client.get("/api/detect")
        assert r.status_code == 404
    await server.stop()


async def test_detect_post_endpoints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """POST /api/detect/collect toggles; /api/detect/action is control-gated."""
    pytest.importorskip("fastapi")
    import httpx

    from pycentauri import server as server_module
    from tests.test_client import MAINBOARD, _FakePrinter

    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app(
        "127.0.0.1",
        mainboard_id=MAINBOARD,
        detect_config=DetectConfig(
            model_path=tmp_path / "m.tflite", collect_dir=tmp_path / "collect"
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        r = await client.post("/api/detect/collect", json={"enabled": True})
        assert r.status_code == 200
        assert r.json()["collect"]["enabled"] is True
        r = await client.post("/api/detect/action", json={"action": "pause"})
        assert r.status_code == 403  # server without --enable-control
    await server.stop()

    app2 = server_module.create_app(
        "127.0.0.1",
        mainboard_id=MAINBOARD,
        enable_control=True,
        detect_config=DetectConfig(
            model_path=tmp_path / "m.tflite", collect_dir=tmp_path / "collect"
        ),
    )
    transport2 = httpx.ASGITransport(app=app2)
    async with (
        app2.router.lifespan_context(app2),
        httpx.AsyncClient(transport=transport2, base_url="http://test") as client2,
    ):
        r = await client2.post("/api/detect/action", json={"action": "stop"})
        assert r.status_code == 200
        assert r.json()["action"] == "stop"
        r = await client2.post("/api/detect/action", json={"action": "destroy"})
        assert r.status_code == 400
    await server.stop()


# --- telegram notifications -----------------------------------------------------


def test_telegram_notifier_factory_unset_credentials() -> None:
    from pycentauri.detect.pipeline import telegram_notifier_for

    assert telegram_notifier_for(None, "42") is None
    assert telegram_notifier_for("TOK", None) is None
    assert telegram_notifier_for("", "42") is None
    assert telegram_notifier_for("TOK", "42") is not None


def test_send_telegram_photo_fallback_and_failure(tmp_path: Path) -> None:
    import httpx

    from pycentauri.detect.pipeline import send_telegram

    jpg = tmp_path / "ev.jpg"
    jpg.write_bytes(_jpeg())
    payload = {
        "event": {
            "label": "spaghetti",
            "score": 0.87,
            "when": "t",
            "backend": "edgetpu",
            "evidence": str(jpg),
        }
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    asyncio.run(send_telegram("TOK", "42", payload, transport=httpx.MockTransport(handler)))
    assert "sendPhoto" in str(seen[0].url)
    assert b"42" in seen[0].read()

    no_photo = {"event": {"label": "spaghetti", "score": 0.5, "when": "t", "backend": "cpu"}}
    asyncio.run(send_telegram("TOK", "42", no_photo, transport=httpx.MockTransport(handler)))
    assert "sendMessage" in str(seen[1].url)

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    with pytest.raises(RuntimeError, match="telegram send failed"):
        asyncio.run(send_telegram("TOK", "42", no_photo, transport=httpx.MockTransport(bad)))


async def test_controller_sends_telegram_on_fire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        detect_backend.Detector,
        "from_model",
        staticmethod(lambda path, force_cpu=False: FakeDetector()),
    )
    cfg = DetectConfig(
        model_path=tmp_path / "m_edgetpu.tflite",
        grace_s=0.0,
        min_frame_interval_s=0.0,
        window_size=4,
        window_needed=3,
        action="notify",
        evidence_dir=tmp_path / "ev",
    )
    camera = FakeCamera([_jpeg()] * 12)
    tg: list[dict[str, Any]] = []

    async def telegram_poster(payload: dict[str, Any]) -> None:
        tg.append(payload)

    controller = DetectionController(
        cfg,
        camera=camera,
        get_printer=lambda: FakePrinter([0] + [PRINTING] * 20 + [9]),
        telegram_poster=telegram_poster,
    )
    await controller.start()
    fired = await _wait_for(lambda: controller._last_event is not None)
    assert fired, f"no event fired; state={controller.state()}"
    await _wait_for(lambda: bool(tg) or controller.state()["processing"] is False)
    await controller.stop()

    assert len(tg) == 1  # exactly once per print
    assert tg[0]["type"] == "spaghetti_detected"
    assert tg[0]["event"]["evidence"]  # evidence JPEG saved alongside
