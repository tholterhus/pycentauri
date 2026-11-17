"""Tests for the chamber-heating automation (``pycentauri.heating``).

Drives :class:`HeatingAutomation` with synthetic status pushes; the Shelly
side is a recording ``request_fn`` (no real HTTP), persistence runs against
a tmp_path JSON. Every push drains the pending command tasks — the same
timing model as production (one status push per second, the HTTP call
completes between pushes).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from pycentauri import heating
from pycentauri.heating import HeatingAutomation, HeatingConfig
from pycentauri.models import Status

URL_ON = "http://shelly/on"
URL_OFF = "http://shelly/off"


class FakePrinter:
    def __init__(self, statuses: list[Status]) -> None:
        self.statuses = statuses

    async def watch(self):  # type: ignore[no-untyped-def]
        for st in self.statuses:
            yield st
        raise RuntimeError("watch exploded")


class Recorder:
    """Stands in for the HTTP layer; records URLs, can fail N times."""

    def __init__(self, fail_left: int = 0) -> None:
        self.calls: list[str] = []
        self.fail_left = fail_left

    async def __call__(self, url: str) -> None:
        if self.fail_left > 0:
            self.fail_left -= 1
            raise RuntimeError("shelly down")
        self.calls.append(url)


def _status(
    *,
    code: int | None,
    bed_target: float | None = None,
    chamber: float | None = None,
    filename: str | None = None,
) -> Status:
    payload: dict[str, Any] = {}
    if code is not None:
        info: dict[str, Any] = {"Status": code}
        if filename is not None:
            info["Filename"] = filename
        payload["PrintInfo"] = info
    if bed_target is not None:
        payload["TempTargetHotbed"] = bed_target
    if chamber is not None:
        payload["TempOfBox"] = chamber
    return Status.from_payload(payload)


def _config(tmp_path: Path, **overrides: Any) -> HeatingConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "url_on": URL_ON,
        "url_off": URL_OFF,
        "bed_temp": 90.0,
        "chamber_min": 40.0,
        "chamber_max": 45.0,
        "material_markers": [],
        "state_path": tmp_path / "heating.json",
    }
    values.update(overrides)
    return HeatingConfig(**values)


async def _drain(auto: HeatingAutomation) -> None:
    """Let the fire-and-forget command tasks run to completion."""
    await asyncio.gather(*list(auto._cmds))


async def _push(auto: HeatingAutomation, st: Status) -> None:
    """Apply one status push and let its commands complete (production
    timing: the Shelly call finishes well before the next push)."""
    auto._on_status(st)
    await _drain(auto)


# --- activation ---------------------------------------------------------------


async def test_bed_target_arms_and_turns_heater_on(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=0, chamber=25.0))
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0, filename="hook.gcode"))
    assert rec.calls == [URL_ON]
    assert auto._armed and "Bett-Ziel" in auto._armed_reason


async def test_no_activation_below_threshold(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=80.0, chamber=25.0, filename="hook.gcode"))
    await _push(auto, _status(code=9, chamber=25.0))
    assert rec.calls == []
    assert not auto._armed


async def test_marker_in_filename_activates(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, material_markers=["ABS"]),
        get_printer=lambda: FakePrinter([]),
        request_fn=rec,
    )
    await _push(auto, _status(code=13, bed_target=60.0, chamber=25.0, filename="hook_ABS_v2.gcode"))
    assert rec.calls == [URL_ON]
    assert "ABS" in auto._armed_reason


async def test_marker_match_is_case_insensitive(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, material_markers=["ASA"]),
        get_printer=lambda: FakePrinter([]),
        request_fn=rec,
    )
    await _push(auto, _status(code=13, bed_target=60.0, chamber=25.0, filename="HOOK-asa-x.gcode"))
    assert rec.calls == [URL_ON]


async def test_attach_mid_print_arms_from_first_push(tmp_path: Path) -> None:
    """Service restart mid-print: the first push re-evaluates the activation
    (bed target 100 → armed, chamber below max → heater on)."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=30.0))
    assert rec.calls == [URL_ON]


async def test_feature_enabled_mid_print_arms(tmp_path: Path) -> None:
    """Enabling the feature while a print runs evaluates the activation on
    the next push instead of waiting for the next print."""
    rec = Recorder()
    cfg = _config(tmp_path, enabled=False)
    auto = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))
    assert rec.calls == []
    auto.apply_settings(enabled=True)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))
    assert rec.calls == [URL_ON]


# --- hysteresis ---------------------------------------------------------------


async def test_hysteresis_roundtrip(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))  # armed → ON
    await _push(auto, _status(code=13, bed_target=100.0, chamber=46.0))  # > max → OFF
    await _push(auto, _status(code=13, bed_target=100.0, chamber=44.0))  # band → hold
    await _push(auto, _status(code=13, bed_target=100.0, chamber=39.0))  # < min → ON
    await _push(auto, _status(code=13, bed_target=100.0, chamber=43.0))  # band → hold
    assert rec.calls == [URL_ON, URL_OFF, URL_ON]


async def test_attach_above_max_stays_off_until_cold(tmp_path: Path) -> None:
    """Arming while the chamber already sits above the maximum must not
    switch the heater on — the hysteresis takes over once it cools."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=50.0))
    assert rec.calls == []
    assert auto._armed
    await _push(auto, _status(code=13, bed_target=100.0, chamber=39.0))
    assert rec.calls == [URL_ON]


# --- session end ----------------------------------------------------------------


async def test_print_end_turns_heater_off(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))  # ON
    await _push(auto, _status(code=13, bed_target=100.0, chamber=39.0))  # hysteresis keeps ON
    await _push(auto, _status(code=9, chamber=39.0))  # completed → OFF regardless
    assert rec.calls == [URL_ON, URL_OFF]
    assert not auto._armed


async def test_end_without_session_never_touches_heater(tmp_path: Path) -> None:
    """A print that never armed the session (PLA) must not send anything —
    a manually used heater stays untouched."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=80.0, chamber=25.0, filename="hook.gcode"))
    await _push(auto, _status(code=9, chamber=25.0))
    assert rec.calls == []


# --- master switch ---------------------------------------------------------------


async def test_disabled_never_calls(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, enabled=False), get_printer=lambda: FakePrinter([]), request_fn=rec
    )
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))
    assert rec.calls == []
    assert not auto._armed


async def test_disable_releases_a_running_heater(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))  # ON
    auto.apply_settings(enabled=False)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))
    assert rec.calls == [URL_ON, URL_OFF]
    assert not auto._armed


# --- settings + persistence -------------------------------------------------------


def test_settings_persist_and_precede(tmp_path: Path) -> None:
    """UI changes land in the state JSON and a fresh automation loads them
    over the .conf/CLI defaults."""
    cfg = _config(tmp_path)
    auto = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=Recorder())
    auto.apply_settings(bed_temp=70.0, material_markers="ABS, ASA", chamber_max=50.0)
    raw = json.loads((tmp_path / "heating.json").read_text(encoding="utf-8"))
    assert raw["bed_temp"] == 70.0
    assert raw["material_markers"] == ["ABS", "ASA"]
    assert raw["chamber_max"] == 50.0

    fresh = HeatingConfig(state_path=tmp_path / "heating.json", chamber_max=45.0)  # conf default
    HeatingAutomation(fresh, get_printer=lambda: FakePrinter([]), request_fn=Recorder())
    assert fresh.bed_temp == 70.0
    assert fresh.material_markers == ["ABS", "ASA"]
    assert fresh.chamber_max == 50.0  # JSON wins over the conf default
    assert fresh.enabled is True  # the JSON stores the full set — it IS the truth


def test_apply_settings_validation_rejects_bad_values(tmp_path: Path) -> None:
    cfg = _config(tmp_path, chamber_min=40.0, chamber_max=45.0)
    auto = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=Recorder())
    with pytest.raises(ValueError):
        auto.apply_settings(chamber_min=50.0)  # >= chamber_max
    with pytest.raises(ValueError):
        auto.apply_settings(url_on="ftp://shelly/on")
    with pytest.raises(ValueError):
        auto.apply_settings(bed_temp=200.0)
    # rejected values never landed
    assert cfg.chamber_min == 40.0
    assert cfg.url_on == URL_ON
    assert cfg.bed_temp == 90.0


# --- robustness ----------------------------------------------------------------


async def test_send_failure_backs_off_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(heating, "RETRY_BACKOFF_START", 0.01)
    rec = Recorder(fail_left=1)
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))  # ON → fails
    assert rec.calls == []
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))  # within backoff: skipped
    assert rec.calls == []
    await asyncio.sleep(0.02)  # backoff elapses
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))  # retry → success
    assert rec.calls == [URL_ON]


async def test_print_end_sends_off_after_failed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The belief stays 'unknown' when the ON call failed — the session end
    still releases (retried, backoff-paced), never leaves it running."""
    monkeypatch.setattr(heating, "RETRY_BACKOFF_START", 0.01)
    rec = Recorder(fail_left=1)
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))  # ON → fails
    assert rec.calls == []
    await _push(auto, _status(code=9, chamber=25.0))  # end → OFF attempt (within backoff)
    assert rec.calls == []
    await asyncio.sleep(0.02)
    await _push(auto, _status(code=9, chamber=25.0))  # release retry → success
    assert rec.calls == [URL_OFF]
    assert auto._applied is False
