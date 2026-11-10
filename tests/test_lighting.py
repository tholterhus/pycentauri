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

    def __init__(self, chunks: list[list[Status]] | None = None) -> None:
        self.calls: list[bool] = []
        self.fail = False  # make set_light raise
        self.chunks: list[list[Status]] = chunks if chunks is not None else []

    async def set_light(self, on: bool) -> None:
        if self.fail:
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


# --- rule 1: print start → light on -----------------------------------------


async def test_print_start_turns_light_on() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=27.0, light=False))
    auto._on_status(_status(code=13, nozzle=30.0, light=False))
    await _drain(auto)
    assert printer.calls == [True]


async def test_no_redundant_on_when_already_lit() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=True))  # attach mid-print, lit
    auto._on_status(_status(code=0, nozzle=60.0, light=True))  # idle, warm, lit
    auto._on_status(_status(code=13, nozzle=215.0, light=True))  # next start, lit
    await _drain(auto)
    assert printer.calls == []


async def test_attach_mid_print_turns_light_on() -> None:
    printer = FakePrinter()
    auto = _automation(printer)  # fresh watcher, first push is mid-print
    auto._on_status(_status(code=13, nozzle=215.0, light=False))
    await _drain(auto)
    assert printer.calls == [True]


async def test_cold_start_light_survives_heating() -> None:
    """The cool-down rule must not kill the print-start light while the
    nozzle is still heating up from cold — the classic flicker bug."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=30.0, light=False))  # start → ON
    auto._on_status(_status(code=13, nozzle=40.0, light=True))  # still cold!
    auto._on_status(_status(code=13, nozzle=60.0, light=True))  # heated → armed
    auto._on_status(_status(code=13, nozzle=220.0, light=True))
    auto._on_status(_status(code=9, nozzle=45.0, light=True))  # end, cold → OFF
    await _drain(auto)
    assert printer.calls == [True, False]


# --- rule 2: cold means dark, no exceptions ---------------------------------


async def test_light_off_once_nozzle_cools_below_threshold() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=27.0, light=False))
    auto._on_status(_status(code=13, nozzle=215.0, light=False))  # start → ON
    auto._on_status(_status(code=9, nozzle=180.0, light=True))  # completed, hot
    auto._on_status(_status(code=9, nozzle=80.0, light=True))
    auto._on_status(_status(code=9, nozzle=49.9, light=True))  # cold → OFF
    auto._on_status(_status(code=9, nozzle=49.0, light=True))  # deduped
    auto._on_status(_status(code=9, nozzle=55.0, light=False))  # warm again
    auto._on_status(_status(code=9, nozzle=49.0, light=True))  # cold again → OFF
    await _drain(auto)
    assert printer.calls == [True, False, False]


async def test_off_during_pause() -> None:
    """A paused job does not protect the light: if the nozzle cools below
    the threshold during a long pause, the light goes out."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=True))
    auto._on_status(_status(code=6, nozzle=80.0, light=True))  # paused, cooling
    auto._on_status(_status(code=6, nozzle=45.0, light=True))  # cold → OFF
    await _drain(auto)
    assert printer.calls == [False]


async def test_manual_cold_light_is_cleared() -> None:
    """A light toggled on by hand while the nozzle is cold goes out again —
    'immer aus bei <50°', regardless of who switched it on."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=27.0, light=False))  # attach, dark
    auto._on_status(_status(code=0, nozzle=26.0, light=True))  # user switches on
    auto._on_status(_status(code=0, nozzle=26.5, light=True))  # deduped
    await _drain(auto)
    assert printer.calls == [False]


async def test_reconnect_clears_cold_light() -> None:
    """Service restart / outage during cool-down: attaching while idle,
    cold and lit goes dark immediately."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=False))  # ON, print
    auto._first_push = True  # watcher reconnect after downtime
    auto._on_status(_status(code=0, nozzle=45.0, light=True))  # idle, cold, lit
    await _drain(auto)
    assert printer.calls == [True, False]


async def test_print_cancelled_before_heating_goes_dark() -> None:
    """A print that never heats the nozzle (cancelled during file check or
    leveling) ends cold — the suspension lifts with the job and the light
    goes out instead of burning until the next print."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=10, nozzle=30.0, light=False))  # file check → ON
    auto._on_status(_status(code=8, nozzle=30.0, light=True))  # user cancelled
    await _drain(auto)
    assert printer.calls == [True, False]


# --- robustness --------------------------------------------------------------


async def test_control_disabled_logs_but_never_sends() -> None:
    printer = FakePrinter()
    auto = _automation(printer, control_allowed=False)
    auto._on_status(_status(code=0, nozzle=27.0, light=False))
    auto._on_status(_status(code=13, nozzle=30.0, light=False))
    await _drain(auto)
    assert printer.calls == []


async def test_send_failure_does_not_break_the_machine() -> None:
    printer = FakePrinter()
    printer.fail = True
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=False))  # ON attempted, fails
    await _drain(auto)
    auto._on_status(_status(code=9, nozzle=45.0, light=True))  # OFF attempted, fails
    await _drain(auto)
    assert printer.calls == []


async def test_unknown_light_state_still_acts_without_spam() -> None:
    """Without LightStatus in the push the rules fire anyway (harmless
    no-op on the firmware if the light already matches) — but exactly once
    per cold episode."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=None))  # start, state unknown
    await _drain(auto)
    assert printer.calls == [True]
    auto._on_status(_status(code=9, nozzle=180.0, light=None))
    auto._on_status(_status(code=9, nozzle=45.0, light=None))  # cold, unknown → OFF
    auto._on_status(_status(code=9, nozzle=44.0, light=None))  # deduped
    await _drain(auto)
    assert printer.calls == [True, False]


async def test_status_loop_survives_watch_deaths_and_reseeds(
    monkeypatch: Any,
) -> None:
    """watch() dying must not kill the automation; a reconnect re-arms the
    attach rules (first-push rules fire again on the new connection)."""
    monkeypatch.setattr(lighting, "RETRY_BACKOFF_START", 0.01)
    monkeypatch.setattr(lighting, "RETRY_BACKOFF_MAX", 0.02)
    printer = FakePrinter(
        chunks=[
            [_status(code=13, nozzle=215.0, light=False)],  # attach mid-print → ON
        ]
    )
    auto = _automation(printer)
    await auto.start()
    try:
        await _until(lambda: printer.calls == [True])
        # Next connection: idle, cold, lit → invariant clears it.
        printer.chunks.append([_status(code=0, nozzle=45.0, light=True)])
        await _until(lambda: printer.calls == [True, False])
    finally:
        await auto.stop()
    await _drain(auto)
