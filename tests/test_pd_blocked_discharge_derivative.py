"""Regression: no-discharge-available cycles froze ``previous_error``.

With every battery blocked for discharge (min SOC / allow-discharge off) the
"No available batteries" early return skipped the end-of-cycle PD state update.
``previous_error`` stayed at the last full cycle's value, so each blocked cycle
fed the derivative filter the same stale step: after a 1300 W import pulse fell
back to ~60 W, D wound up to kd * -1240 W and drove a CHARGE command from the
grid while the house was still importing (live 2026-10-06, Zendure pulsing
400-1275 W every ~20 s).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from custom_components.omnibattery import ChargeDischargeController

from tests.test_control_sample_scheduling import _main_controller, _state


def test_blocked_discharge_does_not_wind_derivative_into_grid_charge():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(1300, t0)}
    controller = _main_controller(state_holder, [])
    battery = controller.coordinators[0]
    commands = []

    async def _record(_coordinator, charge, discharge):
        commands.append(charge)

    controller.__dict__.update(
        previous_power=0.0,
        last_output_sign=0,
        kp=0.75,
        kd=0.45,
        ki=0,
        dt=1.0,
        derivative_tau=3.0,
        max_power_change_per_cycle=2000,
        direction_hysteresis=200,
        _measured_battery_power=lambda: None,
        _should_log_rate_limiter=lambda _change: False,
        _clear_rate_limiter_state=lambda: None,
        # Discharge blocked on every battery, charge still allowed.
        _get_available_batteries=lambda is_charging, include_operation_blocks=True: (
            [battery] if is_charging else []
        ),
        _set_battery_power=_record,
        _is_grid_at_min_soc_discharge_window=lambda: False,
        min_charge_power=400,
        min_discharge_power=0,
    )
    for name in ("_compute_pd_new_power", "_apply_min_power"):
        setattr(controller, name, getattr(ChargeDischargeController, name).__get__(
            controller, ChargeDischargeController
        ))

    # The battery's own charge pulse shows as a 1300 W import (the last full
    # cycle saw it), the PD asks to discharge and is blocked; then the pulse
    # ends and the house settles at a 60 W import.
    controller.previous_error = 1250.0
    for i in range(1, 12):
        ts = t0 + timedelta(seconds=i)
        state_holder["state"] = _state(1300 if i == 1 else 60 + i % 2, ts)
        asyncio.run(controller._run_control_cycle(now=ts))

    assert max(commands) == 0, f"charged from the grid while importing: {commands}"
