"""The Timers sensor: what the clock should show. Its attributes are a stable format other things can follow."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EchoWallClockConfigEntry
from .entity import ClockEntity

FORMAT_VERSION = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: EchoWallClockConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([TimersSensor(entry)])


class TimersSensor(ClockEntity, SensorEntity):
    """Number of timers the clock should know about, with the list in attributes."""

    def __init__(self, entry: EchoWallClockConfigEntry) -> None:
        super().__init__(entry, "timers")

    @property
    def native_value(self) -> int:
        return len(self._controller.timers)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        timers = sorted(
            self._controller.timers.values(), key=lambda t: (t.ends_at is None, t.ends_at or 0, t.key)
        )
        return {
            "format": FORMAT_VERSION,
            "timers": [
                {
                    "id": t.key,
                    "token": t.token,
                    "name": t.name,
                    "ends_at": t.ends_at.replace(microsecond=0).isoformat() if t.ends_at else None,
                    "paused": t.paused,
                }
                for t in timers
            ],
        }
