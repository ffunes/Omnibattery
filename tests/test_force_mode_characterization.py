"""Characterization of manual-control mode and critical control keys (T7.7).

Pins today's behaviour for the eight brands (and the model/version variants that
change a driver's definitions) so the force-mode capability and the critical
control keys can move into the driver without changing it (plan #45, #24).
Every value is a literal observed before the move.
"""

from __future__ import annotations

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery import ChargeDischargeController
from tests.test_driver_construction_characterization import (
    BRAND_CONFIGS,
    _build_coordinator,
)


@pytest.fixture(autouse=True)
def _no_ha_runtime(monkeypatch):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)


def _critical_keys(coordinator):
    """The critical key set the coordinator applies to this driver's reads."""
    return coordinator.driver.critical_control_keys


_ZENDURE_MODELS = (
    "solarflow_800", "solarflow_800_plus", "solarflow_800_pro", "1600ac_plus",
    "2400ac_pro", "2400ac_plus", "3000mix_ac_plus", "4000mix_ac_plus", "4000mix_pro",
)
_HOYMILES_MODELS = ("MS-A2", "HMS-800W", "MS-A2X-1", "HAS-5G", "Unknown")


def _variants():
    for version in ("v2", "v3", "vA", "vD"):
        yield f"marstek {version}", dict(BRAND_CONFIGS["marstek"], battery_version=version)
    for model in _ZENDURE_MODELS:
        yield f"zendure {model}", dict(BRAND_CONFIGS["zendure"], zendure_model=model)
    for brand in ("esphome", "anker", "sessy", "indevolt", "huawei"):
        yield brand, BRAND_CONFIGS[brand]
    for model in _HOYMILES_MODELS:
        yield f"hoymiles {model}", dict(BRAND_CONFIGS["hoymiles"], hoymiles_model=model)


# name -> (capabilities.has_force_mode, needs_software_manual_control)
# Huawei is the one driver where they differ: its register map has a force-mode
# register (the capability is True) but it publishes no force_mode select nor
# set_charge_power number, so manual control is asserted by software.
EXPECTED_MODE = {
    "marstek v2": (True, False), "marstek v3": (True, False),
    "marstek vA": (True, False), "marstek vD": (True, False),
    "esphome": (True, False),
    "huawei": (True, True),
    "anker": (False, True), "sessy": (False, True), "indevolt": (False, True),
}
for _model in _ZENDURE_MODELS:
    EXPECTED_MODE[f"zendure {_model}"] = (False, True)
for _model in _HOYMILES_MODELS:
    EXPECTED_MODE[f"hoymiles {_model}"] = (False, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,config", list(_variants()), ids=[n for n, _ in _variants()])
async def test_manual_control_decision(name, config):
    coordinator = _build_coordinator(config)
    has_force_mode, needs_software = EXPECTED_MODE[name]
    assert coordinator.driver.capabilities.has_force_mode is has_force_mode
    assert coordinator.needs_software_manual_control is needs_software


DATA = {
    "empty": {},
    "charge": {"force_mode": 1, "set_charge_power": 700, "set_discharge_power": 0},
    "discharge": {"force_mode": 2, "set_charge_power": 0, "set_discharge_power": 600},
    "none": {"force_mode": 0, "set_charge_power": 700, "set_discharge_power": 600},
    "charge_partial": {"force_mode": 1, "set_charge_power": 700},
    "discharge_partial": {"force_mode": 2, "set_discharge_power": 600},
    "charge_none": {"force_mode": 1, "set_charge_power": None, "set_discharge_power": 0},
    "charge_negative": {"force_mode": 1, "set_charge_power": -5, "set_discharge_power": 0},
    "charge_float": {"force_mode": 1.4, "set_charge_power": 700.4, "set_discharge_power": 0},
    "mode_text": {"force_mode": "x", "set_charge_power": 700, "set_discharge_power": 0},
}

# A driver whose net_power_from_data decodes force_mode + set_*_power itself
# (Marstek, ESPHome, Huawei) answers first; the rest fall through to the
# fallback decode, which clamps a negative setpoint to 0 and does not round.
_GENERIC = {
    "empty": 0.0, "charge": 700.0, "discharge": -600.0, "none": 0.0,
    "charge_partial": 700.0, "discharge_partial": -600.0, "charge_none": 0.0,
    "charge_negative": 0.0, "charge_float": 700.4, "mode_text": 0.0,
}
_OWN_DECODE = {**_GENERIC, "charge_negative": -5.0, "charge_float": 700.0}
EXPECTED_COMMANDED = {
    "marstek": _OWN_DECODE, "esphome": _OWN_DECODE, "huawei": _OWN_DECODE,
    "zendure": _GENERIC, "anker": _GENERIC, "sessy": _GENERIC,
    "indevolt": _GENERIC, "hoymiles": _GENERIC,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("brand", list(BRAND_CONFIGS))
async def test_commanded_power_from_telemetry(brand):
    for label, data in DATA.items():
        coordinator = _build_coordinator(BRAND_CONFIGS[brand])
        coordinator.manual_force_mode = "None"
        coordinator.commanded_charge_power = 0
        coordinator.commanded_discharge_power = 0
        coordinator.data = dict(data)
        assert (
            ChargeDischargeController._manual_battery_commanded_power(coordinator)
            == EXPECTED_COMMANDED[brand][label]
        ), (brand, label)


@pytest.mark.asyncio
@pytest.mark.parametrize("brand", list(BRAND_CONFIGS))
async def test_commanded_power_from_manual_setpoints(brand):
    coordinator = _build_coordinator(BRAND_CONFIGS[brand])
    coordinator.data = {}
    commanded = ChargeDischargeController._manual_battery_commanded_power

    coordinator.manual_force_mode = "Charge"
    coordinator.manual_set_charge_power = 500
    assert commanded(coordinator) == 500
    coordinator.manual_set_charge_power = 0
    coordinator.commanded_charge_power = 300
    assert commanded(coordinator) == 300

    coordinator.manual_force_mode = "Discharge"
    coordinator.manual_set_discharge_power = 400
    assert commanded(coordinator) == -400
    coordinator.manual_set_discharge_power = 0
    coordinator.commanded_discharge_power = 250
    assert commanded(coordinator) == -250

    coordinator.manual_force_mode = "None"
    coordinator.commanded_charge_power = 200
    coordinator.commanded_discharge_power = 50
    assert commanded(coordinator) == 150


_FULL_SET = [
    "ac_power", "battery_power", "battery_soc", "force_mode", "inverter_state",
    "set_charge_power", "set_discharge_power",
]
# brand -> (critical keys any read group of the driver carries, sizes of the
# read groups that carry one)
EXPECTED_CRITICAL = {
    "marstek": (_FULL_SET, [2, 1, 1, 1, 1, 1]),
    "zendure": (["battery_power", "battery_soc"], [26]),
    "esphome": (_FULL_SET, [67]),
    "anker": (["battery_power", "battery_soc"], [8]),
    "sessy": (["battery_power", "battery_soc"], [5]),
    "indevolt": (["ac_power", "battery_power", "battery_soc", "inverter_state"], [8, 15]),
    "huawei": (
        ["battery_power", "battery_soc", "force_mode", "inverter_state",
         "set_charge_power", "set_discharge_power"], [19, 6]),
    "hoymiles": (["battery_power", "battery_soc", "inverter_state"], [15]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("brand", list(BRAND_CONFIGS))
async def test_critical_keys_in_a_read(brand):
    coordinator = _build_coordinator(BRAND_CONFIGS[brand])
    critical = frozenset(_critical_keys(coordinator))
    groups = coordinator.driver.read_groups
    keys = sorted({k for g in groups for k in g.keys} & critical)
    sizes = [len(g.keys) for g in groups if critical & set(g.keys)]
    assert (keys, sizes) == EXPECTED_CRITICAL[brand]


@pytest.mark.asyncio
@pytest.mark.parametrize("brand", list(BRAND_CONFIGS))
async def test_critical_key_vocabulary(brand):
    coordinator = _build_coordinator(BRAND_CONFIGS[brand])
    assert frozenset(_critical_keys(coordinator)) == frozenset(_FULL_SET)
