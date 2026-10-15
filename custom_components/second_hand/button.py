"""Sync time: send the time to the clock now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EchoWallClockConfigEntry
from .entity import ClockEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EchoWallClockConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if entry.runtime_data.has_link:
        async_add_entities([SyncTimeButton(entry)])


class SyncTimeButton(ClockEntity, ButtonEntity):
    def __init__(self, entry: EchoWallClockConfigEntry) -> None:
        super().__init__(entry, "sync_time")

    @property
    def available(self) -> bool:
        return self._controller.connected

    async def async_press(self) -> None:
        self._controller.resync_time()
