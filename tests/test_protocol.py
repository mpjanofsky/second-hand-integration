"""The vendored protocol against the shared golden frames (also used by the Kiosk plugin's Java tests)."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.second_hand import protocol as p

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden-frames.txt"
NY = ZoneInfo("America/New_York")


def _golden() -> dict[str, tuple[dict[str, str], bytes]]:
    out = {}
    for line in GOLDEN.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, params, hexbytes = (part.strip() for part in line.split("|"))
        kv = dict(item.split("=", 1) for item in params.split() if "=" in item)
        out[name] = (kv, bytes.fromhex(hexbytes))
    return out


G = _golden()


def _seq(name: str) -> p.Framer:
    return p.Framer(int(G[name][0]["seq"]))


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("discover", lambda kv: p.discover()),
        ("timeinfo", lambda kv: p.time_info(kv["time"])),
        ("clear-indicator", lambda kv: p.clear_indicator()),
        ("set-alert", lambda kv: p.set_timer(kv["token"], kv["end"])),
        ("timers-active", lambda kv: p.timers_active()),
        ("delete-alert", lambda kv: p.delete_alert(kv["token"])),
        ("timers-cleared", lambda kv: p.timers_cleared()),
        ("set-alert-derived-token", lambda kv: p.set_timer(p.alert_token(kv["timerId"]), kv["end"])),
    ],
)
def test_messages_match_golden(name, build) -> None:
    kv, want = G[name]
    assert _seq(name).frame(build(kv)) == want


def test_derived_token_matches_plugin() -> None:
    kv, _ = G["set-alert-derived-token"]
    assert p.alert_token(kv["timerId"]) == kv["token"]


def test_escaping() -> None:
    assert p.Framer(1).frame(bytes([0x01, 0xF0, 0x02, 0xF1, 0x03, 0xF2, 0x04])) == G["escape-payload"][1]
    assert p.Framer(1).frame(bytes([0xEE])) == G["escape-checksum-low"][1]
    assert p.Framer(1).frame(bytes([0xFF] * 240 + [0xEE])) == G["escape-checksum-high"][1]


def test_seq_skips_reserved() -> None:
    f = p.Framer(0xEF)
    assert f.frame(b"\x00") == G["seq-before-reserved"][1]
    assert f.frame(b"\x00") == G["seq-skips-reserved"][1]


def test_deframer_every_split_point() -> None:
    wire = G["inbound-discover-response"][1]
    for cut in range(len(wire) + 1):
        d = p.Deframer()
        frames = d.feed(b"\x00garbage" + wire[:cut]) + d.feed(wire[cut:])
        assert len(frames) == 1, cut
        assert frames[0].checksum_ok and frames[0].seq == 0
        event = p.decode_event(frames[0].payload)
        assert event is not None
        assert (event.namespace, event.name) == ("Alexa.Discovery", "Discover.Response")


def test_deframer_round_trip_and_bad_checksum() -> None:
    raw = bytes([0x01, 0xF0, 0x02, 0xF1, 0x03, 0xF2, 0x04])
    (frame,) = p.Deframer().feed(p.Framer(1).frame(raw))
    assert frame.payload == raw and frame.checksum_ok
    broken = bytearray(p.Framer(1).frame(b"\x01\x02"))
    broken[4] ^= 0x01
    (frame,) = p.Deframer().feed(bytes(broken))
    assert not frame.checksum_ok


def test_deframer_drops_oversize_garbage() -> None:
    d = p.Deframer()
    assert d.feed(bytes([p.STX]) + b"\x01" * (p.Deframer.MAX_FRAME + 10)) == []
    assert len(d.feed(p.Framer(1).frame(p.discover()))) == 1


def test_decode_event_rejects_junk() -> None:
    assert p.decode_event(b"\xff\xff\xff") is None
    assert p.decode_event(b"") is None


def test_clock_time_is_local_and_25_chars() -> None:
    assert p.clock_time(datetime(2026, 10, 14, 12, 0, 0, 999, tzinfo=NY)) == "2026-10-14T12:00:00-04:00"
    # Across the DST change (2026-11-01 02:00 EDT -> 01:00 EST).
    assert p.clock_time(datetime(2026, 11, 1, 3, 0, tzinfo=NY)) == "2026-11-01T03:00:00-05:00"
    with pytest.raises(ValueError):
        p.clock_time(datetime(2026, 10, 14, 12, 0))
