"""Characterization of ``soc_status`` and ``soc_set`` in the shared core (T7.3, #41, #44).

Pins, for the eight drivers, which of the two keys each one publishes and what the
core does with them, so the ``is_self_managed()`` and ``device_max_soc()`` queries
can replace the direct key reads without changing a result.

Only Zendure publishes either key (``socStatus`` -> ``soc_status``, ``socSet`` ->
``soc_set``); the other seven never do. ``None`` ("published but unknown", which
the coordinator stores as-is) and an absent key are added for ``soc_status`` on every
driver; for ``soc_set`` only Zendure can carry a value, so the others pin "absent".

Core sites:

* ``ChargeDischargeController._is_battery_manual_owned`` and
  ``_sync_calibration_ownership`` treat ``soc_status == 1`` as "calibrating";
* ``WeeklyFullChargeManager._original_max_soc`` treats the PRESENCE of ``soc_set``
  in ``coordinator.data`` (even with a None value) as "the device keeps the
  maximum SOC itself" and then restores the coordinator's live ``max_soc`` when
  it is between 1 and 99; otherwise the stored configuration wins.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

import custom_components.omnibattery.drivers as drivers_pkg
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
)
from custom_components.omnibattery.drivers import zendure
from tests.test_driver_construction_characterization import (
    BRAND_CONFIGS,
    _build_coordinator,
)

ABSENT = object()  # the driver does not publish the key

# brand -> every value the driver can leave under the key
SOC_STATUS = {
    "zendure": [0, 1, 2, None, ABSENT],
    **{b: [None, ABSENT] for b in (
        "marstek", "esphome", "huawei", "anker", "hoymiles", "indevolt", "sessy",
    )},
}
SOC_SET = {
    "zendure": [95.0, 100.0, 70.0, None, ABSENT],
    # The other seven never write the key, so only "absent" is reachable.
    **{b: [ABSENT] for b in (
        "marstek", "esphome", "huawei", "anker", "hoymiles", "indevolt", "sessy",
    )},
}
STATUS_CASES = [(b, v) for b, vs in SOC_STATUS.items() for v in vs]
STATUS_IDS = [f"{b}-{'absent' if v is ABSENT else v!r}" for b, v in STATUS_CASES]
SET_CASES = [(b, v) for b, vs in SOC_SET.items() for v in vs]
SET_IDS = [f"{b}-{'absent' if v is ABSENT else v!r}" for b, v in SET_CASES]


def _driver(brand):
    """The real driver for the brand (needs a running loop: Marstek's client)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)
        return _build_coordinator(BRAND_CONFIGS[brand]).driver


class _Hashable(SimpleNamespace):
    """A coordinator stand-in usable as a dict key (the controller keys by it)."""

    __hash__ = object.__hash__


def _data(key, value, **rest):
    data = dict(rest)
    if value is not ABSENT:
        data[key] = value
    return data


def test_published_keys_per_driver():
    assert zendure._PROP_TO_KEY["socStatus"] == "soc_status"
    assert zendure._PROP_TO_KEY["socSet"] == "soc_set"
    # No other driver module names either key (nor the Zendure property).
    for path in Path(inspect.getfile(drivers_pkg)).parent.glob("*.py"):
        if path.name == "zendure.py":
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("soc_status", "soc_set", "socStatus", "socSet"):
            assert needle not in text, f"{path.name} publishes {needle}"


# ---------------------------------------------------------------------------
# #41: calibration ownership
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "value"), STATUS_CASES, ids=STATUS_IDS)
async def test_calibrating_battery_is_manual_owned(brand, value):
    coord = _Hashable(
        name="BAT1", battery_manual_mode_enabled=False,
        driver=_driver(brand), data=_data("soc_status", value, battery_soc=50),
    )
    expected = value == 1  # the core's old reading: soc_status == 1
    assert ChargeDischargeController._is_battery_manual_owned(coord) is expected

    ctrl = SimpleNamespace(
        coordinators=[coord], _calibrating_batteries=set(),
        _non_responsive=Mock(), _reset_battery_ownership_state=Mock(),
    )
    ChargeDischargeController._sync_calibration_ownership(ctrl)
    assert (coord in ctrl._calibrating_batteries) is expected
    assert ctrl._non_responsive.clear.called is expected
    assert ctrl._reset_battery_ownership_state.called is expected


@pytest.mark.parametrize("data", [None, {}], ids=["no-data", "empty-data"])
async def test_a_battery_without_data_is_not_calibrating(data):
    coord = _Hashable(
        name="BAT1", battery_manual_mode_enabled=False,
        driver=_driver("zendure"), data=data,
    )
    assert ChargeDischargeController._is_battery_manual_owned(coord) is False


# ---------------------------------------------------------------------------
# #44: where the weekly run restores the maximum SOC from
# ---------------------------------------------------------------------------

def _expected_original(value, live, stored):
    """What the core made of the key before the query existed."""
    if value is not ABSENT:  # presence, whatever the value
        if 0 < live < 100:
            return live
    return stored


@pytest.mark.parametrize("live", [95, 100, 0])
@pytest.mark.parametrize(("brand", "value"), SET_CASES, ids=SET_IDS)
async def test_original_max_soc(brand, value, live):
    coord = SimpleNamespace(
        name="BAT1", max_soc=live, driver=_driver(brand),
        data=_data("soc_set", value, battery_soc=50),
        _config_entry=SimpleNamespace(
            data={"batteries": [{"name": "BAT1", "max_soc": 85}]}
        ),
    )
    assert WeeklyFullChargeManager._original_max_soc(coord) == _expected_original(
        value, live, 85
    )


@pytest.mark.parametrize("data", [None, {}], ids=["no-data", "empty-data"])
async def test_original_max_soc_without_data_uses_the_stored_value(data):
    coord = SimpleNamespace(
        name="BAT1", max_soc=95, driver=_driver("zendure"), data=data,
        _config_entry=SimpleNamespace(
            data={"batteries": [{"name": "BAT1", "max_soc": 85}]}
        ),
    )
    assert WeeklyFullChargeManager._original_max_soc(coord) == 85
