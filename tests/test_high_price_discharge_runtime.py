"""Runtime tests for deliberate export in high-price slots (#270).

No Home Assistant and no Modbus: the manager talks to stand-ins exposing only
what it reads. The clock is injected, never mocked globally.

The first test is the one that matters most: the pricing layer hands out naive
local datetimes and the planner refuses naive ones, so a manager that forgets
to re-attach the zone would silently never sell anything.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from custom_components.omnibattery.control.high_price_discharge import (
    GUARD_CAPACITY_PROTECTION,
    GUARD_CHARGE_ORDER,
    GUARD_CURTAILMENT,
    GUARD_DISCHARGE_BLOCKED,
    GUARD_GRID_METER,
    GUARD_MANUAL,
    GUARD_NO_MAX_POWER,
    GUARD_NOT_ENABLED,
    GUARD_WEEKLY_FULL_CHARGE,
    OVERRIDE_PRIORITY,
    OVERRIDE_SOURCE,
    STATE_ACTIVE,
    STATE_BLOCKED,
    STATE_DISABLED,
    STATE_INVALID_CONFIGURATION,
    STATE_NO_DATA,
    STATE_WAITING,
    HighPriceDischargeManager,
    solar_wh_by_slot,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.high_price_discharge import STATUS_PLANNED

DAY = datetime(2026, 8, 2)
NOW = DAY + timedelta(hours=18, minutes=30)
HORIZON_END = DAY + timedelta(days=1, hours=7)

# The peak is the slot covering ``NOW``; everything after it is cheap to buy
# back, which is what makes the sale repurchasable.
PEAK_PRICE = 0.50
BASE_PRICE = 0.10


def _price_slots(export: bool) -> list[PriceSlot]:
    """Hourly slots covering 18:00 through 07:00 the next morning."""
    slots = []
    cursor = DAY + timedelta(hours=18)
    while cursor < HORIZON_END:
        price = PEAK_PRICE if (export and cursor.hour == 18) else BASE_PRICE
        slots.append(PriceSlot(cursor, cursor + timedelta(hours=1), price))
        cursor += timedelta(hours=1)
    return slots


def _forecast():
    """A flat 0.3 kWh/h learned profile for both dates in the horizon."""
    bins = [0.075] * 96
    return SimpleNamespace(
        intervals_by_date={DAY.date(): list(bins), (DAY + timedelta(days=1)).date(): list(bins)},
        intervals_kwh=list(bins),
        source="profile",
    )


def _pricing(**overrides):
    base = SimpleNamespace(
        energy_horizon_end=lambda now: HORIZON_END,
        get_future_export_price_slots=lambda horizon_end=None: _price_slots(True),
        get_future_price_slots=lambda horizon_end=None: _price_slots(False),
        _profile_remaining_consumption=lambda start, end: _forecast(),
        _curtailment_forecast_model=lambda now: (0.0, None, 0.0),
        _curtailment_battery_snapshots=lambda: [
            BatterySnapshot("battery-1", 80.0, 10.0, 100.0, 10.0, 2500.0, True, True)
        ],
        is_in_dynamic_pricing_slot=lambda: False,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _manager(now: datetime = NOW, *, pricing=None, **overrides):
    overrides.setdefault("_pricing_mgr", pricing if pricing is not None else _pricing())
    setpoints: dict[str, tuple[int, float]] = {}

    controller = SimpleNamespace(
        high_price_discharge_enabled=True,
        high_price_discharge_max_power_w=2000.0,
        min_arbitrage_margin=None,
        round_trip_efficiency=1.0,
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        consumption_sensor="sensor.grid_power",
        solar_forecast_sensor=None,
        solar_forecast_remaining_sensor=None,
        _current_price_slot_active=False,
        _force_full_charge=False,
        _weekly_charge_mgr=None,
        _curtailment_runtime_status=None,
        _capacity_protection_active=False,
        manual_mode_enabled=False,
        _manual_slot_owned=None,
        is_discharge_blocked=lambda *a, **k: False,
        _apply_meter_transform=lambda state: 0.0,
        _sensor_is_within_stale_tolerance=lambda reported_at, now=None: True,
        set_setpoint_override=lambda source, value_w, priority=0: setpoints.__setitem__(
            source, (priority, value_w)
        ),
        remove_setpoint_override=lambda source: setpoints.pop(source, None),
    )
    for key, value in overrides.items():
        setattr(controller, key, value)

    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity_id: SimpleNamespace(last_reported=None))
    )
    manager = HighPriceDischargeManager(hass, controller)
    manager._now = lambda: now
    return manager, controller, setpoints


# ----------------------------------------------------------------------
# The timezone boundary
# ----------------------------------------------------------------------


def test_naive_price_slots_still_produce_a_plan():
    """The adapter re-attaches the local zone the planner requires."""
    manager, _controller, setpoints = _manager()

    manager.refresh_override()

    assert manager.plan is not None
    assert manager.plan.status == STATUS_PLANNED, manager.plan.reason
    assert manager.get_status()["state"] == STATE_ACTIVE
    assert setpoints[OVERRIDE_SOURCE][0] == OVERRIDE_PRIORITY
    assert setpoints[OVERRIDE_SOURCE][1] == pytest.approx(
        -manager.plan.allocation_at(
            NOW.replace(tzinfo=manager.plan.allocations[0].start.tzinfo)
        ).power_w
    )
    assert setpoints[OVERRIDE_SOURCE][1] < 0


def test_export_never_exceeds_the_configured_net_ceiling():
    manager, _controller, setpoints = _manager()

    manager.refresh_override()

    assert -setpoints[OVERRIDE_SOURCE][1] <= 2000.0 + 1e-6


# ----------------------------------------------------------------------
# Scope and configuration
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"high_price_discharge_enabled": False},
        {"predictive_charging_enabled": False},
        {"predictive_charging_overridden": True},
        {"predictive_charging_mode": "time_slot"},
    ],
)
def test_out_of_scope_disables_the_feature(override):
    manager, _controller, setpoints = _manager(**override)

    manager.refresh_override()

    assert manager.get_status() == {
        "state": STATE_DISABLED,
        "reason": GUARD_NOT_ENABLED,
        "enabled": override.get("high_price_discharge_enabled", True),
        "target_w": None,
        "surplus_export_enabled": False,
        "trigger_1_budget_kwh": 0.0,
        "refill_price": None,
        "trigger_1_reason": None,
    }
    assert OVERRIDE_SOURCE not in setpoints


@pytest.mark.parametrize("power", [0.0, -100.0, None, "abc"])
def test_export_without_a_limit_is_an_invalid_configuration(power):
    manager, _controller, setpoints = _manager(high_price_discharge_max_power_w=power)

    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_INVALID_CONFIGURATION
    assert manager.get_status()["reason"] == GUARD_NO_MAX_POWER
    assert OVERRIDE_SOURCE not in setpoints


def test_arbitrage_margin_above_the_spread_stops_the_sale():
    """The charge-side margin is the sell-side margin (no separate knob)."""
    manager, _controller, setpoints = _manager(min_arbitrage_margin=PEAK_PRICE)

    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_WAITING
    assert OVERRIDE_SOURCE not in setpoints


# ----------------------------------------------------------------------
# Missing data fails safe
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "pricing_override",
    [
        {"get_future_export_price_slots": lambda horizon_end=None: []},
        {"_profile_remaining_consumption": lambda start, end: None},
        {"get_future_price_slots": lambda horizon_end=None: []},
    ],
)
def test_missing_inputs_never_export(pricing_override):
    manager, _controller, setpoints = _manager(pricing=_pricing(**pricing_override))

    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_NO_DATA
    assert OVERRIDE_SOURCE not in setpoints


def test_unreadable_configured_solar_forecast_is_not_treated_as_zero():
    manager, _controller, setpoints = _manager(
        pricing=_pricing(_curtailment_forecast_model=lambda now: (None, None, None)),
        solar_forecast_remaining_sensor="sensor.pv_remaining",
    )

    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_NO_DATA
    assert OVERRIDE_SOURCE not in setpoints


def test_battery_only_install_plans_without_a_solar_forecast():
    manager, _controller, setpoints = _manager(
        pricing=_pricing(_curtailment_forecast_model=lambda now: (None, None, None))
    )

    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_ACTIVE
    assert OVERRIDE_SOURCE in setpoints


# ----------------------------------------------------------------------
# Guards
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"_current_price_slot_active": True}, GUARD_CHARGE_ORDER),
        ({"_force_full_charge": True}, GUARD_WEEKLY_FULL_CHARGE),
        (
            {"_weekly_charge_mgr": SimpleNamespace(is_active=lambda: True)},
            GUARD_WEEKLY_FULL_CHARGE,
        ),
        ({"_curtailment_runtime_status": "predischarging"}, GUARD_CURTAILMENT),
        ({"_curtailment_runtime_status": "protected_window"}, GUARD_CURTAILMENT),
        ({"_capacity_protection_active": True}, GUARD_CAPACITY_PROTECTION),
        ({"manual_mode_enabled": True}, GUARD_MANUAL),
        ({"is_discharge_blocked": lambda *a, **k: True}, GUARD_DISCHARGE_BLOCKED),
        ({"_apply_meter_transform": lambda state: None}, GUARD_GRID_METER),
        ({"consumption_sensor": None}, GUARD_GRID_METER),
    ],
)
def test_guards_withdraw_the_override(override, reason):
    manager, controller, setpoints = _manager()
    manager.refresh_override()
    assert OVERRIDE_SOURCE in setpoints

    for key, value in override.items():
        setattr(controller, key, value)
    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_BLOCKED
    assert manager.get_status()["reason"] == reason
    assert OVERRIDE_SOURCE not in setpoints


def test_a_scheduled_charge_slot_wins():
    manager, _controller, setpoints = _manager(
        pricing=_pricing(is_in_dynamic_pricing_slot=lambda: True)
    )

    manager.refresh_override()

    assert manager.get_status()["reason"] == GUARD_CHARGE_ORDER
    assert OVERRIDE_SOURCE not in setpoints


def test_a_stale_grid_meter_withdraws_the_override():
    manager, _controller, setpoints = _manager(
        _sensor_is_within_stale_tolerance=lambda reported_at, now=None: False
    )
    manager._hass.states.get = lambda entity_id: SimpleNamespace(
        last_reported=NOW - timedelta(minutes=30)
    )

    manager.refresh_override()

    assert manager.get_status()["reason"] == GUARD_GRID_METER
    assert OVERRIDE_SOURCE not in setpoints


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


def test_the_override_is_withdrawn_when_the_slot_ends():
    manager, _controller, setpoints = _manager()
    manager.refresh_override()
    assert OVERRIDE_SOURCE in setpoints

    # Same cached plan, one hour later: the peak slot is over.
    manager._now = lambda: DAY + timedelta(hours=19, minutes=30)
    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_WAITING
    assert OVERRIDE_SOURCE not in setpoints


def test_the_plan_is_rebuilt_when_the_configuration_changes():
    rebuilds = []

    def _slots(horizon_end=None):
        rebuilds.append(1)
        return _price_slots(True)

    manager, controller, _setpoints = _manager(
        pricing=_pricing(get_future_export_price_slots=_slots)
    )
    manager.refresh_override()
    manager.refresh_override()
    assert len(rebuilds) == 1

    controller.min_arbitrage_margin = 0.01
    manager.refresh_override()

    assert len(rebuilds) == 2


def test_reserve_toggle_passes_coverage_flag_and_rebuilds(monkeypatch):
    import custom_components.omnibattery.control.high_price_discharge as runtime

    planner = Mock(wraps=runtime.plan_high_price_discharge)
    monkeypatch.setattr(runtime, "plan_high_price_discharge", planner)
    manager, controller, _ = _manager()
    manager.refresh_override()
    assert planner.call_args.kwargs["chronological_coverage"] is True
    manager.refresh_override()
    assert planner.call_count == 1

    controller.discharge_reserve_enabled = True
    manager.refresh_override()
    assert planner.call_count == 2
    assert planner.call_args.kwargs["chronological_coverage"] is False

    controller.discharge_reserve_enabled = False
    manager.refresh_override()
    assert planner.call_count == 3
    assert planner.call_args.kwargs["chronological_coverage"] is True


def test_status_carries_the_plan_for_diagnostics():
    manager, _controller, _setpoints = _manager()

    manager.refresh_override()
    status = manager.get_status()

    assert status["plan_status"] == STATUS_PLANNED
    assert status["protected_demand_kwh"] > 0
    assert status["usable_energy_kwh"] > 0
    assert status["total_allocated_kwh"] > 0
    assert status["allocations"][0]["power_w"] > 0
    assert status["target_w"] < 0
    assert isinstance(status["horizon_end"], str)


def test_clear_runtime_drops_a_live_override():
    """Unload has no next control cycle to withdraw the setpoint on."""
    manager, _controller, setpoints = _manager()
    manager.refresh_override()
    assert OVERRIDE_SOURCE in setpoints

    manager.clear_runtime("unload")

    assert OVERRIDE_SOURCE not in setpoints
    status = manager.get_status()
    assert (status["state"], status["reason"]) == (STATE_DISABLED, "unload")
    assert status["target_w"] is None


# ----------------------------------------------------------------------
# The protected horizon after midnight
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected_end"),
    [
        # Before sunrise the night being protected ends this morning; asking
        # for the next day's sunrise needs prices not published until ~13:00.
        (2, DAY + timedelta(hours=7)),
        (9, DAY + timedelta(days=1, hours=7)),
    ],
)
def test_horizon_ends_at_the_next_sunrise_not_the_next_days(hour, expected_end):
    requested: list[datetime] = []

    def export_slots(horizon_end=None):
        requested.append(horizon_end)
        return []

    pricing = _pricing(
        # Mirrors PricingManager: always the sunrise (07:00) of now's date + 1.
        energy_horizon_end=lambda now: datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time()
        ) + timedelta(hours=7),
        get_future_export_price_slots=export_slots,
    )
    manager, _controller, _setpoints = _manager(DAY + timedelta(hours=hour), pricing=pricing)

    manager.refresh_override()

    assert requested == [expected_end]


# Trigger 1 wiring uses a patched planner so these tests are independent of the
# separate pure-planner implementation.
def test_surplus_only_flag_and_rebuild_on_toggle(monkeypatch):
    import custom_components.omnibattery.control.high_price_discharge as runtime

    manager, controller, setpoints = _manager(high_price_discharge_enabled=False)
    assert not manager.feature_enabled()
    manager.refresh_override()
    assert manager.get_status()["state"] == STATE_DISABLED
    assert OVERRIDE_SOURCE not in setpoints

    planner = Mock(return_value=SimpleNamespace(
        status="waiting", reason="no_refill_price", allocations=[SimpleNamespace(
            start=manager._aware(NOW), end=manager._aware(NOW + timedelta(hours=1)),
            export_price=.5, threshold=.2, energy_kwh=.6, surplus_kwh=.4, power_w=600,
        )],
        horizon_end=manager._aware(HORIZON_END), protected_demand_kwh=0,
        usable_energy_kwh=0, total_allocated_kwh=0, trigger_1_budget_kwh=1.2,
        refill_price=0.1, trigger_1_reason="ready",
        allocation_at=lambda now: None,
    ))
    monkeypatch.setattr(runtime, "plan_high_price_discharge", planner)
    controller.high_price_surplus_export_enabled = True
    controller._predictive_safety_margin_kwh = 0.4
    manager._solar_wh_fetched_mono = runtime.monotonic()
    assert manager.feature_enabled()
    manager.refresh_override()
    assert planner.call_args.kwargs["enabled"] is False
    assert planner.call_args.kwargs["trigger_1_enabled"] is True
    assert planner.call_args.kwargs["safety_margin_kwh"] == 0.4
    assert manager.get_status()["trigger_1_budget_kwh"] == 1.2
    assert manager.get_status()["refill_price"] == 0.1
    assert manager.get_status()["trigger_1_reason"] == "ready"
    assert manager.get_status()["allocations"][0]["surplus_kwh"] == .4
    planner.reset_mock()
    manager.refresh_override()
    planner.assert_not_called()
    controller.high_price_discharge_enabled = True
    manager.refresh_override()
    planner.assert_called_once()
    controller.high_price_surplus_export_enabled = False
    controller.high_price_discharge_enabled = False
    manager.refresh_override()
    assert manager.get_status()["state"] == STATE_DISABLED
    assert OVERRIDE_SOURCE not in setpoints


def test_solar_half_hours_map_to_hourly_and_quarter_hour_slots(monkeypatch):
    import custom_components.omnibattery.control.high_price_discharge as runtime

    monkeypatch.setattr(runtime.dt_util, "as_local", lambda dt: dt.astimezone(ZoneInfo("Europe/Madrid")))
    start = datetime(2026, 8, 3, 7)
    keys = {"2026-08-03T05:00:00+00:00": 100,
            "2026-08-03T05:30:00+00:00": 200,
            "2026-08-03T07:00:00+00:00": 400}
    hourly = [PriceSlot(start + timedelta(hours=i), start + timedelta(hours=i+1), .1)
              for i in range(5)]
    result = solar_wh_by_slot(hourly, keys, start)
    assert list(result.values()) == pytest.approx([.3, 0, .4])
    quarter = [PriceSlot(start + timedelta(minutes=15*i), start + timedelta(minutes=15*(i+1)), .1)
               for i in range(6)]
    result = solar_wh_by_slot(quarter, keys, start)
    # Each half hour is spread over its two quarter hours, not dumped in the first.
    assert list(result.values()) == pytest.approx([.05, .05, .1, .1, 0, 0])
    assert solar_wh_by_slot(hourly, {"2026-08-03T04:00:00+00:00": 100}, start) == {}


@pytest.mark.asyncio
async def test_missing_energy_platform_fails_closed(monkeypatch):
    import custom_components.omnibattery.control.high_price_discharge as runtime

    manager, controller, _ = _manager(solar_forecast_sensor="sensor.forecast")
    registry = SimpleNamespace(async_get=lambda sensor: SimpleNamespace(config_entry_id="abc"))
    monkeypatch.setattr(runtime.er, "async_get", lambda hass: registry)
    manager._hass.config_entries = SimpleNamespace(async_get_entry=lambda id: SimpleNamespace(domain="solar", entry_id=id))

    async def fail(*args):
        raise ImportError("no energy platform")

    monkeypatch.setattr(runtime, "async_get_integration", fail)
    await manager._fetch_solar_forecast()
    assert manager._solar_wh_hours == {}
    assert manager._build_fill_slots(controller._pricing_mgr, NOW, HORIZON_END) == ()
    registry.async_get = lambda sensor: SimpleNamespace(config_entry_id=None)
    await manager._fetch_solar_forecast()
    assert manager._solar_wh_hours == {}


def test_fill_slots_use_tomorrows_window_and_profile(monkeypatch):
    import custom_components.omnibattery.control.high_price_discharge as runtime

    monkeypatch.setattr(runtime.dt_util, "as_local", lambda dt: dt.astimezone(ZoneInfo("Europe/Madrid")))
    requested = []
    starts = [HORIZON_END + timedelta(hours=i) for i in range(3)]
    slots = [PriceSlot(start, start + timedelta(hours=1), .2) for start in starts]

    def export_slots(horizon_end=None):
        requested.append(horizon_end)
        return slots

    profile_calls = []
    def profile(start, end):
        profile_calls.append((start, end))
        return _forecast()

    manager, _, _ = _manager(pricing=_pricing(
        get_future_export_price_slots=export_slots,
        _profile_remaining_consumption=profile,
    ))
    manager._solar_wh_hours = {"2026-08-03T05:00:00+00:00": 600,
                               "2026-08-03T06:00:00+00:00": 200}
    fill = manager._build_fill_slots(manager._controller._pricing_mgr, NOW, HORIZON_END)
    assert requested == [HORIZON_END + timedelta(hours=24)]
    assert profile_calls == [(NOW, starts[2])]
    assert [slot.solar_kwh for slot in fill] == pytest.approx([.6, .2])
    assert [slot.consumption_kwh for slot in fill] == pytest.approx([.3, .3])
    assert all(slot.start.tzinfo for slot in fill)


def test_fill_window_starts_at_an_unaligned_sunrise(monkeypatch):
    import custom_components.omnibattery.control.high_price_discharge as runtime

    monkeypatch.setattr(runtime.dt_util, "as_local", lambda dt: dt.astimezone(ZoneInfo("Europe/Madrid")))
    sunrise = HORIZON_END + timedelta(minutes=30)
    slots = [PriceSlot(HORIZON_END + timedelta(hours=i), HORIZON_END + timedelta(hours=i + 1), .2)
             for i in range(3)]
    manager, _, _ = _manager(pricing=_pricing(
        get_future_export_price_slots=lambda horizon_end=None: slots))
    manager._solar_wh_hours = {"2026-08-03T05:00:00+00:00": 600,
                               "2026-08-03T06:00:00+00:00": 200}
    fill = manager._build_fill_slots(manager._controller._pricing_mgr, NOW, sunrise)
    assert fill[0].start == manager._aware(sunrise)
    assert [slot.solar_kwh for slot in fill] == pytest.approx([.3, .2])
    assert [slot.consumption_kwh for slot in fill] == pytest.approx([.15, .3])


@pytest.mark.asyncio
async def test_energy_platform_exception_keeps_fill_empty(monkeypatch):
    import custom_components.omnibattery.control.high_price_discharge as runtime

    manager, _, _ = _manager(solar_forecast_sensor="sensor.forecast")
    monkeypatch.setattr(runtime.er, "async_get", lambda hass: SimpleNamespace(
        async_get=lambda sensor: SimpleNamespace(config_entry_id="abc")))
    manager._hass.config_entries = SimpleNamespace(async_get_entry=lambda id: SimpleNamespace(domain="solar", entry_id=id))
    async def integration(*args):
        return SimpleNamespace(async_get_platform=fail)
    async def fail(*args):
        raise RuntimeError("platform broken")
    monkeypatch.setattr(runtime, "async_get_integration", integration)
    await manager._fetch_solar_forecast()
    assert manager._solar_wh_hours == {}
