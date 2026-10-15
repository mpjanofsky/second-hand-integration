"""The connection to one clock: connect, handshake, hold the link, retry. Transport-agnostic.

Behaviour learned on real hardware (protocol notes; Kiosk plugin spike and soak):
- Connect to RFCOMM channel 4, send Discover, wait for Discover.Response, then the clock is ready.
- Hold the link like a real Echo; the controller sends timeinfo every half hour, which also keeps it alive
  (a link left quiet died after about 31 minutes).
- The first connect right after pairing often fails once; the retry works.
- When the link is down the clock calls in now and then; a call-in wakes the retry loop immediately.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import Protocol

from . import protocol

_LOGGER = logging.getLogger(__name__)

BACKOFF = (5, 10, 20, 40, 60)
CONNECT_TIMEOUT = 45.0
HANDSHAKE_TIMEOUT = 10.0


class Connection(Protocol):
    """An open byte stream to the clock (an RFCOMM socket on Linux, a fake in tests)."""

    async def read(self) -> bytes:
        """Some bytes, or b"" once the connection is closed."""

    async def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


Opener = Callable[[], Awaitable[Connection]]


class LinkState(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    WAITING = "waiting"  # last attempt failed or the link dropped; retrying after a pause


@dataclass(slots=True)
class LinkStatus:
    state: LinkState = LinkState.STOPPED
    reason: str = ""
    info: protocol.ClockInfo | None = None


class ClockLinkError(Exception):
    """The clock didn't answer the way a clock does."""


class BluetoothLink:
    """Implements controller.ClockLink on top of an Opener."""

    def __init__(self, opener: Opener, on_status: Callable[[LinkStatus], None] | None = None) -> None:
        self._opener = opener
        self._on_status = on_status
        self.status = LinkStatus()
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._ready = False
        self._on_up: Callable[[], None] = lambda: None
        self._on_down: Callable[[], None] = lambda: None

    # ------------------------------------------------------------ ClockLink

    def start(self, on_up: Callable[[], None], on_down: Callable[[], None]) -> None:
        self._on_up, self._on_down = on_up, on_down
        self._task = asyncio.get_running_loop().create_task(self._run(), name="Second Hand link")

    def send(self, payload: bytes) -> None:
        if self._ready:
            self._queue.put_nowait(payload)

    async def async_stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._set(LinkState.STOPPED)

    # ------------------------------------------------------------ extras

    def wake(self) -> None:
        """Try to connect now (the clock called in, or the user asked)."""
        self._wake.set()

    # ------------------------------------------------------------ the owner task

    async def _run(self) -> None:
        failures = 0
        while True:
            self._set(LinkState.CONNECTING)
            conn: Connection | None = None
            try:
                conn = await asyncio.wait_for(self._opener(), CONNECT_TIMEOUT)
                info = await asyncio.wait_for(self._handshake(conn), HANDSHAKE_TIMEOUT)
            except (OSError, TimeoutError, ClockLinkError) as err:
                if conn is not None:
                    conn.close()
                failures += 1
                await self._pause(failures, _reason(err))
                continue
            failures = 0
            self._set(LinkState.CONNECTED, info=info)
            reason = await self._session(conn)
            await self._pause(1, reason)

    async def _handshake(self, conn: Connection) -> protocol.ClockInfo:
        self._framer, self._deframer, info = await handshake(conn)
        return info

    async def _session(self, conn: Connection) -> str:
        """Run until the connection drops. Returns why."""
        self._queue = asyncio.Queue()
        self._ready = True
        reader = asyncio.create_task(self._reader(conn))
        writer = asyncio.create_task(self._writer(conn))
        try:
            self._on_up()
            done, _ = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            reason = next(iter(done)).result()
        except OSError as err:
            reason = _reason(err)
        finally:
            self._ready = False
            for task in (reader, writer):
                task.cancel()
            for task in (reader, writer):
                with contextlib.suppress(asyncio.CancelledError, OSError):
                    await task
            conn.close()
            if self.status.state is LinkState.CONNECTED:
                self._on_down()
        return reason

    async def _reader(self, conn: Connection) -> str:
        try:
            while data := await conn.read():
                for frame in self._deframer.feed(data):
                    _LOGGER.debug("From the clock: %s", frame.payload.hex(" "))
        except OSError as err:
            return _reason(err)
        return "The clock closed the connection"

    async def _writer(self, conn: Connection) -> str:
        try:
            while True:
                payload = await self._queue.get()
                await conn.write(self._framer.frame(payload))
        except OSError as err:
            return _reason(err)

    async def _pause(self, failures: int, reason: str) -> None:
        delay = BACKOFF[min(failures, len(BACKOFF)) - 1]
        self._set(LinkState.WAITING, reason=reason)
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), delay)

    def _set(self, state: LinkState, reason: str = "", info: protocol.ClockInfo | None = None) -> None:
        was_connected = self.status.state is LinkState.CONNECTED
        self.status = LinkStatus(state, reason, info or self.status.info)
        if was_connected and state is not LinkState.CONNECTED:
            _LOGGER.info("Clock link down: %s", reason or state)
        if self._on_status is not None:
            self._on_status(self.status)


async def handshake(conn: Connection) -> tuple[protocol.Framer, protocol.Deframer, protocol.ClockInfo]:
    """Send Discover and wait for Discover.Response. Returns the framer and deframer to keep using."""
    framer, deframer = protocol.Framer(1), protocol.Deframer()
    await conn.write(framer.frame(protocol.discover()))
    while True:
        data = await conn.read()
        if not data:
            raise ClockLinkError("The clock closed the connection during setup")
        for frame in deframer.feed(data):
            if not frame.checksum_ok:
                continue
            event = protocol.decode_event(frame.payload)
            if event is not None and (info := protocol.decode_clock_info(event)) is not None:
                return framer, deframer, info


def _reason(err: BaseException) -> str:
    if isinstance(err, TimeoutError):
        return "The clock didn't answer in time"
    if isinstance(err, ClockLinkError):
        return str(err)
    if isinstance(err, OSError) and err.strerror:
        return err.strerror
    return str(err) or type(err).__name__
