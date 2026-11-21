"""Temperature charge limit status: is charging being throttled by heat right now?

The derate was computed per battery but only shown as an attribute of the
integration status. The status sensor is on while any battery is derated and
lists, per battery, the power it may take as a percentage of normal.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.binary_sensor import TemperatureChargeLimitStatusSensor
from custom_components.omnibattery.control.temperature_limit import TemperatureChargeLimitManager

PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


def _sensor(temperatures, enabled=True):
    controller = SimpleNamespace(
        temp_charge_limit_enabled=enabled,
        temp_limit_apply_discharge=False,
        _temp_charge_limit_c=40,
        _temp_charge_limit_band_c=10,
        _temp_charge_limit_floor_pct=40,
        coordinators=[
            SimpleNamespace(name=name, data={"internal_temperature": temp})
            for name, temp in temperatures.items()
        ],
    )
    controller._temp_limit_mgr = TemperatureChargeLimitManager(None, controller)
    return TemperatureChargeLimitStatusSensor(None, SimpleNamespace(data={}), controller)


@pytest.mark.parametrize(
    ("temperature", "on", "pct"),
    [
        (35, False, 100.0),  # below the limit
        (40, False, 100.0),  # at the limit: still full power
        (45, True, 70.0),  # half way through the band: 1 - 0.5 x 0.6
        (55, True, 40.0),  # past the band: the floor
    ],
)
def test_on_while_a_battery_is_derated(temperature, on, pct):
    sensor = _sensor({"Venus": temperature, "SolarFlow": 30})

    assert sensor.is_on is on
    attrs = sensor.extra_state_attributes
    assert attrs["batteries"]["Venus"]["derate_pct"] == pct
    assert attrs["hottest_battery"] == "Venus"
    assert (attrs["limit_c"], attrs["band_c"]) == (40, 10)


def test_an_unknown_temperature_does_not_derate():
    sensor = _sensor({"Venus": None})

    assert sensor.is_on is False
    assert sensor.extra_state_attributes["hottest_battery"] is None


def test_off_while_the_feature_is_off():
    sensor = _sensor({"Venus": 55}, enabled=False)

    assert sensor.is_on is False
    assert sensor.extra_state_attributes["enabled"] is False


def test_the_temperature_card_shows_its_status():
    panel = PANEL.read_text(encoding="utf-8")
    card = panel[panel.index('tk: "secTempLimit"') :]
    card = card[: card.index("],")]
    assert '{ key: "temperature_charge_limit_status", domain: "binary_sensor", lk: "statusTempLimit"' in card
    assert panel.count("statusTempLimit:") == 6
