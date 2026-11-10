"""Chamber-light automation: light on at print start, off after cool-down.

Replaces the Home-Assistant automation pair that used to do exactly this
job: the chamber light comes on when a print starts, and goes out once the
nozzle has cooled below a threshold (default 50 °C) while no print is
active anymore. Lives in the server daemon
(``centauri server --light-automation``) and follows the same status stream
as the spaghetti detection — ``printer.watch()`` with backoff-retry across
reconnects.
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

    #: Nozzle temperature below which — with no active print — the light
    #: goes out ("wenn die Düse auf X° abgekühlt ist").
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

    Rules (the Home-Assistant pair this replaces):

    * a print starts — light ON. Also fires when attaching while a print is
      already running (service restart mid-print);
    * the nozzle cools below ``cool_temp`` while no print is active — light
      OFF. A print that ends with the nozzle already cold turns the light
      off immediately (no crossing will come);
    * attaching while idle, cold and lit *and* the automation itself was
      the last thing seen switching the light on — light OFF once, so a
      service restart or outage during cool-down cannot leave the light on
      forever. A light the *user* switched on is never fought here.

    Commands fire only on these transitions and only when the reported
    light state disagrees with the rule's outcome — a manually toggled
    light at idle is never fought, and redundant commands are skipped.
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
        self._prev_temp: float | None = None  # nozzle temp of the previous push
        self._first_push = True  # next status is the first of a (re)connect
        # True while the last light switch this automation knows of was its
        # own ON — gates rule 4 so a user's manual light survives reconnects.
        self._our_on = False

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
        # An observed "light off" is the newest truth: the automation can no
        # longer claim responsibility for the current light state.
        if light_on is False:
            self._our_on = False

        # 1) Print start — also on the first push when attaching mid-print.
        #    (Already lit → nothing to send, and the light stays the user's.)
        if active and (first or not was_active) and light_on is not True:
            self._fire(True, "Druck gestartet")

        # 2) Job ended with the nozzle already cold — light off right away.
        if (
            not active
            and was_active
            and temp is not None
            and temp < self.cfg.cool_temp
            and light_on is not False
        ):
            self._fire(False, f"Druck beendet, Düse schon kalt ({temp:.1f} °C)")

        # 3) The usual path: the nozzle crosses below the threshold while
        #    idle. Fires once per crossing; suppressed while any job is
        #    active (a long pause may cool the nozzle, but the light stays).
        if (
            not active
            and not first
            and temp is not None
            and temp < self.cfg.cool_temp
            and self._prev_temp is not None
            and self._prev_temp >= self.cfg.cool_temp
            and light_on is not False
        ):
            self._fire(False, f"Düse abgekühlt ({temp:.1f} °C < {self.cfg.cool_temp:.0f} °C)")

        # 4) Attaching while idle, cold and lit: a print ended while the
        #    watcher was down (service restart, outage) and the light is one
        #    the automation itself switched on — clear it. Only fires while
        #    _our_on, so a manually lit light survives reconnects.
        if (
            first
            and not active
            and self._our_on
            and temp is not None
            and temp < self.cfg.cool_temp
            and light_on
        ):
            self._fire(False, f"neu verbunden: kalt und leer ({temp:.1f} °C)")

        self._prev_temp = temp

    # --- command path -------------------------------------------------------

    def _fire(self, on: bool, reason: str) -> None:
        if not self._control_allowed:
            log.warning(
                "light-automation: Licht %s fällig (%s), aber --enable-control fehlt",
                "AN" if on else "AUS",
                reason,
            )
            return
        # Our own command is the newest known truth about the light.
        self._our_on = on
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
