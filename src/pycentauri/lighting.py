"""Chamber-light automation driven purely by the nozzle temperature.

Replaces the Home-Assistant automation pair that used to do exactly this
job. One input, one target state — the light is ON while the nozzle is
above a threshold (default 50 °C) and OFF while it is below; the print
status plays no role. Lives in the server daemon
(``centauri server --light-automation``) and follows the same status
stream as the spaghetti detection — ``printer.watch()`` with backoff-retry
across reconnects.
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

#: Retry backoff after a failed light command — the invariant would
#: otherwise re-fire on every status push (1/s) while sends keep failing.
RETRY_BACKOFF_START = 2.0
RETRY_BACKOFF_MAX = 60.0
#: A status push can predate the effect of a command we just sent; reports
#: matching the pre-command state are ignored for this long (they are lag,
#: not a fresh manual toggle).
LAG_GRACE_S = 3.0


@dataclass(frozen=True)
class LightConfig:
    """Tunables for :class:`LightAutomation` (CLI: ``--light-cool-temp``)."""

    #: The nozzle temperature that decides the light: above it → ON,
    #: at/below it → OFF ("nur die Nozzle-Temp entscheidet").
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
    """Drive the chamber light purely from the nozzle temperature.

    Every status push reconciles the reported light state with the target
    state (nozzle above ``cool_temp`` → ON, at/below → OFF):

    * a heat-up crossing the threshold turns the light on — a print
      starting cold lights up as soon as the nozzle passes the threshold;
    * a cool-down below the threshold turns it off — print end, long pause,
      aborted file check, all the same;
    * a light toggled by hand is put back in line with the temperature on
      the next push ("nur die Nozzle-Temp entscheidet", beide Richtungen);
    * attaching to an already hot or cold printer (service restart,
      outage) normalizes the state immediately.

    Reconciliation dedupes via ``_applied`` — the state we last observed or
    commanded — so each correction costs one command; a report that still
    shows the pre-command state within :data:`LAG_GRACE_S` is treated as
    lag, not as a new deviation. Failed commands back off exponentially
    (from :data:`RETRY_BACKOFF_START` up to :data:`RETRY_BACKOFF_MAX`)
    instead of retrying on every push.
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
        #: Light state we last observed (report is truth) or commanded
        #: (assumed success). ``None`` = never seen; the first push or
        #: command pins it, and every deviation from the temperature target
        #: fires exactly one correction.
        self._applied: bool | None = None
        self._pre_cmd: bool | None = None  # believed state before the last command
        self._cmd_at = 0.0  # monotonic ts of the last dispatched command
        self._retry_not_before = 0.0
        self._retry_backoff = RETRY_BACKOFF_START

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if not self._control_allowed:
            log.warning(
                "light-automation: --enable-control fehlt — Beobachtung ohne "
                "Licht-Befehle (die Invariante kann nicht durchgesetzt werden)"
            )
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
        backoff = 1.0
        while not self._closing:
            try:
                printer = self._get_printer()
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
            backoff = min(backoff * 2, 30.0)

    def _on_status(self, st: Status) -> None:
        """Reconcile the reported light state with the temperature target."""
        temp = st.temp_nozzle
        if temp is None:
            return  # no temperature reading → no decision basis
        loop = asyncio.get_running_loop()
        observed = _light_is_on(st)
        if observed is not None:
            if (
                observed != self._applied
                and observed == self._pre_cmd
                and loop.time() - self._cmd_at < LAG_GRACE_S
            ):
                pass  # the report predates our command's effect — lag, ignore
            else:
                self._applied = observed
                self._pre_cmd = None
        # Exactly at the threshold counts as the cold side — the partition
        # must have no gap, or a crossing could fire twice or never.
        desired = temp > self.cfg.cool_temp
        if desired != self._applied and self._fire(desired, temp):
            self._pre_cmd = self._applied
            self._applied = desired

    # --- command path -------------------------------------------------------

    def _fire(self, on: bool, temp: float) -> bool:
        """Dispatch a light command; ``True`` when a task was created."""
        if not self._control_allowed:
            return False
        loop = asyncio.get_running_loop()
        if loop.time() < self._retry_not_before:
            return False  # backing off after failures — retry later
        self._cmd_at = loop.time()
        reason = (
            f"Düse {'warm' if on else 'kalt'}: {temp:.1f} °C (Schwelle {self.cfg.cool_temp:.0f} °C)"
        )
        task = asyncio.create_task(self._send(on, reason), name=f"pycentauri-light-{on}")
        self._cmds.add(task)
        task.add_done_callback(self._cmds.discard)
        return True

    async def _send(self, on: bool, reason: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            printer = self._get_printer()
            await printer.set_light(on)
            self._retry_backoff = RETRY_BACKOFF_START
            self._retry_not_before = 0.0
            log.info("light-automation: Licht %s — %s", "AN" if on else "AUS", reason)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._retry_not_before = loop.time() + self._retry_backoff
            self._retry_backoff = min(self._retry_backoff * 2, RETRY_BACKOFF_MAX)
            log.warning(
                "light-automation: set_light(%s) fehlgeschlagen: %r — nächster "
                "Versuch nach %.0fs Backoff",
                on,
                err,
                self._retry_backoff,
            )


__all__ = ["LightAutomation", "LightConfig"]
