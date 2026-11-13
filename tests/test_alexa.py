"""Fork: timers from Alexa Media Player's next_timer sensors."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
import yaml

from custom_components.second_hand import protocol
from custom_components.second_hand.const import CONF_ALEXA_SENSORS, CONF_ASSIST_DEVICES, CONF_HELPERS, DOMAIN
from custom_components.second_hand.controller import ClockController
from custom_components.second_hand.sources_entities import parse_alexa

from .test_integration import FakeLink, _settle

FIXTURE = Path(__file__).parent.parent / "fixtures" / "alexa-next-timer-two-active.yaml"
ECHO = "sensor.echo_kitchen_next_timer"
ID1 = "A1P493D1054TAW-G095590263470531-4ed6dbdc-5a3c-4ca6-89e4-caae2feed9c7_TIMER"
ID2 = "A1P493D1054TAW-G095590263470531-55ae7a73-0965-475e-9e49-2442d9dea25c_TIMER"


def _item(timer_id: str, ends_at: datetime | None, status: str = "ON", label: str | None = None) -> dict[str, Any]:
    """One notification like Alexa Media Player lists it (the fields we read, plus a few we don't)."""
    return {
        "id": timer_id,
        "type": "Timer",
        "status": status,
        "timerLabel": label,
        "label": None,
        "remainingTime": 600000,
        "originalDurationInMillis": 900000,
        "triggerTime": int(ends_at.timestamp() * 1000) if ends_at else 0,
        "deviceSerialNumber": "G095590263470531",
    }


def _attrs(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "process_timestamp": dt_util.now().isoformat(),
        "total_active": len(items),
        "sorted_active": list(items),
        "sorted_all": list(items),
        "device_class": "timestamp",
        "friendly_name": "Echo Kitchen Next timer",
    }


def _set(hass: HomeAssistant, *items: dict[str, Any]) -> None:
    ends = [datetime.fromtimestamp(i["triggerTime"] / 1000, UTC) for i in items if i["triggerTime"]]
    state = min(ends).isoformat() if ends else "unavailable"
    hass.states.async_set(ECHO, state, _attrs(*items))


# ---------------------------------------------------------------- parser


def test_parse_real_sensor() -> None:
    attrs = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    timers = {t.key: t for t in parse_alexa(attrs)}
    assert set(timers) == {f"alexa:{ID1}", f"alexa:{ID2}"}
    # triggerTime is epoch milliseconds: created 22:51:06.828 + 15 min.
    assert timers[f"alexa:{ID1}"].ends_at == datetime(2026, 11, 13, 22, 6, 6, 809000, tzinfo=UTC)
    assert timers[f"alexa:{ID2}"].ends_at == datetime(2026, 11, 13, 22, 11, 7, 212000, tzinfo=UTC)
    assert all(t.name == "" and not t.paused for t in timers.values())


def test_parse_older_formats_and_fallbacks() -> None:
    ends = datetime(2026, 11, 13, 22, 0, tzinfo=UTC)
    item = _item("a", ends, label="pasta")
    # JSON strings of [id, dict] pairs, as older Alexa Media Player versions wrote them.
    (timer,) = parse_alexa({"sorted_active": json.dumps([["a", item]])})
    assert timer.key == "alexa:a" and timer.ends_at == ends and timer.name == "pasta"
    # No triggerTime: process_timestamp + remainingTime.
    no_trigger = {**item, "triggerTime": None, "remainingTime": 60000}
    (timer,) = parse_alexa({"sorted_active": [no_trigger], "process_timestamp": "2026-11-13T22:00:00+00:00"})
    assert timer.ends_at == ends + timedelta(minutes=1)
    # Only sorted_all: finished (OFF) timers in it are skipped.
    (timer,) = parse_alexa({"sorted_all": [item, _item("b", ends, status="OFF")]})
    assert timer.key == "alexa:a"


def test_parse_paused_and_skips_what_it_does_not_understand() -> None:
    ends = datetime(2026, 11, 13, 22, 0, tzinfo=UTC)
    alarm = {**_item("alarm", ends), "type": "Alarm"}
    timers = parse_alexa(
        {"sorted_active": [_item("p", None, status="PAUSED"), alarm, 42, "x", {"type": "Timer"}, _item("ok", ends)]}
    )
    assert [(t.key, t.paused) for t in timers] == [("alexa:p", True), ("alexa:ok", False)]
    assert parse_alexa({}) == []
    assert parse_alexa({"sorted_active": "not json"}) == []
    assert parse_alexa({"sorted_active": [_item("bad", None, status="ON")]}) == []


# ---------------------------------------------------------------- with the controller


async def test_alexa_timer_lifecycle(hass: HomeAssistant) -> None:
    link = FakeLink()
    controller = ClockController(hass, [], [], link, alexa_sensors=[ECHO])
    await controller.async_start()
    link.up()
    assert link.labels() == ["timeinfo", "ClearIndicator"]

    ends1 = dt_util.utcnow() + timedelta(minutes=15)
    ends2 = dt_util.utcnow() + timedelta(minutes=20)
    _set(hass, _item(ID1, ends1), _item(ID2, ends2, label="pasta"))
    await _settle(hass)
    assert link.labels()[2:] == ["SetAlert", "SetAlert"]
    assert controller.timers[f"alexa:{ID2}"].name == "pasta"

    # Polling the same timers again (the sensor's process_timestamp moves) sends nothing.
    _set(hass, _item(ID1, ends1), _item(ID2, ends2, label="pasta"))
    await _settle(hass)
    assert len(link.sent) == 4

    # Pause deletes it; resume (later end) shows it again with the same token.
    _set(hass, _item(ID1, None, status="PAUSED"), _item(ID2, ends2))
    await _settle(hass)
    assert link.labels()[4:] == ["DeleteAlert"]
    assert controller.timers[f"alexa:{ID1}"].paused
    _set(hass, _item(ID1, ends1 + timedelta(minutes=1)), _item(ID2, ends2))
    await _settle(hass)
    assert link.labels()[5:] == ["SetAlert"]
    token = protocol.alert_token(f"alexa:{ID1}").encode()
    assert token in link.sent[4] and token in link.sent[5]

    # Cancel (drops off the sensor long before its end): DeleteAlert, no pulse.
    _set(hass, _item(ID2, ends2))
    await _settle(hass)
    assert link.labels()[6:] == ["DeleteAlert"]
    hass.states.async_set(ECHO, "unavailable", {"sorted_active": [], "sorted_all": []})
    await _settle(hass)
    assert link.labels()[7:] == ["DeleteAlert"]
    assert controller.timers == {}
    await controller.async_stop()


async def test_alexa_timer_finishing_pulses_once(hass: HomeAssistant) -> None:
    """The alarm at the end time pulses, and the sensor dropping the timer afterwards doesn't pulse again."""
    ends = dt_util.utcnow() + timedelta(seconds=1)
    _set(hass, _item(ID1, ends))  # already running when Home Assistant starts
    link = FakeLink()
    controller = ClockController(hass, [], [], link, alexa_sensors=[ECHO])
    await controller.async_start()
    link.up()
    assert link.labels() == ["timeinfo", "ClearIndicator", "SetAlert"]
    await asyncio.sleep(2.3)  # the timer ends, then the pulse is due 1 s later
    await _settle(hass)
    assert link.labels()[3:] == ["timers=active"]
    _set(hass)  # Alexa Media Player catches up
    await _settle(hass)
    await asyncio.sleep(1.2)
    await _settle(hass)
    assert link.labels()[3:] == ["timers=active"]
    await controller.async_stop()


async def test_alexa_timer_dropping_off_at_its_end_pulses(hass: HomeAssistant) -> None:
    """The sensor drops the timer right before its end: finished, not cancelled; the alarm doesn't pulse again."""
    link = FakeLink()
    controller = ClockController(hass, [], [], link, alexa_sensors=[ECHO])
    await controller.async_start()
    link.up()
    _set(hass, _item(ID1, dt_util.utcnow() + timedelta(seconds=2.5)))
    await _settle(hass)
    await asyncio.sleep(1)
    _set(hass)
    await _settle(hass)
    await asyncio.sleep(2.8)  # past the pulse and past the old alarm time
    await _settle(hass)
    assert link.labels()[2:] == ["SetAlert", "timers=active"]
    await controller.async_stop()


async def test_changing_to_alexa_keeps_the_link(hass: HomeAssistant) -> None:
    ends = dt_util.utcnow() + timedelta(minutes=3)
    hass.states.async_set("timer.tea", "active", {"finishes_at": ends.isoformat()})
    _set(hass, _item(ID1, ends + timedelta(minutes=1)))
    link = FakeLink()
    controller = ClockController(hass, [], ["timer.tea"], link)
    await controller.async_start()
    link.up()
    assert link.labels()[2:] == ["SetAlert"]
    controller.update_sources([], ["timer.tea"], [ECHO])
    await _settle(hass)
    assert controller.connected
    assert set(controller.timers) == {"helper:timer.tea", f"alexa:{ID1}"}
    assert link.labels()[3:] == ["SetAlert"]
    controller.update_sources([], ["timer.tea"], [])
    await _settle(hass)
    assert link.labels()[4:] == ["DeleteAlert"]
    await controller.async_stop()


async def test_options_with_alexa_only(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Kitchen clock", options={CONF_HELPERS: ["timer.tea"]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    _set(hass, _item(ID1, dt_util.utcnow() + timedelta(minutes=5)))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ASSIST_DEVICES: [], CONF_HELPERS: [], CONF_ALEXA_SENSORS: [ECHO]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    state = hass.states.get("sensor.kitchen_clock_timers")
    assert state.state == "1"
    assert state.attributes["timers"][0]["id"] == f"alexa:{ID1}"
    assert await hass.config_entries.async_unload(entry.entry_id)
