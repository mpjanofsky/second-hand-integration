"""Set up a clock: name it, pair it over Bluetooth, then pick which timers it shows.

Timers-only setup (no clock) is behind ALLOW_TIMERS_ONLY, off by default."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ADAPTER,
    CONF_ADDRESS,
    CONF_ASSIST_DEVICES,
    CONF_HELPERS,
    ALLOW_TIMERS_ONLY,
    CONF_TIMERS_ONLY,
    DOMAIN,
    PAIR_SECONDS,
    SEARCH_SECONDS,
)
from .sources import timer_devices

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Echo Wall Clock"


def _timer_schema(hass: HomeAssistant, current: dict[str, Any]) -> vol.Schema:
    registry = dr.async_get(hass)
    device_ids = list(dict.fromkeys([*timer_devices(hass), *current.get(CONF_ASSIST_DEVICES, [])]))
    options = []
    for device_id in device_ids:
        device = registry.async_get(device_id)
        label = (device.name_by_user or device.name) if device else None
        options.append(SelectOptionDict(value=device_id, label=label or device_id))
    return vol.Schema(
        {
            vol.Optional(CONF_ASSIST_DEVICES, default=current.get(CONF_ASSIST_DEVICES, [])): SelectSelector(
                SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
            ),
            vol.Optional(CONF_HELPERS, default=current.get(CONF_HELPERS, [])): EntitySelector(
                EntitySelectorConfig(domain="timer", multiple=True)
            ),
        }
    )


def _picked_something(user_input: dict[str, Any]) -> bool:
    return bool(user_input.get(CONF_ASSIST_DEVICES) or user_input.get(CONF_HELPERS))


async def _bluetooth_adapters() -> dict[str, str] | None:
    """Adapter path -> address, or None if this host can't do Bluetooth Classic for us."""
    try:
        from .bluez import BlueZ, BlueZError, sockets_supported
    except ImportError:
        return None
    if not sockets_supported():
        return None
    bluez = BlueZ()
    try:
        await bluez.connect()
        return await bluez.adapters() or None
    except BlueZError:
        return None
    finally:
        bluez.close()


async def _search(adapter: str) -> dict[str, str]:
    from .bluez import BlueZ

    bluez = BlueZ()
    try:
        await bluez.connect()
        return await bluez.find_clocks(adapter, SEARCH_SECONDS)
    finally:
        bluez.close()


async def _pair(adapter: str, address: str) -> Any:
    from .bluez import BlueZ, pair_and_connect

    bluez = BlueZ()
    try:
        await bluez.connect()
        return await pair_and_connect(bluez, adapter, address, PAIR_SECONDS)
    finally:
        bluez.close()


class EchoWallClockConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._name = DEFAULT_NAME
        self._data: dict[str, Any] = {}
        self._adapters: dict[str, str] = {}
        self._adapter = ""
        self._found: dict[str, str] = {}
        self._task: asyncio.Task[Any] | None = None
        self._error = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._name = user_input[CONF_NAME]
            self._adapters = await _bluetooth_adapters() or {}
            if not self._adapters:
                if not ALLOW_TIMERS_ONLY:
                    return self.async_abort(reason="no_bluetooth")
                return await self.async_step_no_bluetooth()
            return await self.async_step_pair_mode()
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required(CONF_NAME, default=DEFAULT_NAME): str})
        )

    async def async_step_no_bluetooth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """No usable Bluetooth here: offer timers-only (the Timers sensor, nothing sent to a clock)."""
        if user_input is not None:
            return await self.async_step_timers()
        return self.async_show_form(step_id="no_bluetooth", data_schema=vol.Schema({}))

    async def async_step_pair_mode(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for pairing mode (pick the adapter if there are several), or skip pairing for timers-only."""
        errors = {"base": self._error} if self._error else {}
        self._error = ""
        if user_input is not None:
            if user_input.get(CONF_TIMERS_ONLY):
                self._data = {}
                return await self.async_step_timers()
            self._adapter = user_input.get(CONF_ADAPTER) or next(iter(self._adapters))
            return await self.async_step_search()
        schema: dict[Any, Any] = {}
        if len(self._adapters) > 1:
            schema[vol.Required(CONF_ADAPTER, default=next(iter(self._adapters)))] = SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=p, label=f"{p.rsplit('/', 1)[-1]} ({a})") for p, a in self._adapters.items()]
                )
            )
        if ALLOW_TIMERS_ONLY:
            schema[vol.Optional(CONF_TIMERS_ONLY, default=False)] = bool
        return self.async_show_form(step_id="pair_mode", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._task is None:
            # Always show progress once, even if the task finished at once: a progress-done result on the call
            # that carried form input would pass that input on to the next step.
            self._task = self.hass.async_create_task(_search(self._adapter))
            return self.async_show_progress(step_id="search", progress_action="search", progress_task=self._task)
        if not self._task.done():
            return self.async_show_progress(step_id="search", progress_action="search", progress_task=self._task)
        task, self._task = self._task, None
        try:
            self._found = task.result()
        except Exception:  # noqa: BLE001  (shown to the user as "search failed")
            _LOGGER.exception("Bluetooth search failed")
            self._found = {}
        if not self._found:
            self._error = "not_found"
            return self.async_show_progress_done(next_step_id="pair_mode")
        if len(self._found) == 1:
            self._data = {CONF_ADDRESS: next(iter(self._found)), CONF_ADAPTER: self._adapter}
            return self.async_show_progress_done(next_step_id="confirm")
        return self.async_show_progress_done(next_step_id="choose")

    async def async_step_choose(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data = {CONF_ADDRESS: user_input[CONF_ADDRESS], CONF_ADAPTER: self._adapter}
            return await self.async_step_confirm()
        options = [SelectOptionDict(value=a, label=f"{n} ({a[-5:]})") for a, n in self._found.items()]
        return self.async_show_form(
            step_id="choose",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): SelectSelector(SelectSelectorConfig(options=options))}),
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Name the clock we found and get a yes before pairing: pairing takes it away from an Echo."""
        address = self._data[CONF_ADDRESS]
        if user_input is not None:
            return await self.async_step_pair()
        await self.async_set_unique_id(address.upper())
        self._abort_if_unique_id_configured()
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"name": self._found.get(address) or "Echo Wall Clock", "address": address},
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(_pair(self._adapter, self._data[CONF_ADDRESS]))
            return self.async_show_progress(step_id="pair", progress_action="pair", progress_task=self._task)
        if not self._task.done():
            return self.async_show_progress(step_id="pair", progress_action="pair", progress_task=self._task)
        task, self._task = self._task, None
        try:
            task.result()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Pairing with the clock failed")
            self._error = "pair_failed"
            return self.async_show_progress_done(next_step_id="pair_mode")
        return self.async_show_progress_done(next_step_id="timers")

    async def async_step_timers(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if _picked_something(user_input):
                return self.async_create_entry(title=self._name, data=self._data, options=user_input)
            errors["base"] = "nothing_picked"
        return self.async_show_form(
            step_id="timers", data_schema=_timer_schema(self.hass, user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EchoWallClockOptionsFlow()


class EchoWallClockOptionsFlow(OptionsFlow):
    """Change which timers the clock shows."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if _picked_something(user_input):
                return self.async_create_entry(data=user_input)
            errors["base"] = "nothing_picked"
        current = user_input or dict(self.config_entry.options)
        return self.async_show_form(step_id="init", data_schema=_timer_schema(self.hass, current), errors=errors)
