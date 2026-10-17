"""A paused Indevolt says so on its battery card, in every language.

The card's inverter chip shows what the battery itself does; in its own
self-consumption mode a paused Indevolt may be discharging. A second chip shows
the translated control status while Omnibattery is not steering the battery.
Static assertions on the panel source, same style as the other panel tests.
"""

import json
from pathlib import Path

from custom_components.omnibattery.drivers.indevolt import CONTROL_STATUS_OPTIONS

COMPONENT = Path("custom_components/omnibattery")
PANEL = COMPONENT / "frontend" / "marstek-panel.js"
LANGUAGES = ("ca", "de", "en", "es", "fr", "nl")


def test_every_control_status_has_a_label_in_every_language():
    files = [COMPONENT / "strings.json"] + [COMPONENT / "translations" / f"{l}.json" for l in LANGUAGES]
    for path in files:
        states = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]["control_status"]["state"]
        assert set(states) == set(CONTROL_STATUS_OPTIONS), path


def test_the_battery_card_shows_the_control_status_unless_active():
    panel = PANEL.read_text(encoding="utf-8")
    assert 'controlStatus: "control_status"' in panel
    assert '<span class="chip bat-control" style="display:none"></span>' in panel
    start = panel.index("  _patchBatteryCard(r, b) {")
    body = panel[start:panel.index("    // SOC ring", start)]
    assert 'ctlState !== "active" && ctlState !== "starting"' in body
    assert "this._hass.formatEntityState(b.controlStatus)" in body
    assert 'r.control.style.display = "none";' in body


def test_two_header_chips_wrap_at_phone_width():
    panel = PANEL.read_text(encoding="utf-8")
    assert ".bat-head { display: flex; flex-wrap: wrap;" in panel
    assert ".bat-chips { display: flex; flex-wrap: wrap;" in panel
