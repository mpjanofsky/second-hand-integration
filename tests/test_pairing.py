"""Pairing in the config flow, and a paired clock's link and entities, with BlueZ replaced by fakes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from custom_components.second_hand import link as link_mod, protocol as p
from custom_components.second_hand.const import (
    CONF_ADAPTER,
    CONF_ADDRESS,
    CONF_ASSIST_DEVICES,
    CONF_HELPERS,
    CONF_TIMERS_ONLY,
    DOMAIN,
)

from .fake_clock import FakeClock

ADAPTER = "/org/bluez/hci0"
CLOCK = "AA:BB:CC:00:11:22"
CF = "custom_components.second_hand.config_flow"
INFO = p.ClockInfo("EchoWallClk", "102", "AlexaGadgetsEWC")


@pytest.fixture(autouse=True)
async def intent(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "intent", {})


async def _to_pair_mode(hass: HomeAssistant, adapters: dict[str, str] | None) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch(f"{CF}._bluetooth_adapters", return_value=adapters):
        return await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Kitchen clock"})


async def _progress(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    """Let a progress step's task finish, then move on."""
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def test_pair_a_clock(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, {ADAPTER: "00:11:22:33:44:55"})
    assert result["step_id"] == "pair_mode"
    with (
        patch(f"{CF}._search", return_value={CLOCK: "EchoWallClk10U"}),
        patch(f"{CF}._pair", return_value=INFO) as pair,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS and result["step_id"] == "search"
        result = await _progress(hass, result)
        assert result["step_id"] == "confirm"
        assert result["description_placeholders"] == {"name": "EchoWallClk10U", "address": CLOCK}
        pair.assert_not_called()  # nothing is paired until the user says yes
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _progress(hass, result)
    pair.assert_called_once_with(ADAPTER, CLOCK)
    assert result["step_id"] == "timers"
    with patch("custom_components.second_hand.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HELPERS: ["timer.tea"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: CLOCK, CONF_ADAPTER: ADAPTER}
    assert result["result"].unique_id == CLOCK


async def test_several_adapters_and_clocks(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, {ADAPTER: "00:11:22:33:44:55", "/org/bluez/hci1": "66:77:88:99:AA:BB"})
    with (
        patch(f"{CF}._search", return_value={CLOCK: "EchoWallClk10U", "AA:BB:CC:33:44:55": "EchoWallClk2"}) as search,
        patch(f"{CF}._pair", return_value=INFO) as pair,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADAPTER: "/org/bluez/hci1"})
        result = await _progress(hass, result)
        assert result["step_id"] == "choose"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: "AA:BB:CC:33:44:55"})
        assert result["step_id"] == "confirm"
        pair.assert_not_called()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _progress(hass, result)
    search.assert_called_once_with("/org/bluez/hci1")
    pair.assert_called_once_with("/org/bluez/hci1", "AA:BB:CC:33:44:55")
    assert result["step_id"] == "timers"


async def test_no_clock_found_goes_back_with_a_hint(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, {ADAPTER: "00:11:22:33:44:55"})
    with patch(f"{CF}._search", return_value={}):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _progress(hass, result)
    assert result["step_id"] == "pair_mode"
    assert result["errors"] == {"base": "not_found"}


async def test_pairing_failure_goes_back_with_a_hint(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, {ADAPTER: "00:11:22:33:44:55"})
    with (
        patch(f"{CF}._search", return_value={CLOCK: "EchoWallClk10U"}),
        patch(f"{CF}._pair", side_effect=OSError("Connection refused")),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _progress(hass, result)
        assert result["step_id"] == "confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _progress(hass, result)
    assert result["step_id"] == "pair_mode"
    assert result["errors"] == {"base": "pair_failed"}


async def test_same_clock_twice_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=CLOCK, data={CONF_ADDRESS: CLOCK, CONF_ADAPTER: ADAPTER}).add_to_hass(hass)
    result = await _to_pair_mode(hass, {ADAPTER: "00:11:22:33:44:55"})
    with patch(f"{CF}._search", return_value={CLOCK: "EchoWallClk10U"}), patch(f"{CF}._pair") as pair:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _progress(hass, result)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    pair.assert_not_called()


@patch(f"{CF}.ALLOW_TIMERS_ONLY", True)
async def test_timers_only_from_pair_mode(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, {ADAPTER: "00:11:22:33:44:55"})
    assert result["step_id"] == "pair_mode"
    with patch(f"{CF}._search") as search, patch(f"{CF}._pair") as pair:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TIMERS_ONLY: True})
        assert result["step_id"] == "timers"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HELPERS: ["timer.tea"]})
    search.assert_not_called()
    pair.assert_not_called()
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"] == {}
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_clock_connected") is None


@patch(f"{CF}.ALLOW_TIMERS_ONLY", True)
async def test_timers_only_is_still_there_after_no_clock_found(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, {ADAPTER: "00:11:22:33:44:55"})
    with patch(f"{CF}._search", return_value={}):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _progress(hass, result)
    assert result["errors"] == {"base": "not_found"}
    assert CONF_TIMERS_ONLY in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TIMERS_ONLY: True})
    assert result["step_id"] == "timers"


@patch(f"{CF}.ALLOW_TIMERS_ONLY", True)
async def test_no_bluetooth_offers_timers_only(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, None)
    assert result["step_id"] == "no_bluetooth"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "timers"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HELPERS: ["timer.tea"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_clock_timers") is not None
    assert hass.states.get("binary_sensor.kitchen_clock_connected") is None  # nothing to connect to


class FakeBlueZ:
    """Stands in for bluez.BlueZ."""

    instances: list[FakeBlueZ] = []

    def __init__(self) -> None:
        self.call_in: Callable[[str], None] | None = None
        self.removed: list[tuple[str, str]] = []
        self.closed = False
        FakeBlueZ.instances.append(self)

    async def connect(self) -> None:
        pass

    async def adapters(self) -> dict[str, str]:
        return {ADAPTER: "00:11:22:33:44:55"}

    async def listen_for_call_ins(self, adapter: str, on_call_in: Callable[[str], None]) -> Any:
        self.call_in = on_call_in

        async def unregister() -> None:
            self.call_in = None

        return unregister

    async def remove(self, adapter: str, address: str) -> None:
        self.removed.append((adapter, address))

    def close(self) -> None:
        self.closed = True


async def test_paired_clock_end_to_end(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(link_mod, "BACKOFF", (0.05, 0.1, 0.2, 0.2, 0.2))
    FakeBlueZ.instances.clear()
    clock = FakeClock(["fail", "ok"])  # the first connect after pairing fails

    async def open_rfcomm(address: str, adapter_address: str | None = None) -> Any:
        assert (address, adapter_address) == (CLOCK, "00:11:22:33:44:55")
        return await clock.open()

    hass.states.async_set("timer.tea", "idle", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen clock",
        unique_id=CLOCK,
        data={CONF_ADDRESS: CLOCK, CONF_ADAPTER: ADAPTER},
        options={CONF_ASSIST_DEVICES: [], CONF_HELPERS: ["timer.tea"]},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.second_hand.bluez.BlueZ", FakeBlueZ),
        patch("custom_components.second_hand.bluez.open_rfcomm", open_rfcomm),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for _ in range(100):
            if hass.states.get("binary_sensor.kitchen_clock_connected").state == "on":
                break
            await asyncio.sleep(0.02)
        assert hass.states.get("binary_sensor.kitchen_clock_connected").state == "on"
        assert clock.attempts == 2
        await asyncio.sleep(0.05)
        assert clock.received == ["Discover", "timeinfo", "ClearIndicator"]

        device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
        assert device.sw_version == "102"
        assert (dr.CONNECTION_BLUETOOTH, CLOCK) in device.connections

        # A helper timer goes to the clock.
        hass.states.async_set("timer.tea", "active", {"finishes_at": "2099-01-01T00:00:00+00:00"})
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)
        assert clock.received[-1] == "SetAlert"

        # Sync time.
        await hass.services.async_call("button", "press", {"entity_id": "button.kitchen_clock_sync_time"}, blocking=True)
        await asyncio.sleep(0.05)
        assert clock.received[-1] == "timeinfo"

        # The clock drops (battery pull): Connected goes off, then it reconnects and everything is resent.
        clock.current.drop()
        await asyncio.sleep(0.01)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.kitchen_clock_connected").state == "off"
        for _ in range(100):
            if clock.received.count("Discover") == 2 and clock.received[-1] == "SetAlert":
                break
            await asyncio.sleep(0.02)
        assert clock.received[-4:] == ["Discover", "timeinfo", "ClearIndicator", "SetAlert"]

        # Removing the entry forgets the pairing.
        bluez = FakeBlueZ.instances[0]
        assert bluez.call_in is not None
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert bluez.call_in is None and bluez.closed
        assert FakeBlueZ.instances[-1].removed == [(ADAPTER, CLOCK)]


async def test_no_bluetooth_aborts_by_default(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, None)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_bluetooth"


async def test_pair_mode_has_no_skip_box_by_default(hass: HomeAssistant) -> None:
    result = await _to_pair_mode(hass, {ADAPTER: "00:11:22:33:44:55"})
    assert result["step_id"] == "pair_mode"
    assert CONF_TIMERS_ONLY not in result["data_schema"].schema


def test_clock_names_include_the_mickey_mouse_edition():
    from custom_components.second_hand.bluez import is_clock_name

    assert is_clock_name("EchoWallClk10U")
    assert is_clock_name("EWCMickeyMouse05B")
    assert not is_clock_name("JBL Flip 5")
    assert not is_clock_name("")
