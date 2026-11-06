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
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from pycentauri.camera import EOI, SOI
from pycentauri.detect.backend import detection_features

if TYPE_CHECKING:
    from pycentauri.models import Status

log = logging.getLogger(__name__)

#: ``print_status`` code for an actively running print (both models).
PRINTING = 13
PAUSED = 6  # print paused (filament runout on the CC1, user pause)
STOPPING = 7
STOPPED = 8
COMPLETED = 9
ERROR = 14

#: Status-code → Telegram one-liner; sent (with the latest camera frame)
#: whenever the printer's code CHANGES while a telegram poster is wired.
#: Consecutive transitions that map to the SAME text are collapsed into one
#: message (the abort walk 13→7→8 would otherwise post "print aborted" twice).
STATUS_ALERTS = {
    PAUSED: "⏸️ print paused",
    27: "🔁 filament switch",
    28: "🔁 filament switch",
    29: "🔁 filament switch",
    STOPPING: "🛑 print aborted",
    STOPPED: "🛑 print aborted",
    COMPLETED: "✅ print finished",
    ERROR: "❗ printer error",
}
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
    "send_telegram",
    "telegram_notifier_for",
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
    # Optional Telegram push (photo + caption); both must be set.
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    # Optional bot password (BotFather): attached to every outgoing Telegram
    # request as the X-Telegram-Bot-Api-Secret-Token header. Nothing is ever
    # received from Telegram — alerts are strictly one-way.
    telegram_password: str | None = None
    evidence_dir: Path = Path("data/evidence")
    force_cpu: bool = False
    # Training-data collection: while a print runs, frames are written to
    # collect_dir (frame one every collect_interval_s). Collection needs no
    # model — it exists so users can gather a dataset before any model
    # exists. collect_target is the "enough data" hint shown in the UI.
    collect_dir: Path = Path("data/collect")
    collect_target: int = 1000
    collect_interval_s: float = 5.0
    # Layer-aware collection: the first layers densely (interval), then one
    # frame per total//max_per_print layer changes — evenly spread over the
    # WHOLE print instead of burning the budget in the first minutes.
    collect_first_layers: int = 3
    collect_min_gap_s: float = 20.0  # floor between two adaptive frames
    # Retention: max frames per print and global FIFO cap — a 10 h print
    # must not fill the disk with near-duplicate frames.
    collect_max_per_print: int = 500
    collect_max_files: int = 4000
    # Failure-Ring: die letzten N Frames des Drucks bleiben im RAM und
    # werden beim Alarm als Evidenz-Phase weggeschrieben.
    collect_ring_frames: int = 600  # die Ablösephase (5-10 min @ 1s) muss komplett rein
    collect_retention_days: float = 30.0  # Timeline-Vergleiche brauchen Historie (Tim 4.11.)
    backend_alert_cooldown_s: float = 600.0
    classifier_path: Path | None = None  # two-stage: None = legacy threshold

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


def telegram_notifier_for(
    token: str | None,
    chat_id: str | None,
    password: str | None = None,
) -> WebhookPoster | None:
    """Bind :func:`send_telegram` to credentials; ``None`` when either is unset."""
    if not token or not chat_id:
        return None

    async def _post(payload: dict[str, Any]) -> None:
        await send_telegram(token, chat_id, payload, password=password)

    return _post


async def send_telegram(
    token: str,
    chat_id: str,
    payload: dict[str, Any],
    *,
    password: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Send an alert to a Telegram chat: evidence photo + caption.

    Falls back to a text message when the evidence JPEG is missing.
    ``password`` (the BotFather bot password, optional) is attached to
    every request as the ``X-Telegram-Bot-Api-Secret-Token`` header —
    the documented place for a bot secret. Raises on failure — callers
    decide policy (the controller logs).
    """
    event = payload.get("event", {})
    caption = event.get("text") or (
        f"{event.get('label', 'detection')} detected — score {event.get('score', 0):.2f}\n"
        f"{event.get('when', '')} · backend {event.get('backend', '?')}"
    )
    photo: bytes | None = event.get("photo")
    if photo is None:
        evidence = event.get("evidence")
        path = Path(evidence) if evidence else None
        if path and path.is_file():
            photo = path.read_bytes()
    headers = {"X-Telegram-Bot-Api-Secret-Token": password} if password else None
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        if photo:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": ("frame.jpg", photo, "image/jpeg")},
                headers=headers,
            )
        else:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": caption},
                headers=headers,
            )
        ok = resp.status_code == 200 and resp.json().get("ok") is True
        if not ok:
            raise RuntimeError(f"telegram send failed: HTTP {resp.status_code} {resp.text[:200]}")


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
        telegram_poster: WebhookPoster | None = None,
    ) -> None:
        self.cfg = cfg
        self._camera = camera
        self._get_printer = get_printer
        self._control_allowed = control_allowed
        self._action_runner = action_runner
        self._webhook_poster = webhook_poster
        self._telegram_poster = telegram_poster
        self._closing = False
        self._status_task: asyncio.Task[None] | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._printing = False  # status == 13 — inference runs
        self._active = False  # printing-ish (13 or filament switch) — subscribed
        self._fired = False  # one alert per print
        self._last_code: int | None = None  # previous print_status
        self._last_alert_text: str | None = None  # dedupes consecutive same-text alerts
        self._layer: int | None = None
        self._total_layers: int | None = None
        self._print_ctx: str = ""  # human context appended to alerts
        self._last_codes: str = ""  # raw status codes, logged for debugging
        self._last_collect_layer: int | None = None
        self._last_frame: bytes | None = None  # latest camera JPEG
        self._status_posts: set[asyncio.Task[None]] = set()
        self._window: deque[bool] = deque(maxlen=cfg.window_size)
        self._backend: Any = None  # detect.backend.Detector, loaded lazily
        self._backend_lock = asyncio.Lock()  # start() und _session_loop racen um den ersten Load
        self._last_print_status: int | None = None
        self._failure_ring: deque[tuple[str, bytes]] = deque(maxlen=cfg.collect_ring_frames)
        self._backend_alert_sent = False  # one alert per edgetpu→cpu episode
        self._last_backend_alert_time = 0.0
        # Training-frame collection toggle — restored across restarts via a
        # dotfile in collect_dir (the FIFO only touches *.jpg / *.json).
        marker = cfg.collect_dir / ".collecting"
        try:
            self._collecting = marker.is_file()
        except OSError:
            self._collecting = False
        # The armed response persists across restarts (dotfile in
        # evidence_dir); the config's DETECT_ACTION is only the default.
        action_marker = cfg.evidence_dir / ".action"
        try:
            saved = action_marker.read_text(encoding="utf-8").strip()
        except OSError:
            saved = ""
        if saved in ACTIONS:
            cfg.action = saved
        # Detection threshold (GUI slider) — persists like the armed action.
        try:
            t = float((self.cfg.evidence_dir / ".threshold").read_text(encoding="utf-8").strip())
            if 0.0 <= t <= 1.0:
                cfg.threshold = t
        except (OSError, ValueError):
            pass
        self._last_event: DetectionEvent | None = None
        self._error: str | None = None
        self._last_look: float = 0.0  # monotonic ts of the last model run
        self._last_top_score = 0.0  # top score der letzten Inferenz (Collect-Hochtakt)
        self._classifier: Any = None  # two-stage Klassifikator, lazy geladen

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._status_task = asyncio.create_task(
            self._status_loop(), name="pycentauri-detect-status"
        )
        await self._ensure_backend()

    async def stop(self) -> None:
        self._closing = True
        for task in (self._session_task, self._status_task, *self._status_posts):
            if task is not None and not task.done():
                task.cancel()
                # Bounded await: unter Python 3.11 kann der Cancel an einem
                # im async-for suspendierten watch()-Generator nie zugestellt
                # werden (CI-Hang, 6.11.) — dann wird die Task verlassen,
                # statt den Shutdown zu blockieren.
                with contextlib.suppress(
                    asyncio.TimeoutError, asyncio.CancelledError, Exception
                ):
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
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
            "last_look_s": round(max(0.0, time.time() - self._last_look), 1)
            if self._last_look
            else None,
            "window": {
                "positives": sum(self._window),
                "size": self.cfg.window_size,
                "needed": self.cfg.window_needed,
            },
            "processing": self._session_task is not None and not self._session_task.done(),
            "printing": self._printing,
            "collect": {
                "enabled": self._collecting,
                "count": self._collect_count(),
                "target": self.cfg.collect_target,
                "interval_s": self.cfg.collect_interval_s,
                "max_per_print": self.cfg.collect_max_per_print,
                "max_files": self.cfg.collect_max_files,
            },
            "control_allowed": self._control_allowed,
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

    def _persist_collect(self) -> None:
        marker = self.cfg.collect_dir / ".collecting"
        try:
            self.cfg.collect_dir.mkdir(parents=True, exist_ok=True)
            if self._collecting:
                marker.write_text("on\n")
            else:
                marker.unlink(missing_ok=True)
        except OSError as err:
            log.warning("detection: cannot persist collect toggle: %r", err)

    def _collect_count(self) -> int:
        try:
            return len(list(self.cfg.collect_dir.glob("*.jpg")))
        except OSError:
            return 0

    # --- runtime switching (dashboard) --------------------------------------

    def set_collecting(self, enabled: bool) -> None:
        """Toggle training-frame collection. Needs no model, only a camera.

        Enabling mid-print starts a session immediately (the status watcher
        would otherwise only start one on the next printing transition).
        """
        self._collecting = bool(enabled)
        self._persist_collect()
        if (
            self._collecting
            and self._active
            and (self._session_task is None or self._session_task.done())
        ):
            self._begin_session()

    def set_threshold(self, threshold: float) -> None:
        """Set the detection threshold at runtime; persists in evidence_dir.

        The GUI slider enforces a sensible range (0.30-0.80); the
        controller accepts the full 0..1 (tests, API callers with their
        own validation).
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within 0..1, got {threshold!r}")
        self.cfg.threshold = float(threshold)
        try:
            (self.cfg.evidence_dir / ".threshold").write_text(f"{threshold:.2f}", encoding="utf-8")
        except OSError as err:
            log.debug("detection: threshold-state unbeschreibbar: %r", err)

    def set_action(self, action: str) -> None:
        """Arm the response at runtime (the dashboard's notify/pause/stop)."""
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
        if action in ("pause", "stop") and not self._control_allowed:
            raise PermissionError("arming pause/stop requires --enable-control")
        self.cfg.action = action
        marker = self.cfg.evidence_dir / ".action"
        try:
            self.cfg.evidence_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{action}\n", encoding="utf-8")
        except OSError as err:
            log.warning("detection: cannot persist armed action: %r", err)

    # --- model --------------------------------------------------------------

    async def _ensure_backend(self) -> None:
        if self._backend is not None:
            return
        # start() und _session_loop rufen gleichzeitig — OHNE Lock opened
        # der Verlierer-Race ein zweites Interpreter-Handle bzw. faellt auf
        # CPU und flapped das backend-state file (Alarm-Paar pro Restart).
        async with self._backend_lock:
            if self._backend is not None:  # der andere Call hat gewonnen
                return
            await self._load_backend()

    async def _load_backend(self) -> None:
        try:
            from pycentauri.detect.backend import Detector

            loop = asyncio.get_running_loop()
            self._backend = await loop.run_in_executor(
                None,
                lambda: Detector.from_model(self.cfg.model_path, force_cpu=self.cfg.force_cpu),
            )
            self._error = None
            await self._check_backend_regression()
            self._load_classifier()
        except Exception as err:
            # Not fatal for the server: status watching continues, sessions
            # no-op, and /api/detect surfaces the reason.
            self._error = f"model load failed: {err}"
            log.error("detection: %s", self._error)

    def _backend_state_file(self) -> Path:
        return self.cfg.evidence_dir / ".backend"

    async def _check_backend_regression(self) -> None:
        """Warn (once per episode) when the Edge TPU degrades to CPU.

        The Coral's runtime SIGABRTs the whole process on USB errors and a
        restart then silently lands on CPU — persist the last backend name
        so the regression is detected across restarts. Startup on CPU (no
        Coral attached, ``--detect-cpu``) does NOT alert: only the
        transition edgetpu → anything-else does. Re-arms when edgetpu is
        back.
        """
        try:
            name = getattr(self._backend, "backend_name", None)
            if name is None:
                return
            state_file = self._backend_state_file()
            previous = None
            try:
                if state_file.is_file():
                    previous = state_file.read_text(encoding="utf-8").strip() or None
            except OSError as err:
                log.debug("detection: backend-state unlesbar: %r", err)
            # Alerts ZUERST — ein fehlgeschlagener State-Write darf sie
            # nicht verschlucken (state-file kann fremd-owned sein).
            if previous is not None and previous != name and not self.cfg.force_cpu:
                # Cooldown: der Coral re-enumeriert gelegentlich am Open —
                # ohne Cooldown gäbe es ein ⚠️/✅-Paar pro Restart (16:58/17:03).
                cooled = (
                    time.time() - self._last_backend_alert_time
                ) >= self.cfg.backend_alert_cooldown_s
                if not cooled:
                    log.warning(
                        "detection: backend-Wechsel %s→%s erkannt — Alert UNTERDRÜCKT (cooldown %.0fs seit dem letzten)",
                        previous,
                        name,
                        time.time() - self._last_backend_alert_time,
                    )
                if name != "edgetpu" and not self._backend_alert_sent and cooled:
                    self._backend_alert_sent = True
                    self._last_backend_alert_time = time.time()
                    log.warning(
                        "detection: Edge TPU nicht mehr erreichbar (vorher %s, jetzt %s) — CPU-Fallback",
                        previous,
                        name,
                    )
                    await self._post_backend_alert(name, back=False)
                elif name == "edgetpu" and cooled:
                    self._last_backend_alert_time = time.time()
                    log.info(
                        "detection: Edge TPU wieder erreichbar (vorher %s) — zurück auf edgetpu",
                        previous,
                    )
                    await self._post_backend_alert(name, back=True)
            if name == "edgetpu":
                self._backend_alert_sent = False  # episode over — re-arm
            try:
                state_file.write_text(name, encoding="utf-8")
            except OSError as err:
                log.debug("detection: backend-state unbeschreibbar: %r", err)
        except Exception as err:  # never let the gate break the load
            log.debug("detection: backend-state check fehlgeschlagen: %r", err)

    async def _post_backend_alert(self, name: str, *, back: bool) -> None:
        if self._telegram_poster is None:
            return
        if back:
            text = f"✅ Edge TPU (Coral) wieder erreichbar — Erkennung läuft wieder auf {name}."
        else:
            text = (
                "⚠️ Edge TPU (Coral) nicht mehr erreichbar — Erkennung läuft "
                f"auf {name} weiter. Coral am Server ab-/anstecken, dann "
                "Dienst neu starten."
            )
        payload = {
            "type": "backend_status",
            "event": {
                "text": text,
                "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        }
        # inline awaited — ein Task könnte vom nächsten Deploy-Restart
        # gecancelt werden, bevor Telegram die Meldung bekommt (00:31-Bug).
        await self._send_backend_alert(payload)

    async def _send_backend_alert(self, payload: dict[str, Any]) -> None:
        poster = self._telegram_poster
        if poster is None:
            log.warning(
                "detection: backend-alert NICHT gesendet — kein Telegram-Poster konfiguriert"
            )
            return
        try:
            await poster(payload)
            log.warning(
                "detection: backend-alert VERSENDET: %s",
                payload["event"]["text"].split(" — ")[0],
            )
        except Exception as err:
            log.error("detection: telegram delivery failed: %r", err)

    @property
    def printer_busy(self) -> bool:
        """True while a job occupies the printer (incl. paused — resumable).

        Same definition as the UI's busy-state: every print_status except
        IDLE/STOPPED/COMPLETED/ERROR (0/8/9/14). Unknown status (no push
        yet) counts as not busy.
        """
        return self._last_print_status is not None and self._last_print_status not in (0, 8, 9, 14)

    def _load_classifier(self) -> None:
        """Two-stage Klassifikator laden (None = legacy threshold)."""
        path = self.cfg.classifier_path
        if path is None or not Path(path).is_file():
            self._classifier = None
            return
        try:
            import joblib  # type: ignore[import-untyped]

            self._classifier = joblib.load(path)
            log.info("detection: two-stage Klassifikator geladen: %s", path)
        except Exception as err:
            self._classifier = None
            log.error("detection: Klassifikator %s unladbar: %r", path, err)

    def _two_stage_verdict(self, detections: list[Any]) -> bool:
        """Two-stage verdict: die Klassifikator-Wahrscheinlichkeit über den
        Detektions-Features; ohne Klassifikator: die Legacy-Detektion."""
        if self._classifier is None or not detections:
            return bool(detections)
        feats = detection_features(detections)
        prob = float(self._classifier.predict_proba([feats])[0][1])
        return prob >= 0.5

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
        pi = st.print_info
        self._layer = pi.current_layer if pi else None
        self._total_layers = pi.total_layer if pi else None
        self._last_codes = (
            f"CurrentStatus={st.current_status} PrintInfo.Status={pi.status if pi else None}"
        )
        if pi is not None and pi.filename:
            self._print_ctx = f" — {pi.filename}"
            if pi.current_layer and pi.total_layer:
                self._print_ctx += f", layer {pi.current_layer}/{pi.total_layer}"
            elif pi.progress is not None:
                self._print_ctx += f", {pi.progress}%"
        code = st.print_status
        self._last_print_status = code
        # Best-effort pause reason: the SDCP protocol has no reason code
        # (6 = "paused", period). CC2 MQTT at least exposes the runout
        # sensor; log the raw codes so firmware quirks can be mapped later.
        cc2 = st.raw.get("_cc2", {}) if isinstance(st.raw, dict) else {}
        if (
            code == PAUSED
            and isinstance(cc2, dict)
            and cc2.get("filament_detect_enable")
            and not cc2.get("filament_detected", 1)
        ):
            self._pause_reason = " (filament runout)"
        else:
            self._pause_reason = ""
        prev = self._last_code
        self._last_code = code
        was_active = self._active
        self._printing = code == PRINTING
        self._active = code == PRINTING or (code is not None and code in FILAMENT_SWITCH)
        if self._active and not was_active:
            self._begin_session()
        elif not self._active and was_active:
            # Session loop observes the flag and unsubscribes itself.
            self._fired = False
        text = STATUS_ALERTS.get(code) if code is not None else None
        if (
            self._telegram_poster is not None
            and prev is not None
            and code != prev
            and text is not None
            and text != self._last_alert_text
        ):
            self._last_alert_text = text
            self._post_status_alert(text)

    def _post_status_alert(self, text: str) -> None:
        task = asyncio.create_task(self._send_status_alert(text))
        self._status_posts.add(task)
        task.add_done_callback(self._status_posts.discard)

    async def _send_status_alert(self, text: str) -> None:
        if text.startswith("⏸️"):
            text += getattr(self, "_pause_reason", "")
        if self._print_ctx:
            text += self._print_ctx
        log.info("detection: status alert %r (raw: %s)", text, self._last_codes)
        payload = {
            "type": "print_status",
            "event": {
                "text": text,
                "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "photo": self._last_frame,
            },
        }
        poster = self._telegram_poster
        if poster is None:  # raced away — nothing to send
            return
        try:
            await poster(payload)
        except Exception as err:
            log.error("detection: telegram delivery failed: %r", err)

    def _begin_session(self) -> None:
        self._fired = False
        self._last_alert_text = None  # a fresh print alerts on its own events
        self._window.clear()
        if self._session_task is None or self._session_task.done():
            self._session_task = asyncio.create_task(
                self._session_loop(), name="pycentauri-detect-session"
            )

    # --- detection session --------------------------------------------------

    async def _session_loop(self) -> None:
        # Self-healing: retry the model load once per print — if the user
        # adds the model (or the Coral) later, the next print picks it up.
        await self._ensure_backend()
        if self._backend is None and not self._collecting:
            # No usable model and nothing to collect: do NOT hold the camera
            # subscription open for nothing — the printer's camera slots are
            # scarce.
            log.debug("detection: no backend and no collection — session skipped")
            return
        started = time.monotonic()
        last_infer = 0.0
        last_collect = 0.0
        collect_saved = 0
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
                if not self._printing:  # filament switch: hold, don't work
                    continue
                jpeg = jpeg_from_part(part)
                if jpeg is None:
                    continue
                self._last_frame = jpeg
                self._failure_ring.append(
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), jpeg)
                )
                # Collection has no grace period — priming and first layers
                # are valuable training material too.
                if self._collecting and self._collect_due(now, last_collect, collect_saved):
                    last_collect = now
                    collect_saved += 1
                    self._last_collect_layer = self._layer
                    await self._save_collect_frame(jpeg)
                    await self._enforce_collect_fifo()
                if self._backend is None or now - started < self.cfg.grace_s:
                    continue
                if now - last_infer < self.cfg.min_frame_interval_s:
                    continue
                last_infer = now
                self._last_look = time.time()
                await self._evaluate(jpeg)

    def _collect_interval(self) -> float:
        """Adaptive Cadence (Tim 4.11.): 1 Bild/MINUTE im stillen Normalbetrieb,
        5-s-Takt sobald der Score steigt (Fehler-Phase dicht aufnehmen)."""
        if self._last_top_score >= 0.4 or sum(self._window) >= 2:
            return self.cfg.collect_interval_s
        if self.cfg.collect_interval_s <= 0:  # expliziter Jede-Frame-Modus
            return 0.0
        return max(self.cfg.collect_interval_s, 60.0)

    def _collect_due(self, now: float, last_collect: float, collect_saved: int) -> bool:
        """Decide whether to save a training frame at ``now``.

        Budget-aware pacing so coverage reaches the END of a print (both
        observed failures happened in the last layers):

        * First layers (and prints without layer info): dense on the time
          interval, capped at a quarter of the per-print budget — the
          3.11. print burned all 200 frames in the first 16 minutes and
          the detach phase 77 minutes later went unrecorded.
        * Afterwards the REMAINING budget is spread over the REMAINING
          layers as a float gap (int rounding would exhaust the budget
          before the final layers), floor-limited by ``collect_min_gap_s``.
        """
        budget = self.cfg.collect_max_per_print
        if collect_saved >= budget:
            return False
        layer = self._layer or 0
        total = self._total_layers or 0
        dense_share = max(20, budget // 4)
        if layer <= 0 or total <= 0 or layer <= self.cfg.collect_first_layers:
            if collect_saved < dense_share:
                return now - last_collect >= self._collect_interval()
            # Dense-Share aus — ohne Layer-Info aufs Mindest-Intervall
            # ausweichen statt weiter im 5-s-Takt zu feuern.
            return now - last_collect >= max(self._collect_interval(), self.cfg.collect_min_gap_s)
        remaining_budget = max(1, budget - collect_saved)
        remaining_layers = max(1, total - layer)
        gap = max(1.0, remaining_layers / remaining_budget)
        last = self._last_collect_layer
        if last is not None and layer - last < gap:
            return False
        return now - last_collect >= self.cfg.collect_min_gap_s

    async def _save_collect_frame(self, jpeg: bytes) -> None:
        """Write one training frame (off the event loop), timestamped."""
        collect_dir = self.cfg.collect_dir

        def _write() -> None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            path = (collect_dir / stamp).with_suffix(".jpg")
            for i in range(1, 100):
                if not path.exists():
                    break
                path = (collect_dir / f"{stamp}-{i}").with_suffix(".jpg")
            collect_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(jpeg)

        await asyncio.get_running_loop().run_in_executor(None, _write)

    async def _enforce_collect_fifo(self) -> None:
        """Global FIFO cap: delete the oldest frames beyond the limit."""
        limit = self.cfg.collect_max_files

        def _trim() -> int:
            frames = sorted(self.cfg.collect_dir.glob("*.jpg"))
            removed = 0
            # Retention (Tim 4.11.): Frames älter als collect_retention_days
            # fliegen zuerst — Vorher/Nachher-Vergleiche brauchen die
            # Historie, aber nicht endlos.
            cutoff = time.time() - self.cfg.collect_retention_days * 86400
            for f in list(frames):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    f.with_suffix(".json").unlink(missing_ok=True)
                    frames.remove(f)
                    removed += 1
            for old in frames[: max(0, len(frames) - limit)]:
                old.unlink(missing_ok=True)
                old.with_suffix(".json").unlink(missing_ok=True)
                removed += 1
            return removed

        removed = await asyncio.get_running_loop().run_in_executor(None, _trim)
        if removed:
            log.info("detection: collect FIFO trimmed %d oldest frames", removed)

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
        if detections:
            detections = [d for d in detections if d.score >= 0.30]  # noise floor
        verdict = self._two_stage_verdict(detections)
        self._window.append(verdict)
        self._last_top_score = float(detections[0].score) if detections else 0.0
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
        await self._flush_failure_ring(event)
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
        if self._telegram_poster is not None:
            try:
                await self._telegram_poster(
                    {"type": "spaghetti_detected", "event": event.as_dict()}
                )
            except Exception as err:
                log.error("detection: telegram delivery failed: %r", err)
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

    async def _flush_failure_ring(self, event: DetectionEvent) -> None:
        """Preserve the last N frames before the alarm as failure evidence.

        The ring holds the print's recent frames in RAM (see
        ``collect_ring_frames``); on a confirmed detection the whole phase
        is written out deterministically — no collect cap can make the
        failure phase go unrecorded (3.11. lesson). The ring is cleared
        afterwards so the next print starts fresh.
        """
        ring = list(self._failure_ring)
        self._failure_ring.clear()
        if not ring:
            return

        def _write() -> str:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            out = self.cfg.evidence_dir / "failure-ring" / stamp
            out.mkdir(parents=True, exist_ok=True)
            for ts, jpeg in ring:
                name = re.sub(r"[^0-9T]", "", ts)[:15] + ".jpg"
                (out / name).write_bytes(jpeg)
            (out / "alarm.json").write_text(
                json.dumps(event.as_dict(), indent=1, default=str), encoding="utf-8"
            )
            return str(out)

        out = await asyncio.get_running_loop().run_in_executor(None, _write)
        log.info("detection: failure-ring gepackt (%d frames) → %s", len(ring), out)

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
