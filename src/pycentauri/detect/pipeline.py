"""Failed-print ("spaghetti") detection pipeline.

Lifecycle: a status watcher consumes ``printer.watch()``; while the
printer reports ``print_status == 13`` (Printing) a detection session
subscribes to the shared :class:`CameraBroadcaster` — the detector is
deliberately *just another subscriber*, so the printer only ever sees one
upstream camera connection no matter who else is watching — and runs the
model on roughly one frame per second. When the print leaves the printing
state (including CC2 Canvas filament switches) the session unsubscribes,
letting the broadcaster's idle close free the printer's scarce camera
slots exactly as it does for human viewers.

Trigger rule: a frame counts as positive when the model returns any
detection above the configured threshold; an alert fires when ≥ K of the
last M frames were positive (debounce against single-frame noise), at
most once per print. The first :attr:`DetectConfig.grace_s` seconds of a
print are skipped — priming, purging and brim-laying all look like
spaghetti to any model.

Actions are safety-gated: evidence + webhook + log always happen;
``pause``/``stop`` additionally require the server to run with
``--enable-control`` (the same gate as the HTTP control endpoints) and
are refused with a logged error otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from pycentauri.camera import EOI, SOI

if TYPE_CHECKING:
    from pycentauri.models import Status

log = logging.getLogger(__name__)

#: ``print_status`` code for an actively running print (both models).
PRINTING = 13
#: CC2 Canvas filament-switch states: the head parks at the purge chute,
#: physically outside the print area. Keep the camera subscription (it
#: would churn the printer's camera slots to drop it for the duration of
#: every switch) but don't run inference — the parked head and the purge
#: pile are a known false-positive source.
FILAMENT_SWITCH = frozenset({27, 28, 29})

#: What to do on a trigger. ``notify`` (default) only records evidence and
#: posts the webhook; ``pause``/``stop`` act on the printer and require
#: control to be enabled.
ACTIONS = ("notify", "pause", "stop")

__all__ = [
    "ACTIONS",
    "DetectConfig",
    "DetectionController",
    "DetectionEvent",
    "jpeg_from_part",
    "post_webhook",
    "save_evidence",
    "webhook_poster_for",
]


@dataclasses.dataclass
class DetectConfig:
    """Tunables for :class:`DetectionController`."""

    model_path: Path
    threshold: float = 0.5
    window_size: int = 6  # M — how many recent frames the decision looks at
    window_needed: int = 4  # K — positives among the last M before alerting
    min_frame_interval_s: float = 1.0
    grace_s: float = 90.0
    action: str = "notify"
    webhook_url: str | None = None
    evidence_dir: Path = Path("data/evidence")
    force_cpu: bool = False

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, got {self.action!r}")
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError(f"threshold must be within 0..1, got {self.threshold}")
        if self.window_needed > self.window_size:
            raise ValueError("window_needed cannot exceed window_size")
        if self.window_size < 1:
            raise ValueError("window_size must be ≥ 1")


@dataclasses.dataclass
class DetectionEvent:
    """One fired alert, also the shape of the webhook payload."""

    when: str
    score: float
    label: str
    backend: str
    model: str
    evidence: str
    detections: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


ActionRunner = Callable[[str], Awaitable[None]]
WebhookPoster = Callable[[dict[str, Any]], Awaitable[None]]


def jpeg_from_part(part: bytes) -> bytes | None:
    """Extract the JPEG payload from one multipart frame.

    The broadcaster fans out whole multipart *parts* — boundary line
    excluded, part headers included — so the actual JPEG is found by the
    same SOI/EOI scan :func:`pycentauri.camera.snapshot` uses.
    """
    start = part.find(SOI)
    if start < 0:
        return None
    end = part.find(EOI, start + 2)
    if end < 0:
        return None
    return part[start : end + 2]


async def post_webhook(url: str, payload: dict[str, Any]) -> None:
    """POST an alert as JSON. Raises on failure — callers decide policy."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(url, json=payload)


def save_evidence(evidence_dir: Path, event: DetectionEvent, jpeg: bytes) -> Path:
    """Write the triggering frame + a JSON sidecar; returns the JPEG path.

    Blocking filesystem work — callers on the event loop must run this in
    an executor (both the controller and ``centauri detect watch`` do).
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = evidence_dir / stamp
    path = base.with_suffix(".jpg")
    for i in range(1, 100):
        if not path.exists():
            break
        path = (evidence_dir / f"{stamp}-{i}").with_suffix(".jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg)
    path.with_suffix(".json").write_text(
        json.dumps(event.as_dict(), indent=2, default=str), encoding="utf-8"
    )
    return path


class DetectionController:
    """Owns the detection lifecycle for one server (or standalone session).

    Constructed with callables rather than concrete printer/broadcaster
    types so tests can drive it entirely with fakes:

    * ``camera`` — the :class:`CameraBroadcaster` to subscribe to.
    * ``get_printer`` — returns the current :class:`Printer` (raises if
      not connected; the status watcher retries with backoff).
    * ``action_runner`` — executes ``"pause"``/``"stop"`` against the
      printer (wired by the server to the manager's printer).
    """

    def __init__(
        self,
        cfg: DetectConfig,
        *,
        camera: Any,
        get_printer: Callable[[], Any],
        control_allowed: bool = False,
        action_runner: ActionRunner | None = None,
        webhook_poster: WebhookPoster | None = None,
    ) -> None:
        self.cfg = cfg
        self._camera = camera
        self._get_printer = get_printer
        self._control_allowed = control_allowed
        self._action_runner = action_runner
        self._webhook_poster = webhook_poster
        self._closing = False
        self._status_task: asyncio.Task[None] | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._printing = False  # status == 13 — inference runs
        self._active = False  # printing-ish (13 or filament switch) — subscribed
        self._fired = False  # one alert per print
        self._window: deque[bool] = deque(maxlen=cfg.window_size)
        self._backend: Any = None  # detect.backend.Detector, loaded lazily
        self._last_event: DetectionEvent | None = None
        self._error: str | None = None

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._status_task = asyncio.create_task(
            self._status_loop(), name="pycentauri-detect-status"
        )
        await self._ensure_backend()

    async def stop(self) -> None:
        self._closing = True
        for task in (self._session_task, self._status_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._session_task = self._status_task = None

    def state(self) -> dict[str, Any]:
        """Snapshot for ``GET /api/detect``."""
        backend_name = getattr(self._backend, "backend_name", None)
        return {
            "enabled": True,
            "backend": backend_name,
            "model": str(self.cfg.model_path),
            "action": self.cfg.action,
            "threshold": self.cfg.threshold,
            "window": {
                "positives": sum(self._window),
                "size": self.cfg.window_size,
                "needed": self.cfg.window_needed,
            },
            "processing": self._session_task is not None and not self._session_task.done(),
            "printing": self._printing,
            "last_event": self._last_event.as_dict() if self._last_event else None,
            "recent_evidence": self._recent_evidence(),
            "error": self._error,
            "evidence_dir": str(self.cfg.evidence_dir),
        }

    def _recent_evidence(self, limit: int = 8) -> list[str]:
        """Filenames (no path) of the newest evidence frames, for the UI strip."""
        try:
            frames = sorted(self.cfg.evidence_dir.glob("*.jpg"), reverse=True)
            return [p.name for p in frames[:limit]]
        except OSError:
            return []

    # --- model --------------------------------------------------------------

    async def _ensure_backend(self) -> None:
        if self._backend is not None:
            return
        try:
            from pycentauri.detect.backend import Detector

            loop = asyncio.get_running_loop()
            self._backend = await loop.run_in_executor(
                None,
                lambda: Detector.from_model(self.cfg.model_path, force_cpu=self.cfg.force_cpu),
            )
            self._error = None
        except Exception as err:
            # Not fatal for the server: status watching continues, sessions
            # no-op, and /api/detect surfaces the reason.
            self._error = f"model load failed: {err}"
            log.error("detection: %s", self._error)

    # --- status watch -------------------------------------------------------

    async def _status_loop(self) -> None:
        backoff = 1.0
        while not self._closing:
            try:
                printer = self._get_printer()
                async for st in printer.watch():
                    self._on_status(st)
                if not self._closing:
                    log.debug("detection: status stream ended, retrying")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not self._closing:
                    log.debug("detection: status watch unavailable: %r", err)
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _on_status(self, st: Status) -> None:
        code = st.print_status
        was_active = self._active
        self._printing = code == PRINTING
        self._active = code == PRINTING or (code is not None and code in FILAMENT_SWITCH)
        if self._active and not was_active:
            self._begin_session()
        elif not self._active and was_active:
            # Session loop observes the flag and unsubscribes itself.
            self._fired = False

    def _begin_session(self) -> None:
        self._fired = False
        self._window.clear()
        if self._session_task is None or self._session_task.done():
            self._session_task = asyncio.create_task(
                self._session_loop(), name="pycentauri-detect-session"
            )

    # --- detection session --------------------------------------------------

    async def _session_loop(self) -> None:
        started = time.monotonic()
        last_infer = 0.0
        try:
            _media_type, chunks = await self._camera.subscribe()
        except Exception as err:
            log.warning("detection: camera subscribe failed: %r", err)
            return
        async with contextlib.aclosing(chunks) as frames:
            async for part in frames:
                if self._closing or not self._active:
                    break
                now = time.monotonic()
                if now - started < self.cfg.grace_s:
                    continue
                if not self._printing:  # filament switch: hold, don't infer
                    continue
                if now - last_infer < self.cfg.min_frame_interval_s:
                    continue
                jpeg = jpeg_from_part(part)
                if jpeg is None:
                    continue
                last_infer = now
                await self._evaluate(jpeg)

    async def _evaluate(self, jpeg: bytes) -> None:
        if self._backend is None:
            return
        loop = asyncio.get_running_loop()
        try:
            detections = await loop.run_in_executor(
                None,
                lambda: self._backend.detect(jpeg, threshold=self.cfg.threshold),
            )
        except Exception as err:
            log.warning("detection: inference failed: %r", err)
            return
        self._window.append(bool(detections))
        if not detections or self._fired:
            return
        if sum(self._window) < self.cfg.window_needed:
            log.debug(
                "detection: positive frame (%d/%d in window)",
                sum(self._window),
                self.cfg.window_size,
            )
            return
        self._fired = True
        await self._fire(detections, jpeg)

    # --- alerting -----------------------------------------------------------

    async def _fire(self, detections: list[Any], jpeg: bytes) -> None:
        top = detections[0]
        event = DetectionEvent(
            when=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            score=round(float(top.score), 4),
            label=top.label,
            backend=self._backend.backend_name,
            model=self.cfg.model_path.name,
            evidence="",
            detections=[d.as_dict() for d in detections],
        )
        event.evidence = str(await self._save_evidence(event, jpeg))
        self._last_event = event
        log.warning(
            "SPAGHETTI DETECTED: %s score=%.2f backend=%s evidence=%s",
            event.label,
            event.score,
            event.backend,
            event.evidence,
        )
        if self.cfg.webhook_url and self._webhook_poster is not None:
            payload = {"type": "spaghetti_detected", "event": event.as_dict()}
            try:
                await self._webhook_poster(payload)
            except Exception as err:
                log.error("detection: webhook delivery failed: %r", err)
        if self.cfg.action in ("pause", "stop"):
            if not self._control_allowed or self._action_runner is None:
                log.error(
                    "detection: refusing %s — requires --enable-control; notify only",
                    self.cfg.action,
                )
                return
            try:
                await self._action_runner(self.cfg.action)
            except Exception as err:
                log.error("detection: %s action failed: %r", self.cfg.action, err)

    async def _save_evidence(self, event: DetectionEvent, jpeg: bytes) -> Path:
        """Write evidence off the event loop (see :func:`save_evidence`)."""
        return await asyncio.get_running_loop().run_in_executor(
            None, save_evidence, self.cfg.evidence_dir, event, jpeg
        )


def webhook_poster_for(url: str) -> WebhookPoster:
    """Bind :func:`post_webhook` to a URL for controller injection."""

    async def _post(payload: dict[str, Any]) -> None:
        await post_webhook(url, payload)

    return _post
