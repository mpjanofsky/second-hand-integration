"""Base entity: one device per clock."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from . import EchoWallClockConfigEntry
from .const import CONF_ADDRESS, DOMAIN
from .controller import ClockController


class ClockEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: EchoWallClockConfigEntry, key: str) -> None:
        self._controller: ClockController = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        address = entry.data.get(CONF_ADDRESS)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            connections={(CONNECTION_BLUETOOTH, address.upper())} if address else set(),
            name=entry.title,
            manufacturer="Amazon",
            model="Echo Wall Clock",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._controller.add_listener(self._updated))

    def _updated(self) -> None:
        self.async_write_ha_state()
