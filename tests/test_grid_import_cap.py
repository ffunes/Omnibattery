"""One grid import ceiling, all day (#506).

``max_contracted_power`` ("Maximum grid import") is the only ceiling. It is a
cap, not a target: it engages only when something holds the battery back
(an excluded load hidden from the controller, a grid target above the cap, the
SOC reserve) and the physical import would cross it. With normal control at
zero nothing changes: the battery keeps covering the whole house.

The assertions run a small closed loop (meter -> ``_apply_capacity_protection``
-> proportional step -> battery limited to its maximum) and check the physical
grid import it settles at, not the internal target.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from tests.conftest import _FakeMarstekDriver

TOL = 20.0


def _ctrl(*, cap: float, soc: float = 60, reserve: bool = True, threshold: float = 20):
    ctrl = object.__new__(ChargeDischargeController)
    ctrl.max_contracted_power = cap
    ctrl.capacity_protection_enabled = reserve
    ctrl.capacity_protection_soc_threshold = threshold
    ctrl.coordinators = [SimpleNamespace(driver=_FakeMarstekDriver(), max_soc=100, data={"battery_soc": soc})]
    ctrl._non_responsive = SimpleNamespace(is_excluded=lambda _c: False)
    ctrl._measured_battery_power = lambda: None
    ctrl.previous_power = 0.0
    ctrl._excluded_included_adjustment = 0.0
    ctrl._setpoint_offsets = {"user_target": 0.0}
    ctrl._setpoint_overrides = {}
    ctrl._capacity_protection_active = False
    ctrl._capacity_protection_force_idle = False
    ctrl._capacity_protection_status = {}
    ctrl.deadband = 50
    return ctrl


def _settle(ctrl, *, house, excluded=0.0, target=0.0, max_w=2500.0, discharge=0.0, steps=120):
    """Run the loop to steady state; return (physical grid import, discharge).

    ``house`` is the load the controller sees, ``excluded`` a load marked as
    excluded (hidden from the controller, still on the meter).
    """
    ctrl._setpoint_offsets["user_target"] = target
    cap = ctrl.max_contracted_power
    d = discharge
    for _ in range(steps):
        grid = house + excluded - d
        ctrl.previous_power = -d
        ctrl._excluded_included_adjustment = excluded
        t, s = ctrl._apply_capacity_protection(grid - excluded, target)
        if ctrl._capacity_protection_force_idle:
            ctrl._capacity_protection_force_idle = False
            d = 0.0
            continue
        d += 0.5 * (s - t)
        # Battery limits, and the existing charge clamp on the physical meter.
        d = max(-max_w, min(max_w, d))
        if d < 0:
            d = max(d, -(cap - (house + excluded)))
    return house + excluded - d, d


# --- #483: excluded device, high SOC ---------------------------------------

@pytest.mark.parametrize("reserve", [True, False], ids=["reserve_on", "reserve_off"])
def test_483_excluded_device_above_cap_is_covered(reserve):
    ctrl = _ctrl(cap=4500, soc=60, reserve=reserve)
    grid, _ = _settle(ctrl, house=500, excluded=5200)
    assert grid == pytest.approx(4500, abs=TOL)


@pytest.mark.parametrize("reserve", [True, False], ids=["reserve_on", "reserve_off"])
def test_483_literal_case_keeps_covering_the_house(reserve):
    # 1700 W house + 4000 W excluded: the excluded load alone is under the cap,
    # so the battery keeps covering the 1700 W (it does not back off to sit
    # the meter at 4500 W).
    ctrl = _ctrl(cap=4500, soc=60, reserve=reserve)
    grid, d = _settle(ctrl, house=1700, excluded=4000)
    assert grid == pytest.approx(4000, abs=TOL)
    assert d == pytest.approx(1700, abs=TOL)


def test_excluded_device_with_low_soc_reserve_is_shaved_to_the_cap():
    ctrl = _ctrl(cap=4500, soc=15)
    grid, _ = _settle(ctrl, house=500, excluded=5200)
    assert grid == pytest.approx(4500, abs=TOL)


def test_zero_cap_disables_the_cap():
    # Absorbed from test_icp_excluded_protection.py: 0 W means no cap.
    ctrl = _ctrl(cap=0, soc=60, reserve=False)
    grid, _ = _settle(ctrl, house=500, excluded=5200)
    assert grid == pytest.approx(5200, abs=TOL)
    assert ctrl._capacity_protection_active is False


# --- A grid target above the cap --------------------------------------------

def test_positive_grid_target_is_capped():
    ctrl = _ctrl(cap=4500, soc=60, reserve=False)
    grid, _ = _settle(ctrl, house=6000, target=5000)
    assert grid == pytest.approx(4500, abs=TOL)


# --- Reserve (peak shaving below the SOC threshold) uses the cap -------------

def test_reserve_holds_the_battery_below_the_cap():
    ctrl = _ctrl(cap=4500, soc=15)
    grid, d = _settle(ctrl, house=3000)
    assert d == pytest.approx(0, abs=TOL)
    assert grid == pytest.approx(3000, abs=TOL)
    assert ctrl._capacity_protection_status["action"] == "conserving"


def test_reserve_covers_only_the_excess_above_the_cap():
    ctrl = _ctrl(cap=4500, soc=15)
    grid, d = _settle(ctrl, house=6000)
    assert grid == pytest.approx(4500, abs=TOL)
    assert d == pytest.approx(1500, abs=TOL)
    assert ctrl._capacity_protection_status["action"] == "shaving"


# --- Regression: the cap is a ceiling, never a target (§3.7) -----------------

@pytest.mark.parametrize("reserve", [True, False], ids=["reserve_on", "reserve_off"])
def test_high_soc_without_holds_battery_covers_everything(reserve):
    ctrl = _ctrl(cap=4500, soc=60, reserve=reserve)
    grid, d = _settle(ctrl, house=5700, max_w=8000)
    assert d == pytest.approx(5700, abs=TOL)
    assert grid == pytest.approx(0, abs=TOL)
    assert ctrl._capacity_protection_active is False


def test_saturated_battery_keeps_its_maximum_when_the_house_drops():
    ctrl = _ctrl(cap=4500, soc=60)
    grid, d = _settle(ctrl, house=8000)
    assert d == pytest.approx(2500, abs=TOL)
    assert grid == pytest.approx(5500, abs=TOL)
    grid, d = _settle(ctrl, house=6000, discharge=d)
    assert d == pytest.approx(2500, abs=TOL)
    assert grid == pytest.approx(3500, abs=TOL)


# --- Exit hysteresis -----------------------------------------------------------

def test_reserve_shaving_holds_through_the_band_without_grid_charging():
    """Below the cap but inside the release band the shave stays engaged for
    two fresh samples, holding the meter instead of charging from the grid."""
    ctrl = _ctrl(cap=4500, soc=15)
    _settle(ctrl, house=6000)
    # House drops just under the cap: inside the band (4500 - 200).
    ctrl.previous_power = -1500.0
    t, s = ctrl._apply_capacity_protection(4400 - 1500, 0.0)
    assert ctrl._capacity_protection_status["action"] == "shaving"
    assert t <= s + 1e-6  # never asks the battery to charge
    assert ctrl._capacity_protection_force_idle is False
    # Below the band: released after two consecutive samples.
    ctrl._apply_capacity_protection(4000 - 1500, 0.0)
    assert ctrl._capacity_protection_status["action"] == "shaving"
    ctrl._apply_capacity_protection(4000 - 1500, 0.0)
    assert ctrl._capacity_protection_status["action"] == "conserving"


# --- The predictive slot uses the same ceiling ---------------------------------

def test_predictive_slot_ceiling_is_the_grid_import_cap():
    ctrl = _ctrl(cap=4500, soc=15)
    assert ctrl._predictive_charge_ceiling() == 4500


# --- The entity: one number, always present ------------------------------------

def test_cap_entity_is_always_present_and_reaches_500_w():
    from custom_components.omnibattery.const import CONFIG_NUMBER_DEFINITIONS

    keys = [d["key"] for d in CONFIG_NUMBER_DEFINITIONS]
    assert "capacity_protection_limit" not in keys
    cap = next(d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] == "max_contracted_power")
    assert "condition" not in cap
    assert cap["min"] == 500


# --- Migration 16 -> 17 (needs the real hass: run with -o addopts="") ----------

DOMAIN = "omnibattery"
LIMIT_UID = "marstek_venus_system_capacity_protection_limit"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"capacity_protection_enabled": True, "max_contracted_power": 7000,
          "capacity_protection_limit": 4500}, 4500),
        ({"capacity_protection_enabled": False, "max_contracted_power": 7000,
          "capacity_protection_limit": 4500}, 7000),
        ({"capacity_protection_enabled": True, "max_contracted_power": 5750}, 2500),
        ({"capacity_protection_enabled": False}, 7000),
    ],
    ids=["peak_shaving_on", "peak_shaving_off", "on_without_limit_key", "no_keys"],
)
async def test_v17_folds_the_peak_limit_into_the_cap(hass, data, expected) -> None:
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.omnibattery import async_migrate_entry

    entry = MockConfigEntry(domain=DOMAIN, version=16, data={"batteries": [], **data})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    old = reg.async_get_or_create("number", DOMAIN, LIMIT_UID, config_entry=entry)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 22
    assert entry.data["max_contracted_power"] == expected
    assert "capacity_protection_limit" not in entry.data
    assert reg.async_get(old.entity_id) is None


# --- Manual mode: grid charging stays under the cap (no forced discharge) -------

def _manual_ctrl(grid_w: float):
    ctrl = object.__new__(ChargeDischargeController)
    ctrl.max_contracted_power = 5750
    ctrl.offgrid_mode_enabled = False
    ctrl.primary_consumption_sensor = "sensor.grid"
    ctrl.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: grid_w))
    ctrl._apply_meter_transform = lambda state: state
    ctrl.writes = []

    async def _set(coordinator, charge, discharge, **_kw):
        ctrl.writes.append((coordinator.name, charge, discharge))

    ctrl._set_battery_power = _set
    return ctrl


def _manual_battery(*, software: bool, charging_w: float):
    applied = []

    async def apply_power(w):
        applied.append(w)

    return SimpleNamespace(
        name="b1", needs_software_manual_control=software,
        manual_force_mode="Charge", manual_set_charge_power=2000,
        manual_set_discharge_power=0, commanded_charge_power=2000,
        commanded_discharge_power=0, battery_manual_mode_enabled=False,
        data={}, is_available=True, rs485_user_disabled=False,
        apply_power=apply_power, applied=applied, charging_w=charging_w,
        driver=SimpleNamespace(net_power_from_data=lambda _d: charging_w),
    )


def _delivered(coordinator):
    return coordinator.charging_w


async def test_manual_charge_is_clamped_to_the_cap_software_driver():
    # House 4000 W + manual charge 2000 W on a 5750 W cap -> 1750 W.
    ctrl = _manual_ctrl(grid_w=6000)
    battery = _manual_battery(software=True, charging_w=2000)
    ctrl.coordinators = [battery]
    ctrl._coordinator_delivered_power = _delivered
    await ctrl._apply_software_manual_setpoints()
    assert ctrl.writes == [("b1", 1750, 0)]
    assert battery.manual_set_charge_power == 2000  # the user's setpoint stays
