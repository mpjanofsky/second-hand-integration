"""One clock: timer sources in, desired state, reconciler, and the link to the clock out."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Protocol

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ISSUE_ASSIST_UNSUPPORTED, POLL_SECONDS
from .reconcile import ClockTimer, Command, Reconciler
from .sources import AssistTimers, HelperTimers

_LOGGER = logging.getLogger(__name__)


class ClockLink(Protocol):
    """The connection to one clock (Bluetooth comes in a later milestone).

    The link owns framing and the Discover exchange. It calls `on_up` once the clock has answered Discover,
    and `on_down` when the connection is lost. `send` queues one protobuf payload; frames go out in order.
    """

    def start(self, on_up: Callable[[], None], on_down: Callable[[], None]) -> None: ...

    def send(self, payload: bytes) -> None: ...

    async def async_stop(self) -> None: ...


class ClockController:
    """Keeps one clock in step with the timers the user picked for it."""

    def __init__(
        self,
        hass: HomeAssistant,
        assist_devices: list[str],
        helpers: list[str],
        link: ClockLink | None = None,
    ) -> None:
        self.hass = hass
        self._reconciler = Reconciler(dt_util.get_default_time_zone())
        self._assist = AssistTimers(hass, assist_devices, self._changed, self._finished)
        self._helpers = HelperTimers(hass, helpers, self._changed, self._finished)
        self._link = link
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._tick_unsub: CALLBACK_TYPE | None = None
        self._update_pending = False
        self.timers: dict[str, ClockTimer] = {}
        self.link_status: object | None = None

    @property
    def connected(self) -> bool:
        return self._reconciler.connected

    @property
    def has_link(self) -> bool:
        return self._link is not None

    @callback
    def attach_link(self, link: ClockLink) -> None:
        """Set the link before async_start (it is created after the controller so it can report status here)."""
        self._link = link

    @callback
    def link_status_changed(self, status: object) -> None:
        self.link_status = status
        self._notify()

    async def async_start(self) -> None:
        self._start_sources()
        self._unsubs.append(
            async_track_time_interval(self.hass, self._poll, timedelta(seconds=POLL_SECONDS), name="Second Hand poll")
        )
        self._refresh(dt_util.utcnow())
        if self._link is not None:
            self._link.start(self._link_up, self._link_down)

    @callback
    def update_sources(self, assist_devices: list[str], helpers: list[str]) -> None:
        """Change which timers are shown without touching the link to the clock."""
        self._assist.stop()
        self._helpers.stop()
        self._assist = AssistTimers(self.hass, assist_devices, self._changed, self._finished)
        self._helpers = HelperTimers(self.hass, helpers, self._changed, self._finished)
        self._start_sources()
        self._refresh(dt_util.utcnow())

    def _start_sources(self) -> None:
        if self._assist.start():
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_ASSIST_UNSUPPORTED)
        else:
            _LOGGER.warning("Home Assistant's Assist timer internals changed; voice timers can't be shown")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_ASSIST_UNSUPPORTED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_ASSIST_UNSUPPORTED,
            )
        self._helpers.start()

    async def async_stop(self) -> None:
        self._assist.stop()
        self._helpers.stop()
        while self._unsubs:
            self._unsubs.pop()()
        self._cancel_tick()
        if self._link is not None:
            await self._link.async_stop()
        self._reconciler.on_disconnected()

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Called whenever the desired timers or the link state change."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def resync_time(self) -> None:
        self._send(self._reconciler.resync_time(dt_util.utcnow()))

    # ------------------------------------------------------------ sources

    @callback
    def _changed(self) -> None:
        """A source changed. Coalesce bursts (pause = cancel + update, several events in one call)."""
        if not self._update_pending:
            self._update_pending = True
            self.hass.loop.call_soon(self._run_update)

    @callback
    def _run_update(self) -> None:
        self._update_pending = False
        self._refresh(dt_util.utcnow())

    @callback
    def _finished(self, key: str) -> None:
        self._reconciler.on_finished(dt_util.utcnow(), key)
        self._schedule_tick()

    @callback
    def _poll(self, now: datetime) -> None:
        self._refresh(dt_util.utcnow())

    @callback
    def _refresh(self, now: datetime) -> None:
        timers = {**self._assist.snapshot(now), **self._helpers.snapshot(now)}
        changed = _visible(timers) != _visible(self.timers)
        self.timers = timers
        self._send(self._reconciler.update(now, timers))
        if changed:
            self._notify()

    # ------------------------------------------------------------ link

    @callback
    def _link_up(self) -> None:
        now = dt_util.utcnow()
        self.timers = {**self._assist.snapshot(now), **self._helpers.snapshot(now)}
        self._send(self._reconciler.on_connected(now, self.timers))
        self._notify()

    @callback
    def _link_down(self) -> None:
        self._reconciler.on_disconnected()
        self._cancel_tick()
        self._notify()

    @callback
    def _send(self, commands: list[Command]) -> None:
        if commands and self._link is not None:
            for command in commands:
                _LOGGER.debug("To the clock: %s", command.label)
                self._link.send(command.payload)
        self._schedule_tick()

    # ------------------------------------------------------------ ticks

    @callback
    def _schedule_tick(self) -> None:
        self._cancel_tick()
        if (due := self._reconciler.next_due()) is not None:
            self._tick_unsub = async_track_point_in_utc_time(self.hass, self._tick, due)

    @callback
    def _cancel_tick(self) -> None:
        if self._tick_unsub is not None:
            self._tick_unsub()
            self._tick_unsub = None

    @callback
    def _tick(self, now: datetime) -> None:
        self._tick_unsub = None
        self._send(self._reconciler.tick(dt_util.utcnow()))

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


def _visible(timers: dict[str, ClockTimer]) -> dict[str, tuple[str, str]]:
    """What the Timers sensor shows, at whole-second precision, so jitter doesn't write new states."""
    return {
        key: (t.name, t.ends_at.replace(microsecond=0).isoformat() if t.ends_at else "paused")
        for key, t in timers.items()
    }
