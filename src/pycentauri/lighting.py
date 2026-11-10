"""Chamber-light automation: light on at print start, off when cold.

Replaces the Home-Assistant automation pair that used to do exactly this
job: the chamber light comes on when a print starts, and goes out whenever
the nozzle sits below a threshold (default 50 °C) — no exceptions. Lives in
the server daemon (``centauri server --light-automation``) and follows the
same status stream as the spaghetti detection — ``printer.watch()`` with
backoff-retry across reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pycentauri.models import Status

log = logging.getLogger(__name__)

#: ``print_status`` codes meaning "no job occupies the printer" — the same
#: set the detection pipeline and the UI treat as not busy (IDLE, STOPPED,
#: COMPLETED, ERROR). Everything else — FILE_CHECKING, HOMING, PAUSED,
#: PREHEATING, PRINTING, ... — counts as an active job.
INACTIVE_CODES = frozenset({0, 8, 9, 14})

RETRY_BACKOFF_START = 1.0
RETRY_BACKOFF_MAX = 30.0


@dataclass(frozen=True)
class LightConfig:
    """Tunables for :class:`LightAutomation` (CLI: ``--light-cool-temp``)."""

    #: Nozzle temperature below which the light goes out, no matter what
    #: ("Immer aus bei <50°").
    cool_temp: float = 50.0


def _light_is_on(st: Status) -> bool | None:
    """Reported chamber-light state from a status push (``None`` = unknown).

    ``LightStatus.SecondLight`` is 1/0 on both models (verified live on the
    CC1's status pushes; the CC2 maps its LED state onto the same field).
    """
    raw = st.light.get("SecondLight") if isinstance(st.light, dict) else None
    if isinstance(raw, (int, float)):
        return bool(int(raw))
    return None


class LightAutomation:
    """Drive the chamber light from the printer's status stream.

    Two rules, straight from the Home-Assistant pair this replaces:

    * a print starts — light ON. Also fires when attaching while a print is
      already running (service restart mid-print);
    * the nozzle sits below ``cool_temp`` — light OFF, **no exceptions**: a
      manually toggled light does not protect it, and a paused job does not
      either. One OFF command per cold episode (deduped until the light is
      seen off or the nozzle warms up).

    The rules only meet in the seconds after a cold start: while the nozzle
    has not yet passed the threshold, the cool-down rule waits for the
    print's heat-up — or for the job to end, which is how a print cancelled
    during file check / leveling (never heats at all) still goes dark.
    Attaching while idle, cold and lit (service restart during cool-down)
    goes dark immediately: there is no heat-up to wait for.
    """

    def __init__(
        self,
        cfg: LightConfig,
        *,
        get_printer: Callable[[], Any],
        control_allowed: bool = True,
    ) -> None:
        self.cfg = cfg
        self._get_printer = get_printer
        self._control_allowed = control_allowed
        self._closing = False
        self._status_task: asyncio.Task[None] | None = None
        self._cmds: set[asyncio.Task[None]] = set()
        self._active = False  # a job occupies the printer
        self._first_push = True  # next status is the first of a (re)connect
        # True between a cold print start and the nozzle first passing the
        # threshold (or the job ending) — the window in which the print-
        # start rule keeps the light on against the cool-down rule.
        self._awaiting_heat = False
        # An OFF command for the current cold episode has been sent; suppress
        # further ones until the light is seen off or the nozzle warms up
        # (a status push can predate the command's effect on the printer).
        self._cold_off_sent = False

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._status_task = asyncio.create_task(self._status_loop(), name="pycentauri-light-status")

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

    # --- status watch -------------------------------------------------------

    async def _status_loop(self) -> None:
        """Consume ``printer.watch()`` forever; reconnect with backoff.

        Same shape as the detection pipeline's status loop: ``get_printer``
        may raise while the manager reconnects, and a watch stream dies with
        every printer disconnect — both just fall through to the backoff.
        """
        backoff = RETRY_BACKOFF_START
        while not self._closing:
            try:
                printer = self._get_printer()
                self._first_push = True  # attach rules re-arm per connection
                async for st in printer.watch():
                    self._on_status(st)
                if not self._closing:
                    log.debug("light-automation: status stream ended, retrying")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not self._closing:
                    log.debug("light-automation: status watch unavailable: %r", err)
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RETRY_BACKOFF_MAX)

    def _on_status(self, st: Status) -> None:
        """Apply the rules to one status push (see class docstring)."""
        code = st.print_status
        active = code is not None and code not in INACTIVE_CODES
        temp = st.temp_nozzle
        light_on = _light_is_on(st)
        was_active = self._active
        self._active = active
        first = self._first_push
        self._first_push = False

        # Rule 1 — print start (also when attaching mid-print): light on.
        # A start with the nozzle still below the threshold suspends the
        # cool-down rule until the heat-up passes it or the job ends.
        if active and (first or not was_active):
            self._awaiting_heat = temp is None or temp < self.cfg.cool_temp
            if light_on is not True:
                self._fire(True, "Druck gestartet")
        elif not active and was_active:
            # Job over (finished/stopped/errored) — the suspension lifts
            # even if the nozzle never heated (cancelled file check or
            # leveling): the invariant cleans the light up.
            self._awaiting_heat = False

        # Rule 2 — the invariant: cold means dark, no exceptions. Neither a
        # pause nor a manually toggled light protects it. ``_cold_off_sent``
        # only dedupes: one OFF per cold episode, because the status push
        # right after a command can predate its effect on the printer.
        if temp is not None and temp >= self.cfg.cool_temp:
            self._awaiting_heat = False
            self._cold_off_sent = False
        if light_on is False:
            self._cold_off_sent = False
        if (
            temp is not None
            and temp < self.cfg.cool_temp
            and not self._awaiting_heat
            and not self._cold_off_sent
            and light_on is not False
        ):
            self._fire(False, f"Düse kalt ({temp:.1f} °C < {self.cfg.cool_temp:.0f} °C)")
            self._cold_off_sent = True

    # --- command path -------------------------------------------------------

    def _fire(self, on: bool, reason: str) -> None:
        if not self._control_allowed:
            log.warning(
                "light-automation: Licht %s fällig (%s), aber --enable-control fehlt",
                "AN" if on else "AUS",
                reason,
            )
            return
        task = asyncio.create_task(self._send(on, reason), name=f"pycentauri-light-{on}")
        self._cmds.add(task)
        task.add_done_callback(self._cmds.discard)

    async def _send(self, on: bool, reason: str) -> None:
        try:
            printer = self._get_printer()
            await printer.set_light(on)
            log.info("light-automation: Licht %s — %s", "AN" if on else "AUS", reason)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.warning("light-automation: set_light(%s) fehlgeschlagen: %r", on, err)


__all__ = ["INACTIVE_CODES", "LightAutomation", "LightConfig"]
