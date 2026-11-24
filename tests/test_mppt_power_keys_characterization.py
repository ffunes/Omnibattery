"""Characterization of the MPPT channel keys in the shared core (T7.6, #43).

The core reads the four channel keys ``mppt1_power`` ... ``mppt4_power`` from a
battery's telemetry in five places, each from a hard-coded list:

* ``MarstekVenusAggregateSensor._calculate_battery_cell_power`` (system cell power)
* ``MarstekVenusEfficiencySensor._accumulate`` (dual-plane efficiency, MPPT=0 windows)
* ``ConsumptionTracker._read_total_solar_power_kw`` (total solar production)
* ``SolarProfileTracker.configuration_fingerprint`` (channels in the profile hash;
  frozen, plan #49: it must not change a single byte)
* ``ChargeDischargeController._daily_operation_runtime_decision`` (direct PV in the
  net cell power)

All are driven through the real coordinator and driver. Rows are literals observed on
the pre-refactor code (d49ad636); if one changes, behaviour changed.

Which drivers publish the channels (definitions): Marstek vA/vD (registers),
Zendure on every model that is not AC-coupled (a 2400 AC Pro publishes them although
its ``has_mppt_pv`` is False) and Huawei (one per PV string). The others never write
the keys, so for them the lists are inert; only absent keys are exercised for them
(injecting a channel into a non-publisher is unreachable and is not characterized).

A Zendure 4000 Mix Pro has ``dc_pv_connected`` forced to False (plan section 8), so
the aggregate cell power leaves its channels out. Its MPPT inputs are not optional,
so ``has_connected_mppt_pv`` follows the hardware capability and the tracker,
fingerprint and decision count them (#942).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery.const.registers_common import (
    EFFICIENCY_SENSOR_DEFINITIONS,
)
from custom_components.omnibattery.sensors.aggregate_sensors import (
    MarstekVenusAggregateSensor,
)
from custom_components.omnibattery.sensors.calculated_sensors import (
    MarstekVenusEfficiencySensor,
)
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker
from custom_components.omnibattery.tracking.solar_profile import SolarProfileTracker
from tests.test_ac_contribution_characterization import _decision
from tests.test_driver_construction_characterization import (
    ANKER,
    ESPHOME,
    HOYMILES,
    HUAWEI,
    INDEVOLT,
    MARSTEK,
    SESSY,
    ZENDURE,
    _build_coordinator,
)

MPPT_KEYS = ("mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power")

# variant -> (battery_config, product code or None)
VARIANTS = {
    "marstek-v2": ({**MARSTEK, "battery_version": "v2"}, None),
    "marstek-v3": (MARSTEK, None),
    "marstek-vA-dc": ({**MARSTEK, "battery_version": "vA", "dc_pv_connected": True}, None),
    "marstek-vA-nodc": ({**MARSTEK, "battery_version": "vA", "dc_pv_connected": False}, None),
    "marstek-vD-dc": ({**MARSTEK, "battery_version": "vD", "dc_pv_connected": True}, None),
    "esphome": (ESPHOME, None),
    "zendure-2400ac_pro": (ZENDURE, None),
    "zendure-4000mix_pro": ({**ZENDURE, "zendure_model": "4000mix_pro", "dc_pv_connected": True}, None),
    "zendure-1600ac_plus": ({**ZENDURE, "zendure_model": "1600ac_plus"}, None),
    "zendure-solarflow_800": ({**ZENDURE, "zendure_model": "solarflow_800"}, None),
    "anker-ac-family": (ANKER, None),
    "anker-independent-pv": (ANKER, "DN7M"),
    "sessy": (SESSY, None),
    "indevolt": (INDEVOLT, None),
    "huawei": (HUAWEI, None),
    "hoymiles": (HOYMILES, None),
}

# The channel keys each variant publishes in its definitions (observed).
PUBLISHED = {
    "marstek-v2": (), "marstek-v3": (),
    "marstek-vA-dc": MPPT_KEYS, "marstek-vA-nodc": MPPT_KEYS, "marstek-vD-dc": MPPT_KEYS,
    "esphome": (),
    "zendure-2400ac_pro": MPPT_KEYS, "zendure-4000mix_pro": MPPT_KEYS,
    "zendure-1600ac_plus": (), "zendure-solarflow_800": MPPT_KEYS,
    "anker-ac-family": (), "anker-independent-pv": (),
    "sessy": (), "indevolt": (), "huawei": ("mppt1_power", "mppt2_power"),
    "hoymiles": (),
}

# scenario -> telemetry added to the base data (None values are kept as None)
SCENARIOS = {
    "none": {},
    "solar_only": {"solar_power": 500},
    "full": {"mppt1_power": 100, "mppt2_power": 200, "mppt3_power": 300, "mppt4_power": 400},
    "partial": {"mppt1_power": 250},
    "none_first": {"mppt1_power": None, "mppt2_power": 150},
    "negative": {"mppt1_power": -50, "mppt2_power": 100},
    "zero": {"mppt1_power": 0, "mppt2_power": 0, "mppt3_power": 0, "mppt4_power": 0},
    "full_and_solar": {
        "mppt1_power": 100, "mppt2_power": 200, "mppt3_power": 300, "mppt4_power": 400,
        "solar_power": 500,
    },
}
BASE_DATA = {"ac_power": -200, "battery_power": 500, "battery_soc": 50, "ac_delivered_power": 150}


@pytest.fixture(autouse=True)
def _no_ha_runtime(monkeypatch):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)


def _coordinator(variant):
    config, product_code = VARIANTS[variant]
    coordinator = _build_coordinator(config)
    coordinator._is_connected = True
    coordinator._is_shutting_down = False
    if product_code:
        coordinator.driver._product_code = product_code
    return coordinator


def _n(value):
    if value is None or isinstance(value, str):
        return value
    return round(float(value), 3) + 0.0


def _aggregate(coordinator):
    sensor = MarstekVenusAggregateSensor.__new__(MarstekVenusAggregateSensor)
    sensor.coordinators = [coordinator]
    sensor.definition = {"key": "system_battery_cell_power", "precision": 3}
    return _n(sensor._calculate_battery_cell_power())


def _efficiency(coordinator):
    """(AC kWh, DC kWh) integrated over one 10 s step; None when the mode is off."""
    sensor = MarstekVenusEfficiencySensor(coordinator, EFFICIENCY_SENSOR_DEFINITIONS[0])
    if not sensor._integrate_mode:
        return None
    ticks = iter([1000.0, 1010.0])
    with patch(
        "custom_components.omnibattery.sensors.calculated_sensors.time.monotonic",
        lambda: next(ticks),
    ):
        sensor._accumulate()
        sensor._accumulate()
    return (
        round(sensor._charge_ac_kwh + sensor._discharge_ac_kwh, 9),
        round(sensor._charge_dc_kwh + sensor._discharge_dc_kwh, 9),
    )


def _tracker_solar(coordinator):
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._hass = SimpleNamespace(states=None)
    tracker._controller = SimpleNamespace(
        solar_production_sensor=None, coordinators=[coordinator]
    )
    return _n(tracker._read_total_solar_power_kw())


def _fingerprint(coordinator):
    tracker = SolarProfileTracker.__new__(SolarProfileTracker)
    tracker._hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid"))
    tracker._config_entry = SimpleNamespace(data={})
    tracker._controller = SimpleNamespace(
        solar_production_sensor=None, coordinators=[coordinator]
    )
    return tracker.configuration_fingerprint()[:16]


def _net_cell_power(coordinator):
    plain = _decision(coordinator, False)
    if plain["source"] != "runtime_measured":
        return (None, plain["action_mask"])
    return (_n(plain["charge_power_w"] - plain["discharge_power_w"]), plain["action_mask"])


def _reachable(variant, scenario):
    keys_in_scenario = set(SCENARIOS[scenario]) & set(MPPT_KEYS)
    return keys_in_scenario <= set(PUBLISHED[variant])


def compute(variant, scenario):
    coordinator = _coordinator(variant)
    coordinator.data = {**BASE_DATA, **SCENARIOS[scenario]}
    return {
        "aggregate": _aggregate(coordinator),
        "efficiency": _efficiency(coordinator),
        "tracker": _tracker_solar(coordinator),
        "fingerprint": _fingerprint(coordinator),
        "decision": _net_cell_power(coordinator),
    }


@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_published_channel_keys(variant):
    coordinator = _coordinator(variant)
    published = tuple(
        d["key"] for d in coordinator.driver.all_definitions
        if d.get("key") in MPPT_KEYS
    )
    assert published == PUBLISHED[variant]


@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_capability_declares_exactly_the_published_keys(variant):
    """T7.6: ``capabilities.mppt_power_keys`` is what the driver publishes, in order."""
    coordinator = _coordinator(variant)
    assert coordinator.capabilities.mppt_power_keys == PUBLISHED[variant]


CASES = [(v, s) for v in VARIANTS for s in SCENARIOS if _reachable(v, s)]


@pytest.mark.parametrize(("variant", "scenario"), CASES, ids=[f"{v}|{s}" for v, s in CASES])
async def test_channel_sites(variant, scenario):
    assert compute(variant, scenario) == GOLDEN[(variant, scenario)]


GOLDEN: dict = {
    ('marstek-v2', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-v2', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-v3', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-v3', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vA-dc', 'none'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': 'd9a285795f531019', 'decision': (200.0, 2)},
    ('marstek-vA-dc', 'solar_only'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': 'd9a285795f531019', 'decision': (200.0, 2)},
    ('marstek-vA-dc', 'full'): {'aggregate': 1200.0, 'efficiency': (0.0, 0.0), 'tracker': 1.0, 'fingerprint': '53489c0233807744', 'decision': (1200.0, 1)},
    ('marstek-vA-dc', 'partial'): {'aggregate': 450.0, 'efficiency': (0.0, 0.0), 'tracker': 0.25, 'fingerprint': '3900b043b04ce616', 'decision': (450.0, 1)},
    ('marstek-vA-dc', 'none_first'): {'aggregate': 350.0, 'efficiency': (0.0, 0.0), 'tracker': 0.15, 'fingerprint': 'e0a1830b4ef5280b', 'decision': (350.0, 1)},
    ('marstek-vA-dc', 'negative'): {'aggregate': 250.0, 'efficiency': (0.0, 0.0), 'tracker': 0.1, 'fingerprint': 'e0a1830b4ef5280b', 'decision': (300.0, 1)},
    ('marstek-vA-dc', 'zero'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': 0.0, 'fingerprint': '53489c0233807744', 'decision': (200.0, 2)},
    ('marstek-vA-dc', 'full_and_solar'): {'aggregate': 1200.0, 'efficiency': (0.0, 0.0), 'tracker': 1.0, 'fingerprint': '53489c0233807744', 'decision': (1200.0, 1)},
    ('marstek-vA-nodc', 'none'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vA-nodc', 'solar_only'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vA-nodc', 'full'): {'aggregate': 200.0, 'efficiency': (0.0, 0.0), 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vA-nodc', 'partial'): {'aggregate': 200.0, 'efficiency': (0.0, 0.0), 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vA-nodc', 'none_first'): {'aggregate': 200.0, 'efficiency': (0.0, 0.0), 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vA-nodc', 'negative'): {'aggregate': 200.0, 'efficiency': (0.0, 0.0), 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vA-nodc', 'zero'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vA-nodc', 'full_and_solar'): {'aggregate': 200.0, 'efficiency': (0.0, 0.0), 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('marstek-vD-dc', 'none'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': '6d6d4391b04949f5', 'decision': (200.0, 2)},
    ('marstek-vD-dc', 'solar_only'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': '6d6d4391b04949f5', 'decision': (200.0, 2)},
    ('marstek-vD-dc', 'full'): {'aggregate': 1200.0, 'efficiency': (0.0, 0.0), 'tracker': 1.0, 'fingerprint': '7a3093adbb8e2c6f', 'decision': (1200.0, 1)},
    ('marstek-vD-dc', 'partial'): {'aggregate': 450.0, 'efficiency': (0.0, 0.0), 'tracker': 0.25, 'fingerprint': '25b3ad16651ec65b', 'decision': (450.0, 1)},
    ('marstek-vD-dc', 'none_first'): {'aggregate': 350.0, 'efficiency': (0.0, 0.0), 'tracker': 0.15, 'fingerprint': '0d8fcd0eed75742f', 'decision': (350.0, 1)},
    ('marstek-vD-dc', 'negative'): {'aggregate': 250.0, 'efficiency': (0.0, 0.0), 'tracker': 0.1, 'fingerprint': '0d8fcd0eed75742f', 'decision': (300.0, 1)},
    ('marstek-vD-dc', 'zero'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': 0.0, 'fingerprint': '7a3093adbb8e2c6f', 'decision': (200.0, 2)},
    ('marstek-vD-dc', 'full_and_solar'): {'aggregate': 1200.0, 'efficiency': (0.0, 0.0), 'tracker': 1.0, 'fingerprint': '7a3093adbb8e2c6f', 'decision': (1200.0, 1)},
    ('esphome', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('esphome', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('zendure-2400ac_pro', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-2400ac_pro', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 0.5, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 1)},
    ('zendure-2400ac_pro', 'full'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-2400ac_pro', 'partial'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-2400ac_pro', 'none_first'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-2400ac_pro', 'negative'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-2400ac_pro', 'zero'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-2400ac_pro', 'full_and_solar'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 0.5, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 1)},
    ('zendure-4000mix_pro', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': '824752e5d252829c', 'decision': (150.0, 2)},
    ('zendure-4000mix_pro', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': '824752e5d252829c', 'decision': (150.0, 2)},
    ('zendure-4000mix_pro', 'full'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 1.0, 'fingerprint': '20df4d25e1765fcc', 'decision': (150.0, 1)},
    ('zendure-4000mix_pro', 'partial'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 0.25, 'fingerprint': 'a74a04874b6a57ce', 'decision': (150.0, 1)},
    ('zendure-4000mix_pro', 'none_first'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 0.15, 'fingerprint': '21ae823908c8a518', 'decision': (150.0, 1)},
    ('zendure-4000mix_pro', 'negative'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 0.1, 'fingerprint': '21ae823908c8a518', 'decision': (150.0, 1)},
    ('zendure-4000mix_pro', 'zero'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 0.0, 'fingerprint': '20df4d25e1765fcc', 'decision': (150.0, 2)},
    ('zendure-4000mix_pro', 'full_and_solar'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 1.0, 'fingerprint': '20df4d25e1765fcc', 'decision': (150.0, 1)},
    ('zendure-1600ac_plus', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('zendure-1600ac_plus', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('zendure-solarflow_800', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-solarflow_800', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 0.5, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 1)},
    ('zendure-solarflow_800', 'full'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-solarflow_800', 'partial'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-solarflow_800', 'none_first'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-solarflow_800', 'negative'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-solarflow_800', 'zero'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 2)},
    ('zendure-solarflow_800', 'full_and_solar'): {'aggregate': 200.0, 'efficiency': None, 'tracker': 0.5, 'fingerprint': 'a3d25d7296a11430', 'decision': (150.0, 1)},
    ('anker-ac-family', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('anker-ac-family', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (200.0, 2)},
    ('anker-independent-pv', 'none'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': 'e78cac37dfc21702', 'decision': (200.0, 2)},
    ('anker-independent-pv', 'solar_only'): {'aggregate': 200.0, 'efficiency': (0.0, 0.0), 'tracker': 0.5, 'fingerprint': 'e78cac37dfc21702', 'decision': (200.0, 1)},
    ('sessy', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('sessy', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('indevolt', 'none'): {'aggregate': 200.0, 'efficiency': (0.000555556, 0.001388889), 'tracker': None, 'fingerprint': '1adefd224c3e671b', 'decision': (200.0, 2)},
    ('indevolt', 'solar_only'): {'aggregate': 200.0, 'efficiency': (0.0, 0.0), 'tracker': 0.5, 'fingerprint': '1adefd224c3e671b', 'decision': (200.0, 1)},
    ('huawei', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('huawei', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('huawei', 'partial'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('huawei', 'none_first'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('huawei', 'negative'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('hoymiles', 'none'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
    ('hoymiles', 'solar_only'): {'aggregate': 200.0, 'efficiency': None, 'tracker': None, 'fingerprint': 'a74472309aa6b1ef', 'decision': (150.0, 2)},
}
