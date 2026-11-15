"""Characterization of what the core does with ``data["inverter_state"]`` (T7.1, #39).

Pins, for the eight drivers, the value each one publishes under ``inverter_state``
and what the six shared sites do with it, so the ``inverter_status()`` query can
replace the numeric comparisons without changing a result.

The core reads the key as Marstek's register table: 1 Standby, 4 Backup Mode,
6 Bypass (0 Sleep, 2 Charge, 3 Discharge, 5 OTA). Published values per driver:

* marstek    the raw register, 0..6;
* anker, hoymiles, indevolt  translate their own states to 0..3 (Indevolt also
  publishes ``None`` while it is starting);
* esphome    translates the firmware text with ``_INVERTER_STATE_TO_CODE``; its
  "fault" is 4, which the core reads as Backup Mode (plan section 8, defect kept);
* huawei     publishes a text ("Standby", "Charge", "Running", "Unknown (n)"...),
  which never matches a code;
* sessy, zendure  never publish the key.

The six sites (rows of ``tests/guard_allowlists/no_native_keys.txt``):
``_check_non_delivery`` (standby), ``_is_backup_function_active`` via
``_inverter_in_ac_bypass`` (bypass), ``_daily_operation_runtime_decision`` (backup),
``MaxSocChargeManager._bms_cut_signature`` (standby),
``WeeklyFullChargeManager.tick_bms_cutoff`` (standby, or a missing key on a driver
with ``cutoff_without_inverter_state``) and
``MarstekVenusAggregateSensor._calculate_battery_cell_power`` (backup).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import NORMAL_BALANCE_TAPER_CELL_VOLTAGE
from custom_components.omnibattery.control.max_soc_charge import MaxSocChargeManager
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
    _BMS_CUTOFF_REQUIRED_CYCLES,
)
from custom_components.omnibattery.drivers import anker, esphome, huawei, indevolt
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator
from tests.test_aggregate_power import _sensor
from tests.test_daily_operation_manager import _runtime_controller
from tests.test_driver_construction_characterization import (
    BRAND_CONFIGS,
    _build_coordinator,
)

ABSENT = object()  # the driver does not publish the key
MADRID = ZoneInfo("Europe/Madrid")
_IN_ZONE = NORMAL_BALANCE_TAPER_CELL_VOLTAGE + 0.05

_HUAWEI_TEXTS = [*huawei._STORAGE_STATUS_LABELS.values(), "Charge", "Discharge", "Unknown (9)"]

# brand -> every value the driver can leave under ``inverter_state``
PUBLISHED = {
    "marstek": [0, 1, 2, 3, 4, 5, 6, None, ABSENT],
    "esphome": [*esphome._INVERTER_STATE_TO_CODE.values(), None, ABSENT],
    "anker": [*anker._INVERTER_STATE_BY_BATTERY_STATUS.values(), ABSENT],
    "hoymiles": [1, 2, 3, ABSENT],
    "indevolt": [*indevolt._BATTERY_STATE.values(), None, ABSENT],
    "huawei": [*_HUAWEI_TEXTS, ABSENT],
    "sessy": [ABSENT, None],
    "zendure": [ABSENT, None],
}
CASES = [(b, v) for b, values in PUBLISHED.items() for v in values]
IDS = [f"{b}-{'absent' if v is ABSENT else v!r}" for b, v in CASES]

def _coordinator(brand):
    """The real coordinator (and its real driver) for the brand.

    Must run inside a test coroutine: the Marstek driver creates a Modbus client
    that needs a running loop.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)
        return _build_coordinator(BRAND_CONFIGS[brand])


def _is(value, code):
    """What every site accepts as ``code``: a number equal to it (no text, no None)."""
    return (
        value is not ABSENT
        and not isinstance(value, (bool, str))
        and isinstance(value, (int, float))
        and value == code
    )


def _data(value, **rest):
    data = dict(rest)
    if value is not ABSENT:
        data["inverter_state"] = value
    return data


# ---------------------------------------------------------------------------
# What each driver publishes (literal pins)
# ---------------------------------------------------------------------------

def test_published_values_per_driver():
    assert esphome._INVERTER_STATE_TO_CODE == {
        "sleep": 0, "standby": 1, "charge": 2, "discharge": 3,
        "fault": 4, "idle": 5, "ac bypass": 6,
    }
    assert anker._INVERTER_STATE_BY_BATTERY_STATUS == {0: 1, 1: 2, 2: 3, 3: 0}
    assert indevolt._BATTERY_STATE == {1000: 1, 1001: 2, 1002: 3}
    assert huawei._STORAGE_STATUS_LABELS == {
        0: "Offline", 1: "Standby", 2: "Running", 3: "Fault", 4: "Sleep",
    }


# ---------------------------------------------------------------------------
# Site 1: _check_non_delivery (standby)
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.reasons = []

    def record_non_delivery(self, coordinator, commanded, delivered, *, reason, retry_attempted):
        self.reasons.append(reason)

    def clear(self, coordinator):
        pass


class _Hashable(SimpleNamespace):
    """A coordinator stand-in usable as a dict key (the controller keys by it)."""

    __hash__ = object.__hash__


class _Coord(FakeCoordinator):
    """FakeCoordinator whose ``driver`` is the real one for the brand."""

    real_driver = None

    @property
    def driver(self):
        return self.real_driver


async def _judge(brand, value, *, soc):
    coord = _Coord(
        name="BAT1", min_soc=10,
        data=_data(value, battery_power=0, battery_soc=soc),
    )
    coord.real_driver = _coordinator(brand).driver
    ctrl = SimpleNamespace(
        _discharge_engage_started={}, _charge_engage_started={},
        _high_soc_taper_started={}, _non_responsive=_Recorder(),
        _weekly_charge_mgr=SimpleNamespace(is_battery_full=lambda c: False),
        _attempt_wake=_Wake(),
    )
    await ChargeDischargeController._check_non_delivery(
        ctrl, coord, 675, 0, attempt=0, direction="discharge",
    )
    return ctrl._non_responsive.reasons, ctrl._attempt_wake.calls


class _Wake:
    def __init__(self):
        self.calls = 0

    async def __call__(self, coordinator, *, is_standby=False):
        self.calls += 1
        return True


@pytest.mark.parametrize(("brand", "value"), CASES, ids=IDS)
async def test_check_non_delivery(brand, value):
    standby = _is(value, 1)
    reasons, wakes = await _judge(brand, value, soc=50)
    assert reasons == ["standby_no_delivery" if standby else "non_delivery"]
    assert wakes == 0
    # Below the BMS cutoff floor the standby battery gets the one-shot wake.
    reasons, wakes = await _judge(brand, value, soc=16)
    assert reasons == []
    assert wakes == (1 if standby else 0)


# ---------------------------------------------------------------------------
# Site 2: _is_backup_function_active -> _inverter_in_ac_bypass (bypass)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "value"), CASES, ids=IDS)
async def test_backup_function_active_in_bypass(brand, value):
    coord = _Hashable(
        name="BAT1", driver=_coordinator(brand).driver, backup_offgrid_threshold=50,
        readings_from_same_poll=lambda *keys: True,
        data=_data(value, backup_function=0, ac_offgrid_power=200),
    )
    ctrl = SimpleNamespace(_backup_cooldown_until={})
    active = ChargeDischargeController._is_backup_function_active(ctrl, coord)
    # A grid-bypass reading says the port load is passthrough, not the battery.
    assert active is (not _is(value, 6))


# ---------------------------------------------------------------------------
# Site 3: _daily_operation_runtime_decision (backup mode)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "value"), CASES, ids=IDS)
async def test_daily_operation_runtime_decision(brand, value):
    coord = SimpleNamespace(
        driver=_coordinator(brand).driver,
        capabilities=SimpleNamespace(has_mppt_pv=True, has_solar_telemetry=True),
        data=_data(value, ac_power=300, ac_offgrid_power=100, mppt1_power=0),
    )
    controller = _runtime_controller([coord])
    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )
    # Off-grid output only draws from the cells in Backup Mode (4).
    assert decision["discharge_power_w"] == (400 if _is(value, 4) else 300)


# ---------------------------------------------------------------------------
# Site 4: MaxSocChargeManager._bms_cut_signature (standby)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "value"), CASES, ids=IDS)
async def test_bms_cut_signature(brand, value):
    coord = SimpleNamespace(driver=_coordinator(brand).driver, commanded_charge_power=200)
    data = _data(value, battery_power=0)
    assert MaxSocChargeManager._bms_cut_signature(coord, data) is _is(value, 1)


# ---------------------------------------------------------------------------
# Site 5: WeeklyFullChargeManager.tick_bms_cutoff (standby, or missing key on
# a driver that cannot publish it)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "value"), CASES, ids=IDS)
async def test_tick_bms_cutoff(brand, value, bms_cutoff_clock):
    real = _coordinator(brand)
    coord = SimpleNamespace(
        name="bat", driver=real.driver, traits=real.traits, commanded_charge_power=200,
        data=_data(value, battery_soc=99, battery_power=0, max_cell_voltage=_IN_ZONE),
    )
    ctrl = SimpleNamespace(coordinators=[coord], weekly_full_charge_enabled=True)
    m = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    m._controller = ctrl
    m._bms_cutoff_counts = {}
    m._bms_charge_commanded_at = {coord.name: 0.0}
    m._already_complete_logged = False
    m.is_active = lambda: True
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()
    key_missing = value is ABSENT or value is None
    expected = _is(value, 1) or (real.traits.cutoff_without_inverter_state and key_missing)
    assert (m._bms_cutoff_counts.get("bat", 0) > 0) is expected


# ---------------------------------------------------------------------------
# Site 6: _calculate_battery_cell_power (backup mode)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "value"), CASES, ids=IDS)
async def test_battery_cell_power(brand, value):
    coord = SimpleNamespace(
        is_available=True, driver=_coordinator(brand).driver,
        capabilities=SimpleNamespace(has_mppt_pv=True, has_solar_telemetry=False),
        data=_data(value, ac_power=-100, ac_offgrid_power=50, mppt1_power=100),
    )
    sensor = _sensor([coord], "system_battery_cell_power")
    assert sensor._calculate_battery_cell_power() == (150 if _is(value, 4) else 200)


# ---------------------------------------------------------------------------
# The defects this pins (plan section 8): not fixed here
# ---------------------------------------------------------------------------

def test_esphome_fault_is_read_as_backup_mode():
    """ESPHome's "fault" is code 4, which the core reads as Backup Mode."""
    assert esphome._INVERTER_STATE_TO_CODE["fault"] == 4
    assert _is(4, 4)


async def test_only_zendure_confirms_a_cutoff_without_the_key():
    flags = {b: _coordinator(b).traits.cutoff_without_inverter_state for b in PUBLISHED}
    assert [b for b, on in flags.items() if on] == ["zendure"]
