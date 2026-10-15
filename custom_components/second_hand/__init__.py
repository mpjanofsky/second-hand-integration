"""Second Hand: shows Home Assistant timers on an Echo Wall Clock and keeps its time right."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_ADAPTER, CONF_ADDRESS, CONF_ASSIST_DEVICES, CONF_HELPERS
from .controller import ClockController

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

type EchoWallClockConfigEntry = ConfigEntry[ClockController]


async def async_setup_entry(hass: HomeAssistant, entry: EchoWallClockConfigEntry) -> bool:
    controller = ClockController(
        hass,
        list(entry.options.get(CONF_ASSIST_DEVICES, [])),
        list(entry.options.get(CONF_HELPERS, [])),
    )
    stop_call_ins = None
    if address := entry.data.get(CONF_ADDRESS):
        from .bluez import BlueZ, BlueZError, open_rfcomm  # Linux only
        from .link import BluetoothLink

        adapter = entry.data[CONF_ADAPTER]
        bluez = BlueZ()
        try:
            await bluez.connect()
            adapter_address = (await bluez.adapters()).get(adapter)
            if adapter_address is None:
                raise BlueZError("adapter missing", adapter)
        except BlueZError as err:
            bluez.close()
            raise ConfigEntryNotReady(f"Bluetooth isn't ready: {err}") from err

        link = BluetoothLink(lambda: open_rfcomm(address, adapter_address), controller.link_status_changed)
        controller.attach_link(link)
        try:
            stop_call_ins = await bluez.listen_for_call_ins(
                adapter, lambda caller: link.wake() if caller.upper() == address.upper() else None
            )
        except BlueZError:
            stop_call_ins = None  # the link still connects out; it just won't hear call-ins

        async def _close_bluez() -> None:
            if stop_call_ins is not None:
                await stop_call_ins()
            bluez.close()

        entry.async_on_unload(_close_bluez)

    entry.runtime_data = controller
    await controller.async_start()
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EchoWallClockConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: EchoWallClockConfigEntry) -> None:
    """Forget the pairing, so the clock can be paired with something else."""
    if address := entry.data.get(CONF_ADDRESS):
        from .bluez import BlueZ, BlueZError

        bluez = BlueZ()
        try:
            await bluez.connect()
            await bluez.remove(entry.data[CONF_ADAPTER], address)
        except BlueZError:
            pass
        finally:
            bluez.close()


async def _options_updated(hass: HomeAssistant, entry: EchoWallClockConfigEntry) -> None:
    """Swap the timer sources in place: a reload would drop the Bluetooth link for over a minute."""
    entry.runtime_data.update_sources(
        list(entry.options.get(CONF_ASSIST_DEVICES, [])),
        list(entry.options.get(CONF_HELPERS, [])),
    )
