"""A chronological slot's energy is split so each battery can charge it in time.

The split by gap to max_soc gave the battery with the larger gap more than it
could charge in a 15-minute slot, while the other reached its smaller share
after a few minutes and stopped. The slot then delivered less than planned and
logged "not delivered before deadline" (seen with a Venus E and a SolarFlow
2400 AC).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import SLOT_PURPOSE_DEFICIT, PriceSlot


class _Coord:
    def __init__(self, name, soc, capacity_kwh, power_w, derate=1.0, max_soc=90):
        self.name = name
        self.max_soc = max_soc
        self.max_charge_power = power_w
        self.effective_max_charge_power = power_w
        self.derate = derate
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}


def _ctrl(coords, planned_kwh, **extra):
    return SimpleNamespace(
        coordinators=list(coords),
        _weekly_charge_mgr=SimpleNamespace(is_active=lambda: False),
        _last_decision_data={"energy_deficit_kwh": 7.3, "planned_grid_charge_kwh": planned_kwh},
        _max_soc_mgr=SimpleNamespace(apply_charge_taper=lambda c, limit: limit),
        _temp_limit_mgr=SimpleNamespace(
            apply_temperature_limit=lambda c, limit: int(limit * c.derate)
        ),
        _apply_slot_power_ceiling=lambda c, is_charging, limit: limit,
        **extra,
    )


def _fleet(zendure_derate=1.0):
    # Gaps to 90 %: Venus 2.05 kWh, SolarFlow 4.90 kWh.
    venus = _Coord("Venus", 50.0, 5.12, 2400)
    solarflow = _Coord("SolarFlow", 30.0, 8.16, 2400, derate=zendure_derate)
    return venus, solarflow


def _share_kwh(coord, targets):
    return (targets[coord] - coord.data["battery_soc"]) / 100.0 * coord.data["battery_total_energy"]


def _in_window(power_w, hours):
    return power_w / 1000.0 * hours * CHARGE_EFFICIENCY


def test_each_share_fits_what_the_battery_charges_in_the_slot():
    venus, solarflow = _fleet()
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _ctrl([venus, solarflow], 1.02), planned_kwh=1.02, window_hours=0.25
    )

    cap = _in_window(2400, 0.25)
    assert _share_kwh(venus, targets) == pytest.approx(cap, abs=1e-6)
    assert _share_kwh(solarflow, targets) == pytest.approx(cap, abs=1e-6)
    assert _share_kwh(venus, targets) + _share_kwh(solarflow, targets) == pytest.approx(1.02)


def test_a_derated_battery_gets_only_what_it_charges_in_time():
    """46 °C: the SolarFlow charges at 64 % of its power."""
    venus, solarflow = _fleet(zendure_derate=0.64)
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _ctrl([venus, solarflow], 0.8), planned_kwh=0.8, window_hours=0.25
    )

    solarflow_cap = _in_window(1536, 0.25)
    assert _share_kwh(solarflow, targets) == pytest.approx(solarflow_cap, abs=1e-6)
    assert _share_kwh(venus, targets) == pytest.approx(0.8 - solarflow_cap, abs=1e-6)
    assert _share_kwh(venus, targets) <= _in_window(2400, 0.25) + 1e-6


def test_more_than_the_slot_can_take_keeps_every_battery_charging():
    """What fits nowhere stays in the targets, so the shortfall is still counted."""
    venus, solarflow = _fleet(zendure_derate=0.64)
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _ctrl([venus, solarflow], 1.02), planned_kwh=1.02, window_hours=0.25
    )

    assert _share_kwh(venus, targets) >= _in_window(2400, 0.25) - 1e-6
    assert _share_kwh(solarflow, targets) >= _in_window(1536, 0.25) - 1e-6
    assert _share_kwh(venus, targets) + _share_kwh(solarflow, targets) == pytest.approx(1.02)


def test_a_share_never_passes_the_battery_s_gap():
    venus = _Coord("Venus", 88.0, 5.12, 2400)
    solarflow = _Coord("SolarFlow", 30.0, 8.16, 800)
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _ctrl([venus, solarflow], 1.02), planned_kwh=1.02, window_hours=0.25
    )

    assert targets[venus] == pytest.approx(90.0)
    assert _share_kwh(solarflow, targets) == pytest.approx(1.02 - 0.1024, abs=1e-6)


def test_without_a_slot_window_the_split_stays_by_gap():
    venus, solarflow = _fleet()
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _ctrl([venus, solarflow], 1.02), planned_kwh=1.02
    )

    gap_venus, gap_solarflow = 0.4 * 5.12, 0.6 * 8.16
    total = gap_venus + gap_solarflow
    assert _share_kwh(venus, targets) == pytest.approx(1.02 * gap_venus / total)
    assert _share_kwh(solarflow, targets) == pytest.approx(1.02 * gap_solarflow / total)


def test_the_active_chronological_slot_passes_its_remaining_time():
    venus, solarflow = _fleet()
    now = datetime.now()
    slot = PriceSlot(now - timedelta(minutes=5), now + timedelta(minutes=10), 0.44)
    schedule = SimpleNamespace(
        chronological_planning_active=True,
        selected_slots=[slot],
        slot_energy_targets_kwh={slot: 0.6},
    )
    ctrl = _ctrl(
        [venus, solarflow],
        7.3,
        predictive_charging_mode="dynamic_pricing",
        _dynamic_pricing_schedule=schedule,
        _active_dynamic_slot_purpose=SLOT_PURPOSE_DEFICIT,
    )
    targets = ChargeDischargeController._compute_predictive_target_soc(ctrl)

    cap = _in_window(2400, 10 / 60)
    assert _share_kwh(solarflow, targets) == pytest.approx(cap, abs=0.01)
    assert _share_kwh(venus, targets) == pytest.approx(0.6 - cap, abs=0.01)
