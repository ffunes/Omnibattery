"""#958: Net Balance keeps what the plan buys or sells on purpose.

Reporter's hour: a planned grid charge imported ~0.5 kWh from 14:00 to 14:13;
the balance then asked for ~650 W of export for the rest of the hour, which
stopped the solar charge and undid the purchase. The deliberate energy now
moves the hour's target, so the hour closes at +0.5 kWh instead of 0.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from custom_components.omnibattery.control.high_price_discharge import OVERRIDE_SOURCE as SALE
from custom_components.omnibattery.tracking import hourly_balance as hb
from custom_components.omnibattery.tracking.hourly_balance import HourlyBalanceManager

T0 = datetime(2026, 11, 26, 14, 0, 0)


class _Clock:
    def __init__(self):
        self.t = T0

    def now(self):
        return self.t

    def mono(self):
        return (self.t - T0).total_seconds() + 1000.0


def _setup(ext_mode=None):
    clock = _Clock()
    grid = {"w": 0.0, "ext": 0.0}
    offsets: dict[str, float] = {}

    def _state(entity_id):
        value = grid["ext"] if entity_id == "sensor.net" else grid["w"]
        return SimpleNamespace(state=str(value))

    hass = SimpleNamespace(states=SimpleNamespace(get=_state))
    controller = SimpleNamespace(
        hourly_balance_enabled=True,
        manual_mode_enabled=False,
        consumption_sensor="sensor.grid",
        _apply_meter_transform=lambda st: float(st.state),
        set_setpoint_offset=lambda src, w: offsets.__setitem__(src, w),
        remove_setpoint_offset=lambda src: offsets.pop(src, None),
        get_charge_blockers=lambda: {},
        coordinators=[],
        _setpoint_overrides={},
    )
    entry = SimpleNamespace(data={}, entry_id="e")
    async def _save(_data):
        return None

    store = SimpleNamespace(async_save=_save)
    with patch.object(hb, "Store", lambda *a, **k: store):
        mgr = HourlyBalanceManager(hass, entry, controller)
    mgr._ext_detected = True
    if ext_mode:
        mgr._ext_sensor, mgr._ext_mode = "sensor.net", ext_mode
    return mgr, clock, grid, offsets, controller


def _run(mgr, clock, grid, minutes, import_w, **kw):
    """Advance ``minutes`` in 30 s cycles with the grid at ``import_w``."""

    async def go():
        for _ in range(int(minutes * 2)):
            clock.t += timedelta(seconds=30)
            grid["w"] = import_w
            grid["ext"] -= import_w / 120.0 / 1000.0  # kWh, export-positive
            await mgr.async_process(**kw)

    with patch.object(hb.dt_util, "now", clock.now), patch.object(hb, "monotonic", clock.mono):
        asyncio.run(go())


def _first_cycle(mgr, clock):
    with patch.object(hb.dt_util, "now", clock.now), patch.object(hb, "monotonic", clock.mono):
        asyncio.run(mgr.async_process())


def test_planned_grid_charge_is_not_exported_back():
    mgr, clock, grid, offsets, _ = _setup()
    _first_cycle(mgr, clock)
    _run(mgr, clock, grid, 13, 2400.0, grid_charge=True)
    assert mgr._deliberate_wh > 500
    _run(mgr, clock, grid, 2, 0.0)
    assert abs(offsets["hourly_balance"]) < 20


def test_unplanned_import_is_still_compensated():
    mgr, clock, grid, offsets, _ = _setup()
    _first_cycle(mgr, clock)
    _run(mgr, clock, grid, 13, 2400.0)
    _run(mgr, clock, grid, 2, 0.0)
    assert mgr._deliberate_wh == 0.0
    assert offsets["hourly_balance"] < -600


def test_external_energy_sensor_path():
    mgr, clock, grid, offsets, _ = _setup(ext_mode="direct_kwh")
    _first_cycle(mgr, clock)
    _run(mgr, clock, grid, 13, 2400.0, grid_charge=True)
    _run(mgr, clock, grid, 2, 0.0)
    assert mgr._deliberate_wh > 500
    assert abs(offsets["hourly_balance"]) < 20


def test_a_sale_is_not_bought_back():
    mgr, clock, grid, offsets, controller = _setup()
    _first_cycle(mgr, clock)
    controller._setpoint_overrides = {SALE: (7, -2000.0)}
    _run(mgr, clock, grid, 13, -2000.0)
    controller._setpoint_overrides = {}
    _run(mgr, clock, grid, 2, 0.0)
    assert mgr._deliberate_wh < -400
    assert abs(offsets["hourly_balance"]) < 20


def test_deliberate_energy_resets_with_the_hour():
    mgr, clock, grid, _offsets, _ = _setup()
    _first_cycle(mgr, clock)
    _run(mgr, clock, grid, 13, 2400.0, grid_charge=True)
    clock.t = T0 + timedelta(hours=1)
    _run(mgr, clock, grid, 1, 0.0)
    assert mgr._deliberate_wh == 0.0


def test_the_purchase_survives_a_restart_mid_hour():
    mgr, clock, grid, _offsets, _ = _setup()
    saved = {}

    async def _save(data):
        saved.update(data)

    async def _load():
        return dict(saved)

    mgr._store = SimpleNamespace(async_save=_save, async_load=_load)
    _first_cycle(mgr, clock)
    _run(mgr, clock, grid, 13, 2400.0, grid_charge=True)  # saves during the charge
    assert saved["deliberate_wh"] > 0

    again, _c, _g, _o, _ = _setup()
    again._store = mgr._store

    async def _no_detect():
        return None

    again._detect_external_sensor = _no_detect
    with patch.object(hb.dt_util, "now", clock.now):
        asyncio.run(again.async_setup())
    assert again._deliberate_wh == saved["deliberate_wh"]


def _control_cycle(waiting: bool):
    """One real control cycle with a dynamic-pricing grid charge active."""
    from datetime import timezone

    from tests.test_control_sample_scheduling import _main_controller, _state

    calls = []

    class _Balance:
        async def async_process(self, grid_charge=False):
            calls.append(grid_charge)

    async def _slot():
        controller.grid_charging_active = True
        controller._grid_charge_waiting = waiting  # set by the charge, per cycle

    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    controller = _main_controller({"state": _state(6000, first)}, [])
    controller.predictive_charging_enabled = True
    controller.predictive_charging_mode = PREDICTIVE_MODE_DYNAMIC_PRICING
    controller._handle_dynamic_pricing_predictive_charging = _slot
    controller._hourly_balance_mgr = _Balance()
    asyncio.run(controller._run_control_cycle(now=first + timedelta(seconds=1)))
    return calls


def test_the_control_cycle_measures_the_hour_during_a_grid_charge():
    assert _control_cycle(waiting=False) == [True]
    assert _control_cycle(waiting=True) == [False]
