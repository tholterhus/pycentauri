"""Chamber-heating automation: a Shelly-controlled heater for the print room.

The chamber target is resolved per status push, and the slicer wins: when
the printer reports a chamber target (``TempTargetBox``) above zero, the
heater regulates toward it. Otherwise — a print armed via bed target above
the threshold or a filename marker — the configured fallback target
applies. The heater holds the chamber inside the swing the "Faktor"
allows: ±factor/2 around the target (factor 5 at a 45° target = 42.5-47.5).
At print end the heater goes off unconditionally and the session disarms;
a paused print keeps being controlled; attaching mid-print re-evaluates
the activation from the first status push.

Settings persist in a data JSON file that wins over the .conf/CLI defaults
(the same pattern as the detection threshold): the web UI edits them live
via ``POST /api/heating/settings``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pycentauri.models import Status

log = logging.getLogger(__name__)

#: ``print_status`` codes meaning "no job occupies the printer" — same set
#: the lighting automation and the UI treat as not busy (IDLE, STOPPED,
#: COMPLETED, ERROR).
INACTIVE_CODES = frozenset({0, 8, 9, 14})

#: Retry backoff after a failed Shelly call — the hysteresis re-fires on
#: every status push (1/s) while the heater endpoint stays unreachable.
RETRY_BACKOFF_START = 2.0
RETRY_BACKOFF_MAX = 60.0

#: Graph sample cadence: one chamber-temperature point every 5 s while a
#: session is armed (~27 h at the 20 000-point cap).
SAMPLE_INTERVAL_S = 5.0

#: How often the session file is rewritten while samples accumulate.
SESSION_WRITE_INTERVAL_S = 60.0

#: Fields persisted in the state JSON (everything the web UI can edit).
STATE_FIELDS = (
    "enabled",
    "url_on",
    "url_off",
    "bed_temp",
    "chamber_target",
    "chamber_factor",
    "material_markers",
)


class HeatingError(RuntimeError):
    """A heater command could not be executed (no URL / HTTP failure)."""


async def _default_request(url: str) -> None:
    """Perform the heater's ON/OFF call: a plain GET (Shelly Gen1/Gen2 both
    expose switch URLs that answer GET). Fails on network errors and
    non-2xx answers; the caller records them for the backoff."""
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        if resp.status_code >= 400:
            raise HeatingError(f"HTTP {resp.status_code} von {url}")


@dataclasses.dataclass
class HeatingConfig:
    """Mutable settings — the web UI writes them live and persists them."""

    enabled: bool = False
    url_on: str = ""
    url_off: str = ""
    bed_temp: float = 90.0
    # Fallback chamber target when the slicer passes none (TempTargetBox 0).
    chamber_target: float = 42.5
    # Full swing around the target: the heater switches off at
    # target + factor/2 and on at target - factor/2 (factor 5 at 45° =
    # 42.5..47.5).
    chamber_factor: float = 5.0
    material_markers: list[str] = dataclasses.field(default_factory=list)
    # Where the UI-persisted settings live (wins over .conf/CLI defaults).
    state_path: Path = Path("data/heating.json")
    # Where the armed session's graph data (samples + heater phases) lives —
    # served via /api/heating/history, survives service restarts.
    session_path: Path = Path("data/heating-session.json")

    def validate(self) -> None:
        """Raise ValueError on settings the hysteresis cannot honor."""
        if not 0 <= self.chamber_target <= 60:
            raise ValueError(f"chamber_target {self.chamber_target}°C out of range 0..60")
        if not 1 <= self.chamber_factor <= 30:
            raise ValueError(
                f"chamber_factor {self.chamber_factor} out of range 1..30 "
                "(it is the full swing around the target)"
            )
        if not 0 <= self.bed_temp <= 110:
            raise ValueError(f"bed_temp {self.bed_temp}°C out of range 0..110")
        for name in ("url_on", "url_off"):
            url = getattr(self, name)
            if url and not url.startswith(("http://", "https://")):
                raise ValueError(f"{name} must be an http(s) URL")


def load_state(cfg: HeatingConfig) -> None:
    """Overlay the persisted UI settings onto ``cfg`` (JSON wins).

    Migrates the pre-0.17.9 min/max pair to the target/factor model:
    target = (min + max) / 2, factor = max - min.
    """
    try:
        raw = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as err:
        log.warning("heating: state file %s unreadable: %r", cfg.state_path, err)
        return
    if not isinstance(raw, dict):
        return
    if "chamber_min" in raw and "chamber_max" in raw:
        with contextlib.suppress(TypeError, ValueError):
            lo = float(raw["chamber_min"])
            hi = float(raw["chamber_max"])
            if hi > lo:
                cfg.chamber_target = round((lo + hi) / 2, 2)
                cfg.chamber_factor = round(hi - lo, 2)
                log.info(
                    "heating: migrated chamber_min/max (%s/%s) to target %s ± factor/2",
                    lo,
                    hi,
                    cfg.chamber_target,
                )
    if raw.get("chamber_target"):
        with contextlib.suppress(TypeError, ValueError):
            cfg.chamber_target = float(raw["chamber_target"])
    for field in ("enabled", "url_on", "url_off", "bed_temp", "chamber_factor"):
        if field not in raw:
            continue
        value = raw[field]
        if field == "enabled":
            cfg.enabled = bool(value)
        elif field in ("bed_temp", "chamber_factor"):
            with contextlib.suppress(TypeError, ValueError):
                setattr(cfg, field, float(value))
        elif field == "url_on":
            cfg.url_on = str(value)
        elif field == "url_off":
            cfg.url_off = str(value)
    if "material_markers" in raw:
        cfg.material_markers = _coerce_markers(raw["material_markers"])
    log.info("heating: settings loaded from %s", cfg.state_path)


def save_state(cfg: HeatingConfig) -> None:
    """Persist the current settings (atomic-ish: temp file + rename)."""
    payload = {field: getattr(cfg, field) for field in STATE_FIELDS}
    path = cfg.state_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as err:
        log.error("heating: state file %s not writable: %r", path, err)


def _coerce_markers(value: Any) -> list[str]:
    """Accept a list or a comma-separated string, return clean markers."""
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        parts = [str(v) for v in value]
    else:
        parts = []
    return [p.strip() for p in parts if p.strip()]


class HeatingAutomation:
    """Drive the chamber heater from the printer's status stream.

    The target is resolved per push and the slicer wins: a printer chamber
    target above zero (``TempTargetBox``) is the setpoint; when it is zero,
    an armed session uses the configured fallback target. Arming happens at
    print start (or attach mid-print / feature enabled mid-print) when the
    slicer target is set, the bed target exceeds ``bed_temp``, or the
    filename contains a material marker — latched for the whole print. The
    heater holds the chamber inside ±factor/2 around the target (ON below,
    OFF above, in between keep the current state); at print end it goes
    off unconditionally. Commands dedupe through the last *successfully*
    commanded state and back off exponentially (2 s → 60 s) while the
    Shelly is unreachable.
    """

    def __init__(
        self,
        cfg: HeatingConfig,
        *,
        get_printer: Callable[[], Any],
        request_fn: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.cfg = cfg
        self._get_printer = get_printer
        self._request_fn = request_fn or _default_request
        self._closing = False
        self._status_task: asyncio.Task[None] | None = None
        self._cmds: set[asyncio.Task[None]] = set()
        self._active = False  # a job occupies the printer
        self._armed = False  # session: this print wants chamber heat
        self._armed_reason = ""
        self._armed_at: float | None = None  # epoch s of the last session arm
        self._last_target: float | None = None  # effective target of the last push
        # Armed-session graph data, served via /api/heating/history and
        # persisted to session_path so reloads, other devices and service
        # restarts all see the same complete graph.
        self._session_open = False
        self._session_samples: list[list[float]] = []  # [[epoch s, °C], ...]
        self._session_phases: list[list[float | None]] = []  # [[ON s, OFF s | None], ...]
        self._last_sample_s = 0.0
        self._last_session_write = 0.0
        self._sample_interval = SAMPLE_INTERVAL_S
        self._load_session_file()
        # Set when an armed session ends while the heater is not confirmed
        # off: the OFF command retries every push (backoff-paced) until the
        # call lands — a failed end-OFF must never leave the heater running.
        self._release_pending = False
        # Light state we last observed (report is truth) or commanded
        # (assumed success). ``None`` = never seen; the first push or
        # command pins it, and every deviation from the temperature target
        # fires exactly one correction.
        self._applied: bool | None = None
        self._chamber: float | None = None
        self._bed_target: float | None = None
        self._retry_not_before = 0.0
        self._retry_backoff = RETRY_BACKOFF_START
        self._last_error = ""
        self._last_action = ""
        load_state(cfg)

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._status_task = asyncio.create_task(
            self._status_loop(), name="pycentauri-heating-status"
        )

    async def stop(self) -> None:
        self._closing = True
        if self._status_task is not None and not self._status_task.done():
            self._status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._status_task
        for task in list(self._cmds):
            task.cancel()
        if self._cmds:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(*self._cmds)

    # --- settings (web UI) --------------------------------------------------

    def apply_settings(self, **changes: Any) -> dict[str, Any]:
        """Validate + apply UI-provided settings, persist them, return state.

        Only the provided keys change; a ValueError (bad ranges/URLs)
        leaves the configuration untouched so the API can answer 400.
        """
        field_names = {f.name for f in dataclasses.fields(HeatingConfig)}
        staged: dict[str, Any] = {}
        for key, value in changes.items():
            if key not in field_names or key == "state_path":
                continue
            if key == "material_markers":
                staged[key] = _coerce_markers(value)
            elif value is None:
                continue
            else:
                staged[key] = value
        # Validate against a candidate copy so a bad value never lands.
        candidate = dataclasses.replace(
            self.cfg,
            **{
                **{f: getattr(self.cfg, f) for f in STATE_FIELDS},
                **staged,
            },
        )
        candidate.validate()
        for key, value in staged.items():
            setattr(self.cfg, key, value)
        save_state(self.cfg)
        log.info(
            "heating: settings updated (enabled=%s target=%s factor=%s markers=%s)",
            self.cfg.enabled,
            self.cfg.chamber_target,
            self.cfg.chamber_factor,
            self.cfg.material_markers,
        )
        return self.state()

    def state(self) -> dict[str, Any]:
        """Everything the /api/heating endpoint reports."""
        return {
            "enabled": self.cfg.enabled,
            "heater_on": self._applied,
            "armed": self._armed,
            "armed_reason": self._armed_reason,
            "armed_since": self._armed_at,
            "target": self._last_target,
            "chamber_temp": self._chamber,
            "bed_target": self._bed_target,
            "last_error": self._last_error,
            "last_action": self._last_action,
            "settings": {
                "url_on": self.cfg.url_on,
                "url_off": self.cfg.url_off,
                "bed_temp": self.cfg.bed_temp,
                "chamber_target": self.cfg.chamber_target,
                "chamber_factor": self.cfg.chamber_factor,
                "material_markers": self.cfg.material_markers,
            },
            "history": "/api/heating/history",
        }

    def session_data(self) -> dict[str, Any]:
        """The armed session's graph data (GET /api/heating/history)."""
        return {
            "armed_since": self._armed_at,
            "open": self._session_open,
            "samples": self._session_samples,
            "phases": self._session_phases,
        }

    # --- status watch -------------------------------------------------------

    async def _status_loop(self) -> None:
        """Consume ``printer.watch()`` forever; reconnect with backoff."""
        backoff = 1.0
        while not self._closing:
            try:
                printer = self._get_printer()
                async for st in printer.watch():
                    self._on_status(st)
                if not self._closing:
                    log.debug("heating: status stream ended, retrying")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not self._closing:
                    log.debug("heating: status watch unavailable: %r", err)
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _on_status(self, st: Status) -> None:
        # Master switch off: release control (heater off — but only if we
        # ever successfully commanded it; a heater we never touched stays
        # untouched).
        if not self.cfg.enabled:
            self._armed = False
            self._armed_reason = ""
            if self._session_open:
                self._end_session()
            if self._applied is True:
                self._fire(False, "chamber heating disabled")
            return

        code = st.print_status
        active = code is not None and code not in INACTIVE_CODES
        was_active = self._active
        self._active = active
        self._chamber = st.temp_chamber
        self._bed_target = st.temp_bed_target

        was_armed = self._armed
        armed_now = False
        if active:
            # Armed state is re-checked on every push: the slicer can set a
            # chamber target mid-print, which arms the session by itself.
            if not was_armed:
                armed, target, reason = self._evaluate_activation(st)
                if armed:
                    self._armed = True
                    self._armed_reason = reason
                    armed_now = True
                    self._start_session()
                    chamber = st.temp_chamber
                    if chamber is None or chamber < target + self.cfg.chamber_factor / 2:
                        self._set(True, f"print start: {reason}")
        elif not active and was_active:
            self._armed = False
            self._armed_reason = ""
            if was_armed:
                self._end_session()
            if was_armed and self._applied is not False:
                self._release_pending = True

        # Graph samples: one every sample interval while a session is armed.
        if self._armed:
            self._collect_sample(st, time.time())

        # Session-end release: state-driven so a failed OFF command retries
        # on every push (backoff-paced) until the heater is confirmed off.
        if self._release_pending:
            if self._applied is False:
                self._release_pending = False
            else:
                self._fire(False, "print finished")

        # Hysteresis while the session is armed: the target follows the
        # slicer's chamber target when set, else the configured fallback.
        # Skipped on the arming push itself — the arm already set the
        # initial state (an extra dispatch there would fire the same
        # command twice).
        self._last_target = None
        if self._armed and not armed_now:
            chamber = st.temp_chamber
            if chamber is not None:
                target = self._effective_target(st)
                self._last_target = target
                half = self.cfg.chamber_factor / 2
                if chamber > target + half:
                    self._set(
                        False,
                        f"chamber {chamber:.1f}° > target {target:.1f}° + {half:.1f}°",
                    )
                elif chamber < target - half:
                    self._set(
                        True,
                        f"chamber {chamber:.1f}° < target {target:.1f}° - {half:.1f}°",
                    )

    def _effective_target(self, st: Status) -> float:
        """The chamber target for this push: the slicer's chamber target
        (``TempTargetBox``) when it is set above zero, else the configured
        fallback target."""
        st_target = st.temp_chamber_target
        if st_target is not None and st_target > 0:
            return st_target
        return self.cfg.chamber_target

    # --- session (graph data) ------------------------------------------------

    def _load_session_file(self) -> None:
        # Restore a persisted session when it is still open - a service
        # restart mid-print must keep the graph complete. A closed session
        # (previous print ended cleanly) is dropped on the next arm.
        try:
            raw = json.loads(self.cfg.session_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return
        if not isinstance(raw, dict) or not raw.get("open"):
            return
        armed_since = raw.get("armed_since")
        if not isinstance(armed_since, (int, float)) or time.time() - armed_since > 12 * 3600:
            return  # stale beyond any plausible print - start fresh
        self._session_samples = [[float(t), float(v)] for t, v in raw.get("samples", [])]
        self._session_phases = [
            [float(a), float(b) if b is not None else None] for a, b in raw.get("phases", [])
        ]
        self._armed_at = float(armed_since)
        self._session_open = True
        self._last_sample_s = time.time()
        self._last_session_write = time.time()
        log.info(
            "heating: session resumed from %s (%d samples, %d phases)",
            self.cfg.session_path,
            len(self._session_samples),
            len(self._session_phases),
        )

    def _persist_session(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_session_write < SESSION_WRITE_INTERVAL_S:
            return
        self._last_session_write = now
        payload = {
            "armed_since": self._armed_at,
            "open": self._session_open,
            "samples": self._session_samples,
            "phases": self._session_phases,
        }
        try:
            path = self.cfg.session_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError as err:
            log.error("heating: session file %s not writable: %r", path, err)

    def _start_session(self) -> None:
        # Arm a new graph session - resuming the persisted one when it is
        # still open (service restart mid-print), fresh otherwise.
        self._load_session_file()
        if not self._session_open:
            self._session_samples = []
            self._session_phases = []
            self._armed_at = time.time()
            self._session_open = True
            self._last_sample_s = 0.0
        self._persist_session(force=True)

    def _end_session(self) -> None:
        self._session_open = False
        self._persist_session(force=True)

    def _collect_sample(self, st: Status, now: float) -> None:
        if st.temp_chamber is None:
            return
        if now - self._last_sample_s < self._sample_interval:
            return
        self._last_sample_s = now
        self._session_samples.append([now, round(st.temp_chamber, 2)])
        if len(self._session_samples) > 20000:
            self._session_samples.pop(0)  # ~27.8 h at the 5 s cadence
        self._persist_session()

    def _evaluate_activation(self, st: Status) -> tuple[bool, float, str]:
        """Decide whether the current print wants chamber heat, and with
        which target. The slicer's chamber target wins outright (it arms
        the session even without a hot bed); otherwise the bed target above
        the threshold or a filename marker arms it with the configured
        fallback target."""
        st_target = st.temp_chamber_target
        if st_target is not None and st_target > 0:
            return True, st_target, f"slicer chamber target {st_target:.0f}°"
        bed_target = st.temp_bed_target
        if bed_target is not None and bed_target > self.cfg.bed_temp:
            return (
                True,
                self.cfg.chamber_target,
                f"bed target {bed_target:.0f}° > {self.cfg.bed_temp:.0f}°",
            )
        filename = st.filename or ""
        if filename:
            lowered = filename.lower()
            for marker in self.cfg.material_markers:
                needle = marker.strip().lower()
                if needle and needle in lowered:
                    return (
                        True,
                        self.cfg.chamber_target,
                        f'"{filename}" contains "{marker.strip()}"',
                    )
        return False, self.cfg.chamber_target, ""

    # --- command path -------------------------------------------------------

    def _set(self, on: bool, reason: str) -> None:
        """Command the heater when the believed state disagrees. The belief
        only updates once the Shelly call has succeeded, so a failed call
        is retried on the next push (paced by the backoff) until it lands.
        """
        if self._applied is on:
            return
        self._fire(on, reason)

    def _fire(self, on: bool, reason: str) -> None:
        loop = asyncio.get_running_loop()
        if loop.time() < self._retry_not_before:
            return  # backing off after failures — the next push retries
        task = asyncio.create_task(self._send(on, reason), name=f"pycentauri-heating-{on}")
        self._cmds.add(task)
        task.add_done_callback(self._cmds.discard)

    async def _send(self, on: bool, reason: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            url = self.cfg.url_on if on else self.cfg.url_off
            if not url:
                raise HeatingError("no Shelly URL configured")
            await self._request_fn(url)
            self._applied = on
            now = time.time()
            if on:
                self._session_phases.append([now, None])
            else:
                for ph in reversed(self._session_phases):
                    if ph[1] is None:
                        ph[1] = now
                        break
            self._persist_session(force=True)
            self._retry_backoff = RETRY_BACKOFF_START
            self._retry_not_before = 0.0
            self._last_error = ""
            self._last_action = f"{'ON' if on else 'OFF'} — {reason}"
            log.info("heating: heater %s — %s", "ON" if on else "OFF", reason)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._retry_not_before = loop.time() + self._retry_backoff
            self._retry_backoff = min(self._retry_backoff * 2, RETRY_BACKOFF_MAX)
            self._last_error = f"{'ON' if on else 'OFF'} failed: {err}"
            log.warning(
                "heating: Shelly call (%s) failed: %r — retrying after %.0fs backoff",
                "ON" if on else "OFF",
                err,
                self._retry_backoff,
            )


__all__ = ["HeatingAutomation", "HeatingConfig", "HeatingError", "load_state", "save_state"]
