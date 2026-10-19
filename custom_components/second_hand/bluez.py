"""BlueZ (Linux Bluetooth) over D-Bus, and the RFCOMM socket to the clock.

Only Bluetooth Classic is involved; HA's own `bluetooth` integration (BLE) keeps using the same adapter.
What a real Echo does, and what this mirrors (proven from a Raspberry Pi with `pi_echo.py`):
- Pairing is "Just Works" (no PIN). The Echo pairs and opens RFCOMM channel 4 in one connection: an encrypted
  socket connect makes BlueZ pair on the spot, with an agent that says yes. Pairing first and connecting
  later fails, because the clock's pairing window closes.
- After pairing, the clock drops the link and calls back, looking for a Serial Port (0x1101) service. The
  partner must be connectable and offer one, or the clock gives up.
- Channel 4 carries all data. Channel 2 is the firmware-update port: never connect to it.

No `from __future__ import annotations` here: dbus-fast reads the D-Bus signatures from the annotations.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
import contextlib
import logging
import os
import socket
import struct
from typing import Any

from dbus_fast import BusType, DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
# Bluetooth names the clocks advertise: the Echo Wall Clock, and the Mickey Mouse Edition (seen as EWCMickeyMouse05B).
NAME_PREFIXES = ("EchoWallClk", "EWCMickeyMouse")


def is_clock_name(name: str) -> bool:
    return name.startswith(NAME_PREFIXES)
DATA_CHANNEL = 4
SOL_BLUETOOTH, BT_SECURITY, BT_SECURITY_MEDIUM = 274, 4, 2
AGENT_PATH = "/io/github/second_hand/agent"
PROFILE_PATH = "/io/github/second_hand/spp"
CALL_IN_HOLD = 30.0
"""How long to keep a call-in's socket open while the link connects out (the clock waits about a minute)."""


class BlueZError(Exception):
    """A BlueZ call failed, or BlueZ isn't reachable."""

    def __init__(self, name: str, text: str = "") -> None:
        super().__init__(f"{name}: {text}" if text else name)
        self.name = name


def sockets_supported() -> bool:
    """Whether this Python can open Bluetooth sockets (CPython built with Bluetooth headers, on Linux)."""
    return all(hasattr(socket, n) for n in ("AF_BLUETOOTH", "BTPROTO_RFCOMM"))


def device_path(adapter: str, address: str) -> str:
    return f"{adapter}/dev_{address.upper().replace(':', '_')}"


# ---------------------------------------------------------------- RFCOMM socket


class RfcommConnection:
    """controller/link Connection over a non-blocking RFCOMM socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._loop = asyncio.get_running_loop()

    async def read(self) -> bytes:
        return await self._loop.sock_recv(self._sock, 4096)

    async def write(self, data: bytes) -> None:
        await self._loop.sock_sendall(self._sock, data)

    def close(self) -> None:
        self._sock.close()


async def open_rfcomm(address: str, adapter_address: str | None = None) -> RfcommConnection:
    """Encrypted RFCOMM connection to channel 4. Pairs on the spot if an agent is registered and allows it."""
    if not sockets_supported():
        raise OSError("This Python has no Bluetooth socket support")
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)  # type: ignore[attr-defined]
    try:
        sock.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", BT_SECURITY_MEDIUM, 0))
        if adapter_address:
            sock.bind((adapter_address, 0))
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, (address, DATA_CHANNEL))
    except BaseException:
        sock.close()
        raise
    return RfcommConnection(sock)


# ---------------------------------------------------------------- D-Bus objects we export


class PairingAgent(ServiceInterface):
    """org.bluez.Agent1 for pairing one clock: says yes to that clock only, no to everything else."""

    def __init__(self, allowed_device: str) -> None:
        super().__init__("org.bluez.Agent1")
        self.allowed = allowed_device

    def _check(self, device: str) -> None:
        if device != self.allowed:
            raise DBusError("org.bluez.Error.Rejected", "Not the clock being paired")

    @method()
    def Release(self) -> None:  # noqa: N802
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # type: ignore[name-defined]  # noqa: F821, N802
        raise DBusError("org.bluez.Error.Rejected", "The clock pairs without a PIN")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # type: ignore[name-defined]  # noqa: F821, N802
        raise DBusError("org.bluez.Error.Rejected", "The clock pairs without a PIN")

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # type: ignore[name-defined]  # noqa: F821, N802
        raise DBusError("org.bluez.Error.Rejected", "The clock pairs without a passkey")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # type: ignore[name-defined]  # noqa: F821, N802
        self._check(device)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # type: ignore[name-defined]  # noqa: F821, N802
        self._check(device)

    @method()
    def RequestAuthorization(self, device: "o") -> None:  # type: ignore[name-defined]  # noqa: F821, N802
        self._check(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:  # type: ignore[name-defined]  # noqa: F821, N802
        self._check(device)

    @method()
    def Cancel(self) -> None:  # noqa: N802
        pass


class CallInProfile(ServiceInterface):
    """org.bluez.Profile1 server for Serial Port (0x1101), so the clock's call-ins find a partner."""

    def __init__(self, on_call_in: Callable[[str], None]) -> None:
        super().__init__("org.bluez.Profile1")
        self._on_call_in = on_call_in

    @method()
    def Release(self) -> None:  # noqa: N802
        pass

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}") -> None:  # type: ignore[name-defined]  # noqa: F821, N802
        address = device.rsplit("dev_", 1)[-1].replace("_", ":")
        _LOGGER.debug("The clock called in")
        # Keep the socket open briefly so the clock doesn't give up while the link connects out to channel 4.
        loop = asyncio.get_running_loop()
        loop.call_later(CALL_IN_HOLD, _close_fd, fd)
        self._on_call_in(address)

    @method()
    def RequestDisconnection(self, device: "o") -> None:  # type: ignore[name-defined]  # noqa: F821, N802
        pass


def _close_fd(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.close(fd)


# ---------------------------------------------------------------- BlueZ client


class BlueZ:
    """The calls this integration needs, as raw D-Bus messages (no introspection)."""

    def __init__(self) -> None:
        self._bus: MessageBus | None = None

    async def connect(self) -> None:
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        except (OSError, ValueError) as err:
            raise BlueZError("org.freedesktop.DBus.Error.NoServer", str(err)) from err

    def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    @property
    def bus(self) -> MessageBus:
        if self._bus is None:
            raise BlueZError("not connected")
        return self._bus

    async def _call(self, path: str, interface: str, member: str, signature: str = "", body: list[Any] | None = None) -> list[Any]:
        reply = await self.bus.call(
            Message(destination=BLUEZ, path=path, interface=interface, member=member, signature=signature, body=body or [])
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            name = reply.error_name if reply is not None else "no reply"
            raise BlueZError(name or "error", str(reply.body[0]) if reply is not None and reply.body else "")
        return reply.body

    async def _objects(self) -> dict[str, dict[str, dict[str, Variant]]]:
        (objects,) = await self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        return objects

    async def _set(self, path: str, interface: str, prop: str, value: Variant) -> None:
        await self._call(path, "org.freedesktop.DBus.Properties", "Set", "ssv", [interface, prop, value])

    # ------------------------------------------------------------ queries

    async def adapters(self) -> dict[str, str]:
        """Adapter path -> its Bluetooth address."""
        return {
            path: ifaces["org.bluez.Adapter1"]["Address"].value
            for path, ifaces in (await self._objects()).items()
            if "org.bluez.Adapter1" in ifaces
        }

    async def device(self, adapter: str, address: str) -> dict[str, Any] | None:
        """Device1 properties (plain values), or None if BlueZ doesn't know the device."""
        props = (await self._objects()).get(device_path(adapter, address), {}).get("org.bluez.Device1")
        return {k: v.value for k, v in props.items()} if props else None

    async def is_paired(self, adapter: str, address: str) -> bool:
        dev = await self.device(adapter, address)
        return bool(dev and dev.get("Paired"))

    async def find_clocks(self, adapter: str, seconds: float) -> dict[str, str]:
        """Search Bluetooth Classic for clocks in pairing mode. Address -> name."""
        with contextlib.suppress(BlueZError):
            await self._call(adapter, "org.bluez.Adapter1", "SetDiscoveryFilter", "a{sv}", [{"Transport": Variant("s", "bredr")}])
        await self._call(adapter, "org.bluez.Adapter1", "StartDiscovery")
        found: dict[str, str] = {}
        try:
            loop = asyncio.get_running_loop()
            end = loop.time() + seconds
            while loop.time() < end:
                for path, ifaces in (await self._objects()).items():
                    dev = ifaces.get("org.bluez.Device1")
                    if not path.startswith(adapter + "/") or dev is None:
                        continue
                    name = dev.get("Name", Variant("s", "")).value
                    if is_clock_name(name) and "RSSI" in dev:  # RSSI only while it is being seen now
                        found[dev["Address"].value] = name
                if found:
                    break
                await asyncio.sleep(1)
        finally:
            with contextlib.suppress(BlueZError):
                await self._call(adapter, "org.bluez.Adapter1", "StopDiscovery")
        return found

    # ------------------------------------------------------------ changes

    @contextlib.asynccontextmanager
    async def pairing(self, adapter: str, address: str) -> AsyncIterator[None]:
        """Let BlueZ pair with this clock (and nothing else) while the block runs."""
        agent = PairingAgent(device_path(adapter, address))
        self.bus.export(AGENT_PATH, agent)
        try:
            await self._call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os", [AGENT_PATH, "NoInputNoOutput"])
            await self._call("/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent", "o", [AGENT_PATH])
            await self._set(adapter, "org.bluez.Adapter1", "Pairable", Variant("b", True))
            yield
        finally:
            with contextlib.suppress(BlueZError):
                await self._call("/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent", "o", [AGENT_PATH])
            self.bus.unexport(AGENT_PATH)

    async def set_trusted(self, adapter: str, address: str) -> None:
        await self._set(device_path(adapter, address), "org.bluez.Device1", "Trusted", Variant("b", True))

    async def remove(self, adapter: str, address: str) -> None:
        """Forget the clock (unpair)."""
        with contextlib.suppress(BlueZError):
            await self._call(adapter, "org.bluez.Adapter1", "RemoveDevice", "o", [device_path(adapter, address)])

    async def listen_for_call_ins(self, adapter: str, on_call_in: Callable[[str], None]) -> Callable[[], Any]:
        """Offer Serial Port (0x1101) and stay connectable. Returns an async unregister function."""
        profile = CallInProfile(on_call_in)
        self.bus.export(PROFILE_PATH, profile)
        await self._set(adapter, "org.bluez.Adapter1", "Connectable", Variant("b", True))
        options = {
            "Name": Variant("s", "Serial Port"),
            "Role": Variant("s", "server"),
            "RequireAuthentication": Variant("b", False),
            "RequireAuthorization": Variant("b", False),
        }
        await self._call("/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile", "osa{sv}", [PROFILE_PATH, SPP_UUID, options])

        async def unregister() -> None:
            with contextlib.suppress(BlueZError):
                await self._call("/org/bluez", "org.bluez.ProfileManager1", "UnregisterProfile", "o", [PROFILE_PATH])
            if self._bus is not None:
                self._bus.unexport(PROFILE_PATH)

        return unregister


async def pair_and_connect(bluez: BlueZ, adapter: str, address: str, seconds: float = 60.0) -> Any:
    """Pair the clock (in pairing mode) and complete a Discover handshake in one connection, like an Echo.

    Returns the clock's ClockInfo. Retries until `seconds` run out, as the first try often fails.
    """
    from .link import ClockLinkError, handshake  # local import keeps this module importable alone

    adapters = await bluez.adapters()
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    last: BaseException | None = None
    pause = 1.0
    async with bluez.pairing(adapter, address):
        while loop.time() < end:
            conn = None
            try:
                conn = await asyncio.wait_for(open_rfcomm(address, adapters.get(adapter)), 40)
                _, _, info = await asyncio.wait_for(handshake(conn), 10)
            except (OSError, TimeoutError, ClockLinkError) as err:
                last = err
                _LOGGER.debug("Pairing attempt failed: %s", err)
                await asyncio.sleep(pause)
                pause = min(pause * 2, 10)  # the first connect after the bond often fails; later ones work
                continue
            finally:
                if conn is not None:
                    conn.close()
            await bluez.set_trusted(adapter, address)
            return info
    raise BlueZError("pairing failed", str(last) if last else "the clock didn't answer")
