"""Timers read from other integrations' sensors: Alexa Media Player (`sensor.<echo>_next_timer`).

Fork addition. These integrations have no timer events, only sensors whose attributes list the timers, refreshed
by push or polling. So this source re-reads the picked sensors whenever they change, and works out on its own
when a timer finishes: an alarm at each timer's end time, plus a check for timers that drop off the sensor right
at their end (whichever comes first, once per timer).

Parsing is pure and tolerant: anything it doesn't understand is skipped and logged at debug level, so a format
change in the other integration hides timers instead of breaking the clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .reconcile import SAME_END, ClockTimer

_LOGGER = logging.getLogger(__name__)

ALEXA_PREFIX = "alexa:"

FINISH_WINDOW = timedelta(seconds=3)
"""A timer that drops off its sensor this close to its end time finished (it wasn't cancelled)."""

_FORGET_FINISHED = timedelta(minutes=5)

OnChange = Callable[[], None]
OnFinished = Callable[[str], None]
Parser = Callable[[Mapping[str, Any]], list[ClockTimer]]


# ---------------------------------------------------------------- Alexa Media Player


def parse_alexa(attrs: Mapping[str, Any]) -> list[ClockTimer]:
    """Timers in the attributes of an Alexa Media Player `next_timer` sensor.

    Uses `sorted_active` (falling back to `sorted_all`): a list of notification dicts (older versions: JSON
    strings, or `[id, dict]` pairs). Running timers have status ON and an absolute `triggerTime` in epoch ms;
    without one, `process_timestamp + remainingTime`. Paused timers have status PAUSED. Alarms and reminders
    (the `next_alarm` / `next_reminder` sensors) are skipped by `type`.
    """
    items = _json_list(attrs.get("sorted_active"))
    if items is None:
        items = _json_list(attrs.get("sorted_all")) or []
    processed = _parse_time(attrs.get("process_timestamp"))
    out: list[ClockTimer] = []
    for raw in items:
        item = raw[1] if isinstance(raw, (list, tuple)) and len(raw) == 2 else raw
        if not isinstance(item, Mapping):
            _LOGGER.debug("Skipping an Alexa entry this version does not understand: %r", raw)
            continue
        if str(item.get("type", "")).lower() != "timer" or not item.get("id"):
            continue
        key = ALEXA_PREFIX + str(item["id"])
        name = str(item.get("timerLabel") or item.get("label") or "")
        status = str(item.get("status", "")).upper()
        if status == "PAUSED":
            out.append(ClockTimer(key, None, name))
            continue
        if status != "ON":
            continue
        ends_at = _epoch_ms(item.get("triggerTime"))
        if ends_at is None and processed is not None and _is_number(item.get("remainingTime")):
            ends_at = processed + timedelta(milliseconds=item["remainingTime"])
        if ends_at is None:
            _LOGGER.debug("Skipping an Alexa timer without an end time: %r", item)
            continue
        out.append(ClockTimer(key, ends_at, name))
    return out


def _json_list(value: Any) -> list[Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return list(value) if isinstance(value, (list, tuple)) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _epoch_ms(value: Any) -> datetime | None:
    if not _is_number(value) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, str) and (parsed := dt_util.parse_datetime(value)) and parsed.tzinfo:
        return parsed
    return None


# ---------------------------------------------------------------- the source


class EntityTimers:
    """Timers from the attributes of the sensors the user picked, read with `parser`."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_ids: Iterable[str],
        parser: Parser,
        on_change: OnChange,
        on_finished: OnFinished,
    ) -> None:
        self._hass = hass
        self._entities = list(entity_ids)
        self._parser = parser
        self._on_change = on_change
        self._on_finished = on_finished
        self._unsubs: list[CALLBACK_TYPE] = []
        self._ends: dict[str, datetime] = {}  # running timers at the last read: key -> end time
        self._alarms: dict[str, tuple[datetime, CALLBACK_TYPE]] = {}
        self._finished: dict[str, datetime] = {}  # key -> end time already reported, so it pulses once

    def start(self) -> bool:
        if not self._entities:
            return True
        self._unsubs.append(async_track_state_change_event(self._hass, self._entities, self._state_changed))
        self._sync(dt_util.utcnow(), report=False)  # timers already running at start get their alarms
        return True

    def stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        for _, unsub in self._alarms.values():
            unsub()
        self._alarms.clear()

    def snapshot(self, now: datetime) -> dict[str, ClockTimer]:
        out: dict[str, ClockTimer] = {}
        for entity_id in self._entities:
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            try:
                timers = self._parser(state.attributes)
            except Exception:  # noqa: BLE001  (a format we don't know must not break the clock)
                _LOGGER.debug("Couldn't read timers from %s", entity_id, exc_info=True)
                continue
            for timer in timers:
                out.setdefault(timer.key, timer)
        return out

    @callback
    def _state_changed(self, event: Event) -> None:
        self._sync(dt_util.utcnow(), report=True)
        self._on_change()

    @callback
    def _sync(self, now: datetime, report: bool) -> None:
        """Re-read the sensors: report timers that dropped off at their end, and move the end-time alarms."""
        timers = self.snapshot(now)
        running = {key: t.ends_at for key, t in timers.items() if t.ends_at is not None}
        if report:
            for key, ends_at in self._ends.items():
                if key not in timers and abs(ends_at - now) <= FINISH_WINDOW:
                    self._finish(key, ends_at)
        self._ends = running
        for key in [k for k in self._alarms if k not in running]:
            self._alarms.pop(key)[1]()
        for key, ends_at in running.items():
            alarm = self._alarms.get(key)
            if alarm is not None and abs(alarm[0] - ends_at) < SAME_END:
                continue
            if alarm is not None:
                alarm[1]()
                del self._alarms[key]
            if ends_at > now:
                self._alarms[key] = (ends_at, async_track_point_in_utc_time(self._hass, self._alarm(key), ends_at))
        for key in [k for k, at in self._finished.items() if at < now - _FORGET_FINISHED]:
            del self._finished[key]

    def _alarm(self, key: str) -> Callable[[datetime], None]:
        @callback
        def fire(_: datetime) -> None:
            if (alarm := self._alarms.pop(key, None)) is None:
                return
            self._finish(key, alarm[0])
            self._on_change()

        return fire

    @callback
    def _finish(self, key: str, ends_at: datetime) -> None:
        done = self._finished.get(key)
        if done is not None and abs(done - ends_at) < SAME_END:
            return
        self._finished[key] = ends_at
        self._on_finished(key)
