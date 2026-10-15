"""Connected: whether the clock link is up."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EchoWallClockConfigEntry
from .const import DOMAIN
from .entity import ClockEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EchoWallClockConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if entry.runtime_data.has_link:
        async_add_entities([ConnectedSensor(entry)])


class ConnectedSensor(ClockEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: EchoWallClockConfigEntry) -> None:
        super().__init__(entry, "connected")
        self._entry_id = entry.entry_id
        self._firmware: str | None = None

    @property
    def is_on(self) -> bool:
        return self._controller.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._controller.link_status
        return {"state": str(getattr(status, "state", "")), "reason": getattr(status, "reason", "")}

    def _updated(self) -> None:
        info = getattr(self._controller.link_status, "info", None)
        if info is not None and info.firmware and info.firmware != self._firmware:
            self._firmware = info.firmware
            registry = dr.async_get(self.hass)
            if device := registry.async_get_device_by_identifier((DOMAIN, self._entry_id), self._entry_id):
                registry.async_update_device(device.id, sw_version=info.firmware)
        super()._updated()
