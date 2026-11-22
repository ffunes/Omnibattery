"""A house with one commandable battery and one that regulates on its own.

Closed loop around the real ``_run_control_cycle``: real PD law, real
mixed-fleet guards, real capacity protection and, when the battery has a native
mode, the real mode manager. The plant is the meter:

    grid = house - solar + commanded battery + autonomous battery   (+ = import)

``House.run`` moves time in 2 s steps. ``house.nom`` decides what the autonomous
battery does in NOM: a number, or a function of what the meter would read
without it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController as Controller
from custom_components.omnibattery.control.autonomous_mode import (
    AutonomousBatteryModeManager,
)
from custom_components.omnibattery.drivers.base import NativeOperatingMode
from tests.test_autonomous_nom_mode import Battery, record_issues
from tests.test_control_sample_scheduling import _main_controller, _state

NOM = NativeOperatingMode.NOM
MANUAL = NativeOperatingMode.MANUAL
STEP_S = 2.0
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

_REAL = (
    "_compute_pd_new_power", "_apply_min_power", "_compute_no_pd_new_power",
    "_apply_capacity_protection", "_capacity_protection_coordinators",
    "compute_active_target_excluding", "remove_setpoint_override",
    "set_setpoint_override", "compute_active_target", "_measured_battery_power",
    "_signed_power_from_allocations",
)


def regulate(limit_w, target_w=0.0):
    """A NOM battery: takes the meter to its target, up to ``limit_w`` each way."""
    return lambda residual: max(-limit_w, min(limit_w, target_w - residual))


class House:
    """One commandable battery ("Marstek") and one autonomous ("NOM")."""

    def __init__(
        self, *, house=0.0, solar=0.0, nom=0.0, mode=NOM, requested="nom",
        has_mode=True, dc_coupled=False, max_w=2500.0, cap=0.0, with_nom=True,
        with_marstek=True,
    ):
        self.house, self.solar, self.nom = house, solar, nom
        # DC-coupled: what its own panels put in it never reaches the meter.
        self.dc_coupled = dc_coupled
        self.now = 0.0
        self.grid = 0.0
        self.holder = {"state": _state(0, T0)}
        c = self.controller = _main_controller(self.holder, [])
        self.marstek = c.coordinators[0]
        self.marstek.capabilities = SimpleNamespace(
            supports_power_setpoint=True, has_mppt_pv=False, actuator_latency_s=0.5,
            delivered_power_reliable=True,
        )
        self.marstek.data = {"battery_soc": 50, "ac_power": 0.0}
        self.marstek.battery_manual_mode_enabled = False
        self.marstek.rs485_user_disabled = False
        self.marstek.needs_software_manual_control = False
        self.battery = Battery(mode=mode, requested=requested, nom=has_mode)
        if with_nom:
            c.coordinators.append(self.battery)
        if not with_marstek:
            c.coordinators.remove(self.marstek)
        self.commands: list[float] = []     # what Marstek was told, + = charge
        self.history: list[tuple[float, float, float]] = []

        async def _set(coordinator, charge, discharge, **_kwargs):
            if coordinator is self.marstek:
                self.commands.append(charge - discharge)
                self.marstek.data["ac_power"] = discharge - charge
                self.marstek.commanded_charge_power = charge
                self.marstek.commanded_discharge_power = discharge
            return True

        async def _no_rebalance(**_kwargs):
            return False

        c._power_distribution._rebalance_expired_load_sharing_hold = _no_rebalance
        c.__dict__.update(
            previous_power=0.0, last_output_sign=0, kp=0.75, kd=0.0, ki=0, dt=STEP_S,
            derivative_tau=3.0, max_power_change_per_cycle=2500, direction_hysteresis=0,
            _should_log_rate_limiter=lambda _change: False,
            _clear_rate_limiter_state=lambda: None,
            _get_available_batteries=lambda is_charging, include_operation_blocks=True, **_k: (
                [self.marstek] if with_marstek else []
            ),
            _effective_system_capacity=lambda batteries, is_charging: (
                max_w if self.marstek in batteries else 0.0
            ),
            _battery_power_limit=lambda coordinator, is_charging: (
                max_w if coordinator is self.marstek else 0.0
            ),
            _set_battery_power=_set,
            _is_grid_at_min_soc_discharge_window=lambda: False,
            min_charge_power=0, min_discharge_power=0,
            oscillation_threshold=100, _fast_sensor_intervals=1000,
            _saturation_cycles=0, saturation_backcalc_threshold=100,
            _backcalc_is_saturated=lambda _is_charging: False,
            primary_feedforward_enabled=False, primary_battery="",
            _setpoint_overrides={}, _setpoint_offsets={},
            capacity_protection_enabled=False, capacity_protection_soc_threshold=30,
            capacity_protection_excluded_devices=False, max_contracted_power=cap,
            _capacity_protection_status={}, _capacity_protection_active=False,
            _excluded_included_adjustment=0.0,
            _global_discharge_blockers={}, _battery_discharge_blockers={},
            _coordinator_delivered_power=Controller._coordinator_delivered_power,
        )
        for name in _REAL:
            setattr(c, name, getattr(Controller, name).__get__(c, Controller))
        self.manager = c._autonomous_mode_mgr = AutonomousBatteryModeManager(
            c.hass, c.config_entry, c
        )
        self.manager._clock = lambda: self.now
        self.issues = record_issues(self.manager)

    def start_grid_charge(self, ceiling_w=3000.0):
        """Open a predictive slot: the real handler, called as every mode calls it."""
        c = self.controller
        c.max_contracted_power = ceiling_w
        c.predictive_charging_enabled = True
        c.predictive_charging_mode = "time_slot"
        c.grid_charging_active = True
        c.__dict__.update(
            _grid_charging_initialized=False, _predictive_charge_target_soc={},
            _compute_predictive_target_soc=lambda **_k: {},
            _release_grid_import_cap=lambda: None, _cap_overrides_policy=False,
        )
        for name in (
            "_handle_predictive_grid_charging", "_predictive_charge_ceiling",
            "_predictive_min_charge_power", "_predictive_hard_limit_confirmed",
            "_predictive_demand_settle_window_s", "_discharge_held_by_policy",
        ):
            setattr(c, name, getattr(Controller, name).__get__(c, Controller))

        async def _slot():
            if c.grid_charging_active:
                await c._handle_predictive_grid_charging()

        c._handle_time_slot_predictive_charging = _slot

    def stop_grid_charge(self):
        c = self.controller
        c.grid_charging_active = False
        c._grid_charging_initialized = False
        c.first_execution = False
        c.previous_power = self.command

    @property
    def command(self) -> float:
        """What Marstek is doing, + = charge."""
        return -self.marstek.data["ac_power"]

    def _nom_power(self, residual) -> float:
        if self.battery.driver.mode is not NOM:
            return 0.0
        return float(self.nom(residual) if callable(self.nom) else self.nom)

    async def _step(self):
        self.now += STEP_S
        residual = self.house - self.solar + self.command
        power = self._nom_power(residual)
        self.grid = residual + (0.0 if self.dc_coupled else power)
        if self.battery.is_available:
            self.battery.data["battery_power"] = power
        self.history.append((self.grid, self.command, power))
        at = T0 + timedelta(seconds=self.now)
        # The last decimal makes every sample a new one, as a real meter's noise.
        self.holder["state"] = _state(self.grid + (self.now / STEP_S % 2) * 0.1, at)
        await self.controller._run_control_cycle(now=at)
        for state in self.manager._states.values():
            if state.task is not None:
                await state.task

    def run(self, seconds):
        """Advance the plant and the controller."""
        async def _run():
            for _ in range(int(seconds / STEP_S)):
                await self._step()

        asyncio.run(_run())
        return self
