"""Chamber-heating automation: a Shelly-controlled heater for the print room.

Target resolution per status push, and the slicer wins: when the printer
reports a chamber target (``TempTargetBox``) above zero, the heater
regulates toward it. Otherwise — a print armed via bed target above the
threshold or a filename marker — the configured fallback target applies.

Around the target the heater holds a switching band (the
``chamber_hysteresis``): ON below ``target - hysteresis/2``, OFF above ``target + hysteresis/2 -
overshoot``. The **overshoot is learned**: after every heater-OFF the
temperature keeps rising for a while (thermal inertia); the automation
tracks the peak of each phase and folds it into an exponential average
that lowers the OFF point, so the peak lands inside the band instead of
overshooting it. The learned value starts at 0, persists in the settings
file and is editable in the web UI.

A safety watchdog guards every heating phase: when the heater is ON but
the chamber temperature does not rise by ``SAFETY_RISE_C`` within
``SAFETY_WINDOW_S``, something is wrong (heater not switching, Shelly
offline, door/lid open) — the heater goes off for the rest of the
session and the panel shows a red SAFETY STOP warning. At print end (or
when the feature is toggled off/on) the stop clears and the next print
tries again.

At print end the heater goes off unconditionally and the session
disarms; a paused print keeps being controlled; attaching mid-print
re-evaluates the activation from the first status push. Commands dedupe
through the last *successfully* commanded state and back off
exponentially (2 s → 60 s) while the Shelly is unreachable.
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

#: Safety watchdog: a heating phase must raise the chamber temperature by
#: at least SAFETY_RISE_C within SAFETY_WINDOW_S — otherwise the heater is
#: faulty, miswired, the Shelly offline, or a door/lid is open (fire risk).
SAFETY_WINDOW_S = 300.0
SAFETY_RISE_C = 1.0

#: Overshoot learning: EMA weight of the new peak measurement, the
#: drop-confirmed threshold (the temperature has fallen this much below
#: the observed peak → the peak is in), and the measurement cap (a heating
#: phase whose off-point is not passed within 15 min banks what it has).
OVERSHOOT_EMA_ALPHA = 0.4
OVERSHOOT_PEAK_FALL_C = 0.3
OVERSHOOT_MEASURE_MAX_S = 900.0

#: Fields persisted in the settings JSON (everything the web UI can edit).
STATE_FIELDS = (
    "enabled",
    "url_on",
    "url_off",
    "bed_temp",
    "chamber_target",
    "chamber_hysteresis",
    "chamber_overshoot",
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
    # Hysteresis band around the target (the ON point sits hysteresis/2 below it).
    chamber_hysteresis: float = 5.0
    # Learned overshoot: how far the chamber rises past the OFF point after
    # the heater switches off (thermal inertia). The OFF point is lowered
    # by this value; starts at 0 and is folded in after every heating
    # phase (EMA), clamped to the hysteresis. Editable in the web UI.
    chamber_overshoot: float = 0.0
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
        if not 1 <= self.chamber_hysteresis <= 30:
            raise ValueError(
                f"chamber_hysteresis {self.chamber_hysteresis} out of range 1..30 "
                "(it is the hysteresis band around the target)"
            )
        if not 0 <= self.chamber_overshoot <= 60:
            raise ValueError(f"chamber_overshoot {self.chamber_overshoot}°C out of range 0..60")
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
                cfg.chamber_hysteresis = round(hi - lo, 2)
                log.info(
                    "heating: migrated chamber_min/max (%s/%s) to target %s ± hysteresis/2",
                    lo,
                    hi,
                    cfg.chamber_target,
                )
    if raw.get("chamber_target"):
        with contextlib.suppress(TypeError, ValueError):
            cfg.chamber_target = float(raw["chamber_target"])
    if raw.get("chamber_factor") is not None and "chamber_hysteresis" not in raw:
        # pre-0.18.7 key rename: same value, new name
        with contextlib.suppress(TypeError, ValueError):
            cfg.chamber_hysteresis = float(raw["chamber_factor"])
    for field in (
        "enabled",
        "url_on",
        "url_off",
        "bed_temp",
        "chamber_hysteresis",
        "chamber_overshoot",
    ):
        if field not in raw:
            continue
        value = raw[field]
        if field == "enabled":
            cfg.enabled = bool(value)
        elif field in ("bed_temp", "chamber_hysteresis", "chamber_overshoot"):
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

    Target resolution per push, slicer first (see module docstring); the
    heater holds the chamber inside ±hysteresis/2 around the target, with the
    learned overshoot lowering the OFF point so the thermal inertia peaks
    inside the band. At print end the heater goes off unconditionally.
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
        self._session_open = False
        self._session_samples: list[list[float]] = []  # [[epoch s, °C], ...]
        self._session_phases: list[list[float | None]] = []  # [[ON s, OFF s | None], ...]
        self._last_sample_s = 0.0
        self._last_session_write = 0.0
        self._sample_interval = SAMPLE_INTERVAL_S
        # Set when an armed session ends while the heater is not confirmed
        # off: the OFF command retries every push (backoff-paced) until the
        # call lands - a failed end-OFF must never leave the heater running.
        self._release_pending = False
        self._applied: bool | None = None  # last *successfully* commanded state
        self._chamber: float | None = None
        self._bed_target: float | None = None
        self._retry_not_before = 0.0
        self._retry_backoff = RETRY_BACKOFF_START
        self._last_error = ""
        self._last_action = ""
        # Safety watchdog / stop (see SAFETY_WINDOW_S). Instance copies so
        # tests can shrink the window without monkeypatching.
        self._safety_stop = False
        self._safety_message = ""
        self._safety_window = SAFETY_WINDOW_S
        self._safety_rise = SAFETY_RISE_C
        self._watch_started: float | None = None  # epoch s when the ON phase began
        self._watch_baseline: float | None = None  # chamber reading the rise is measured from
        # Overshoot learning (see chamber_overshoot): peak tracking after an
        # OFF command. {"off": off-point °C, "peak": °C, "t": epoch s}
        self._learn: dict[str, float] | None = None
        load_state(cfg)
        self._load_session_file()

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
            if key not in field_names or key == "state_path" or key == "session_path":
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
            "heating: settings updated (enabled=%s target=%s factor=%s overshoot=%s markers=%s)",
            self.cfg.enabled,
            self.cfg.chamber_target,
            self.cfg.chamber_hysteresis,
            self.cfg.chamber_overshoot,
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
            "safety_stop": self._safety_stop,
            "safety_message": self._safety_message,
            "last_error": self._last_error,
            "last_action": self._last_action,
            "settings": {
                "url_on": self.cfg.url_on,
                "url_off": self.cfg.url_off,
                "bed_temp": self.cfg.bed_temp,
                "chamber_target": self.cfg.chamber_target,
                "chamber_hysteresis": self.cfg.chamber_hysteresis,
                "chamber_overshoot": self.cfg.chamber_overshoot,
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
        now = time.time()

        # Master switch off: release control (heater off — but only if we
        # ever successfully commanded it; a heater we never touched stays
        # untouched). A manual off→on toggle clears a safety stop: the user
        # explicitly wants to try again.
        if not self.cfg.enabled:
            self._armed = False
            self._armed_reason = ""
            self._armed_at = None
            if self._session_open:
                self._end_session()
            self._safety_stop = False
            self._safety_message = ""
            self._watch_started = None
            self._watch_baseline = None
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
        if active and not self._safety_stop:
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
                    if chamber is None or chamber < target + self.cfg.chamber_hysteresis / 2:
                        self._set(True, f"print start: {reason}")
        elif not active and was_active:
            self._armed = False
            self._armed_reason = ""
            if was_armed:
                self._end_session()
                self._learn = None
                self._watch_started = None
                self._watch_baseline = None
            if self._safety_stop:
                self._safety_stop = False
                self._safety_message = ""
            if was_armed and self._applied is not False:
                self._release_pending = True

        # Session-end release: state-driven so a failed OFF command retries
        # on every push (backoff-paced) until the heater is confirmed off.
        if self._release_pending:
            if self._applied is False:
                self._release_pending = False
            else:
                self._fire(False, "print finished")

        # Graph samples: one every sample interval while a session is armed.
        if self._armed:
            self._collect_sample(st, now)

        if self._armed and not self._safety_stop:
            chamber = st.temp_chamber
            if chamber is not None:
                target = self._effective_target(st)
                self._last_target = target
                half = self.cfg.chamber_hysteresis / 2
                on_point = target - half
                off_point = max(on_point, target + half - self.cfg.chamber_overshoot)

                # Hysteresis (skipped on the arming push — the arm already
                # set the initial state). Learning banks the peak before an
                # early re-heat.
                if not armed_now:
                    if chamber > off_point:
                        # Learn-start only when the OFF command actually
                        # dispatches — a re-start on every push above the
                        # off-point would reset the peak tracker.
                        if self._applied is not False:
                            self._learn_start(off_point, chamber)
                        self._set(False, f"chamber {chamber:.1f}° > off {off_point:.1f}°")
                    elif chamber < on_point:
                        self._learn_finish()
                        self._set(True, f"chamber {chamber:.1f}° < on {on_point:.1f}°")

                self._learn_track(chamber, now)

            # Safety watchdog: an ON phase must lift the chamber temperature
            # within the window — otherwise heater/Shelly/door trouble. The
            # trip stops heating for the rest of the session and surfaces a
            # fat red warning in the panel.
            if self._applied is True:
                chamber_now = st.temp_chamber
                if self._watch_started is None:
                    self._watch_started = now
                    self._watch_baseline = chamber_now  # may be None until a reading arrives
                elif chamber_now is None:
                    if now - self._watch_started >= self._safety_window:
                        self._safety_trip("no chamber temperature readings while heating")
                elif self._watch_baseline is None:
                    self._watch_started = now
                    self._watch_baseline = chamber_now  # first reading starts the clock
                else:
                    risen = chamber_now - self._watch_baseline
                    if (
                        now - self._watch_started >= self._safety_window
                        and risen < self._safety_rise
                    ):
                        self._safety_trip(
                            f"no temperature rise ({risen:+.1f}° in "
                            f"{self._safety_window / 60:.0f} min)"
                        )
            else:
                self._watch_started = None
                self._watch_baseline = None

            if self._safety_stop and self._applied is not False:
                self._fire(False, f"safety stop: {self._safety_message}")
        else:
            self._watch_started = None
            self._watch_baseline = None

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

    # --- overshoot learning ---------------------------------------------------

    def _learn_start(self, off_point: float, chamber: float) -> None:
        self._learn = {"off": off_point, "peak": chamber, "t": time.time()}

    def _learn_track(self, chamber: float, now: float) -> None:
        """Track the post-OFF peak; a confirmed fall below it (or the
        measurement cap) banks the overshoot sample into the EMA."""
        learn = self._learn
        if learn is None:
            return
        learn["peak"] = max(learn["peak"], chamber)
        if (
            chamber <= learn["peak"] - OVERSHOOT_PEAK_FALL_C
            or now - learn["t"] > OVERSHOOT_MEASURE_MAX_S
        ):
            self._learn_finish()

    def _learn_finish(self) -> None:
        learn = self._learn
        if learn is None:
            return
        sample = max(0.0, learn["peak"] - learn["off"])
        old = self.cfg.chamber_overshoot
        new = round(old * (1 - OVERSHOOT_EMA_ALPHA) + sample * OVERSHOOT_EMA_ALPHA, 2)
        new = min(new, self.cfg.chamber_hysteresis)  # OFF point stays ≥ ON point
        self.cfg.chamber_overshoot = new
        self._learn = None
        save_state(self.cfg)
        log.info(
            "heating: overshoot learned — peak %.1f° over off-point %.1f° → "
            "sample %.1f°, EMA %.1f° → %.1f°",
            learn["peak"],
            learn["off"],
            sample,
            old,
            new,
        )
        self._persist_session(force=True)

    # --- safety watchdog ------------------------------------------------------

    def _safety_trip(self, message: str) -> None:
        """Stop heating for the rest of the session with a fat red warning."""
        self._safety_stop = True
        self._safety_message = message
        self._watch_started = None
        self._watch_baseline = None
        self._learn = None
        log.warning("heating: SAFETY STOP — %s", message)

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
