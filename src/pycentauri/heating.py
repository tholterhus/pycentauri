"""Chamber-heating automation: a Shelly-controlled heater for the print room.

The activation is decided at print start — the bed target above a threshold
(default 90 °C) OR the filename containing a configured material marker
("ABS", "ASA", ... — case-insensitive substring) arms the session for the
whole print, latched (a slicer lowering the bed target mid-print does not
disarm it). While armed, the printer's own chamber sensor (TempOfBox)
drives an on/off hysteresis between two thresholds via two HTTP GET
endpoints (the Shelly's "on" and "off" URLs). At print end the heater goes
off unconditionally and the session disarms. A paused print keeps being
controlled; attaching mid-print (service restart) re-evaluates the
activation from the first status push.

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

#: Fields persisted in the state JSON (everything the web UI can edit).
STATE_FIELDS = (
    "enabled",
    "url_on",
    "url_off",
    "bed_temp",
    "chamber_min",
    "chamber_max",
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
    chamber_min: float = 40.0
    chamber_max: float = 45.0
    material_markers: list[str] = dataclasses.field(default_factory=list)
    # Where the UI-persisted settings live (wins over .conf/CLI defaults).
    state_path: Path = Path("data/heating.json")

    def validate(self) -> None:
        """Raise ValueError on settings the hysteresis cannot honor."""
        if self.chamber_min >= self.chamber_max:
            raise ValueError(
                f"chamber_min ({self.chamber_min}°C) must be below "
                f"chamber_max ({self.chamber_max}°C)"
            )
        if not 0 <= self.bed_temp <= 110:
            raise ValueError(f"bed_temp {self.bed_temp}°C out of range 0..110")
        if not 0 <= self.chamber_min <= 60 or not 0 <= self.chamber_max <= 60:
            raise ValueError("chamber thresholds must be within 0..60 °C")
        for name in ("url_on", "url_off"):
            url = getattr(self, name)
            if url and not url.startswith(("http://", "https://")):
                raise ValueError(f"{name} must be an http(s) URL")


def load_state(cfg: HeatingConfig) -> None:
    """Overlay the persisted UI settings onto ``cfg`` (JSON wins)."""
    try:
        raw = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as err:
        log.warning("heating: state file %s unlesbar: %r", cfg.state_path, err)
        return
    if not isinstance(raw, dict):
        return
    for field in STATE_FIELDS:
        if field not in raw:
            continue
        value = raw[field]
        if field == "material_markers":
            cfg.material_markers = _coerce_markers(value)
        elif field == "enabled":
            cfg.enabled = bool(value)
        elif field == "bed_temp" or field == "chamber_min" or field == "chamber_max":
            with contextlib.suppress(TypeError, ValueError):
                setattr(cfg, field, float(value))
        elif field == "url_on":
            cfg.url_on = str(value)
        elif field == "url_off":
            cfg.url_off = str(value)
    log.info("heating: Einstellungen aus %s geladen", cfg.state_path)


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
        log.error("heating: state file %s nicht schreibbar: %r", path, err)


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

    One status stream (``printer.watch()`` with backoff-retry across
    reconnects, like the other automations) feeds three rules:

    * print start (or attach mid-print / feature enabled mid-print): arm
      when bed target > ``bed_temp`` or the filename contains a marker —
      and switch the heater ON unless the chamber already sits above the
      maximum;
    * while armed: hysteresis on the chamber sensor — below ``chamber_min``
      ON, above ``chamber_max`` OFF, in between keep the current state;
    * print end: heater OFF, session disarmed.

    Commands dedupe through ``_applied`` — the last state we *successfully*
    commanded (the Shelly relay state is not read back): a failed call
    leaves the belief unchanged, so the next status push retries it,
    paced by the exponential backoff until the call lands.
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
        # Set when an armed session ends while the heater is not confirmed
        # off: the OFF command retries every push (backoff-paced) until the
        # call lands — a failed end-OFF must never leave the heater running.
        self._release_pending = False
        self._applied: bool | None = None  # heater state we last commanded
        self._chamber: float | None = None
        self._bed_target: float | None = None
        self._first_push = True
        self._was_enabled = cfg.enabled
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
        staged = {
            key: _coerce_markers(value) if key == "material_markers" else value
            for key, value in changes.items()
            if key in field_names and key != "state_path" and value is not None
        }
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
            "heating: Einstellungen aktualisiert (enabled=%s markers=%s)",
            self.cfg.enabled,
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
            "chamber_temp": self._chamber,
            "bed_target": self._bed_target,
            "last_error": self._last_error,
            "last_action": self._last_action,
            "settings": {
                "url_on": self.cfg.url_on,
                "url_off": self.cfg.url_off,
                "bed_temp": self.cfg.bed_temp,
                "chamber_min": self.cfg.chamber_min,
                "chamber_max": self.cfg.chamber_max,
                "material_markers": self.cfg.material_markers,
            },
        }

    # --- status watch -------------------------------------------------------

    async def _status_loop(self) -> None:
        """Consume ``printer.watch()`` forever; reconnect with backoff."""
        backoff = 1.0
        while not self._closing:
            try:
                printer = self._get_printer()
                self._first_push = True
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
            self._release_pending = False
            if self._applied is True:
                self._fire(False, "Innenraumheizung deaktiviert")
            self._was_enabled = False
            return

        first = self._first_push or not self._was_enabled
        self._first_push = False
        self._was_enabled = True

        code = st.print_status
        active = code is not None and code not in INACTIVE_CODES
        was_active = self._active
        self._active = active
        self._chamber = st.temp_chamber
        self._bed_target = st.temp_bed_target

        was_armed = self._armed
        armed_now = False
        if active and (first or not was_active):
            # Print start, attach mid-print, or the feature just got enabled
            # during a running print — all decide the session now (latched).
            self._armed, self._armed_reason = self._evaluate_activation(st)
            if self._armed and not was_armed:
                armed_now = True
                chamber = st.temp_chamber
                if chamber is None or chamber < self.cfg.chamber_max:
                    self._set(True, f"Druckstart: {self._armed_reason}")
        elif not active and was_active:
            was_armed = self._armed
            self._armed = False
            self._armed_reason = ""
            if was_armed and self._applied is not False:
                self._release_pending = True

        # Session-end release: state-driven so a failed OFF command retries
        # on every push (backoff-paced) until the heater is confirmed off.
        if self._release_pending:
            if self._applied is False:
                self._release_pending = False
            else:
                self._fire(False, "Druck beendet")

        # Hysteresis while the session is armed: below min ON, above max
        # OFF, in between keep the current state. Skipped on the arming
        # push itself — the arm already set the initial state (an extra
        # dispatch there would fire the same command twice).
        if self._armed and not armed_now:
            chamber = st.temp_chamber
            if chamber is not None:
                if chamber > self.cfg.chamber_max:
                    self._set(False, f"Kammer {chamber:.1f}° > {self.cfg.chamber_max:.0f}°")
                elif chamber < self.cfg.chamber_min:
                    self._set(True, f"Kammer {chamber:.1f}° < {self.cfg.chamber_min:.0f}°")

    def _evaluate_activation(self, st: Status) -> tuple[bool, str]:
        """Decide whether the current print wants chamber heat (latched)."""
        bed_target = st.temp_bed_target
        if bed_target is not None and bed_target > self.cfg.bed_temp:
            return True, f"Bett-Ziel {bed_target:.0f}° > {self.cfg.bed_temp:.0f}°"
        filename = st.filename or ""
        if filename:
            lowered = filename.lower()
            for marker in self.cfg.material_markers:
                needle = marker.strip().lower()
                if needle and needle in lowered:
                    return True, f'"{filename}" enthält "{marker.strip()}"'
        return False, ""

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
                raise HeatingError("keine Shelly-URL konfiguriert")
            await self._request_fn(url)
            self._applied = on
            self._retry_backoff = RETRY_BACKOFF_START
            self._retry_not_before = 0.0
            self._last_error = ""
            self._last_action = f"{'ON' if on else 'OFF'} — {reason}"
            log.info("heating: Heizung %s — %s", "AN" if on else "AUS", reason)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._retry_not_before = loop.time() + self._retry_backoff
            self._retry_backoff = min(self._retry_backoff * 2, RETRY_BACKOFF_MAX)
            self._last_error = f"{'ON' if on else 'OFF'} fehlgeschlagen: {err}"
            log.warning(
                "heating: Shelly-Aufruf (%s) fehlgeschlagen: %r — nächster "
                "Versuch nach %.0fs Backoff",
                "ON" if on else "OFF",
                err,
                self._retry_backoff,
            )


__all__ = ["HeatingAutomation", "HeatingConfig", "HeatingError", "load_state", "save_state"]
