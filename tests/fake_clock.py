"""A fake Echo Wall Clock on the other end of a Connection: answers Discover, records everything else."""

from __future__ import annotations

import asyncio

from custom_components.second_hand import protocol as p


def discover_response(firmware: str = "102") -> bytes:
    """A synthetic Discover.Response shaped like the real one (made-up serial and token)."""
    ident = p.field_bytes(1, firmware) + p.field_bytes(2, "0" * 64) + p.field_bytes(3, "1") + p.field_bytes(5, "AlexaGadgetsEWC")
    endpoint = p.field_bytes(1, "TEST000000000000") + p.field_bytes(2, "EchoWallClk") + p.field_bytes(12, ident)
    header = p.field_bytes(1, "Alexa.Discovery") + p.field_bytes(2, "Discover.Response")
    event = p.field_bytes(1, header) + p.field_bytes(2, p.field_bytes(1, endpoint))
    return p.field_bytes(1, event)


def label(payload: bytes) -> str:
    known = {
        p.discover(): "Discover",
        p.clear_indicator(): "ClearIndicator",
        p.timers_active(): "timers=active",
        p.timers_cleared(): "timers=cleared",
        p.set_indicator(): "SetIndicator",
    }
    if payload in known:
        return known[payload]
    for word in ("timeinfo", "SetAlert", "DeleteAlert"):
        if word.encode() in payload:
            return word
    return payload.hex()


class FakeClockConnection:
    """One connection to the fake clock."""

    def __init__(self, clock: FakeClock, answer: bool = True) -> None:
        self._clock = clock
        self._answer = answer
        self._incoming: asyncio.Queue[bytes] = asyncio.Queue()
        self._deframer = p.Deframer()
        self._framer = p.Framer(0)  # the clock's first reply used seq 0
        self.closed = False

    async def read(self) -> bytes:
        if self.closed:
            return b""
        return await self._incoming.get()

    async def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionResetError(104, "Connection reset by peer")
        for frame in self._deframer.feed(data):
            assert frame.checksum_ok
            self._clock.received.append(label(frame.payload))
            self._clock.seqs.append(frame.seq)
            if frame.payload == p.discover() and self._answer:
                wire = self._framer.frame(discover_response())
                # The real clock's reply arrived split across two reads.
                self._incoming.put_nowait(wire[:40])
                self._incoming.put_nowait(wire[40:])

    def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait(b"")

    def drop(self) -> None:
        """The clock goes away (battery pull, out of range)."""
        self.close()


class FakeClock:
    """Plays each connect attempt from a script: "ok", "fail" (connect error) or "silent" (no Discover.Response)."""

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        self.received: list[str] = []
        self.seqs: list[int] = []
        self.attempts = 0
        self.current: FakeClockConnection | None = None

    async def open(self) -> FakeClockConnection:
        self.attempts += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "fail":
            raise ConnectionResetError(104, "Connection reset by peer")
        self.current = FakeClockConnection(self, answer=step != "silent")
        return self.current
