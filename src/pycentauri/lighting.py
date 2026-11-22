"""Chamber-light automation driven by printer activity and nozzle heat.

Replaces the Home-Assistant automation pair that used to do exactly this
job. Two inputs, one target state — the light is ON while the printer
shows any activity (preheating, printing, a pause, whatever occupies it)
OR the nozzle is above a threshold (default 50 °C); it goes OFF only once
the printer is idle again AND the nozzle has cooled below that threshold,
so the temperature keeps acting as the safety net that eventually darkens
an idle printer. Lives in the server daemon
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

from pycentauri.models import INACTIVE_CODES, Status

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
    """Drive the chamber light from printer activity and nozzle heat.

    Every status push reconciles the reported light state with the target
    state (printer active OR nozzle above ``cool_temp`` → ON, idle AND
    cooled → OFF):

    * any activation turns the light on — preheating, a print start, a
      filament change, whatever occupies the printer;
    * the temperature threshold stays as the safety net: it lights a
      merely hot printer and darkens an idle one once it cools down;
    * a light toggled by hand is put back in line with the target on the
      next push (beide Richtungen);
    * attaching to an already hot, cold, busy or idle printer (service
      restart, outage) normalizes the state immediately.

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
        #: A hand toggle from the dashboard pins the light: ON survives the
        #: cool-down net until the user turns it off again. OFF hands
        #: control straight back to the automation.
        self._manual = False

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
        """Reconcile the reported light state with the target state: ON
        while the printer shows any activity (preheating, printing, a
        pause — anything that occupies it) OR the nozzle is above the
        threshold OR the user pinned it on by hand; OFF only once it is
        idle again AND cooled below it AND unpinned."""
        code = st.print_status
        active = code is not None and code not in INACTIVE_CODES
        temp = st.temp_nozzle
        if temp is None and not active:
            return  # neither a temperature reading nor activity → no basis
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
                was_applied = self._applied
                first_seed = was_applied is None
                self._applied = observed
                self._pre_cmd = None
                # A toggle at the printer itself is as manual as a dashboard
                # click: lit while the automation wants dark (idle and cold
                # — a state we never produce) is a hand toggle and pins the
                # light; dark while pinned unpins it again. The most recent
                # human action wins.
                if (
                    observed
                    and not active
                    and temp is not None
                    and temp <= self.cfg.cool_temp
                    and (first_seed or was_applied is False)
                ):
                    self._manual = True
                if not observed and self._manual:
                    self._manual = False
        # Exactly at the threshold counts as the cold side — the partition
        # must have no gap, or a crossing could fire twice or never. An
        # active printer always wants light; the threshold is the net that
        # darkens it again once it goes idle and cools down — unless the
        # user pinned it on by hand (they turn it off themselves).
        desired = active or self._manual or (temp is not None and temp > self.cfg.cool_temp)
        if desired != self._applied and self._fire(desired, code, temp):
            self._pre_cmd = self._applied
            self._applied = desired

    def note_manual(self, on: bool) -> None:
        """Record a hand toggle from the dashboard: ON pins the light (the
        cool-down net does not darken it again until the user turns it off
        by hand), OFF hands control straight back to the automation."""
        self._manual = bool(on)
        if self._applied is not on:
            # Mirror the command bookkeeping of _fire so a lagging report
            # inside LAG_GRACE_S is not mistaken for a deviation.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._pre_cmd = self._applied
            self._cmd_at = loop.time()
            self._applied = bool(on)

    # --- command path -------------------------------------------------------

    def _fire(self, on: bool, code: int | None, temp: float | None) -> bool:
        """Dispatch a light command; ``True`` when a task was created."""
        if not self._control_allowed:
            return False
        loop = asyncio.get_running_loop()
        if loop.time() < self._retry_not_before:
            return False  # backing off after failures — retry later
        self._cmd_at = loop.time()
        if on and code is not None:
            reason = f"Drucker aktiv (Status {code}) — Licht an"
        elif on:
            reason = f"Düse warm: {temp:.1f} °C (Schwelle {self.cfg.cool_temp:.0f} °C)"
        else:
            reason = (
                f"Ruhend und abgekühlt: {temp:.1f} °C ≤ Schwelle "
                f"{self.cfg.cool_temp:.0f} °C — Licht aus"
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
