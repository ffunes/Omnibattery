"""Weekly full charge in Time Slot mode stopped at ~80% overnight (2026-11-09).

Field case: Venus 5.12 kWh at 13% + Zendure 5.28 kWh at 6%, window 00:00-08:00,
solar 7.8 kWh vs 12.9 kWh consumption (no daily surplus). Both batteries stopped
at 80% at 02:32/03:17 and the window idled until 08:00. Two independent faults:

1. ``_unclipped_room`` lands the whole slot's energy at the slot END. The
   controller charges from the slot START and the battery then covers the house
   until the sun, so the planner overestimates how much the midday solar would
   clip and cuts the night charge (field: 9.42 kWh gap -> 7.91 kWh planned;
   a realistic simulation of the full charge clips only 0.26 kWh).
2. A Time Slot re-evaluation refreshes the quota but not the per-battery SOC
   target, which is only recomputed while ``_grid_charging_initialized`` is
   False. The 00:00 target (80%) survived the 01:59 re-evaluation that asked
   for ~95%.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    EnergyInterval,
    allocate_price_slots,
    build_energy_deadlines,
)
from custom_components.omnibattery.pricing.engine import PricingManager

T0 = datetime(2026, 11, 9)


def _hour(i, consumption, solar):
    return EnergyInterval(
        T0 + timedelta(hours=i), T0 + timedelta(hours=i + 1), consumption, solar
    )


def test_night_charge_is_not_cut_by_clipping_the_battery_never_reaches():
    # 10 kWh fleet empty at 00:00, window 00:00-08:00, 3 kW (2.55 kWh/h stored).
    # House: 0.25 kWh/h to 08:00, 0.3 to 10:00, 10-16 solar leaves +0.4 kWh/h
    # (2.4 kWh), 0.5 kWh/h in the evening. Daily solar < daily load.
    intervals = (
        [_hour(i, 0.25, 0.0) for i in range(8)]
        + [_hour(i, 0.30, 0.0) for i in range(8, 10)]
        + [_hour(i, 0.30, 0.7) for i in range(10, 16)]
        + [_hour(i, 0.50, 0.0) for i in range(16, 24)]
    )
    deadlines = build_energy_deadlines(intervals, 0.0, usable_capacity_kwh=10.0)

    plan = allocate_price_slots(
        intervals,
        deadlines,
        [PriceSlot(T0, T0.replace(hour=8), 0.0)],
        total_required_kwh=10.0,  # weekly gap to 100%
        effective_power_kw=3.0,
        now=T0,
        horizon_end=T0 + timedelta(days=1),
        headroom_kwh=10.0,
        usable_initial_kwh=0.0,
    )

    # Charging X kWh from 00:00 ends at X/2.55 h; the battery then drains
    # 0.25 kWh/h to 08:00 and 0.6 kWh to 10:00, and the sun adds 2.4 kWh.
    # Reaching 100% at 16:00 needs X >= 7.6 + drain, i.e. X ~= 9.29 kWh.
    # The slot-end model plans 8.45 kWh: the pack peaks at ~91% and the
    # weekly cycle never completes.
    assert plan.allocated_kwh >= 9.25


def test_time_slot_re_evaluation_refreshes_the_per_battery_target():
    class _Coord:  # hashable: the target map is keyed by coordinator
        name = "battery"
        data = {"battery_soc": 60.0, "battery_total_energy": 10.0}

    coord = _Coord()
    seen = {}

    async def _decision():
        return {"should_charge": True, "solar_forecast_kwh": 7.8}

    async def _handle():
        # What the controller would charge to on this cycle.
        seen["target"] = controller._predictive_charge_target_soc
        seen["initialized"] = controller._grid_charging_initialized

    async def _noop(*_args, **_kwargs):
        return None

    def _apply(decision_data, *, now):
        controller._active_time_slot_quota_kwh = 3.6  # replanned remaining quota
        return decision_data

    controller = SimpleNamespace(
        charging_time_slots=["slot"],
        predictive_charging_overridden=False,
        # Charging since the 00:00 evaluation, which sized an 80% target.
        grid_charging_active=True,
        _grid_charging_initialized=True,
        _predictive_charge_target_soc={coord: 80.0},
        _active_time_slot_quota_kwh=7.39,
        last_evaluation_soc=9.0,  # SOC rose by >= 30% since -> re-evaluation
        _predictive_min_soc_floor=0.0,
        _predictive_min_soc_floor_enabled=False,
        coordinators=[coord],
        max_contracted_power=6900,
        first_execution=False,
        _slot_entry_time=T0,
        _last_decision_data=None,
        _check_time_window=lambda: True,
        solar_forecast_remaining_sensor=None,
        solar_forecast_sensor=None,
        _handle_predictive_grid_charging=_handle,
        hass=SimpleNamespace(),
    )
    engine = PricingManager(hass=controller.hass, controller=controller)
    engine._current_horizon_grid_charging_decision = _decision
    engine._apply_time_slot_chronological_plan = _apply
    engine._send_predictive_charging_notification = _noop
    engine._refresh_solar_forecast_reference = lambda _now: None
    engine._is_solar_forecast_reeval = lambda _now: False
    engine._now = lambda: T0.replace(hour=1, minute=59)

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert controller.last_evaluation_soc == 60.0  # the re-evaluation ran
    # The stale 80% target must not survive: either cleared or flagged for
    # recomputation from the new quota.
    assert seen["target"] is None or seen["initialized"] is False
