"""Echo Wall Clock protocol: framing, a tiny protobuf codec, and the messages a real Echo sends.

Vendored from the protocol project's reference library (agt.py). Pure Python, no I/O, no Home Assistant.

SPP frame:  F0 | cmd=02 | err=00 | seq | escaped(payload + checksum16_be) | F1
  checksum = (cmd + err + sum(payload)) & 0xFFFF
  bytes F0/F1/F2 inside payload/checksum are sent as F2, (byte ^ F2)

The clock is strict and silent: messages must match a real Echo's byte for byte (no messageId, no empty
payloads), and wrong messages are ignored without an error.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import NamedTuple

STX, ETX, ESC = 0xF0, 0xF1, 0xF2
RESERVED = (STX, ETX, ESC)
CMD, ERR = 0x02, 0x00

# ---------------------------------------------------------------- framing


def _escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in RESERVED:
            out += bytes([ESC, ESC ^ b])
        else:
            out.append(b)
    return bytes(out)


class Framer:
    """Builds outgoing frames with an incrementing sequence id. A real Echo starts at 1."""

    def __init__(self, first_seq: int = 1) -> None:
        self._seq = first_seq

    def frame(self, payload: bytes) -> bytes:
        checksum = (CMD + ERR + sum(payload)) & 0xFFFF
        seq = self._seq
        # Never use a reserved byte as seq, so it needs no escaping.
        self._seq = (self._seq + 1) & 0xFF
        while self._seq in RESERVED:
            self._seq = (self._seq + 1) & 0xFF
        body = _escape(bytes(payload) + bytes([checksum >> 8, checksum & 0xFF]))
        return bytes([STX, CMD, ERR, seq]) + body + bytes([ETX])


class Frame(NamedTuple):
    """One frame received from the clock."""

    cmd: int
    err: int
    seq: int
    payload: bytes
    checksum_ok: bool


class Deframer:
    """Feed raw stream bytes (split or coalesced in any way); returns complete frames."""

    MAX_FRAME = 4096

    def __init__(self) -> None:
        self._buf: bytearray | None = None
        self._escaped = False

    def feed(self, data: bytes) -> list[Frame]:
        out: list[Frame] = []
        for b in data:
            if b == STX:
                self._buf, self._escaped = bytearray(), False
                continue
            if self._buf is None:
                continue
            if b == ETX:
                if (frame := self._finish(self._buf)) is not None:
                    out.append(frame)
                self._buf = None
                continue
            if self._escaped:
                self._buf.append(ESC ^ b)
                self._escaped = False
            elif b == ESC:
                self._escaped = True
            else:
                self._buf.append(b)
            if len(self._buf) > self.MAX_FRAME:
                self._buf = None  # garbage; wait for the next STX
        return out

    @staticmethod
    def _finish(buf: bytearray) -> Frame | None:
        if len(buf) < 5:
            return None
        cmd, err, seq = buf[0], buf[1], buf[2]
        payload, found = bytes(buf[3:-2]), (buf[-2] << 8) | buf[-1]
        return Frame(cmd, err, seq, payload, found == ((cmd + err + sum(payload)) & 0xFFFF))


# ---------------------------------------------------------------- protobuf


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field_bytes(num: int, value: str | bytes) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    return _varint((num << 3) | 2) + _varint(len(value)) + bytes(value)


def field_varint(num: int, value: int) -> bytes:
    return _varint(num << 3) + _varint(int(value))


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = n = 0
    while True:
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def parse_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """Raw protobuf -> [(field, wiretype, value)]. Raises ValueError or IndexError on malformed input."""
    fields: list[tuple[int, int, int | bytes]] = []
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise ValueError("field 0")
        v: int | bytes
        if wt == 0:
            v, i = _read_varint(data, i)
        elif wt == 2:
            ln, i = _read_varint(data, i)
            if i + ln > len(data):
                raise ValueError("truncated")
            v, i = bytes(data[i : i + ln]), i + ln
        elif wt == 1:
            v, i = int.from_bytes(data[i : i + 8], "little"), i + 8
        elif wt == 5:
            v, i = int.from_bytes(data[i : i + 4], "little"), i + 4
        else:
            raise ValueError(f"wiretype {wt}")
        fields.append((num, wt, v))
    return fields


# ---------------------------------------------------------------- messages (Echo -> clock)


def _directive(namespace: str, name: str, payload: bytes = b"") -> bytes:
    """Message{Directive{Header{namespace, name}, payload}}. Empty fields are omitted, as the Echo does."""
    d = field_bytes(1, field_bytes(1, namespace) + field_bytes(2, name))
    if payload:
        d += field_bytes(2, payload)
    return field_bytes(1, d)


def discover() -> bytes:
    return _directive("Alexa.Discovery", "Discover")


def state_update(states: dict[str, str]) -> bytes:
    payload = b"".join(field_bytes(1, field_bytes(1, k) + field_bytes(2, v)) for k, v in states.items())
    return _directive("Alexa.Gadget.StateListener", "StateUpdate", payload)


def time_info(clock_time: str) -> bytes:
    return state_update({"timeinfo": clock_time})


def timers_active() -> bytes:
    return state_update({"timers": "active"})


def timers_cleared() -> bytes:
    return state_update({"timers": "cleared"})


def set_timer(token: str, end_clock_time: str) -> bytes:
    """Alerts.SetAlert. The scheduled time is the END time, not a duration."""
    payload = field_bytes(1, token) + field_bytes(2, "TIMER") + field_bytes(3, end_clock_time)
    return _directive("Alerts", "SetAlert", payload)


def delete_alert(token: str) -> bytes:
    return _directive("Alerts", "DeleteAlert", field_bytes(1, token))


def set_indicator() -> bytes:
    """Notifications.SetIndicator{persistVisualIndicator=true}: the centre light pulses yellow a few times."""
    return _directive("Notifications", "SetIndicator", field_varint(1, 1))


def clear_indicator() -> bytes:
    return _directive("Notifications", "ClearIndicator")


# ---------------------------------------------------------------- messages (clock -> Echo)


class Event(NamedTuple):
    """An event from the clock. The only one ever seen is Alexa.Discovery / Discover.Response."""

    namespace: str
    name: str
    payload: bytes


def decode_event(payload: bytes) -> Event | None:
    """Message{Event{Header, payload}} -> Event, or None if it isn't one."""
    try:
        outer = parse_fields(payload)
        if not outer or outer[0][1] != 2:
            return None
        inner = {n: v for n, wt, v in parse_fields(outer[0][2]) if wt == 2}  # type: ignore[arg-type]
        header = {n: v for n, wt, v in parse_fields(inner.get(1, b"")) if wt == 2}  # type: ignore[arg-type]
        return Event(
            header.get(1, b"").decode(errors="replace"),  # type: ignore[union-attr]
            header.get(2, b"").decode(errors="replace"),  # type: ignore[union-attr]
            inner.get(2, b""),  # type: ignore[arg-type]
        )
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- values


def clock_time(when: datetime) -> str:
    """Local time with offset, always 25 chars: 2026-10-14T12:00:00-04:00.

    The clock shows the digits as written and ignores the offset, so `when` must already be in the clock's
    local time zone.
    """
    if when.tzinfo is None:
        raise ValueError("clock_time needs an aware datetime")
    return when.replace(microsecond=0).isoformat()


def alert_token(timer_id: str) -> str:
    """A 10-digit decimal token derived from a timer id, so the same timer always gets the same token.

    Same derivation as the Kiosk plugin's AlertToken.
    """
    n = int.from_bytes(hashlib.sha256(timer_id.encode()).digest()[:8], "big")
    return str(1_000_000_000 + n % 9_000_000_000)


class ClockInfo(NamedTuple):
    """The parts of the clock's Discover.Response that are safe to show. The serial and token are dropped."""

    friendly_name: str
    firmware: str
    model: str


def decode_clock_info(event: Event) -> ClockInfo | None:
    """Discover.Response payload: endpoints[0] {2 friendlyName, 12 additionalIdentification {1 firmware, 5 model}}."""
    if (event.namespace, event.name) != ("Alexa.Discovery", "Discover.Response"):
        return None
    try:
        endpoints = [v for n, wt, v in parse_fields(event.payload) if n == 1 and wt == 2]
        if not endpoints:
            return ClockInfo("", "", "")
        endpoint = {n: v for n, wt, v in parse_fields(endpoints[0]) if wt == 2}  # type: ignore[arg-type]
        ident = {n: v for n, wt, v in parse_fields(endpoint.get(12, b"")) if wt == 2}  # type: ignore[arg-type]

        def text(d: dict[int, bytes], n: int) -> str:
            return d.get(n, b"").decode(errors="replace")

        return ClockInfo(text(endpoint, 2), text(ident, 1), text(ident, 5))
    except (ValueError, IndexError):
        return ClockInfo("", "", "")
