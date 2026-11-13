"""Tests for the chamber-light automation (``pycentauri.lighting``).

Drives the :class:`LightAutomation` state machine with synthetic status
pushes (no printer hardware, no real connection); the command path is
observed through a fake printer's ``set_light`` record.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from pycentauri import lighting
from pycentauri.lighting import LightAutomation, LightConfig
from pycentauri.models import Status


class FakePrinter:
    """Records set_light calls; watch() replays one status chunk per
    connection, then ends the stream (as a printer disconnect would)."""

    def __init__(
        self,
        chunks: list[list[Status]] | None = None,
        fail_left: int | None = None,
    ) -> None:
        self.calls: list[bool] = []
        self.fail_left = fail_left  # fail the first N sends, then succeed
        self.chunks: list[list[Status]] = chunks if chunks is not None else []

    async def set_light(self, on: bool) -> None:
        if self.fail_left is not None and self.fail_left > 0:
            self.fail_left -= 1
            raise RuntimeError("wire down")
        self.calls.append(on)

    async def watch(self) -> AsyncIterator[Status]:
        if not self.chunks:
            raise RuntimeError("watch exploded")  # forces the loop's backoff path
        for st in self.chunks.pop(0):
            yield st


def _status(
    *,
    code: int | None,
    nozzle: float | None,
    light: bool | None = None,
) -> Status:
    """Build a Status the way the CC1 pushes it (scalar temps, LightStatus)."""
    payload: dict[str, Any] = {}
    if code is not None:
        payload["PrintInfo"] = {"Status": code}
    if nozzle is not None:
        payload["TempOfNozzle"] = nozzle
    if light is not None:
        payload["LightStatus"] = {"SecondLight": 1 if light else 0}
    return Status.from_payload(payload)


def _automation(
    printer: FakePrinter,
    *,
    cool_temp: float = 50.0,
    control_allowed: bool = True,
) -> LightAutomation:
    return LightAutomation(
        LightConfig(cool_temp=cool_temp),
        get_printer=lambda: printer,
        control_allowed=control_allowed,
    )


async def _drain(automation: LightAutomation) -> None:
    """Let the fire-and-forget command tasks run to completion."""
    await asyncio.gather(*list(automation._cmds))


async def _until(pred: Callable[[], bool], timeout: float = 2.0) -> None:
    """Poll until ``pred()`` holds (the watch loop retries on a backoff)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# --- the invariant: hot = on, cold = off ------------------------------------


async def test_heat_up_turns_light_on() -> None:
    """Crossing the threshold on the way up → ON — no print status needed."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=30.0, light=False))
    auto._on_status(_status(code=0, nozzle=49.0, light=False))
    auto._on_status(_status(code=0, nozzle=60.0, light=False))  # crossing → ON
    await _drain(auto)
    assert printer.calls == [True]


async def test_cool_down_turns_light_off() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=80.0, light=True))
    auto._on_status(_status(code=0, nozzle=60.0, light=True))
    auto._on_status(_status(code=0, nozzle=45.0, light=True))  # crossing → OFF
    auto._on_status(_status(code=0, nozzle=40.0, light=False))  # deduped
    await _drain(auto)
    assert printer.calls == [False]


async def test_status_is_irrelevant_full_cycle() -> None:
    """Pause, printing, idle — only the temperature decides. A print that
    pauses long enough to cool goes dark and lights up again on resume."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=200.0, light=True))  # printing, hot
    auto._on_status(_status(code=6, nozzle=45.0, light=True))  # paused, cold → OFF
    auto._on_status(_status(code=13, nozzle=80.0, light=False))  # resumed, warm → ON
    await _drain(auto)
    assert printer.calls == [False, True]


async def test_boundary_exactly_50_counts_cold() -> None:
    """The partition has no gap: exactly at the threshold = cold side, so a
    crossing fires exactly once in each direction."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=50.0, light=False))  # at 50 = cold: nothing
    auto._on_status(_status(code=0, nozzle=50.5, light=False))  # above → ON
    auto._on_status(_status(code=0, nozzle=50.0, light=True))  # back at 50 → OFF
    await _drain(auto)
    assert printer.calls == [True, False]


# --- manual toggles are reconciled -------------------------------------------


async def test_manual_off_while_hot_is_corrected() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=200.0, light=True))  # hot, lit: nothing
    auto._on_status(_status(code=0, nozzle=200.0, light=False))  # user off → ON
    auto._on_status(_status(code=0, nozzle=200.0, light=True))  # confirmed: nothing
    await _drain(auto)
    assert printer.calls == [True]


async def test_manual_on_while_cold_is_corrected() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=30.0, light=True))  # user on, cold → OFF
    auto._on_status(_status(code=0, nozzle=30.0, light=True))  # lag: deduped
    await _drain(auto)
    assert printer.calls == [False]


# --- attach normalizes the state ---------------------------------------------


async def test_attach_hot_dark_seeds_on() -> None:
    """Service restart mid-print (hot, dark) → light on immediately."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=False))
    await _drain(auto)
    assert printer.calls == [True]


async def test_attach_cold_lit_seeds_off() -> None:
    """Service restart during cool-down (cold, lit) → light off immediately."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=45.0, light=True))
    await _drain(auto)
    assert printer.calls == [False]


# --- robustness ---------------------------------------------------------------


async def test_no_temperature_no_action() -> None:
    """Without a temperature reading there is no decision basis — but the
    first real reading reconciles the light immediately."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=None, light=True))
    auto._on_status(_status(code=0, nozzle=45.0, light=True))  # cold, lit → OFF
    await _drain(auto)
    assert printer.calls == [False]


async def test_unknown_light_state_still_acts_without_spam() -> None:
    """Without LightStatus in the push the rules fire anyway (harmless
    no-op on the firmware if the light already matches) — but exactly once
    per state change, since our own command pins the assumed state."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=45.0, light=None))  # cold → OFF once
    auto._on_status(_status(code=0, nozzle=44.0, light=None))  # deduped
    auto._on_status(_status(code=0, nozzle=60.0, light=None))  # warm → ON
    auto._on_status(_status(code=0, nozzle=55.0, light=None))  # deduped
    await _drain(auto)
    assert printer.calls == [False, True]


async def test_send_failure_backs_off_then_retries(monkeypatch: Any) -> None:
    """A failed command must not be re-fired on every push (1/s spam while
    the printer is unreachable) — exponential backoff instead."""
    monkeypatch.setattr(lighting, "RETRY_BACKOFF_START", 0.01)
    monkeypatch.setattr(lighting, "LAG_GRACE_S", 0.0)
    printer = FakePrinter(fail_left=1)
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=80.0, light=False))  # ON → fails
    await _drain(auto)
    auto._on_status(_status(code=0, nozzle=80.0, light=False))  # within backoff: skipped
    await _drain(auto)
    assert printer.calls == []
    await asyncio.sleep(0.02)  # backoff elapses
    auto._on_status(_status(code=0, nozzle=80.0, light=False))  # retry → succeeds
    await _drain(auto)
    assert printer.calls == [True]


async def test_control_disabled_never_sends() -> None:
    printer = FakePrinter()
    auto = _automation(printer, control_allowed=False)
    auto._on_status(_status(code=0, nozzle=30.0, light=True))
    auto._on_status(_status(code=0, nozzle=80.0, light=False))
    await _drain(auto)
    assert printer.calls == []


async def test_status_loop_survives_watch_deaths_and_reseeds(
    monkeypatch: Any,
) -> None:
    """watch() dying must not kill the automation; a reconnect reconciles
    the light from the first push of the new connection."""
    monkeypatch.setattr(lighting, "RETRY_BACKOFF_START", 0.01)
    monkeypatch.setattr(lighting, "RETRY_BACKOFF_MAX", 0.02)
    printer = FakePrinter(
        chunks=[
            [_status(code=13, nozzle=215.0, light=False)],  # attach hot+dark → ON
        ]
    )
    auto = _automation(printer)
    await auto.start()
    try:
        await _until(lambda: printer.calls == [True])
        # Next connection: idle, cold, lit → reconciled to OFF.
        printer.chunks.append([_status(code=0, nozzle=45.0, light=True)])
        await _until(lambda: printer.calls == [True, False])
    finally:
        await auto.stop()
    await _drain(auto)
