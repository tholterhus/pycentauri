"""Tests for the shared-upstream MJPEG broadcaster.

A fake source stands in for the printer's camera so we can prove the
core promises: no matter how many subscribers attach, the upstream is
opened exactly once, every subscriber sees the same *whole* frames, and
a slow subscriber can never corrupt the multipart byte stream for
anyone.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from pycentauri.mjpeg_broadcast import (
    CameraBroadcaster,
    CameraUnavailable,
    FrameAssembler,
    _Upstream,
)


def _part(body: bytes, boundary: str = "xyz") -> bytes:
    """One complete multipart part, exactly as the printer's camera emits it."""
    head = (
        f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    return head + body + b"\r\n"


class _FakeSource:
    """A controllable upstream: opens counted, yields chunks on demand."""

    def __init__(self) -> None:
        self.opens = 0
        self.closed = 0
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def open(self) -> _Upstream:
        self.opens += 1

        async def chunks() -> AsyncIterator[bytes]:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item

        async def aclose() -> None:
            self.closed += 1

        return _Upstream("multipart/x-mixed-replace; boundary=xyz", chunks(), aclose)

    def push(self, data: bytes) -> None:
        self._queue.put_nowait(data)


async def _take(gen: AsyncIterator[bytes], n: int) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in gen:
        out.append(chunk)
        if len(out) >= n:
            break
    return out


async def test_single_upstream_for_many_subscribers() -> None:
    src = _FakeSource()
    b = CameraBroadcaster(lambda: "http://x/", open_source=src.open)

    mt1, g1 = await b.subscribe()
    mt2, g2 = await b.subscribe()
    mt3, g3 = await b.subscribe()
    assert mt1 == mt2 == mt3 == "multipart/x-mixed-replace; boundary=xyz"
    # THE POINT: three subscribers, one upstream connection.
    assert src.opens == 1

    src.push(_part(b"frameA"))
    src.push(_part(b"frameB"))
    # A frame ends at the *next* boundary line, so push a trailing delimiter
    # to flush the last one — exactly what the live stream does continuously.
    src.push(b"--xyz\r\n")
    # Every subscriber sees the same broadcast frames — whole parts, byte
    # for byte as the upstream sent them.
    for g in (g1, g2, g3):
        got = await asyncio.wait_for(_take(g, 2), timeout=2.0)
        assert got == [_part(b"frameA"), _part(b"frameB")]

    await b.close()
    assert src.closed >= 1


async def test_subscribe_times_out_when_upstream_never_connects() -> None:
    async def never_opens() -> _Upstream:
        raise RuntimeError("camera down")

    b = CameraBroadcaster(lambda: "http://x/", open_source=never_opens)
    import pycentauri.mjpeg_broadcast as m

    monkeyed = m.CONNECT_WAIT_S
    m.CONNECT_WAIT_S = 0.3
    try:
        with pytest.raises(CameraUnavailable):
            await b.subscribe()
    finally:
        m.CONNECT_WAIT_S = monkeyed
        await b.close()


async def test_slow_subscriber_does_not_stall_others() -> None:
    src = _FakeSource()
    b = CameraBroadcaster(lambda: "http://x/", open_source=src.open, queue_max=4)
    _mt_slow, _g_slow = await b.subscribe()  # never drained → queue overflows
    _mt_fast, g_fast = await b.subscribe()

    for i in range(20):
        src.push(_part(f"f{i}".encode()))
    # The fast subscriber still receives recent frames despite the slow one
    # never draining (old frames get dropped for the slow queue, not blocked).
    got = await asyncio.wait_for(_take(g_fast, 4), timeout=2.0)
    assert len(got) == 4
    # Drops happen at frame boundaries: whatever arrives is a whole part.
    assert all(g.startswith(b"--xyz\r\n") for g in got)
    assert src.opens == 1
    await b.close()


async def test_close_ends_subscriber_generators() -> None:
    src = _FakeSource()
    b = CameraBroadcaster(lambda: "http://x/", open_source=src.open)
    _mt, gen = await b.subscribe()
    await b.close()
    # After close, the generator finishes cleanly (no hang).
    remaining = [chunk async for chunk in gen]
    assert isinstance(remaining, list)


async def test_upstream_closes_after_last_subscriber_leaves() -> None:
    """Nobody watching (the dashboard aborts its fetch when every tab is
    hidden or closed) → after IDLE_CLOSE_S the camera upstream must close
    instead of streaming to nobody, and a later viewer reconnects."""
    import pycentauri.mjpeg_broadcast as m

    src = _FakeSource()
    saved = m.IDLE_CLOSE_S
    m.IDLE_CLOSE_S = 0.2
    try:
        b = CameraBroadcaster(lambda: "http://x/", open_source=src.open)
        _mt, gen = await b.subscribe()
        src.push(_part(b"frameA"))
        src.push(b"--xyz\r\n")
        got = await asyncio.wait_for(_take(gen, 1), timeout=2.0)
        assert got == [_part(b"frameA")]

        await gen.aclose()  # the last viewer left
        await asyncio.sleep(0.35)  # > IDLE_CLOSE_S → idle stop is armed
        # The reader sits in __anext__; real cameras send continuously, so
        # feed wake frames until the reader surfaces and parks.
        for _ in range(25):
            if src.closed >= 1:
                break
            src.push(_part(b"wake"))
            await asyncio.sleep(0.05)
        assert src.closed >= 1

        # A new viewer reconnects cleanly on a fresh upstream.
        _mt2, gen2 = await b.subscribe()
        assert src.opens == 2
        src.push(_part(b"frameB"))
        src.push(b"--xyz\r\n")
        got2 = await asyncio.wait_for(_take(gen2, 1), timeout=2.0)
        assert got2 == [_part(b"frameB")]
    finally:
        m.IDLE_CLOSE_S = saved
        await b.close()


async def test_quick_resubscribe_keeps_upstream_open() -> None:
    """A viewer returning within the grace window must not tear down the
    shared upstream — quick tab flips and page reloads don't churn the
    printer's scarce camera connection slots."""
    import pycentauri.mjpeg_broadcast as m

    src = _FakeSource()
    saved = m.IDLE_CLOSE_S
    m.IDLE_CLOSE_S = 0.5
    try:
        b = CameraBroadcaster(lambda: "http://x/", open_source=src.open)
        _mt, gen1 = await b.subscribe()
        src.push(_part(b"a"))
        src.push(b"--xyz\r\n")
        await asyncio.wait_for(_take(gen1, 1), timeout=2.0)
        await gen1.aclose()

        await asyncio.sleep(0.1)  # back well within the grace window
        _mt2, gen2 = await b.subscribe()
        assert src.opens == 1  # the upstream was never dropped

        src.push(_part(b"b"))
        src.push(b"--xyz\r\n")
        # The delimiter left buffered when frame "a" completed resurfaces as
        # a leading empty part; frame "b" itself needs its own terminator.
        got = await asyncio.wait_for(_take(gen2, 2), timeout=2.0)
        assert _part(b"b") in got
    finally:
        m.IDLE_CLOSE_S = saved
        await b.close()


async def test_reader_reconnects_after_upstream_drop() -> None:
    opens = 0

    async def flaky_open() -> _Upstream:
        nonlocal opens
        opens += 1
        first = opens == 1

        async def chunks() -> AsyncIterator[bytes]:
            if first:
                yield b"before-drop"
                return  # upstream ends → reader should reconnect
            while True:
                await asyncio.sleep(0.01)
                yield b"after-reconnect"

        async def aclose() -> None: ...

        return _Upstream("mt", chunks(), aclose)

    b = CameraBroadcaster(lambda: "http://x/", open_source=flaky_open)
    _mt, gen = await b.subscribe()
    got = await asyncio.wait_for(_take(gen, 2), timeout=2.0)
    assert b"before-drop" in got or b"after-reconnect" in got
    assert opens >= 2  # reconnected after the first upstream ended
    await b.close()


async def test_stale_upstream_triggers_reconnect() -> None:
    """If the upstream stops sending frames (socket alive, no data), the
    reader must reconnect after STALE_TIMEOUT_S instead of hanging."""
    import pycentauri.mjpeg_broadcast as m

    opens = 0

    async def stalling_open() -> _Upstream:
        nonlocal opens
        opens += 1

        async def chunks() -> AsyncIterator[bytes]:
            yield b"one-frame"
            # Then go silent forever — simulating the observed stall.
            await asyncio.sleep(9999)

        async def aclose() -> None: ...

        return _Upstream("mt", chunks(), aclose)

    saved = m.STALE_TIMEOUT_S
    m.STALE_TIMEOUT_S = 0.3  # speed up the test
    try:
        b = CameraBroadcaster(lambda: "http://x/", open_source=stalling_open)
        _mt, gen = await b.subscribe()
        # The first open delivers one frame, then stalls; the reader should
        # time out and reconnect, giving us a second "one-frame".
        got = await asyncio.wait_for(_take(gen, 2), timeout=3.0)
        assert len(got) == 2
        assert opens >= 2
    finally:
        m.STALE_TIMEOUT_S = saved
        await b.close()


async def test_frame_reassembled_from_split_chunks() -> None:
    """Upstream chunks are arbitrary TCP slices, not frames — a part split
    across several chunks (even mid-boundary) must fan out as one frame."""
    src = _FakeSource()
    b = CameraBroadcaster(lambda: "http://x/", open_source=src.open)
    _mt, gen = await b.subscribe()

    part = _part(b"jpegdata" * 50)
    # Split points: inside the delimiter, inside the headers, mid-body.
    for piece in (part[:2], part[2:20], part[20:80], part[80:]):
        src.push(piece)
    # A second part flushes the first one out of the assembler; a trailing
    # delimiter flushes the second.
    src.push(_part(b"next"))
    src.push(b"--xyz\r\n")
    got = await asyncio.wait_for(_take(gen, 2), timeout=2.0)
    assert got[0] == part
    assert got[1] == _part(b"next")
    await b.close()


class TestFrameAssembler:
    # FrameAssembler now takes the full delimiter line prefix (e.g. b"--xyz"),
    # as returned by _boundary_of().
    def test_drops_preamble_before_first_boundary(self) -> None:
        a = FrameAssembler(b"--xyz")
        assert a.feed(b"preamble garbage\r\n") == []
        assert a.feed(_part(b"one") + _part(b"two")) == [_part(b"one")]

    def test_false_boundary_inside_body_does_not_split(self) -> None:
        # "--xyz" occurring inside JPEG entropy data isn't a boundary line
        # unless followed by CRLF; the frame must survive byte-for-byte.
        body = b"\xff\xd8" + b"--xyz" + b"\xff\xd9" * 20
        a = FrameAssembler(b"--xyz")
        assert a.feed(_part(body)) == []
        assert a.feed(_part(b"after")) == [_part(body)]

    def test_boundary_split_across_feeds(self) -> None:
        a = FrameAssembler(b"--xyz")
        part = _part(b"x")
        assert a.feed(part[:5]) == []
        assert a.feed(part[5:]) == []  # still waiting for the next boundary
        assert a.feed(_part(b"y")) == [part]

    def test_unframed_payload_is_flushed_not_buffered_forever(self) -> None:
        import pycentauri.mjpeg_broadcast as m

        a = FrameAssembler(b"--xyz")
        garbage = b"\x00" * (m._MAX_UNFRAMED + 1)
        out = a.feed(garbage)
        assert len(out) == 1 and len(out[0]) > m._MAX_UNFRAMED
        # Stream continues cleanly afterwards.
        assert a.feed(_part(b"ok")) == []  # needs a closing boundary
        assert a.feed(_part(b"ok2")) == [_part(b"ok")]

    def test_unknown_boundary_falls_back_to_default(self) -> None:
        from pycentauri.mjpeg_broadcast import _boundary_of

        assert _boundary_of("multipart/x-mixed-replace") == b"--frame"
        assert _boundary_of('multipart/x-mixed-replace; boundary="cc"') == b"--cc"
        assert _boundary_of("image/jpeg") is None

    def test_cc1_boundary_quirk(self) -> None:
        """The CC1 declares ``boundary=--foo`` (dashes included) but emits
        plain ``--foo`` lines. The assembler must find those lines — this is
        exactly what the live printer at firmware V1.1.46 sends."""
        from pycentauri.mjpeg_broadcast import _boundary_of

        # The live printer's Content-Type, verbatim.
        assert _boundary_of("multipart/x-mixed-replace; boundary=--foo") == b"--foo"

        def cc1_part(body: bytes) -> bytes:
            head = (
                f"--foo\r\nContent-Type: image/jpeg\r\nContent-Length: {len(body)}\r\n\r\n"
            ).encode()
            return head + body + b"\r\n"

        a = FrameAssembler(_boundary_of("multipart/x-mixed-replace; boundary=--foo"))
        assert a.feed(cc1_part(b"\xff\xd8frame1\xff\xd9")) == []
        assert a.feed(cc1_part(b"\xff\xd8frame2\xff\xd9")) == [cc1_part(b"\xff\xd8frame1\xff\xd9")]
