"""The single battery model executes a slot quota as the real controller does.

The controller turns a slot's quota into a SOC target per battery
(``_compute_deficit_target_soc``: split by gap) and keeps a battery charging
while its SOC reading is below the target (``_get_available_batteries``). The
reading is a whole percent, so each battery stops at the first whole percent at
or above its target: the stored energy rounds up.

Hardware assumptions, not validated here: the reading is the true SOC rounded
down, and the charge stops on the first reading at the target (no poll lag).
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing.battery_model import BatteryModel, Quarter
from tests.conftest import FakeCoordinator


def _controller(coords, quota_kwh):
    targets = {}
    ctrl = SimpleNamespace(
        coordinators=list(coords),
        _weekly_charge_mgr=SimpleNamespace(is_active=lambda: False, is_battery_full=lambda c: False),
        _last_decision_data={"planned_grid_charge_kwh": quota_kwh},
        _non_responsive=SimpleNamespace(is_excluded=lambda c: False),
        _is_backup_function_active=lambda c: False,
        _is_manual_slot_owned=lambda c: False,
        _is_battery_manual_owned=lambda c: False,
        is_charge_blocked=lambda c: False,
        get_charge_blockers=lambda c: {},
        _weekly_full_charge_unlocked=lambda: False,
        _charge_ceiling_soc=lambda c: c.max_soc,
        _effective_charge_max_soc=lambda c, w, **_kw: (targets[c], "predictive_target"),
        _should_charge_to_bms_cutoff=lambda c, m: False,
        _normal_balance_recal_override={},
        _predictive_charge_target_soc=targets,
    )
    targets.update(
        ChargeDischargeController._compute_deficit_target_soc(ctrl, planned_kwh=quota_kwh, log=False)
    )
    return ctrl


def controller_stores(fleet, quota_kwh):
    """kWh the real controller stores for ``quota_kwh``: (capacity, true SOC) per battery."""
    coords, true_soc = [], {}
    for capacity, soc in fleet:
        c = FakeCoordinator(max_soc=100, data={"battery_total_energy": capacity})
        c.enable_charge_hysteresis = False
        coords.append(c)
        true_soc[c] = soc
    for c in coords:
        c.data["battery_soc"] = math.floor(true_soc[c] + 1e-9)
    ctrl = _controller(coords, quota_kwh)
    start = dict(true_soc)
    for _ in range(100000):  # 0.001 % ticks
        charging = ChargeDischargeController._get_available_batteries(ctrl, is_charging=True)
        if not charging:
            break
        for c in charging:
            true_soc[c] += 0.001
            c.data["battery_soc"] = math.floor(true_soc[c] + 1e-9)
    return sum((true_soc[c] - start[c]) / 100.0 * c.data["battery_total_energy"] for c in coords)


def model_stores(fleet, quota_kwh):
    capacity = sum(cap for cap, _soc in fleet)
    stored = sum(cap * soc / 100.0 for cap, soc in fleet)
    model = BatteryModel(capacity_kwh=capacity, round_trip_efficiency=0.85, floor_kwh=0.0)
    return model.executed_quota(stored, quota_kwh)


@pytest.mark.parametrize("quota", [0.084, 0.30, 1.0])
@pytest.mark.parametrize("soc", [40.0, 40.37, 40.85])
@pytest.mark.parametrize(
    "capacities", [(5.12,), (5.12, 2.24), (10.0, 5.12, 2.24)], ids=["one", "mixed", "three"]
)
def test_a_quota_stores_what_the_controller_stops_at(capacities, soc, quota):
    # Equal SOC: every battery gets the same target, so the fleet rounds to
    # whole percents of its total capacity whatever the capacities. From a
    # fractional SOC the target starts at the reading, so it can store less.
    fleet = [(cap, soc) for cap in capacities]
    assert model_stores(fleet, quota) == pytest.approx(controller_stores(fleet, quota), abs=2e-4)


@pytest.mark.parametrize("quota", [0.084, 0.30, 1.0])
@pytest.mark.parametrize("fleet", [
    [(5.12, 37.6), (2.24, 61.2)],
    [(5.12, 20.3), (10.0, 80.9)],
    [(2.24, 15.5), (5.12, 55.0), (10.0, 90.2)],
])
def test_unequal_socs_stay_within_one_percent_of_the_fleet(fleet, quota):
    # Each battery rounds from its own SOC: the aggregate model can miss by up
    # to the whole rounding, 1 % of each battery, never more.
    bound = sum(cap for cap, _ in fleet) / 100.0
    assert abs(model_stores(fleet, quota) - controller_stores(fleet, quota)) <= bound


# --- The physics of one quarter, by hand (rte 0.81: eff 0.9 each way) ---

MODEL = BatteryModel(capacity_kwh=10.0, round_trip_efficiency=0.81)
DAY = Quarter(house_kwh=0.5, solar_kwh=0.0, import_price=0.30, export_price=0.05,
              grid_charge_allowed=True, charge_kwh=0.6, discharge_kwh=0.6)


def test_the_battery_covers_the_house_through_the_losses():
    # 0.5 kWh of house drains 0.5 / 0.9 stored, and costs nothing.
    e1, cost = MODEL.normal_step(DAY, 5.0)
    assert float(e1) == pytest.approx(5.0 - 0.5 / 0.9)
    assert float(cost) == pytest.approx(0.0)
    assert float(MODEL.cost(DAY, 5.0, e1, forced=True)) == pytest.approx(0.0)


def test_a_quota_charges_first_and_the_grid_carries_the_house_meanwhile():
    # 0.27 kWh is 2.7 % of 10 kWh: it stores 0.30 (3 %), at 0.6 x 0.9 = 0.54
    # per quarter. Busy 0.30 / 0.54 of the quarter, the grid carries that part
    # of the house; the battery covers the rest.
    busy = 0.30 / 0.54
    e1, cost = MODEL.normal_step(DAY, 2.0, quota=0.27)
    assert float(e1) == pytest.approx(2.0 + 0.30 - 0.5 * (1 - busy) / 0.9)
    # Import = 0.30 / 0.9 for the charge + the house while busy, at 0.30: the
    # charge and the discharge after it do not net out.
    assert float(cost) == pytest.approx((0.30 / 0.9 + 0.5 * busy) * 0.30)


def test_surplus_solar_is_stored_and_the_rest_exported():
    sunny = Quarter(house_kwh=0.1, solar_kwh=1.0, import_price=0.30, export_price=0.05,
                    grid_charge_allowed=True, charge_kwh=0.6, discharge_kwh=0.6)
    e1, cost = MODEL.normal_step(sunny, 2.0)
    assert float(e1) == pytest.approx(2.0 + 0.6 * 0.9)  # charge power binds
    assert float(cost) == pytest.approx(-0.3 * 0.05)


def test_a_permission_off_forbids_the_move():
    full = 5.0
    # Selling 0.1 above the house (0.6 out, the discharge power) is refused
    # without the sell permission.
    e1 = full - 0.6 / 0.9
    assert float(MODEL.cost(DAY, full, e1, sell=False)) >= 1e8
    assert float(MODEL.cost(DAY, full, e1)) == pytest.approx(-0.1 * 0.05)
    # Leaving the house to the grid is refused without the hold permission.
    assert float(MODEL.cost(DAY, full, full, hold=False)) >= 1e8
    # No grid charge in a quarter the plan may not buy in.
    closed = Quarter(**{**DAY.__dict__, "grid_charge_allowed": False})
    assert float(MODEL.cost(closed, 2.0, 2.3)) >= 1e8
