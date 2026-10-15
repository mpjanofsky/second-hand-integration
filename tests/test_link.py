"""The clock link against a fake clock: handshake, retry, reconnect, stop. Also the BlueZ pairing agent."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from dbus_fast import DBusError

from custom_components.second_hand import link as link_mod, protocol as p
from custom_components.second_hand.bluez import PairingAgent, device_path
from custom_components.second_hand.link import BluetoothLink, LinkState, LinkStatus

from .fake_clock import FakeClock, discover_response


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(link_mod, "BACKOFF", (0.05, 0.1, 0.2, 0.2, 0.2))
    monkeypatch.setattr(link_mod, "HANDSHAKE_TIMEOUT", 0.2)


async def until(check: Callable[[], bool], timeout: float = 2.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.01)


class Events:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.statuses: list[LinkStatus] = []

    def up(self) -> None:
        self.log.append("up")

    def down(self) -> None:
        self.log.append("down")

    def status(self, s: LinkStatus) -> None:
        self.statuses.append(s)


async def started(clock: FakeClock) -> tuple[BluetoothLink, Events]:
    ev = Events()
    link = BluetoothLink(clock.open, ev.status)
    link.start(ev.up, ev.down)
    return link, ev


async def test_handshake_then_frames_in_order() -> None:
    clock = FakeClock()
    link, ev = await started(clock)
    await until(lambda: ev.log == ["up"])
    assert link.status.state is LinkState.CONNECTED
    assert link.status.info == p.ClockInfo("EchoWallClk", "102", "AlexaGadgetsEWC")
    link.send(p.time_info("2026-10-14T12:00:00-04:00"))
    link.send(p.clear_indicator())
    await until(lambda: len(clock.received) == 3)
    assert clock.received == ["Discover", "timeinfo", "ClearIndicator"]
    assert clock.seqs == [1, 2, 3]  # like a real Echo
    await link.async_stop()
    assert link.status.state is LinkState.STOPPED


async def test_first_connect_after_pairing_fails_once() -> None:
    clock = FakeClock(["fail", "ok"])
    link, ev = await started(clock)
    await until(lambda: ev.log == ["up"])
    assert clock.attempts == 2
    waiting = [s for s in ev.statuses if s.state is LinkState.WAITING]
    assert waiting and waiting[0].reason == "Connection reset by peer"
    await link.async_stop()


async def test_silent_clock_is_retried() -> None:
    clock = FakeClock(["silent", "ok"])
    link, ev = await started(clock)
    await until(lambda: ev.log == ["up"])
    assert any(s.reason == "The clock didn't answer in time" for s in ev.statuses)
    await link.async_stop()


async def test_drop_reconnects_with_fresh_sequence() -> None:
    clock = FakeClock()
    link, ev = await started(clock)
    await until(lambda: ev.log == ["up"])
    clock.current.drop()
    await until(lambda: ev.log == ["up", "down", "up"])
    assert clock.received == ["Discover", "Discover"]
    assert clock.seqs == [1, 1]
    await link.async_stop()


async def test_send_while_down_is_dropped() -> None:
    clock = FakeClock(["fail"] * 3)
    link, ev = await started(clock)
    link.send(p.clear_indicator())
    await until(lambda: ev.log == ["up"], timeout=3)
    await asyncio.sleep(0.05)
    assert clock.received == ["Discover"]  # the early send did not leak into the new session
    await link.async_stop()


async def test_wake_skips_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(link_mod, "BACKOFF", (60, 60, 60, 60, 60))
    clock = FakeClock(["fail", "ok"])
    link, ev = await started(clock)
    await until(lambda: link.status.state is LinkState.WAITING)
    link.wake()  # the clock called in
    await until(lambda: ev.log == ["up"], timeout=1)
    await link.async_stop()


async def test_stop_is_prompt_and_leaves_nothing_running() -> None:
    clock = FakeClock(["silent"] * 10)
    link, _ = await started(clock)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(link.async_stop(), 0.5)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not [t for t in pending if "Echo Wall Clock" in t.get_name()]


def test_clock_info_from_response() -> None:
    event = p.decode_event(discover_response("103"))
    assert event is not None
    assert p.decode_clock_info(event) == p.ClockInfo("EchoWallClk", "103", "AlexaGadgetsEWC")
    assert p.decode_clock_info(p.Event("Alerts", "Other", b"")) is None


def test_pairing_agent_only_accepts_the_clock() -> None:
    clock = device_path("/org/bluez/hci0", "aa:bb:cc:dd:ee:ff")
    assert clock == "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    agent = PairingAgent(clock)
    for call in ("RequestConfirmation", "RequestAuthorization", "AuthorizeService"):
        fn = getattr(PairingAgent, call)
        fn = getattr(fn, "__wrapped__", fn)
        args = {"RequestConfirmation": (123456,), "RequestAuthorization": (), "AuthorizeService": (p.discover().hex(),)}[call]
        fn(agent, clock, *args)  # accepted: no error
        with pytest.raises(DBusError):
            fn(agent, "/org/bluez/hci0/dev_11_22_33_44_55_66", *args)
    pin = getattr(PairingAgent.RequestPinCode, "__wrapped__", PairingAgent.RequestPinCode)
    with pytest.raises(DBusError):
        pin(agent, clock)
