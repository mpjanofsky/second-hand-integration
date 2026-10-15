# Building and changing Second Hand for Home Assistant

```bash
uv venv --python 3.14 .venv
VIRTUAL_ENV=.venv uv pip install pytest-homeassistant-custom-component
.venv/bin/python -m pytest -q
```

- `custom_components/second_hand/protocol.py`: framing and messages, vendored from the protocol project;
  checked against `fixtures/golden-frames.txt`, the same vectors the Kiosk plugin's Java tests use.
- `reconcile.py`: desired timers in, clock commands out. Pure; no Home Assistant.
- `sources.py`: Assist (voice satellite) timers and timer helpers. All use of HA's internal TimerManager is
  in this file.
- `controller.py`: one clock, wiring sources, reconciler and the link.
- `link.py`: connect, Discover handshake, hold the link, retry with backoff. Transport-agnostic.
- `bluez.py`: BlueZ over D-Bus (pairing agent, search, call-in listener) and the RFCOMM socket. Linux only.
- `tests/fake_clock.py`: a fake clock that answers Discover like the real one.
