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
    chamber_target: float | None = None,
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
    if chamber_target is not None:
        payload["TempTargetBox"] = chamber_target
    return Status.from_payload(payload)


def _config(tmp_path: Path, **overrides: Any) -> HeatingConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "url_on": URL_ON,
        "url_off": URL_OFF,
        "bed_temp": 90.0,
        "chamber_target": 42.5,
        "chamber_hysteresis": 5.0,
        "material_markers": [],
        "state_path": tmp_path / "heating.json",
        "session_path": tmp_path / "heating-session.json",
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


async def test_slicer_chamber_target_arms_and_uses_it(tmp_path: Path) -> None:
    """TempTargetBox > 0 wins outright: it arms the session (even with a
    cold bed and no marker) and becomes the regulation target."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=60.0, chamber=25.0, chamber_target=45.0))
    assert rec.calls == [URL_ON]
    assert auto._armed and "slicer chamber target 45°" in auto._armed_reason


async def test_bed_target_arms_with_fallback_target(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=0, chamber=25.0))
    await _push(
        auto,
        _status(code=13, bed_target=100.0, chamber=25.0, filename="hook.gcode"),
    )
    assert rec.calls == [URL_ON]
    assert auto._armed and "bed target 100°" in auto._armed_reason


async def test_no_activation_without_conditions(tmp_path: Path) -> None:
    """No slicer target, bed below the threshold, no marker: nothing arms,
    nothing fires — even at print end."""
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
    await _push(
        auto,
        _status(
            code=13, bed_target=60.0, chamber=25.0, chamber_target=0.0, filename="hook_ABS_v2.gcode"
        ),
    )
    assert rec.calls == [URL_ON]
    assert "ABS" in auto._armed_reason


async def test_marker_match_is_case_insensitive(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, material_markers=["ASA"]),
        get_printer=lambda: FakePrinter([]),
        request_fn=rec,
    )
    await _push(
        auto,
        _status(
            code=13, bed_target=60.0, chamber=25.0, chamber_target=0.0, filename="HOOK-asa-x.gcode"
        ),
    )
    assert rec.calls == [URL_ON]


async def test_attach_mid_print_arms_from_first_push(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=30.0))
    assert rec.calls == [URL_ON]


async def test_slicer_target_mid_print_arms_late(tmp_path: Path) -> None:
    """A slicer setting the chamber target mid-print (M141) arms the session
    on the next push — no print restart needed."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=80.0, chamber=25.0, filename="job.gcode"))
    assert not auto._armed
    await _push(
        auto,
        _status(code=13, bed_target=80.0, chamber=30.0, chamber_target=45.0, filename="job.gcode"),
    )
    assert rec.calls == [URL_ON]
    assert auto._armed


async def test_feature_enabled_mid_print_arms(tmp_path: Path) -> None:
    rec = Recorder()
    cfg = _config(tmp_path, enabled=False)
    auto = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))
    assert rec.calls == []
    auto.apply_settings(enabled=True)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0))
    assert rec.calls == [URL_ON]


# --- hysteresis ---------------------------------------------------------------


async def test_hysteresis_slicer_target_with_factor(tmp_path: Path) -> None:
    """Slicer target 45° with factor 5 -> hold 42.5-47.5: ON below 42.5,
    OFF above 47.5, in between keep the current state."""
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, chamber_target=42.5, chamber_hysteresis=5.0),
        get_printer=lambda: FakePrinter([]),
        request_fn=rec,
    )
    await _push(auto, _status(code=13, bed_target=100.0, chamber=30.0, chamber_target=45.0))  # ON
    await _push(auto, _status(code=13, bed_target=100.0, chamber=47.0, chamber_target=45.0))  # hold
    await _push(auto, _status(code=13, bed_target=100.0, chamber=48.0, chamber_target=45.0))  # OFF
    await _push(auto, _status(code=13, bed_target=100.0, chamber=46.0, chamber_target=45.0))  # hold
    await _push(auto, _status(code=13, bed_target=100.0, chamber=42.0, chamber_target=45.0))  # ON
    assert rec.calls == [URL_ON, URL_OFF, URL_ON]


async def test_hysteresis_fallback_target_when_slicer_target_zero(tmp_path: Path) -> None:
    """Slicer target 0 but armed via bed: the configured fallback target
    (42.5) with factor 5 governs — hold 40-45°."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=30.0, chamber_target=0.0))  # ON
    await _push(auto, _status(code=13, bed_target=100.0, chamber=45.5, chamber_target=0.0))  # OFF
    await _push(auto, _status(code=13, bed_target=100.0, chamber=44.0, chamber_target=0.0))  # hold
    await _push(auto, _status(code=13, bed_target=100.0, chamber=39.5, chamber_target=0.0))  # ON
    assert rec.calls == [URL_ON, URL_OFF, URL_ON]


async def test_attach_above_max_stays_off_until_cold(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=50.0, chamber_target=0.0))
    assert rec.calls == []
    assert auto._armed
    await _push(auto, _status(code=13, bed_target=100.0, chamber=39.0, chamber_target=0.0))
    assert rec.calls == [URL_ON]


# --- session end ----------------------------------------------------------------


async def test_print_end_turns_heater_off(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0))  # ON
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.5, chamber_target=0.0))  # hold
    await _push(auto, _status(code=9, chamber=25.0, chamber_target=0.0))  # completed → OFF
    assert rec.calls == [URL_ON, URL_OFF]
    assert not auto._armed
    assert auto._armed_at is not None  # the closed session stays addressable


async def test_end_without_session_never_touches_heater(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=80.0, chamber=25.0, filename="hook.gcode"))
    await _push(auto, _status(code=9, chamber=25.0))
    assert rec.calls == []


# --- marker boundaries ----------------------------------------------------------


async def test_marker_with_underscores_matches(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, material_markers=["_ABS_"]),
        get_printer=lambda: FakePrinter([]),
        request_fn=rec,
    )
    await _push(
        auto,
        _status(
            code=13, bed_target=60.0, chamber=25.0, chamber_target=0.0, filename="hook_ABS_v2.gcode"
        ),
    )
    assert rec.calls == [URL_ON]


async def test_bare_marker_does_not_match_inside_words(tmp_path: Path) -> None:
    """A bare ABS marker must not arm on 'ABSchneide' — but ' ABS ' with a
    space delimiter does, and ' ABS ' typed with edges behaves like a bare
    marker with the same boundary semantics (edges get trimmed)."""
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, material_markers=[" ABS "]),
        get_printer=lambda: FakePrinter([]),
        request_fn=rec,
    )
    await _push(
        auto,
        _status(
            code=13,
            bed_target=60.0,
            chamber=25.0,
            chamber_target=0.0,
            filename="ABSchneide_jig.gcode",
        ),
    )
    assert rec.calls == []
    await _push(
        auto,
        _status(
            code=13,
            bed_target=60.0,
            chamber=25.0,
            chamber_target=0.0,
            filename="rod ABS 1h48m.gcode",
        ),
    )
    assert rec.calls == [URL_ON]


# --- overshoot learning -------------------------------------------------------


async def test_overshoot_learned_from_first_phase(tmp_path: Path) -> None:
    """OFF at the band top; the temperature keeps rising (thermal inertia),
    peaks, falls — the measured overshoot folds into the EMA and lowers the
    next OFF point."""
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, chamber_target=45.0, chamber_hysteresis=5.0, chamber_overshoot=0.0),
        get_printer=lambda: FakePrinter([]),
        request_fn=rec,
    )
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=30.0, chamber_target=45.0)
    )  # arm + ON
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=47.6, chamber_target=45.0)
    )  # OFF (47.5)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=48.5, chamber_target=45.0))  # peak
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=48.0, chamber_target=45.0)
    )  # falling → bank
    # sample = 48.5 - 47.5 = 1.0; EMA(0, 1.0, alpha 0.4) = 0.4
    assert auto.cfg.chamber_overshoot == pytest.approx(0.4)
    # The learned overshoot shows in the NEXT heating phase: re-heat below
    # the ON point → ON, then the climb trips the OFF at 47.1 (was 47.5).
    await _push(auto, _status(code=13, bed_target=100.0, chamber=40.0, chamber_target=45.0))
    await _push(auto, _status(code=13, bed_target=100.0, chamber=47.2, chamber_target=45.0))
    assert rec.calls == [URL_ON, URL_OFF, URL_ON, URL_OFF]


async def test_overshoot_is_clamped_to_the_factor(tmp_path: Path) -> None:
    """A huge overshoot may never push the OFF point below the ON point —
    clamped to the hysteresis."""
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, chamber_target=45.0, chamber_hysteresis=2.0, chamber_overshoot=2.5),
        get_printer=lambda: FakePrinter([]),
        request_fn=rec,
    )
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=44.1, chamber_target=45.0)
    )  # OFF (44)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=50.0, chamber_target=45.0))  # peak
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=49.5, chamber_target=45.0)
    )  # fall → bank
    # sample = 50 - 44 = 6; EMA(2.5, 6, alpha 0.4) = 3.9 → clamped to factor 2
    assert auto.cfg.chamber_overshoot == 2.0


def test_overshoot_persists_in_settings(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    auto = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=Recorder())
    auto.apply_settings(chamber_overshoot=1.2)
    raw = json.loads((tmp_path / "heating.json").read_text(encoding="utf-8"))
    assert raw["chamber_overshoot"] == 1.2
    with pytest.raises(ValueError):
        auto.apply_settings(chamber_overshoot=-1.0)
    with pytest.raises(ValueError):
        auto.apply_settings(chamber_overshoot=80.0)


# --- safety watchdog ------------------------------------------------------------


async def test_safety_trip_on_flat_temperature(tmp_path: Path) -> None:
    """Heater ON but the chamber temperature stays flat for the whole
    window → heater off, safety stop, fat red warning in the state."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    auto._safety_window = 0.0  # trip on the first push after ON
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0)
    )  # arm + ON
    assert rec.calls == [URL_ON]
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.4, chamber_target=0.0)
    )  # watch baseline
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.4, chamber_target=0.0)
    )  # flat → trip
    assert rec.calls == [URL_ON, URL_OFF]
    assert auto._safety_stop is True
    assert "no temperature rise" in auto._safety_message
    st = auto.state()
    assert st["safety_stop"] is True
    # the hysteresis stays suppressed: a cold chamber does NOT re-fire ON
    await _push(auto, _status(code=13, bed_target=100.0, chamber=39.0, chamber_target=0.0))
    assert rec.calls == [URL_ON, URL_OFF]


async def test_rising_temperature_does_not_trip(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    auto._safety_window = 0.0
    auto._safety_rise = 1.0
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0))  # ON
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=27.0, chamber_target=0.0)
    )  # +2°: fine
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=29.0, chamber_target=0.0)
    )  # still fine
    assert rec.calls == [URL_ON]
    assert auto._safety_stop is False


async def test_safety_stop_clears_at_session_end_and_retries(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    auto._safety_window = 0.0
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0))  # ON
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.4, chamber_target=0.0)
    )  # trip → OFF
    await _push(auto, _status(code=9, chamber=25.0, chamber_target=0.0))  # print ends
    assert auto._safety_stop is False  # cleared
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0)
    )  # new print
    assert rec.calls == [URL_ON, URL_OFF, URL_ON]  # the next print tries again


async def test_safety_trip_without_chamber_readings(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    auto._safety_window = 0.0
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0))  # ON
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=None, chamber_target=0.0)
    )  # watch baseline
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=None, chamber_target=0.0)
    )  # dead sensor → trip
    assert rec.calls == [URL_ON, URL_OFF]
    assert "no chamber temperature readings" in auto._safety_message


# --- downtime phase: unlearnable -------------------------------------------------


async def test_downtime_phase_is_not_learned(tmp_path: Path) -> None:
    """A phase already open when the session resumed (service downtime with
    the heater unattended) has a garbage overshoot - it must not be banked."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    auto._sample_interval = 0.0
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=30.0, chamber_target=45.0)
    )  # arm + ON
    # simulate: the service went down, the chamber climbed to 52.5 during
    # the gap, then the service resumed (session file already open).
    data = auto.session_data()
    auto._session_samples.append([data["phases"][0][0] + 330, 52.5])
    auto._resume_open_phase = True  # what the restart-resume would set
    await _push(auto, _status(code=13, bed_target=100.0, chamber=52.5, chamber_target=45.0))  # OFF
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=51.5, chamber_target=45.0)
    )  # falling
    await _push(auto, _status(code=13, bed_target=100.0, chamber=51.0, chamber_target=45.0))
    # the downtime overshoot (52.5 - 47.5 = 5°) was NOT banked
    assert auto.cfg.chamber_overshoot == 0.0
    assert auto._resume_open_phase is True  # still flagged: no fresh ON yet
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=42.0, chamber_target=45.0)
    )  # below ON point → re-heat
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=47.6, chamber_target=45.0)
    )  # new phase OFF
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=47.3, chamber_target=45.0)
    )  # fall → bank
    # the fresh phase (peak 47.6 over off-point 47.5 = 0.1) banks normally
    assert auto.cfg.chamber_overshoot == pytest.approx(0.1 * 0.4)


# --- master switch ---------------------------------------------------------------


async def test_disabled_never_calls(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(
        _config(tmp_path, enabled=False), get_printer=lambda: FakePrinter([]), request_fn=rec
    )
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=45.0))
    assert rec.calls == []
    assert not auto._armed


async def test_disable_releases_a_running_heater(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0))  # ON
    auto.apply_settings(enabled=False)
    await _push(auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0))
    assert rec.calls == [URL_ON, URL_OFF]
    assert not auto._armed


# --- session (graph data) -------------------------------------------------------


async def test_session_samples_and_phases_recorded(tmp_path: Path) -> None:
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    auto._sample_interval = 0.0  # sample every push
    await _push(auto, _status(code=13, bed_target=100.0, chamber=30.0, chamber_target=45.0))  # ON
    await _push(auto, _status(code=13, bed_target=100.0, chamber=31.0, chamber_target=45.0))
    await _push(auto, _status(code=9, chamber=31.5, chamber_target=45.0))  # end → OFF closes
    data = auto.session_data()
    assert len(data["samples"]) >= 2
    assert data["phases"] and data["phases"][0][1] is not None  # phase closed
    assert data["open"] is False  # session ended
    assert (tmp_path / "heating-session.json").is_file()


async def test_session_resumes_after_service_restart(tmp_path: Path) -> None:
    """A service restart mid-print must keep the graph: the persisted
    session file is still open, so the fresh automation resumes it (same
    armed_since, samples preserved) instead of starting a new graph."""
    rec1 = Recorder()
    cfg = _config(tmp_path)
    auto1 = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=rec1)
    auto1._sample_interval = 0.0
    await _push(auto1, _status(code=13, bed_target=100.0, chamber=30.0, chamber_target=45.0))  # ON
    first = auto1.session_data()
    assert first["open"] is True
    assert len(first["samples"]) >= 1

    rec2 = Recorder()
    auto2 = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=rec2)
    await _push(auto2, _status(code=13, bed_target=100.0, chamber=31.0, chamber_target=45.0))
    data = auto2.session_data()
    assert data["open"] is True
    assert data["samples"][0] == first["samples"][0]  # resumed, not reset
    assert rec2.calls == [URL_ON]


async def test_new_print_starts_a_fresh_session(tmp_path: Path) -> None:
    """A cleanly ended session (open=False) is dropped on the next arm —
    a fresh print starts a fresh graph."""
    rec = Recorder()
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    auto._sample_interval = 0.0
    await _push(auto, _status(code=13, bed_target=100.0, chamber=30.0, chamber_target=45.0))
    await _push(auto, _status(code=9, chamber=30.0, chamber_target=45.0))  # ends cleanly
    first = auto.session_data()
    assert first["open"] is False
    n_samples = len(first["samples"])

    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=30.0, chamber_target=45.0)
    )  # new print
    data = auto.session_data()
    assert data["open"] is True
    assert len(data["samples"]) < n_samples + 5  # fresh, not appended


# --- settings + persistence -------------------------------------------------------


def test_settings_persist_and_precede(tmp_path: Path) -> None:
    """UI changes land in the state JSON and a fresh automation loads them
    over the .conf/CLI defaults."""
    cfg = _config(tmp_path)
    auto = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=Recorder())
    auto.apply_settings(
        bed_temp=70.0, material_markers="ABS, ASA", chamber_target=45.0, chamber_hysteresis=6.0
    )
    raw = json.loads((tmp_path / "heating.json").read_text(encoding="utf-8"))
    assert raw["bed_temp"] == 70.0
    assert raw["material_markers"] == ["ABS", "ASA"]
    assert raw["chamber_target"] == 45.0
    assert raw["chamber_hysteresis"] == 6.0

    fresh = HeatingConfig(state_path=tmp_path / "heating.json", chamber_target=42.5)
    HeatingAutomation(fresh, get_printer=lambda: FakePrinter([]), request_fn=Recorder())
    assert fresh.bed_temp == 70.0
    assert fresh.material_markers == ["ABS", "ASA"]
    assert fresh.chamber_target == 45.0  # JSON wins over the conf default
    assert fresh.chamber_hysteresis == 6.0
    assert fresh.enabled is True  # the JSON stores the full set — it IS the truth


def test_old_min_max_state_migrates_to_target_and_factor(tmp_path: Path) -> None:
    """A heating.json from before 0.17.9 (min/max pair) maps to the
    target/factor model: 40/45 → target 42.5, factor 5."""
    state = {
        "enabled": True,
        "url_on": URL_ON,
        "url_off": URL_OFF,
        "bed_temp": 90.0,
        "chamber_min": 40.0,
        "chamber_max": 45.0,
        "material_markers": ["ABS"],
    }
    (tmp_path / "heating.json").write_text(json.dumps(state), encoding="utf-8")
    cfg = HeatingConfig(state_path=tmp_path / "heating.json")
    HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=Recorder())
    assert cfg.chamber_target == 42.5
    assert cfg.chamber_hysteresis == 5.0
    assert cfg.enabled is True
    assert cfg.material_markers == ["ABS"]


def test_apply_settings_validation_rejects_bad_values(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    auto = HeatingAutomation(cfg, get_printer=lambda: FakePrinter([]), request_fn=Recorder())
    with pytest.raises(ValueError):
        auto.apply_settings(chamber_hysteresis=0.5)  # degenerate band
    with pytest.raises(ValueError):
        auto.apply_settings(chamber_target=70.0)  # out of 0..60
    with pytest.raises(ValueError):
        auto.apply_settings(url_on="ftp://shelly/on")
    with pytest.raises(ValueError):
        auto.apply_settings(bed_temp=200.0)
    # rejected values never landed
    assert cfg.chamber_hysteresis == 5.0
    assert cfg.chamber_target == 42.5
    assert cfg.url_on == URL_ON
    assert cfg.bed_temp == 90.0


# --- robustness ----------------------------------------------------------------


async def test_send_failure_backs_off_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(heating, "RETRY_BACKOFF_START", 0.01)
    rec = Recorder(fail_left=1)
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0)
    )  # ON → fails
    assert rec.calls == []
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0)
    )  # backoff: skip
    assert rec.calls == []
    await asyncio.sleep(0.02)  # backoff elapses
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0)
    )  # retry → OK
    assert rec.calls == [URL_ON]


async def test_print_end_sends_off_after_failed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The belief stays 'unknown' when the ON call failed — the session end
    still releases (retried, backoff-paced), never leaves it running."""
    monkeypatch.setattr(heating, "RETRY_BACKOFF_START", 0.01)
    rec = Recorder(fail_left=1)
    auto = HeatingAutomation(_config(tmp_path), get_printer=lambda: FakePrinter([]), request_fn=rec)
    await _push(
        auto, _status(code=13, bed_target=100.0, chamber=25.0, chamber_target=0.0)
    )  # ON → fails
    assert rec.calls == []
    await _push(auto, _status(code=9, chamber=25.0))  # end → OFF attempt (within backoff)
    assert rec.calls == []
    await asyncio.sleep(0.02)
    await _push(auto, _status(code=9, chamber=25.0))  # release retry → success
    assert rec.calls == [URL_OFF]
    assert auto._applied is False
