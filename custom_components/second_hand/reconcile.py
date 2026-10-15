"""What to send the clock, and when. Pure: snapshots and the time go in, commands come out.

The rest of the integration computes the *desired* state (which timers the clock should show, and when they
end). The reconciler compares it with what the clock was last told and produces the frames to close the gap,
mirroring what a real Echo sends (protocol notes, sections 5b and 6):

- timer shown, or its end changed (resume, time added or removed): SetAlert, same token for the same timer;
- timer paused or cancelled: DeleteAlert;
- timer ended by itself: nothing (the clock's segments go out on their own);
- timer finished: StateUpdate timers=active, once, about 1 s after the end;
- every connect: timeinfo, ClearIndicator, then SetAlert for every timer still to show;
- timeinfo again on every half hour of local time.

Nothing is sent while disconnected, and nothing is replayed late: a timer that finished while the link was
down is not pulsed after reconnecting. Not thread-safe; call it from the event loop only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import NamedTuple

from . import protocol

RING_DELAY = timedelta(seconds=1)
"""A real Echo sends timers=active about a second after the end time."""

SAME_END = timedelta(seconds=2)
"""End times closer than this are the same (HA's timers only have whole-second precision)."""

ENDED_GRACE = timedelta(seconds=2)
"""A timer that disappears within this long of its end ended by itself; it isn't deleted."""

HALF_HOUR = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class ClockTimer:
    """A timer the clock should know about.

    `key` is unique across sources ("assist:<id>", "helper:timer.kitchen") and gives the clock token.
    `ends_at` is an aware datetime; for a paused timer it is None.
    """

    key: str
    ends_at: datetime | None
    name: str = ""

    @property
    def paused(self) -> bool:
        return self.ends_at is None

    @property
    def token(self) -> str:
        return protocol.alert_token(self.key)


class Command(NamedTuple):
    """One message for the clock: a short label for logs and tests, and the protobuf payload to frame."""

    label: str
    payload: bytes


class Reconciler:
    """Turns desired-state snapshots into clock commands."""

    def __init__(self, zone: tzinfo) -> None:
        self._zone = zone
        self._connected = False
        self._shown: dict[str, datetime] = {}  # key -> end time the clock was told
        self._ring_due: dict[str, datetime] = {}  # key -> when to send timers=active
        self._next_time_info: datetime | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def shown_count(self) -> int:
        return len(self._shown)

    def on_connected(self, now: datetime, timers: Mapping[str, ClockTimer]) -> list[Command]:
        """The link is up and the Discover exchange is done."""
        self._connected = True
        self._shown.clear()
        self._ring_due.clear()
        self._next_time_info = self._next_half_hour(now)
        out = [self._time_info(now), Command("ClearIndicator", protocol.clear_indicator())]
        out += self.update(now, timers)
        return out

    def on_disconnected(self) -> None:
        self._connected = False
        self._shown.clear()
        self._ring_due.clear()
        self._next_time_info = None

    def update(self, now: datetime, timers: Mapping[str, ClockTimer]) -> list[Command]:
        """The desired timers changed (or may have). Returns what to send to match them."""
        if not self._connected:
            return []
        out: list[Command] = []
        visible = {
            key: t.ends_at
            for key, t in timers.items()
            if t.ends_at is not None and t.ends_at > now and key not in self._ring_due
        }
        for key, ends_at in visible.items():
            shown = self._shown.get(key)
            if shown is None or abs(shown - ends_at) >= SAME_END:
                self._shown[key] = ends_at
                out.append(self._set_alert(key, ends_at))
        for key in [k for k in self._shown if k not in visible]:
            ended = self._shown.pop(key)
            if ended > now + ENDED_GRACE:  # paused or cancelled early; otherwise it ended by itself
                out.append(Command("DeleteAlert", protocol.delete_alert(protocol.alert_token(key))))
        return out

    def on_finished(self, now: datetime, key: str) -> list[Command]:
        """A timer reached zero. The pulse goes out on the next tick at or after now + RING_DELAY."""
        if self._connected:
            self._shown.pop(key, None)
            self._ring_due[key] = now + RING_DELAY
        return []

    def tick(self, now: datetime) -> list[Command]:
        """Call when next_due() has passed."""
        if not self._connected:
            return []
        out: list[Command] = []
        due = [key for key, at in self._ring_due.items() if at <= now]
        for key in due:
            del self._ring_due[key]
        if due:
            out.append(Command("timers=active", protocol.timers_active()))  # one pulse burst covers them all
        if self._next_time_info is not None and now >= self._next_time_info:
            out.append(self._time_info(now))
            self._next_time_info = self._next_half_hour(now)
        return out

    def next_due(self) -> datetime | None:
        """When tick() next has something to do, or None."""
        times = list(self._ring_due.values())
        if self._next_time_info is not None:
            times.append(self._next_time_info)
        return min(times, default=None)

    def resync_time(self, now: datetime) -> list[Command]:
        """Send timeinfo now (for example after a DST change or on request)."""
        return [self._time_info(now)] if self._connected else []

    def _set_alert(self, key: str, ends_at: datetime) -> Command:
        end = protocol.clock_time(ends_at.astimezone(self._zone))
        return Command("SetAlert TIMER", protocol.set_timer(protocol.alert_token(key), end))

    def _time_info(self, now: datetime) -> Command:
        return Command("timeinfo", protocol.time_info(protocol.clock_time(now.astimezone(self._zone))))

    def _next_half_hour(self, now: datetime) -> datetime:
        """The next :00 or :30 of local time after now, in UTC.

        Done in UTC with the offset at `now`: Python compares and subtracts datetimes that share a tzinfo by
        their wall-clock digits, which is wrong across a DST change.
        """
        utc = now.astimezone(UTC)
        offset = now.astimezone(self._zone).utcoffset() or timedelta(0)
        local = (utc + offset).replace(tzinfo=None)
        step = HALF_HOUR - timedelta(minutes=local.minute % 30, seconds=local.second, microseconds=local.microsecond)
        return utc + step
