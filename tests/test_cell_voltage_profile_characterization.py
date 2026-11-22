"""Characterization of the per-cell voltage thresholds in the shared core (T7.5, #38).

Pins, for the eight drivers (Marstek in its four versions), whether each one
publishes ``max_cell_voltage`` and what the core decides with the cell voltage
below, at and above each threshold, so the ``cell_voltage_profile`` capability can
replace the four constants without changing a result.

The thresholds are LiFePO4 per-cell levels (chemistry, not brand):

* 3.48 V  top-of-charge taper entry (``>=``)
* 3.44 V  taper latch release (``<``)
* 3.57 V  relaxation voltage for the one-shot retry (``<=``)
* 3.60 V  pause / top-cell point (``>=``; ``>`` for the first-cutoff test)

Only the Marstek registers, ESPHome and Zendure publish the key. The other five
never do, so for them the thresholds are inert today (the core reads ``None``).
The tables below nevertheless exercise every driver with a hypothetical voltage. Before
T7.5 the core applied the LFP levels to all eleven cases; the five non-publishers
(``INERT_BRANDS``) now declare no profile, so with an injected voltage the core takes
no voltage decision for them (``applies`` is False). That one difference is
unreachable with real telemetry: those drivers never write the key.

Sites covered: ``MaxSocChargeManager`` (zone, taper, BMS-cutoff-at-top, retry),
``WeeklyFullChargeManager.tick_bms_cutoff`` and
``ChargeDischargeController._refresh_battery_charge_limit_blocks`` /
``_get_available_batteries`` (the 3.60 V charge-hysteresis arming).
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

import custom_components.omnibattery.drivers as drivers_pkg
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.max_soc_charge import MaxSocChargeManager
from custom_components.omnibattery.control.weekly_full_charge import (
    _BMS_CUTOFF_REQUIRED_CYCLES,
    WeeklyFullChargeManager,
)
from custom_components.omnibattery.drivers.base import InverterStatus
from tests.test_driver_construction_characterization import BRAND_CONFIGS, _build_coordinator

# The LFP thresholds, written out so a change of the shipped numbers fails here.
TAPER_ENTRY = 3.48
TAPER_EXIT = 3.44
RETRY = 3.57
PAUSE = 3.60

VOLTAGES = [3.30, 3.43, 3.44, 3.45, 3.47, 3.48, 3.49, 3.56, 3.57, 3.58, 3.59, 3.60, 3.61]

CASES = [
    ("marstek", "v2"), ("marstek", "v3"), ("marstek", "vA"), ("marstek", "vD"),
    ("zendure", None), ("esphome", None), ("anker", None), ("sessy", None),
    ("indevolt", None), ("huawei", None), ("hoymiles", None),
]
IDS = [f"{b}-{v}" if v else b for b, v in CASES]
PUBLISHERS = {"marstek", "zendure", "esphome"}
# Zendure publishes the key but has no voltage taper: its BMS owns the top (#884).
TOP_VOLTAGE_STOP_BRANDS = {"marstek", "esphome"}
INERT_BRANDS = {"anker", "sessy", "indevolt", "huawei", "hoymiles"}
# The coordinator's taper flag after construction (observed on today's code).
TAPER_FLAG = {
    "marstek": True, "zendure": False, "esphome": True,
    "anker": False, "sessy": True, "indevolt": False, "huawei": False, "hoymiles": False,
}
BMS_TOP_VERSIONS = {"vA", "vD"}


def _applies(brand):
    """The core applies the LFP levels to a driver that declares the profile."""
    return brand in PUBLISHERS


@pytest.fixture(autouse=True)
def _no_ha_runtime(monkeypatch):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)


class _Hashable(SimpleNamespace):
    __hash__ = object.__hash__


def _coordinator(brand, version, vmax, *, soc=90, power=0, inverter_state=1):
    config = dict(BRAND_CONFIGS[brand])
    if version:
        config["battery_version"] = version
    coord = _build_coordinator(config)
    coord._is_connected = True
    coord.data = {
        "battery_soc": soc, "battery_power": power, "inverter_state": inverter_state,
        "max_cell_voltage": vmax, "min_cell_voltage": vmax - 0.05,
    }
    coord.commanded_charge_power = 0
    return coord


def _ctrl(coord, **extra):
    base = dict(
        coordinators=[coord],
        _normal_balance_voltage_tapered={},
        _normal_balance_bms_cutoff_active={},
        _normal_balance_bms_cutoff_retry_pending={},
        _normal_balance_bms_cutoff_retry_active={},
        _normal_balance_bms_cutoff_measurement={},
        _normal_balance_recal_override={},
        _weekly_charge_mgr=object(),
        _weekly_full_charge_unlocked=lambda: False,
    )
    base.update(extra)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Who publishes the key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_publishes_max_cell_voltage(brand, version):
    coord = _coordinator(brand, version, 3.3)
    keys = {d.get("key") for d in coord.driver.all_definitions}
    assert ("max_cell_voltage" in keys) is (brand in PUBLISHERS)


def test_no_other_driver_module_publishes_the_key():
    pattern = re.compile(r"""["']max_cell_voltage["']""")
    for path in Path(drivers_pkg.__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Marstek's registers live in const/ (its driver tests the key in its index).
        assert bool(pattern.search(text)) is (
            path.stem in {"esphome", "zendure", "marstek"}
        ), path.name


# ---------------------------------------------------------------------------
# MaxSocChargeManager
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vmax", VOLTAGES)
@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_zone_and_taper_decisions(brand, version, vmax):
    coord = _coordinator(brand, version, vmax)
    assert coord.full_charge_voltage_taper_enabled is TAPER_FLAG[brand]
    ctrl = _ctrl(coord)
    mgr = MaxSocChargeManager(SimpleNamespace(), ctrl)

    gated = TAPER_FLAG[brand] and _applies(brand)
    in_zone = gated and vmax >= TAPER_ENTRY
    assert mgr._zone_active(coord) is in_zone

    # Fresh latch: capped at the taper power from the entry voltage up.
    expected_fresh = 200 if in_zone else 800
    assert mgr.apply_charge_taper(coord, 800) == expected_fresh
    assert (coord in ctrl._normal_balance_voltage_tapered) is in_zone

    # Latched: held down to the exit voltage, released below it.
    ctrl._normal_balance_voltage_tapered[coord] = True
    latched_cap = 200 if (gated and vmax >= TAPER_EXIT) else 800
    assert mgr.apply_charge_taper(coord, 800) == latched_cap


@pytest.mark.parametrize("vmax", VOLTAGES)
@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_bms_cutoff_at_top_decisions(brand, version, vmax):
    coord = _coordinator(brand, version, vmax)
    ctrl = _ctrl(coord)
    mgr = MaxSocChargeManager(SimpleNamespace(), ctrl)
    coupled = version in BMS_TOP_VERSIONS
    assert coord.traits.bms_owns_top_cutoff is coupled

    # Stay charge-eligible until the BMS cutoff once the top cell is reached.
    assert mgr.should_charge_to_bms_cutoff(coord, 100) is (coupled and vmax >= PAUSE)  # coupled => Marstek

    # A pending first-cutoff retry opens once the cell relaxes to 3.57 V.
    ctrl._normal_balance_bms_cutoff_retry_pending[coord] = True
    state = mgr.prepare_bms_cutoff_retry(coord)
    if not coupled:
        assert state is None
    elif vmax <= RETRY:
        assert state == MaxSocChargeManager._BMS_CUTOFF_RETRY_ACTIVE
    else:
        assert state == MaxSocChargeManager._BMS_CUTOFF_RETRY_PENDING


# ---------------------------------------------------------------------------
# WeeklyFullChargeManager.tick_bms_cutoff: the taper zone arms the counter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vmax", VOLTAGES)
@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_weekly_cutoff_counter_arms_in_the_taper_zone(
    brand, version, vmax, bms_cutoff_clock
):
    coord = _coordinator(brand, version, vmax)
    coord.commanded_charge_power = 200  # commanded, refused, standby: a cutoff
    ctrl = _ctrl(coord, weekly_full_charge_enabled=True)
    weekly = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    weekly._controller = ctrl
    weekly._bms_cutoff_counts = {}
    weekly._bms_charge_commanded_at = {coord.name: 0.0}
    weekly._already_complete_logged = False
    weekly.is_active = lambda: True

    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        weekly.tick_bms_cutoff()

    standby = coord.driver.inverter_status(coord.data) is InverterStatus.STANDBY
    armed = _applies(brand) and vmax >= TAPER_ENTRY and standby  # SOC 90: only the voltage arms it
    assert (weekly._bms_cutoff_counts.get(coord.name, 0) >= _BMS_CUTOFF_REQUIRED_CYCLES) is armed


# ---------------------------------------------------------------------------
# The 3.60 V hysteresis arming in the controller
# ---------------------------------------------------------------------------

def _hysteresis_coord(brand, version, vmax):
    coord = _coordinator(brand, version, vmax)
    coord.max_soc = 100
    coord.min_soc = 10
    coord.enable_charge_hysteresis = True
    coord.charge_hysteresis_percent = 10
    coord._hysteresis_active = False
    coord._hysteresis_base_soc = None
    coord.rs485_user_disabled = False
    coord._discharge_min_soc_latched = False
    return coord


def _hysteresis_ctrl(coord):
    blocks: dict = {}
    ctrl = _ctrl(
        coord,
        _effective_charge_max_soc=lambda c, weekly, **_kw: (c.max_soc, "config"),
        _predictive_solar_only_batteries={},
        _weekly_charge_mgr=SimpleNamespace(is_battery_full=lambda c: False),
        _should_charge_to_bms_cutoff=lambda *_a: False,
        _non_responsive=SimpleNamespace(is_excluded=lambda _c: False),
        _is_backup_function_active=lambda _c: False,
        _is_manual_slot_owned=lambda _c: False,
        get_charge_blockers=lambda _c: {},
        set_charge_block=lambda source, *_a, coordinator=None, **_k: blocks.setdefault(
            coordinator, set()
        ).add(source),
        remove_charge_block=lambda source, coordinator=None: blocks.get(
            coordinator, set()
        ).discard(source),
    )
    ctrl._blocks = blocks
    return ctrl


@pytest.mark.parametrize("vmax", VOLTAGES)
@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_top_voltage_arms_charge_hysteresis_in_the_block_refresh(brand, version, vmax):
    coord = _hysteresis_coord(brand, version, vmax)
    ctrl = _hysteresis_ctrl(coord)
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    armed = brand in TOP_VOLTAGE_STOP_BRANDS and vmax >= PAUSE
    assert coord._hysteresis_active is armed
    assert ("charge_hysteresis" in ctrl._blocks.get(coord, set())) is armed


@pytest.mark.parametrize("vmax", VOLTAGES)
@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_top_voltage_arms_charge_hysteresis_in_the_availability_check(
    brand, version, vmax
):
    coord = _hysteresis_coord(brand, version, vmax)
    ctrl = _hysteresis_ctrl(coord)
    available = ChargeDischargeController._get_available_batteries(ctrl, True, False)
    armed = brand in TOP_VOLTAGE_STOP_BRANDS and vmax >= PAUSE
    assert coord._hysteresis_active is armed
    assert (available == []) is armed


async def test_zendure_top_cell_does_not_stop_the_charge_at_mid_soc():
    """Issue #884: one cell at 3.60 V at 61% SOC latched the charge until SOC fell."""
    coord = _hysteresis_coord("zendure", None, 3.60)
    coord.data["battery_soc"] = 61
    coord.charge_hysteresis_percent = 2
    ctrl = _hysteresis_ctrl(coord)
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    assert not ctrl._blocks.get(coord)
    assert ChargeDischargeController._get_available_batteries(ctrl, True, False) == [coord]

    # A full battery still stops.
    coord.data["battery_soc"] = 100
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    assert "charge_hysteresis" in ctrl._blocks[coord]
    assert ChargeDischargeController._get_available_batteries(ctrl, True, False) == []


async def test_marstek_top_cell_still_stops_the_charge_with_the_taper_switch_off():
    coord = _hysteresis_coord("marstek", "v3", 3.60)
    coord.full_charge_voltage_taper_enabled = False
    ctrl = _hysteresis_ctrl(coord)
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    assert "charge_hysteresis" in ctrl._blocks[coord]


@pytest.mark.parametrize(
    ("brand", "version"),
    [case for case in CASES if case[0] in PUBLISHERS],
    ids=[i for (b, _), i in zip(CASES, IDS) if b in PUBLISHERS],
)
async def test_retry_voltage_is_reported_in_the_waiting_block(brand, version):
    coord = _hysteresis_coord(brand, version, 3.59)
    ctrl = _hysteresis_ctrl(coord)
    details = {}
    ctrl._normal_balance_bms_cutoff_retry_pending = {coord: True}
    ctrl.set_charge_block = (
        lambda source, reason, info=None, coordinator=None: details.update({source: info})
    )
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    assert details["bms_cutoff_retry"]["retry_voltage"] == RETRY


# ---------------------------------------------------------------------------
# The declared profile (T7.5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_declared_profile(brand, version):
    profile = _coordinator(brand, version, 3.3).capabilities.cell_voltage_profile
    if brand in PUBLISHERS:
        assert (profile.taper_entry_v, profile.taper_exit_v, profile.pause_v,
                profile.recal_retry_v) == (TAPER_ENTRY, TAPER_EXIT, PAUSE, RETRY)
    else:
        assert profile is None
