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
import random
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing.battery_model import BatteryModel, Quarter
from tests.conftest import FakeCoordinator


def _controller(coords, quota_kwh, window_hours=None):
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
        # The slot path passes window_hours: _shares_within_window reads the
        # per-battery power limit through these.
        _max_soc_mgr=SimpleNamespace(apply_charge_taper=lambda c, limit: limit),
        _temp_limit_mgr=SimpleNamespace(apply_temperature_limit=lambda c, limit: limit),
        _apply_slot_power_ceiling=lambda c, is_charging, limit: limit,
    )
    targets.update(
        ChargeDischargeController._compute_deficit_target_soc(
            ctrl, planned_kwh=quota_kwh, log=False, window_hours=window_hours
        )
    )
    return ctrl


def controller_stores(fleet, quota_kwh, window_hours=None, resolution=1.0):
    """kWh the real controller stores for ``quota_kwh``.

    ``fleet``: (capacity kWh, true SOC[, max_soc, charge W]) per battery. The
    window only shapes the split (it is long enough not to cut the charge)."""
    coords, true_soc = [], {}
    for capacity, soc, *rest in fleet:
        max_soc, power = (*rest, 100, 2500)[:2]
        c = FakeCoordinator(max_soc=max_soc, max_charge_power=power,
                            data={"battery_total_energy": capacity})
        c.enable_charge_hysteresis = False
        coords.append(c)
        true_soc[c] = soc
    def read(soc):
        return math.floor(soc / resolution + 1e-9) * resolution

    for c in coords:
        c.data["battery_soc"] = read(true_soc[c])
    ctrl = _controller(coords, quota_kwh, window_hours)
    start = dict(true_soc)
    for _ in range(100000):  # 0.001 % ticks
        charging = ChargeDischargeController._get_available_batteries(ctrl, is_charging=True)
        if not charging:
            break
        for c in charging:
            true_soc[c] += 0.001
            c.data["battery_soc"] = read(true_soc[c])
    return sum((true_soc[c] - start[c]) / 100.0 * c.data["battery_total_energy"] for c in coords)


def model_stores(fleet, quota_kwh, resolution=1.0):
    capacity = sum(f[0] for f in fleet)
    stored = sum(f[0] * f[1] / 100.0 for f in fleet)
    top = sum(f[0] * (*f[2:], 100)[0] / 100.0 for f in fleet)  # each battery's max_soc
    model = BatteryModel(capacity_kwh=capacity, round_trip_efficiency=0.85, floor_kwh=0.0,
                         usable_kwh=top, soc_resolution_pct=resolution)
    return model.executed_quota(stored, quota_kwh)


@pytest.mark.parametrize("quota", [0.084, 0.30, 1.0])
@pytest.mark.parametrize("soc", [40.0, 40.37, 40.85])
@pytest.mark.parametrize(
    "capacities", [(5.12,), (5.12, 2.24), (10.0, 5.12, 2.24)], ids=["one", "mixed", "three"]
)
@pytest.mark.parametrize("window", [None, 10.0])
def test_a_quota_stores_what_the_controller_stops_at(capacities, soc, quota, window):
    # Equal SOC and max_soc: every battery gets the same target, so the fleet
    # rounds to whole percents of its total capacity whatever the capacities,
    # through the slot path (window) too. From a
    # fractional SOC the target starts at the reading, so it can store less.
    fleet = [(cap, soc) for cap in capacities]
    assert model_stores(fleet, quota) == pytest.approx(controller_stores(fleet, quota, window), abs=2e-4)


@pytest.mark.parametrize("quota", [0.084, 0.30])
@pytest.mark.parametrize("soc", [40.0, 40.37])
def test_a_tenth_of_a_percent_reading_rounds_to_tenths(soc, quota):
    # Huawei reads the SOC in tenths: the rounding shrinks with it.
    fleet = [(5.12, soc)]
    stored = controller_stores(fleet, quota, resolution=0.1)
    assert float(model_stores(fleet, quota, resolution=0.1)) == pytest.approx(stored, abs=2e-4)
    assert abs(stored - quota) <= 0.00512 + 1e-6


def test_other_fleets_stay_within_two_percent_of_the_fleet():
    # Unequal SOCs or max_soc: each battery rounds from its own reading and
    # target, the aggregate model from the fleet's. Each side is within one
    # whole percent of the quota, so they are within two of each other.
    # Measured on 3000 random fleets: 1.37 (unequal SOC), 0.82 (equal SOC,
    # mixed max_soc), 0 with equal SOC and max_soc.
    rng = random.Random(1)
    for _ in range(300):
        fleet = []
        for _b in range(rng.choice((1, 2, 3))):
            top = rng.choice((90, 95, 100))
            fleet.append((rng.choice((2.24, 5.12, 7.0, 8.16, 10.0)),
                          round(rng.uniform(12, top - 5), 2), top, rng.choice((800, 1200, 2500))))
        quota = rng.choice((0.05, 0.084, 0.2, 0.3, 0.6, 1.0, 2.0))
        window = rng.choice((None, 10.0))
        step = sum(f[0] for f in fleet) / 100.0
        gap = abs(float(model_stores(fleet, quota)) - controller_stores(fleet, quota, window))
        assert gap <= 2 * step + 1e-6, (fleet, quota, window)


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
    e1, cost = MODEL.normal_step(DAY, 2.0, target=MODEL.slot_target(2.0, 0.27))
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


@pytest.mark.parametrize("perms", [
    dict(sell=False, hold=False, solar=False), dict(hold=False), dict(),
])
def test_a_grid_charge_only_executes_as_a_quota(perms):
    # The controller charges from the grid only through a slot quota, then the
    # battery covers the house. A free move that charges and leaves the house
    # on the grid was cheaper than that and is not executable (it skipped the
    # rounding too).
    e1, cost = MODEL.normal_step(DAY, 2.0, target=MODEL.slot_target(2.0, 0.27))
    assert float(MODEL.cost(DAY, 2.0, e1, **perms)) >= 1e8


def test_an_hourly_slot_sets_its_target_once():
    # 10 kWh at 20.8 %: the reading is 20 %, the target 30 %, so a 1.0 kWh quota
    # stores 0.92 over two quarters. Rounding each quarter's remainder again
    # stored 1.02.
    hour = Quarter(house_kwh=0.0, solar_kwh=0.0, import_price=0.10, export_price=0.0,
                   grid_charge_allowed=True, charge_kwh=0.6, discharge_kwh=0.6)
    target = MODEL.slot_target(2.08, 1.0)
    e = 2.08
    for _ in range(4):
        e, _cost = MODEL.normal_step(hour, e, target=target)
    assert float(e) == pytest.approx(3.0)


def test_the_top_band_slows_the_charge_and_splits_the_quarter():
    """D-7: 5.12 kWh, rte 0.81 (eff 0.9), top band 0.256 kWh (5 %) charged at
    half of 2.5 kW. From 4.5 kWh a quarter charges 0.364 kWh to the band edge
    in 0.647 of it (0.5625 stored per quarter), then 0.0993 inside the band
    (0.28125 per quarter) for the rest: 0.4633 stored, 0.5147 kWh AC. The grid
    and the sun fill the same way."""
    model = BatteryModel(capacity_kwh=5.12, round_trip_efficiency=0.81, bands=((4.864, 1.25),))
    grid = Quarter(house_kwh=0.0, solar_kwh=0.0, import_price=0.10, export_price=0.0,
                   grid_charge_allowed=True, charge_kwh=0.625, discharge_kwh=0.625)
    e1, cost = model.normal_step(grid, 4.5, target=5.12)
    assert float(e1) == pytest.approx(4.5 + 0.364 + 0.352889 * 0.28125, abs=1e-5)
    assert float(cost) == pytest.approx((0.647111 * 0.625 + 0.352889 * 0.3125) * 0.10, abs=1e-6)
    sun = Quarter(**{**grid.__dict__, "house_kwh": 0.1, "solar_kwh": 1.0,
                     "grid_charge_allowed": False, "export_price": 0.05})
    e1, cost = model.normal_step(sun, 4.5)
    assert float(e1) == pytest.approx(4.5 + 0.364 + 0.352889 * 0.28125, abs=1e-5)
    assert float(cost) == pytest.approx(-(0.9 - 0.514722) * 0.05, abs=1e-6)
    # Inside the band from the start, and without a band: the full rate.
    assert float(model.normal_step(grid, 4.9, target=5.12)[0]) == pytest.approx(4.9 + 0.22)
    plain = BatteryModel(capacity_kwh=5.12, round_trip_efficiency=0.81)
    assert float(plain.normal_step(grid, 4.5, target=5.12)[0]) == pytest.approx(5.0625)


def test_charge_hours_follow_the_bands():
    """5.12 kWh, eff 0.9, the top 0.256 kWh at half of 2.5 kW. From 4.5 kWh:
    0.364 kWh at 2.25 stored kW, then the band at 1.125. A power below the
    band's is not raised by it."""
    model = BatteryModel(capacity_kwh=5.12, round_trip_efficiency=0.81, bands=((4.864, 1.25),))
    assert model.charge_hours(4.5, 5.12, 2.5) == pytest.approx(0.364 / 2.25 + 0.256 / 1.125)
    assert model.charge_hours(4.9, 5.12, 2.5) == pytest.approx(0.22 / 1.125)
    assert model.charge_hours(4.5, 5.12, 1.0) == pytest.approx(0.62 / 0.9)
    plain = BatteryModel(capacity_kwh=5.12, round_trip_efficiency=0.81)
    assert plain.charge_hours(4.5, 5.12, 2.5) == pytest.approx(0.62 / 2.25)
    assert plain.charge_hours(5.12, 5.12, 2.5) == 0.0
    assert plain.charge_hours(4.5, 5.12, 0.0) == math.inf


def test_two_bands_split_the_quarter_twice():
    """The curve of two 5.12 kWh batteries at 50 % and 90 % (2.5 kW each,
    taper 1.25 kW, eff 0.9): 5 kW until 2.56 kWh below the top, 3.75 kW to
    1.792 below, 2.5 kW to 0.256 below, then 1.25 kW. Over 1.3 kWh below the
    top in a quarter at 5 kW: 0.3 kWh at 3.75 kW take 0.3 / (3.75 x 0.9) h,
    the rest of the quarter at 2.5 kW stores 2.5 x 0.9 x (0.25 - that) kWh."""
    cap = 9.216
    model = BatteryModel(capacity_kwh=10.24, round_trip_efficiency=0.81, usable_kwh=cap,
                         bands=((cap - 2.56, 3.75), (cap - 1.792, 2.5), (cap - 0.256, 1.25)))
    q = Quarter(house_kwh=0.0, solar_kwh=0.0, import_price=0.10, export_price=0.0,
                grid_charge_allowed=True, charge_kwh=1.25, discharge_kwh=1.25)
    e0 = cap - 2.092  # 0.3 kWh inside the 3.75 kW band
    t1 = 0.3 / (3.75 * 0.9)
    expected = 0.3 + 2.5 * 0.9 * (0.25 - t1)
    add, busy = model.charge_phase(q, e0, cap)
    assert float(add) == pytest.approx(expected, abs=1e-9) and float(busy) == pytest.approx(1.0)
    # From below every band: 5 kW until the first edge.
    add, _busy = model.charge_phase(q, cap - 3.0, cap)
    t0 = 0.44 / (5.0 * 0.9)
    assert float(add) == pytest.approx(0.44 + 3.75 * 0.9 * (0.25 - t0), abs=1e-9)
