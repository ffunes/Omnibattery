"""A Dynamic Pricing charge still running at midnight is stopped cleanly.

The new-day reset runs before the slot check and only cleared
``_current_price_slot_active``. ``grid_charging_active`` stayed True, and with
the slot flag already False neither the ``slot_ended`` branch nor (when the
pruned schedule became ``None``) the slot check itself ever stopped it. The
control loop returns early while ``grid_charging_active`` is set, so the
battery held its last charge command until predictive charging was toggled.

The harness mirrors ``test_dynamic_pricing_slot_handover``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import custom_components.omnibattery as omnibattery_module
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from custom_components.omnibattery.control.pack_soc import soc_vs_ceiling
from custom_components.omnibattery.pricing import (
    DynamicPricingSchedule,
    PriceSlot,
    SLOT_PURPOSE_DEFICIT,
)
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.engine import PricingManager

_DAY = datetime(2026, 9, 26)
_MIDNIGHT = _DAY + timedelta(days=1)


@pytest.fixture
def clock(monkeypatch):
    class _Clock(datetime):
        value = _DAY.replace(hour=23, minute=30)

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr(engine_module, "datetime", _Clock)
    monkeypatch.setattr(omnibattery_module, "datetime", _Clock)
    return _Clock


class _Battery:
    def __init__(self, soc: float, capacity_kwh: float = 10.0, max_soc: float = 95.0):
        self.name = "b1"
        self.max_soc = max_soc
        self.is_available = True
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}

    @property
    def soc(self) -> float:
        return self.data["battery_soc"]


def _decision(planned_kwh: float) -> dict:
    return {
        "should_charge": planned_kwh > 0.0,
        "energy_deficit_kwh": planned_kwh,
        "planned_grid_charge_kwh": planned_kwh,
        "stored_energy_kwh": None,
    }


def _slot(start: datetime, hours: int = 1, price: float = 0.10) -> PriceSlot:
    return PriceSlot(start, start + timedelta(hours=hours), price)


def _schedule(slots: list[PriceSlot]) -> DynamicPricingSchedule:
    return DynamicPricingSchedule(
        hours_needed=float(len(slots)),
        selected_slots=list(slots),
        average_price=sum(slot.price for slot in slots) / len(slots),
        estimated_cost=0.0,
        total_available_slots=len(slots),
        evaluation_time=_DAY.replace(hour=13),
        energy_deficit_kwh=5.0,
        charging_needed=True,
        slot_purposes={slot: SLOT_PURPOSE_DEFICIT for slot in slots},
        deficit_charging_needed=True,
        negative_price_charging_needed=False,
    )


def _controller(battery: _Battery, schedule) -> SimpleNamespace:
    ctrl = SimpleNamespace(
        coordinators=[battery],
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        predictive_charging_overridden=False,
        negative_price_charging_enabled=True,
        max_contracted_power=10000,
        max_charge_capacity=10000,
        _last_decision_data=_decision(5.0),
        _dynamic_pricing_schedule=schedule,
        _dynamic_pricing_evaluated_date=_DAY.date(),
        _dp_evening_reevaluated_date=_DAY.date(),
        _dp_eval_retry_count=0,
        _dp_pre_evaluated_slots={},
        _dp_pre_evaluated_purposes={},
        _dp_completed_slots=set(),
        _current_price_slot_active=False,
        _active_dynamic_slot_purpose=None,
        _active_dynamic_price_slot=None,
        _predictive_charge_target_soc=None,
        _predictive_deficit_target_soc=None,
        grid_charging_active=False,
        _grid_charging_initialized=False,
        previous_power=0,
        previous_error=0,
        first_execution=True,
        is_charge_blocked=lambda: False,
        remove_setpoint_override=lambda *_args, **_kwargs: None,
        remove_discharge_block=lambda *_args, **_kwargs: None,
        idle_writes=[],
    )

    async def set_power(coordinator, charge, discharge):
        ctrl.idle_writes.append((coordinator.name, charge, discharge))

    async def handle_predictive():
        if not ctrl._grid_charging_initialized or ctrl._predictive_charge_target_soc is None:
            ctrl._predictive_charge_target_soc = ctrl._compute_predictive_target_soc()
        target = (ctrl._predictive_charge_target_soc or {}).get(battery, battery.max_soc)
        if soc_vs_ceiling(battery, battery.soc) >= target:
            ctrl.grid_charging_active = False
            ctrl._grid_charging_initialized = False
            ctrl.first_execution = True
            return
        if not ctrl._grid_charging_initialized:
            ctrl.previous_power = -2500.0
            ctrl.first_execution = False
            ctrl._grid_charging_initialized = True

    ctrl._set_battery_power = set_power
    ctrl._handle_predictive_grid_charging = handle_predictive
    ctrl._compute_predictive_target_soc = (
        lambda **kwargs: ChargeDischargeController._compute_predictive_target_soc(
            ctrl, **kwargs
        )
    )
    return ctrl


async def _noop(*_args, **_kwargs):
    return None


def _manager(ctrl) -> PricingManager:
    manager = PricingManager(SimpleNamespace(), ctrl)

    async def remaining(*, now=None, horizon_end=None):
        return _decision(5.0)

    manager._evaluate_remaining_grid_charging = remaining
    manager._maybe_refresh_service_prices = _noop
    manager._send_dynamic_pricing_slot_start_notification = _noop
    manager._send_dp_pre_slot_reevaluation_notification = _noop
    manager._is_evening_reevaluation_time = lambda: False
    manager._is_dp_soc_drop_reeval = lambda: False
    manager._is_excluded_demand_reeval = lambda _now: False
    manager._is_solar_forecast_reeval = lambda _now: False
    manager._is_price_publication_reeval = lambda _now: False
    manager._refresh_excluded_demand_reference = lambda: None
    manager._refresh_solar_forecast_reference = lambda _now: None
    manager._get_current_price = lambda: 0.10
    return manager


def _run(manager) -> None:
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())


def _start_late_evening_slot(clock, extra_slots: list[PriceSlot] = ()):
    battery = _Battery(20.0)
    evening = _slot(_DAY.replace(hour=23))
    ctrl = _controller(battery, _schedule([evening, *extra_slots]))
    manager = _manager(ctrl)

    clock.value = _DAY.replace(hour=23, minute=30)
    _run(manager)
    assert ctrl._current_price_slot_active is True
    assert ctrl.grid_charging_active is True
    return ctrl, manager


def test_slot_ending_at_midnight_stops_grid_charging(clock):
    ctrl, manager = _start_late_evening_slot(clock)

    clock.value = _MIDNIGHT.replace(second=5)
    _run(manager)

    # The pruned schedule is empty, so the slot check is skipped entirely.
    assert ctrl._dynamic_pricing_schedule is None
    assert ctrl._current_price_slot_active is False
    assert ctrl.grid_charging_active is False


def test_slot_ending_at_midnight_stops_when_a_later_slot_remains(clock):
    later = _slot(_MIDNIGHT.replace(hour=3))
    ctrl, manager = _start_late_evening_slot(clock, [later])

    clock.value = _MIDNIGHT.replace(second=5)
    _run(manager)

    # The schedule survives and the slot check runs, but finds nothing to stop.
    assert ctrl._dynamic_pricing_schedule is not None
    assert ctrl._dynamic_pricing_schedule.selected_slots == [later]
    assert ctrl._current_price_slot_active is False
    assert ctrl.grid_charging_active is False


def test_slot_ending_at_midnight_does_not_stay_stuck(clock):
    ctrl, manager = _start_late_evening_slot(clock)

    for minute_offset in (0, 10, 60, 8 * 60):
        clock.value = _MIDNIGHT + timedelta(minutes=minute_offset, seconds=5)
        _run(manager)

    assert ctrl.grid_charging_active is False


def test_slot_continuing_past_midnight_restarts_on_the_next_slot(clock):
    after_midnight = _slot(_MIDNIGHT)
    ctrl, manager = _start_late_evening_slot(clock, [after_midnight])

    clock.value = _MIDNIGHT.replace(second=5)
    _run(manager)

    assert ctrl._current_price_slot_active is True
    assert ctrl.grid_charging_active is True
    assert ctrl._active_dynamic_price_slot == after_midnight
