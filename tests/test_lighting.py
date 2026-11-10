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
    auto._on_status(_status(code=0, nozzle=27.0, light=True))
    auto._on_status(_status(code=13, nozzle=30.0, light=True))
    await _drain(auto)
    assert printer.calls == []


async def test_attach_mid_print_turns_light_on() -> None:
    printer = FakePrinter()
    auto = _automation(printer)  # fresh watcher, first push is mid-print
    auto._on_status(_status(code=13, nozzle=215.0, light=False))
    await _drain(auto)
    assert printer.calls == [True]


# --- rules 2 + 3: cool-down → light off -------------------------------------


async def test_light_off_once_nozzle_cools_below_threshold() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=27.0, light=False))
    auto._on_status(_status(code=13, nozzle=215.0, light=False))  # start → ON
    auto._on_status(_status(code=9, nozzle=180.0, light=True))  # completed, hot
    auto._on_status(_status(code=9, nozzle=80.0, light=True))
    auto._on_status(_status(code=9, nozzle=49.9, light=True))  # crossing → OFF
    auto._on_status(_status(code=9, nozzle=49.0, light=True))  # still below: no spam
    auto._on_status(_status(code=9, nozzle=55.0, light=False))  # reheat: nothing
    await _drain(auto)
    assert printer.calls == [True, False]


async def test_no_off_while_pause_keeps_job_active() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=True))
    auto._on_status(_status(code=6, nozzle=80.0, light=True))  # paused, cooling
    auto._on_status(_status(code=6, nozzle=45.0, light=True))  # below threshold
    await _drain(auto)
    assert printer.calls == []


async def test_off_immediately_when_print_ends_cold() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=30.0, light=False))  # cold job → ON
    auto._on_status(_status(code=9, nozzle=30.0, light=True))  # ended cold → OFF
    await _drain(auto)
    assert printer.calls == [True, False]


# --- manual light is not fought ---------------------------------------------


async def test_manual_idle_light_is_not_fought() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=27.0, light=False))  # attach, dark
    auto._on_status(_status(code=0, nozzle=26.0, light=True))  # user switches on
    auto._on_status(_status(code=0, nozzle=26.5, light=True))
    await _drain(auto)
    assert printer.calls == []


async def test_reconnect_does_not_fight_a_manual_light() -> None:
    """User lit the light at idle — a reconnect (fresh first push) must not
    clear it, because the automation was not the one switching it on."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=27.0, light=False))
    auto._on_status(_status(code=0, nozzle=26.0, light=True))  # user switches on
    auto._first_push = True  # simulate watcher reconnect
    auto._on_status(_status(code=0, nozzle=26.0, light=True))
    await _drain(auto)
    assert printer.calls == []


# --- rule 4: reconnect while our own light is still on ----------------------


async def test_reconnect_clears_our_own_stale_light() -> None:
    """Print started (automation lit it), then the watcher went down and
    came back after the print ended and the nozzle cooled — the stale light
    is cleared instead of burning until the next print."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=False))  # ON, _our_on
    auto._first_push = True  # watcher reconnect after downtime
    auto._on_status(_status(code=0, nozzle=45.0, light=True))
    await _drain(auto)
    assert printer.calls == [True, False]


async def test_attach_idle_cold_lit_without_our_on_stays() -> None:
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=0, nozzle=45.0, light=True))  # not ours → keep
    await _drain(auto)
    assert printer.calls == []


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


async def test_unknown_light_state_still_acts() -> None:
    """Without LightStatus in the push the rules fire anyway (harmless
    no-op on the firmware if the light already matches)."""
    printer = FakePrinter()
    auto = _automation(printer)
    auto._on_status(_status(code=13, nozzle=215.0, light=None))  # start, state unknown
    await _drain(auto)
    assert printer.calls == [True]
    auto._on_status(_status(code=9, nozzle=180.0, light=None))
    auto._on_status(_status(code=9, nozzle=45.0, light=None))  # crossing, unknown
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
        # Next connection: idle, cold, lit — and it was our light (rule 4).
        printer.chunks.append([_status(code=0, nozzle=45.0, light=True)])
        await _until(lambda: printer.calls == [True, False])
    finally:
        await auto.stop()
    await _drain(auto)
