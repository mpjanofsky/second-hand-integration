"""The integration against Home Assistant's real intent TimerManager, a fake satellite and a fake clock link."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.intent.timers import TimerEventType, async_register_timer_handler
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.second_hand import protocol
from custom_components.second_hand.const import (
    CONF_ASSIST_DEVICES,
    CONF_HELPERS,
    DOMAIN,
    ISSUE_ASSIST_UNSUPPORTED,
)
from custom_components.second_hand.controller import ClockController
from custom_components.second_hand.sources import TIMER_DATA


class FakeLink:
    """Records what would go to the clock."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.up: Callable[[], None] | None = None
        self.down: Callable[[], None] | None = None

    def start(self, on_up: Callable[[], None], on_down: Callable[[], None]) -> None:
        self.up, self.down = on_up, on_down

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def async_stop(self) -> None:
        pass

    def labels(self) -> list[str]:
        names = {
            protocol.clear_indicator(): "ClearIndicator",
            protocol.timers_active(): "timers=active",
        }
        out = []
        for p in self.sent:
            if p in names:
                out.append(names[p])
            elif b"timeinfo" in p:
                out.append("timeinfo")
            elif b"SetAlert" in p:
                out.append("SetAlert")
            elif b"DeleteAlert" in p:
                out.append("DeleteAlert")
            else:
                out.append(p.hex())
        return out


class Satellite:
    """A voice satellite: a device with a timer handler, like ESPHome registers."""

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        entry = MockConfigEntry(domain="test")
        entry.add_to_hass(hass)
        self.device_id = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("test", name)}, name=name
        ).id
        self.events: list[tuple[TimerEventType, str]] = []
        self.unregister = async_register_timer_handler(hass, self.device_id, self.handler)

    def handler(self, event_type: TimerEventType, timer: Any) -> None:
        self.events.append((event_type, timer.id))


@pytest.fixture
async def intent(hass: HomeAssistant) -> Any:
    assert await async_setup_component(hass, "intent", {})
    return hass.data[TIMER_DATA]


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()
    await asyncio.sleep(0)
    await hass.async_block_till_done()


async def test_voice_timer_lifecycle(hass: HomeAssistant, intent: Any) -> None:
    kitchen, office = Satellite(hass, "Kitchen"), Satellite(hass, "Office")
    link = FakeLink()
    controller = ClockController(hass, [kitchen.device_id], [], link)
    await controller.async_start()
    link.up()
    assert link.labels() == ["timeinfo", "ClearIndicator"]

    # A timer on the picked satellite shows; one on another satellite doesn't.
    before = dt_util.utcnow()
    timer_id = intent.start_timer(kitchen.device_id, None, 10, None, language="en")
    intent.start_timer(office.device_id, None, 5, None, language="en")
    await _settle(hass)
    assert link.labels()[2:] == ["SetAlert"]
    assert kitchen.events == [(TimerEventType.STARTED, timer_id)]  # the satellite's own handler still runs
    (clock_timer,) = controller.timers.values()
    assert clock_timer.key == f"assist:{timer_id}"
    assert abs((clock_timer.ends_at - before) - timedelta(minutes=10)) < timedelta(seconds=2)

    # Pause deletes it; resume shows it again with the same token.
    intent.pause_timer(timer_id)
    await _settle(hass)
    assert link.labels()[3:] == ["DeleteAlert"]
    assert controller.timers[f"assist:{timer_id}"].paused
    intent.unpause_timer(timer_id)
    await _settle(hass)
    assert link.labels()[4:] == ["SetAlert"]
    token = protocol.alert_token(f"assist:{timer_id}").encode()
    assert token in link.sent[2] and token in link.sent[3] and token in link.sent[4]  # same token throughout

    # Cancel deletes it.
    intent.cancel_timer(timer_id)
    await _settle(hass)
    assert link.labels()[5:] == ["DeleteAlert"]
    assert controller.timers == {}
    await controller.async_stop()


async def test_finished_timer_pulses_once(hass: HomeAssistant, intent: Any) -> None:
    sat = Satellite(hass, "Kitchen")
    link = FakeLink()
    controller = ClockController(hass, [sat.device_id], [], link)
    await controller.async_start()
    link.up()
    intent.start_timer(sat.device_id, None, None, 1, language="en")
    await _settle(hass)
    await asyncio.sleep(2.3)  # the 1 s timer ends, then the pulse is due 1 s later
    await _settle(hass)
    assert link.labels() == ["timeinfo", "ClearIndicator", "SetAlert", "timers=active"]
    assert sat.events[-1][0] == TimerEventType.FINISHED
    await controller.async_stop()


async def test_satellite_reregistering_stays_tapped(hass: HomeAssistant, intent: Any) -> None:
    sat = Satellite(hass, "Kitchen")
    link = FakeLink()
    controller = ClockController(hass, [sat.device_id], [], link)
    await controller.async_start()
    link.up()
    sat.unregister()  # ESPHome reconnect: unregister, then register a new handler
    sat.unregister = async_register_timer_handler(hass, sat.device_id, sat.handler)
    intent.start_timer(sat.device_id, None, 3, None, language="en")
    await _settle(hass)
    assert link.labels()[-1] == "SetAlert"
    assert sat.events[-1][0] == TimerEventType.STARTED
    await controller.async_stop()


async def test_stop_restores_handlers(hass: HomeAssistant, intent: Any) -> None:
    sat = Satellite(hass, "Kitchen")
    first = ClockController(hass, [sat.device_id], [], FakeLink())
    second = ClockController(hass, [sat.device_id], [], FakeLink())
    await first.async_start()
    await second.async_start()
    assert type(intent.handlers) is not dict
    await first.async_stop()
    assert type(intent.handlers) is not dict  # still needed by the second clock
    await second.async_stop()
    assert type(intent.handlers) is dict
    assert intent.handlers[sat.device_id] == sat.handler
    sat.unregister()
    assert sat.device_id not in intent.handlers


async def test_changed_internals_raise_a_repair_and_helpers_still_work(hass: HomeAssistant) -> None:
    hass.data[TIMER_DATA] = object()  # pretend HA reorganised its timers
    hass.states.async_set("timer.tea", "active", {"finishes_at": (dt_util.utcnow() + timedelta(minutes=3)).isoformat()})
    link = FakeLink()
    controller = ClockController(hass, ["some-device"], ["timer.tea"], link)
    await controller.async_start()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ASSIST_UNSUPPORTED) is not None
    link.up()
    assert link.labels() == ["timeinfo", "ClearIndicator", "SetAlert"]
    await controller.async_stop()


async def test_helper_timers(hass: HomeAssistant) -> None:
    ends = dt_util.utcnow() + timedelta(minutes=3)
    link = FakeLink()
    controller = ClockController(hass, [], ["timer.tea"], link)
    await controller.async_start()
    link.up()
    hass.states.async_set("timer.tea", "active", {"finishes_at": ends.isoformat(), "friendly_name": "Tea"})
    await _settle(hass)
    assert link.labels()[2:] == ["SetAlert"]
    assert controller.timers["helper:timer.tea"].name == "Tea"
    hass.states.async_set("timer.tea", "paused", {"remaining": "0:02:00"})
    await _settle(hass)
    assert link.labels()[3:] == ["DeleteAlert"]
    hass.states.async_set("timer.tea", "active", {"finishes_at": ends.isoformat()})
    await _settle(hass)
    hass.bus.async_fire("timer.finished", {"entity_id": "timer.tea"})
    hass.bus.async_fire("timer.finished", {"entity_id": "timer.other"})
    hass.states.async_set("timer.tea", "idle", {})
    await _settle(hass)
    await asyncio.sleep(1.2)
    await _settle(hass)
    assert link.labels()[4:] == ["SetAlert", "timers=active"]
    await controller.async_stop()


async def test_changing_sources_keeps_the_link(hass: HomeAssistant) -> None:
    ends = dt_util.utcnow() + timedelta(minutes=3)
    link = FakeLink()
    controller = ClockController(hass, [], ["timer.tea"], link)
    await controller.async_start()
    link.up()
    hass.states.async_set("timer.tea", "active", {"finishes_at": ends.isoformat()})
    hass.states.async_set("timer.mug", "active", {"finishes_at": ends.isoformat()})
    await _settle(hass)
    assert link.labels()[2:] == ["SetAlert"]
    controller.update_sources([], ["timer.mug"])
    await _settle(hass)
    assert controller.connected  # still up, no reconnect
    assert set(controller.timers) == {"helper:timer.mug"}
    assert sorted(link.labels()[3:]) == ["DeleteAlert", "SetAlert"]
    await controller.async_stop()


async def test_reconnect_resends_running_timers(hass: HomeAssistant, intent: Any) -> None:
    sat = Satellite(hass, "Kitchen")
    link = FakeLink()
    controller = ClockController(hass, [sat.device_id], [], link)
    await controller.async_start()
    intent.start_timer(sat.device_id, None, 5, None, language="en")
    await _settle(hass)
    assert link.sent == []  # not connected yet: nothing sent
    link.up()
    assert link.labels() == ["timeinfo", "ClearIndicator", "SetAlert"]
    link.down()
    link.up()
    assert link.labels()[3:] == ["timeinfo", "ClearIndicator", "SetAlert"]
    await controller.async_stop()


@patch("custom_components.second_hand.config_flow.ALLOW_TIMERS_ONLY", True)
async def test_config_flow_and_sensor(hass: HomeAssistant, intent: Any) -> None:
    sat = Satellite(hass, "Kitchen")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    with patch("custom_components.second_hand.config_flow._bluetooth_adapters", return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Kitchen clock"})
    assert result["step_id"] == "no_bluetooth"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "timers"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "nothing_picked"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ASSIST_DEVICES: [sat.device_id], CONF_HELPERS: []}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen clock"
    await hass.async_block_till_done()

    state = hass.states.get("sensor.kitchen_clock_timers")
    assert state is not None and state.state == "0"
    timer_id = intent.start_timer(sat.device_id, None, 4, None, language="en", name="pasta")
    await _settle(hass)
    state = hass.states.get("sensor.kitchen_clock_timers")
    assert state.state == "1"
    (item,) = state.attributes["timers"]
    assert item["id"] == f"assist:{timer_id}"
    assert item["name"] == "pasta" and item["paused"] is False
    assert item["token"] == protocol.alert_token(f"assist:{timer_id}")
    assert state.attributes["format"] == 1

    # Options: switch to a helper only.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_ASSIST_DEVICES: [], CONF_HELPERS: []})
    assert result["errors"] == {"base": "nothing_picked"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ASSIST_DEVICES: [], CONF_HELPERS: ["timer.tea"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_clock_timers").state == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert type(intent.handlers) is dict
