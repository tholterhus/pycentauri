"""One shared upstream MJPEG connection, fanned out to many subscribers.

The printer's camera server has very few connection slots and releases them
badly — a closed connection lingers (``FIN-WAIT-2``: the printer never sends
its FIN) and keeps holding a slot. Opening a fresh upstream per browser
request — which the old ``/stream`` did, once per tab *and* once per
client-side reload — exhausts those slots and starves the stream. Observed
live 2026-07-08 on CC1 mid-print: a fresh sole-client connection got zero
frames because stale slots were still held.

:class:`CameraBroadcaster` holds a *single* upstream connection — for as
long as anyone is watching, plus a :data:`IDLE_CLOSE_S` grace window — and
broadcasts whole multipart *frames* to every subscriber, so the printer
only ever sees one camera connection no matter how many browsers, tabs,
or reloads happen. Slow subscribers get old frames dropped rather than
stalling the reader or their peers. When the last subscriber leaves
(every tab hidden or closed — the dashboard aborts its fetch on
``visibilitychange``), the upstream is closed instead of streaming to
nobody.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEFAULT_MEDIA_TYPE = "multipart/x-mixed-replace; boundary=frame"
#: Per-subscriber queue depth, in whole frames. Dropping old frames past this
#: keeps a slow browser from backing up the shared reader. Frames (not raw
#: chunks) so a drop can never tear a hole in the multipart byte stream.
QUEUE_MAX = 16
#: How long ``subscribe()`` waits for the first upstream connect before 502.
CONNECT_WAIT_S = 8.0
#: If no frame arrives from the upstream for this long, consider it stale and
#: reconnect. The printer's camera can silently stop sending while the TCP
#: socket stays open (observed 2026-07-09 on CC1: ESTAB connection, zero
#: frames until service restart).
STALE_TIMEOUT_S = 15.0
#: After the *last* subscriber disconnects, keep the camera upstream open for
#: this long, then close it. The dashboard aborts its stream fetch when the
#: tab is hidden or closed, so "no subscribers" means nobody is watching —
#: and the camera stream is ~2 MB/s, far too expensive to hold forever. A
#: grace window (rather than instant close) so page reloads and quick tab
#: flips don't churn the printer's scarce camera connection slots.
IDLE_CLOSE_S = 30.0
_BACKOFF_START = 1.0
_BACKOFF_MAX = 10.0


class CameraUnavailable(RuntimeError):
    """The upstream camera could not be reached in time."""


_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)
#: If this much arrives with no boundary marker in sight, the payload isn't
#: multipart (or the boundary isn't what the header claims). Flush it as one
#: blob rather than buffering forever — concatenated blobs still reproduce
#: the original byte stream, so the browser's own parser stays in sync.
_MAX_UNFRAMED = 2 * 1024 * 1024


def _boundary_of(media_type: str) -> bytes | None:
    """The multipart boundary *name* from a ``Content-Type``, or ``None`` if
    the payload isn't multipart (the stream is then passed through
    untouched).

    :class:`FrameAssembler` — so the returned value is the full delimiter
    line prefix as it appears in the body. The CC1 declares
    ``boundary=--foo`` — dashes already included — but then emits plain
    ``--foo`` delimiter lines, so the name is stripped of leading dashes
    and ``--`` prepended once; a compliant ``boundary=foo`` yields the
    same ``--foo``.
    """
    if not media_type.lower().startswith("multipart/"):
        return None
    match = _BOUNDARY_RE.search(media_type)
    name = match.group(1) if match else "frame"
    return b"--" + name.lstrip("-").encode("ascii", "ignore")


class FrameAssembler:
    """Reassembles a raw ``multipart/x-mixed-replace`` byte stream into
    whole parts.

    A frame is the bytes from one boundary line to just before the next.
    Reassembling before fanout is what makes slow-subscriber drops safe:
    dropping raw network chunks would punch holes in the byte stream, so the
    browser's multipart parser desyncs (boundaries no longer line up with
    ``Content-Length``) and every later frame renders as garbage.
    """

    def __init__(self, boundary: bytes) -> None:
        # `boundary` is the full delimiter line prefix (e.g. b"--foo"), as
        # returned by _boundary_of().
        self._sep = boundary
        self._buf = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        frames: list[bytes] = []
        while True:
            start = self._buf.find(self._sep)
            if start < 0:
                if len(self._buf) > _MAX_UNFRAMED:
                    frames.append(self._buf)
                    self._buf = b""
                break
            end = self._find_next(start + len(self._sep))
            if end < 0:
                # Frame still streaming in; drop any preamble before it.
                self._buf = self._buf[start:]
                break
            frames.append(self._buf[start:end])
            self._buf = self._buf[end:]
        return frames

    def _find_next(self, from_pos: int) -> int:
        """The next *real* boundary line at or after ``from_pos``, or -1.

        The boundary byte pattern can occur inside JPEG entropy data; a
        genuine boundary line is followed by CRLF (or the terminal ``--``).
        A candidate at the buffer edge stays unresolved until more bytes
        arrive, so it reads as "not found yet".
        """
        buf, sep = self._buf, self._sep
        while True:
            i = buf.find(sep, from_pos)
            if i < 0:
                return -1
            after = i + len(sep)
            if after + 2 > len(buf):
                return -1
            if buf[after : after + 2] in (b"\r\n", b"--"):
                return i
            from_pos = i + 1


@dataclass
class _Upstream:
    media_type: str
    chunks: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]]


class CameraBroadcaster:
    def __init__(
        self,
        url_factory: Callable[[], str],
        *,
        open_source: Callable[[], Awaitable[_Upstream]] | None = None,
        queue_max: int = QUEUE_MAX,
        on_start: Callable[[], Awaitable[None]] | None = None,
        on_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._url_factory = url_factory
        self._open_source = open_source or self._httpx_open
        self._queue_max = queue_max
        self._on_start = on_start
        self._on_stop = on_stop
        self._camera_started = False
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._reader: asyncio.Task[None] | None = None
        self._media_type = DEFAULT_MEDIA_TYPE
        self._connected = asyncio.Event()
        self._idle = asyncio.Event()  # set → reader parks: nobody is watching
        self._idle_timer: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._closing = False

    async def subscribe(self) -> tuple[str, AsyncIterator[bytes]]:
        """Register a subscriber; returns ``(media_type, chunk iterator)``.

        Starts the shared reader on the first subscriber. Raises
        :class:`CameraUnavailable` if the upstream never connects.
        """
        if self._closing:
            raise CameraUnavailable("broadcaster is shutting down")
        self._idle.clear()
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        first_subscriber = not self._subscribers
        if first_subscriber and self._on_start is not None:
            try:
                await self._on_start()
            except Exception as err:
                log.warning("camera stream enable failed: %r", err)
                raise CameraUnavailable(f"camera stream could not be enabled: {err}") from err
            self._camera_started = True
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._queue_max)
        self._subscribers.add(queue)
        if self._reader is None or self._reader.done():
            self._connected.clear()
            self._reader = asyncio.create_task(self._reader_loop(), name="pycentauri-mjpeg")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=CONNECT_WAIT_S)
        except (TimeoutError, asyncio.TimeoutError) as err:
            self._subscribers.discard(queue)
            self._subscriber_left()
            raise CameraUnavailable("camera did not start streaming in time") from err
        return self._media_type, self._drain(queue)

    def _subscriber_left(self) -> None:
        """A subscriber is gone. If it was the last one, arm the timer that
        eventually parks the reader and closes the camera upstream
        (see :data:`IDLE_CLOSE_S`).
        """
        if self._subscribers or self._closing:
            return
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = asyncio.get_running_loop().call_later(IDLE_CLOSE_S, self._idle.set)
        on_stop = self._on_stop
        if on_stop is not None and self._camera_started:

            async def stop_when_idle() -> None:
                await self._idle.wait()
                if not self._subscribers and self._camera_started:
                    try:
                        await on_stop()
                    except Exception as err:
                        log.warning("camera stream disable failed: %r", err)
                    finally:
                        self._camera_started = False

            self._idle_task = asyncio.create_task(
                stop_when_idle(), name="pycentauri-camera-disable"
            )

    async def _drain(self, queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:  # shutdown sentinel
                    return
                yield chunk
        finally:
            self._subscribers.discard(queue)
            self._subscriber_left()

    def _fanout(self, chunk: bytes) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # Slow consumer: drop the oldest frame, keep the newest.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(chunk)

    async def _reader_loop(self) -> None:
        backoff = _BACKOFF_START
        while not self._closing and not self._idle.is_set():
            try:
                upstream = await self._open_source()
            except Exception as err:
                log.warning("camera upstream connect failed: %r", err)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue
            self._media_type = upstream.media_type
            self._connected.set()
            backoff = _BACKOFF_START
            boundary = _boundary_of(upstream.media_type)
            assembler = FrameAssembler(boundary) if boundary is not None else None
            try:
                it = upstream.chunks.__aiter__()
                while not self._closing and not self._idle.is_set():
                    try:
                        chunk = await asyncio.wait_for(it.__anext__(), timeout=STALE_TIMEOUT_S)
                    except (TimeoutError, asyncio.TimeoutError):
                        log.warning(
                            "camera upstream stale (%ds no frame), reconnecting", STALE_TIMEOUT_S
                        )
                        break
                    except StopAsyncIteration:
                        break
                    # Multipart: fan out whole frames only. Anything else
                    # (plain MJPEG-ish payload) passes through untouched.
                    pieces = assembler.feed(chunk) if assembler else [chunk]
                    for frame in pieces:
                        self._fanout(frame)
            except Exception as err:
                log.warning("camera upstream read ended: %r", err)
            finally:
                await upstream.aclose()
            # Upstream dropped or stale; reconnect (unless we're shutting down).
            self._connected.clear()
            if self._idle.is_set() and not self._closing:
                log.info("no subscribers for %.0fs — camera upstream closed", IDLE_CLOSE_S)

    async def _httpx_open(self) -> _Upstream:
        url = self._url_factory()
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)
        )
        cm = client.stream("GET", url)
        resp = await cm.__aenter__()
        if resp.status_code != 200:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            await client.aclose()
            raise CameraUnavailable(f"camera returned HTTP {resp.status_code}")
        media_type = resp.headers.get("content-type", DEFAULT_MEDIA_TYPE)

        async def aclose() -> None:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            with contextlib.suppress(Exception):
                await client.aclose()

        return _Upstream(media_type, resp.aiter_raw(), aclose)

    async def close(self) -> None:
        self._closing = True
        if self._on_stop is not None and self._camera_started:
            with contextlib.suppress(Exception):
                await self._on_stop()
            self._camera_started = False
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_task
        self._idle_task = None
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        # Release every subscriber's generator.
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        self._subscribers.clear()
