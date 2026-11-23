"""Where timers come from: Assist (voice satellite) timers, timer helpers and timer sensors from other integrations.

Each source reports the timers the user picked for a clock as ClockTimers, and calls back when they change
or finish. Keys are unique across sources and stable for the life of a timer, so a timer keeps its clock token
through pause, resume and reconnects.

Assist timers have no public API to observe (checked against HA 2026.10): the intent integration's
TimerManager keeps `timers` (id -> TimerInfo) and `handlers` (device_id -> one handler per satellite), fires no
bus events, and has no "list all timers" call. So this module reads `timers` and *taps* `handlers`: it swaps
the dict for a subclass that wraps every handler set into it, calls the satellite's original handler first,
then tells us. Everything that touches those internals is in this file and checked by `assist_supported()`;
if HA changes them, the Assist source stays off and a repair issue says why, and nothing else breaks.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import json
import logging
import time
from typing import Any

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .reconcile import ClockTimer

_LOGGER = logging.getLogger(__name__)

ASSIST_PREFIX = "assist:"
HELPER_PREFIX = "helper:"
SENSOR_PREFIX = "sensor:"

TIMER_DATA = "intent.timer"  # homeassistant.components.intent.const.TIMER_DATA
_TAP = f"{DOMAIN}_assist_tap"

OnChange = Callable[[], None]
OnFinished = Callable[[str], None]


# ---------------------------------------------------------------- Assist timers (HA internals)


def _timer_manager(hass: HomeAssistant) -> Any | None:
    return hass.data.get(TIMER_DATA)


def assist_supported(hass: HomeAssistant) -> bool:
    """True if HA's TimerManager still looks the way this integration expects."""
    tm = _timer_manager(hass)
    return (
        tm is not None
        and isinstance(getattr(tm, "timers", None), dict)
        and isinstance(getattr(tm, "handlers", None), dict)
        and callable(getattr(tm, "register_handler", None))
    )


def timer_devices(hass: HomeAssistant) -> list[str]:
    """Device ids of the voice satellites that can have timers right now."""
    tm = _timer_manager(hass)
    if not assist_supported(hass):
        return []
    return list(tm.handlers)


class _Tap:
    """Shared by every clock: wraps satellites' timer handlers and fans events out to listeners."""

    def __init__(self, tm: Any) -> None:
        self.tm = tm
        self.listeners: list[Callable[[str, Any], None]] = []
        self._original_dict = tm.handlers
        tm.handlers = _TappedHandlers(self, tm.handlers)

    def wrap(self, handler: Callable[..., None]) -> Callable[..., None]:
        if getattr(handler, "_second_hand_original", None) is not None:
            return handler

        def tapped(event_type: Any, timer: Any) -> None:
            try:
                handler(event_type, timer)
            finally:
                self._notify(str(event_type), timer)

        tapped._second_hand_original = handler  # type: ignore[attr-defined]
        return tapped

    def _notify(self, event_type: str, timer: Any) -> None:
        for listener in list(self.listeners):
            try:
                listener(event_type, timer)
            except Exception:  # never let a clock problem break a satellite
                _LOGGER.exception("Second Hand timer listener failed")

    def remove(self) -> None:
        """Put the plain dict and the original handlers back."""
        current = self.tm.handlers
        self.tm.handlers = self._original_dict
        self._original_dict.clear()
        for device_id, handler in dict.items(current):
            self._original_dict[device_id] = getattr(handler, "_second_hand_original", handler)


class _TappedHandlers(dict):
    """TimerManager.handlers replacement: every handler set into it is wrapped by the tap."""

    def __init__(self, tap: _Tap, existing: dict[str, Any]) -> None:
        super().__init__()
        self._tap = tap
        for device_id, handler in existing.items():
            self[device_id] = handler

    def __setitem__(self, device_id: str, handler: Any) -> None:
        super().__setitem__(device_id, self._tap.wrap(handler))


class AssistTimers:
    """Assist timers from the satellites the user picked."""

    def __init__(self, hass: HomeAssistant, device_ids: list[str], on_change: OnChange, on_finished: OnFinished) -> None:
        self._hass = hass
        self._devices = set(device_ids)
        self._on_change = on_change
        self._on_finished = on_finished
        self._running = False

    def start(self) -> bool:
        """Start listening. False if HA's internals changed (the caller raises a repair issue)."""
        if not self._devices:
            return True
        if not assist_supported(self._hass):
            return False
        tap: _Tap | None = self._hass.data.get(_TAP)
        if tap is None or tap.tm is not _timer_manager(self._hass):
            tap = self._hass.data[_TAP] = _Tap(_timer_manager(self._hass))
        tap.listeners.append(self._event)
        self._running = True
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        tap: _Tap | None = self._hass.data.get(_TAP)
        if tap is None:
            return
        if self._event in tap.listeners:
            tap.listeners.remove(self._event)
        if not tap.listeners:
            tap.remove()
            self._hass.data.pop(_TAP, None)

    def snapshot(self, now: datetime) -> dict[str, ClockTimer]:
        if not self._running:
            return {}
        tm = _timer_manager(self._hass)
        out: dict[str, ClockTimer] = {}
        mono = time.monotonic_ns()
        for timer in list(tm.timers.values()):
            try:
                if getattr(timer, "conversation_command", None) or timer.device_id not in self._devices:
                    continue
                key = ASSIST_PREFIX + timer.id
                if not timer.is_active:
                    out[key] = ClockTimer(key, None, timer.name or "")
                    continue
                left_ns = timer.updated_at + timer.seconds * 1_000_000_000 - mono
                ends_at = now + timedelta(microseconds=max(0, left_ns) // 1000)
                out[key] = ClockTimer(key, ends_at, timer.name or "")
            except (AttributeError, TypeError):
                _LOGGER.debug("Skipping a timer this version does not understand: %r", timer)
        return out

    @callback
    def _event(self, event_type: str, timer: Any) -> None:
        if getattr(timer, "device_id", None) not in self._devices or getattr(timer, "conversation_command", None):
            return
        if event_type == "finished":
            self._on_finished(ASSIST_PREFIX + timer.id)
        self._on_change()


# ---------------------------------------------------------------- timer helpers (public API)


class HelperTimers:
    """`timer.*` helper entities the user picked."""

    def __init__(self, hass: HomeAssistant, entity_ids: list[str], on_change: OnChange, on_finished: OnFinished) -> None:
        self._hass = hass
        self._entities = list(entity_ids)
        self._on_change = on_change
        self._on_finished = on_finished
        self._unsubs: list[Callable[[], None]] = []

    def start(self) -> bool:
        if not self._entities:
            return True
        self._unsubs.append(async_track_state_change_event(self._hass, self._entities, self._state_changed))
        self._unsubs.append(self._hass.bus.async_listen("timer.finished", self._finished, self._is_ours))
        return True

    def stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def snapshot(self, now: datetime) -> dict[str, ClockTimer]:
        out: dict[str, ClockTimer] = {}
        for entity_id in self._entities:
            state = self._hass.states.get(entity_id)
            if (timer := _helper_timer(entity_id, state)) is not None:
                out[timer.key] = timer
        return out

    @callback
    def _is_ours(self, event_data: dict[str, Any]) -> bool:
        return event_data.get("entity_id") in self._entities

    @callback
    def _finished(self, event: Event) -> None:
        self._on_finished(HELPER_PREFIX + event.data["entity_id"])
        self._on_change()

    @callback
    def _state_changed(self, event: Event) -> None:
        self._on_change()


def _helper_timer(entity_id: str, state: State | None) -> ClockTimer | None:
    if state is None:
        return None
    key = HELPER_PREFIX + entity_id
    name = state.attributes.get("friendly_name", "")
    if state.state == "paused":
        return ClockTimer(key, None, name)
    if state.state == "active" and (ends := dt_util.parse_datetime(str(state.attributes.get("finishes_at", "")))):
        return ClockTimer(key, ends, name)
    return None


# ---------------------------------------------------------------- timer sensors from other integrations (public API)

STALE_AFTER = timedelta(minutes=1)
"""A sensor timer whose end is longer ago than this is dropped (the other integration may be slow to clear it)."""

SAME_RING = timedelta(seconds=5)

LATE_RING = timedelta(seconds=10)
"""An end further back than this is not pulsed: like the other sources, nothing is replayed late."""


class SensorTimers:
    """Timestamp sensors the user picked, for timers Home Assistant doesn't run itself (Alexa, for example).

    A plain timestamp sensor is one timer that ends at its state. A sensor with Alexa Media Player's
    `sorted_all` / `sorted_active` attributes (`sensor.<echo>_next_timer`) gives every timer of that Echo,
    paused ones included. Those sensors fire no "finished" event, so this source schedules its own at each end.
    """

    def __init__(self, hass: HomeAssistant, entity_ids: list[str], on_change: OnChange, on_finished: OnFinished) -> None:
        self._hass = hass
        self._entities = list(entity_ids)
        self._on_change = on_change
        self._on_finished = on_finished
        self._unsubs: list[Callable[[], None]] = []
        self._alarm: Callable[[], None] | None = None
        self._rung: set[str] = set()  # keys already reported as finished in their current run

    def start(self) -> bool:
        if not self._entities:
            return True
        self._unsubs.append(async_track_state_change_event(self._hass, self._entities, self._state_changed))
        self._schedule()
        return True

    def stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        self._cancel_alarm()

    def snapshot(self, now: datetime) -> dict[str, ClockTimer]:
        out: dict[str, ClockTimer] = {}
        for entity_id in self._entities:
            for timer in sensor_timers(entity_id, self._hass.states.get(entity_id)):
                if timer.ends_at is None or timer.ends_at > now - STALE_AFTER:
                    out[timer.key] = timer
        return out

    @callback
    def _state_changed(self, event: Event) -> None:
        self._schedule()
        self._on_change()

    @callback
    def _schedule(self) -> None:
        """Wake up at the next end time of a running timer."""
        self._cancel_alarm()
        now = dt_util.utcnow()
        timers = self.snapshot(now)
        # A timer rings once per run. Alexa may keep listing a ringing timer, its computed end moving with each
        # refresh, so forget the ring only once the timer is gone, paused, or set to end well in the future again.
        for key in list(self._rung):
            timer = timers.get(key)
            if timer is None or timer.ends_at is None or timer.ends_at > now + SAME_RING:
                self._rung.discard(key)
        ends = [t.ends_at for t in timers.values() if t.ends_at is not None and t.ends_at > now]
        if ends:
            self._alarm = async_track_point_in_utc_time(self._hass, self._ring, min(ends))

    @callback
    def _cancel_alarm(self) -> None:
        if self._alarm is not None:
            self._alarm()
            self._alarm = None

    @callback
    def _ring(self, _now: datetime) -> None:
        self._alarm = None
        now = dt_util.utcnow()
        rang = False
        for timer in self.snapshot(now).values():
            if timer.ends_at is None or not now - LATE_RING <= timer.ends_at <= now + timedelta(seconds=1):
                continue
            if timer.key in self._rung:
                continue
            self._rung.add(timer.key)
            self._on_finished(timer.key)
            rang = True
        self._schedule()
        if rang:
            self._on_change()


def sensor_timers(entity_id: str, state: State | None) -> list[ClockTimer]:
    """The timers one sensor stands for."""
    if state is None:
        return []
    name = str(state.attributes.get("friendly_name", ""))
    entries = _alexa_entries(state.attributes)
    if entries is None:
        ends = dt_util.parse_datetime(state.state) if state.state else None
        return [ClockTimer(SENSOR_PREFIX + entity_id, ends, name)] if ends and ends.tzinfo else []
    base = dt_util.parse_datetime(str(state.attributes.get("process_timestamp") or ""))
    out = []
    for index, entry in enumerate(entries):
        try:
            status = str(entry.get("status", "")).upper()
            if status not in ("ON", "PAUSED"):
                continue
            key = f"{SENSOR_PREFIX}{entity_id}:{entry.get('id') or index}"
            label = str(entry.get("timerLabel") or entry.get("label") or name)
            if status == "PAUSED":
                out.append(ClockTimer(key, None, label))
            elif (ends := _alexa_end(entry, base)) is not None:
                out.append(ClockTimer(key, ends, label))
        except (AttributeError, TypeError, ValueError):
            _LOGGER.debug("Skipping a timer from %s this version does not understand: %r", entity_id, entry)
    return out


def _alexa_entries(attributes: Any) -> list[dict[str, Any]] | None:
    """Alexa Media Player's timer list, or None if the sensor doesn't have one.

    `sorted_all` (paused timers too) is preferred over `sorted_active`. Older versions put a JSON string of
    [id, timer] pairs there; newer ones a list of timer dicts.
    """
    for name in ("sorted_all", "sorted_active"):
        raw = attributes.get(name)
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return []
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
                item = {"id": item[0], **item[1]}
            if isinstance(item, dict):
                out.append(item)
        return out
    return None


def _alexa_end(entry: dict[str, Any], base: datetime | None) -> datetime | None:
    """When a running Alexa timer ends: the time of that data plus remainingTime (ms), else triggerTime."""
    remaining = entry.get("remainingTime")
    if base is not None and base.tzinfo is not None and isinstance(remaining, (int, float)):
        return dt_util.as_utc(base) + timedelta(milliseconds=remaining)
    if isinstance(remaining, str) and (ends := dt_util.parse_datetime(remaining)) and ends.tzinfo:
        return dt_util.as_utc(ends)
    trigger = entry.get("triggerTime")
    if isinstance(trigger, (int, float)) and trigger > 0:
        return datetime.fromtimestamp(trigger / 1000, dt_util.UTC)
    return None
