"""Characterization tests for the excluded-device load logic.

These pin the *current* behavior of the two excluded-device methods on
ExternalLoads so the extraction can be proven to change nothing.

No hardware and no real Home Assistant: ExternalLoads is built with a stub
controller (SimpleNamespace) and a fake hass whose ``states.get`` returns
lightweight state stand-ins. Both methods only read ``config_entry.data`` and
``hass.states``, plus ``previous_power`` for the charge/discharge branch.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.infra.external_loads import ExternalLoads


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------

class _FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


def _state(value, unit="W", **extra_attributes):
    """A minimal stand-in for a Home Assistant State object."""
    attributes = {"unit_of_measurement": unit}
    attributes.update(extra_attributes)
    return SimpleNamespace(state=str(value), attributes=attributes)


def _controller(excluded_devices, states=None, previous_power=0.0,
                solar_production_sensor=None, home_consumption_sensor=None):
    controller_stub = SimpleNamespace(
        previous_power=previous_power,
        _excluded_included_adjustment=None,
        _solar_surplus_discharge_blocked=False,
        solar_production_sensor=solar_production_sensor,
        home_consumption_sensor=home_consumption_sensor,
        _ev_charging_states={},
        _ev_pause_until={},
    )
    config_entry = SimpleNamespace(data={"excluded_devices": excluded_devices})
    hass = SimpleNamespace(states=_FakeStates(states or {}))
    return ExternalLoads(hass, config_entry, controller_stub)


def _device(**overrides):
    """A telemetry excluded device with sensible defaults; override per test."""
    base = {
        "enabled": True,
        "ev_charger_no_telemetry": False,
        "power_sensor": "sensor.dev",
        "included_in_consumption": True,
        "allow_solar_surplus": False,
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------
# consumption_delta_kw  (returns kW; W->kW converted)
# ----------------------------------------------------------------------

def test_delta_kw_no_devices_is_zero():
    assert _controller([]).consumption_delta_kw() == 0.0


def test_delta_kw_included_device_is_subtracted():
    loads = _controller([_device(included_in_consumption=True)],
                       {"sensor.dev": _state(500)})
    assert loads.consumption_delta_kw() == pytest.approx(-0.5)


def test_delta_kw_not_included_device_is_added():
    loads = _controller([_device(included_in_consumption=False)],
                       {"sensor.dev": _state(500)})
    assert loads.consumption_delta_kw() == pytest.approx(0.5)


def test_delta_kw_kilowatt_unit_not_reconverted():
    loads = _controller([_device(included_in_consumption=True)],
                       {"sensor.dev": _state(0.5, unit="kW")})
    assert loads.consumption_delta_kw() == pytest.approx(-0.5)


def test_delta_kw_two_devices_net():
    devices = [
        _device(power_sensor="sensor.a", included_in_consumption=True),
        _device(power_sensor="sensor.b", included_in_consumption=False),
    ]
    loads = _controller(devices,
                       {"sensor.a": _state(500), "sensor.b": _state(200)})
    # -0.5 (subtracted) + 0.2 (added)
    assert loads.consumption_delta_kw() == pytest.approx(-0.3)


@pytest.mark.parametrize("device, states", [
    (_device(enabled=False), {"sensor.dev": _state(500)}),
    (_device(ev_charger_no_telemetry=True), {"sensor.dev": _state(500)}),
    (_device(power_sensor=None), {}),
    (_device(), {"sensor.dev": _state("unavailable")}),
    (_device(), {}),  # sensor missing entirely
])
def test_delta_kw_skips_unusable_devices(device, states):
    assert _controller([device], states).consumption_delta_kw() == 0.0


# ----------------------------------------------------------------------
# calculate_adjustment  (returns W; kW unit handled)
# Positive = reduce battery discharge, negative = increase it.
# ----------------------------------------------------------------------

def test_adjustment_no_devices_is_zero_and_resets_included():
    loads = _controller([])
    assert loads.calculate_adjustment() == 0.0
    assert loads._controller._excluded_included_adjustment == 0.0


def test_adjustment_included_no_surplus_subtracts():
    loads = _controller([_device(included_in_consumption=True, allow_solar_surplus=False)],
                       {"sensor.dev": _state(500)})
    assert loads.calculate_adjustment() == pytest.approx(500.0)
    assert loads._controller._excluded_included_adjustment == pytest.approx(500.0)


def test_adjustment_not_included_adds_discharge():
    loads = _controller([_device(included_in_consumption=False)],
                       {"sensor.dev": _state(500)})
    assert loads.calculate_adjustment() == pytest.approx(-500.0)
    assert loads._controller._excluded_included_adjustment == pytest.approx(0.0)


def test_adjustment_solar_surplus_no_solar_sensor_always_zero_no_discharge_blocked_when_idle():
    # No solar sensor configured + device idle → no adjustment, no discharge block
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(0)},
    )
    loads.calculate_adjustment()
    assert loads._controller._solar_surplus_discharge_blocked is False


def test_adjustment_solar_surplus_no_solar_sensor_sets_discharge_blocked_when_active():
    # No solar sensor configured + device active → no adjustment, discharge blocked
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(500)},
    )
    assert loads.calculate_adjustment() == pytest.approx(0.0)
    assert loads._controller._solar_surplus_discharge_blocked is True


def test_adjustment_solar_surplus_no_solar_sensor_kw_sensor_converted():
    # No solar sensor; device sensor in kW → 1500 W active → discharge blocked
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(1.5, unit="kW")},
    )
    assert loads.calculate_adjustment() == pytest.approx(0.0)
    assert loads._controller._solar_surplus_discharge_blocked is True


# PV-surplus priority (#421/#415): surplus = max(0, solar − home_base_load),
# home_base_load = home_consumption − excluded devices. For a single device,
# base_load = home − device; the device is offset by the surplus first, the
# battery only excludes the grid portion it must import.

def _surplus_loads(device_w, solar_w, home_w, **device_overrides):
    return _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True, **device_overrides)],
        {"sensor.dev": _state(device_w), "sensor.solar": _state(solar_w),
         "sensor.home": _state(home_w)},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )


def test_adjustment_pv_priority_partial_surplus_offsets_device():
    # Discussion #421 numbers: EV=3419 W, solar=2000 W, home=4438 W (base 1019).
    # surplus = 2000 − 1019 = 981 → exclude 3419 − 981 = 2438 W; battery idle.
    loads = _surplus_loads(3419, 2000, 4438)
    assert loads.calculate_adjustment() == pytest.approx(2438.0)
    assert loads._controller._excluded_included_adjustment == pytest.approx(2438.0)
    assert loads._controller._solar_surplus_discharge_blocked is False


def test_adjustment_pv_priority_surplus_covers_device_battery_charges_rest():
    # device=1000, solar=3000, home=2000 (base 1000) → surplus=2000 ≥ device.
    # Device fully offset → adjustment 0; PD sees export → battery charges leftover.
    loads = _surplus_loads(1000, 3000, 2000)
    assert loads.calculate_adjustment() == pytest.approx(0.0)
    assert loads._controller._solar_surplus_discharge_blocked is False


def test_adjustment_pv_priority_no_surplus_full_exclusion():
    # solar ≤ base load → no surplus for the device → full exclusion.
    # device=1500, solar=800, home=2300 (base 800) → surplus=0 → exclude 1500.
    loads = _surplus_loads(1500, 800, 2300)
    assert loads.calculate_adjustment() == pytest.approx(1500.0)


def test_adjustment_pv_priority_home_consumption_unavailable_full_exclusion():
    # Home Consumption sensor down → conservative full exclusion (battery never
    # powers the device), regardless of solar.
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(1000), "sensor.solar": _state(3000),
         "sensor.home": _state("unavailable")},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )
    assert loads.calculate_adjustment() == pytest.approx(1000.0)
    assert loads._controller._solar_surplus_discharge_blocked is False


def test_adjustment_pv_priority_held_home_consumption_uses_conservative_fallback():
    # The display sensor may retain its last value, but load control must not
    # use it while the balance is marked held_last_valid.
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(1000), "sensor.solar": _state(3000),
         "sensor.home": _state(2000, balance_quality="held_last_valid")},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )
    assert loads.calculate_adjustment() == pytest.approx(1000.0)


def test_adjustment_pv_priority_kw_units():
    # All sensors in kW: device=4.5, solar=2.0, home=5.0 (base 0.5 kW).
    # surplus = 2.0 − 0.5 = 1.5 kW → exclude 4.5 − 1.5 = 3.0 kW = 3000 W.
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(4.5, unit="kW"), "sensor.solar": _state(2.0, unit="kW"),
         "sensor.home": _state(5.0, unit="kW")},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )
    assert loads.calculate_adjustment() == pytest.approx(3000.0)


def test_adjustment_pv_priority_shared_budget_across_two_devices():
    # Two surplus devices share one budget: base = home − (devA+devB).
    # home=4000, devA=1500, devB=1000 → base=1500; solar=2500 → surplus=1000.
    # Loop order: A takes min(1500,1000)=1000 → exclude 500; B sees 0 left → exclude 1000.
    devices = [
        _device(power_sensor="sensor.a", allow_solar_surplus=True),
        _device(power_sensor="sensor.b", allow_solar_surplus=True),
    ]
    loads = _controller(
        devices,
        {"sensor.a": _state(1500), "sensor.b": _state(1000),
         "sensor.solar": _state(2500), "sensor.home": _state(4000)},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )
    assert loads.calculate_adjustment() == pytest.approx(1500.0)  # 500 + 1000


# cover_home_when_active (#42): opt-in pre-#415 rule. The device is offset by raw
# PV, so only its real grid draw max(0, device − solar) is excluded and the
# battery covers the remaining home deficit instead of sitting idle.

def test_adjustment_cover_home_battery_covers_home_deficit():
    # Live #42 numbers: AC=2411, solar=1415, home=3000 (base ~589).
    # cover_home ON → exclude max(0, 2411−1415)=996 (not the #421 value 1500),
    # so sensor_actual leaves base_load ~589 for the battery to discharge.
    loads = _surplus_loads(2411, 1415, 3000, cover_home_when_active=True)
    assert loads.calculate_adjustment() == pytest.approx(996.0)


def test_adjustment_cover_home_device_fully_solar_covered_excludes_nothing():
    # solar ≥ device → device imports nothing → exclusion 0 → battery covers all
    # of the home deficit. (The #421 default would exclude a positive grid_portion.)
    loads = _surplus_loads(2000, 2600, 2976, cover_home_when_active=True)
    assert loads.calculate_adjustment() == pytest.approx(0.0)


def test_adjustment_cover_home_off_is_unchanged_421_behavior():
    # Same numbers as the #421 pinned test but with the flag explicitly OFF:
    # default home-first rule still excludes 2438 (battery idle). No regression.
    loads = _surplus_loads(3419, 2000, 4438, cover_home_when_active=False)
    assert loads.calculate_adjustment() == pytest.approx(2438.0)


def test_adjustment_cover_home_no_home_sensor_still_uses_raw_pv():
    # Home Consumption down: #421 falls back to full exclusion, but cover_home
    # only needs solar → still excludes just max(0, device − solar).
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True,
                 cover_home_when_active=True)],
        {"sensor.dev": _state(2411), "sensor.solar": _state(1415),
         "sensor.home": _state("unavailable")},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )
    assert loads.calculate_adjustment() == pytest.approx(996.0)


def test_adjustment_included_no_surplus_kw_sensor_converted_correctly():
    # 1.5 kW sensor → adjustment should be 1500 W
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=False)],
        {"sensor.dev": _state(1.5, unit="kW")},
    )
    assert loads.calculate_adjustment() == pytest.approx(1500.0)


@pytest.mark.parametrize("device", [
    _device(enabled=False),
    _device(ev_charger_no_telemetry=True),
    _device(power_sensor=None),
])
def test_adjustment_skips_unusable_devices(device):
    loads = _controller([device], {"sensor.dev": _state(500)})
    assert loads.calculate_adjustment() == 0.0


# ----------------------------------------------------------------------
# exclusion_pct slider: scales the excluded portion (100 = full, default).
# ----------------------------------------------------------------------

def test_exclusion_pct_partial_scales_adjustment():
    # 60% excluded → battery covers 40% → only 60% of 500 W is excluded.
    loads = _controller(
        [_device(included_in_consumption=True, exclusion_pct=60)],
        {"sensor.dev": _state(500)},
    )
    assert loads.calculate_adjustment() == pytest.approx(300.0)
    assert loads._controller._excluded_included_adjustment == pytest.approx(300.0)


def test_exclusion_pct_partial_scales_delta_kw():
    loads = _controller(
        [_device(included_in_consumption=True, exclusion_pct=60)],
        {"sensor.dev": _state(500)},
    )
    assert loads.consumption_delta_kw() == pytest.approx(-0.3)


def test_exclusion_pct_zero_means_no_exclusion():
    loads = _controller(
        [_device(included_in_consumption=True, exclusion_pct=0)],
        {"sensor.dev": _state(500)},
    )
    assert loads.calculate_adjustment() == pytest.approx(0.0)


def test_exclusion_pct_scales_solar_surplus_grid_portion():
    # device=4500, solar=2000, home=5000 (base 500) → surplus 1500, grid portion
    # 4500 − 1500 = 3000 W, scaled by 50% → 1500 W.
    loads = _surplus_loads(4500, 2000, 5000, exclusion_pct=50)
    assert loads.calculate_adjustment() == pytest.approx(1500.0)


def test_exclusion_pct_default_is_full_exclusion():
    # No exclusion_pct key → factor 1.0 (unchanged from original behaviour).
    loads = _controller(
        [_device(included_in_consumption=True)],
        {"sensor.dev": _state(500)},
    )
    assert loads.calculate_adjustment() == pytest.approx(500.0)


# ----------------------------------------------------------------------
# dynamic power control: one-sensor activity detection + yield/hold state
# ----------------------------------------------------------------------

def _dynamic_device(**overrides):
    values = {
        "allow_solar_surplus": True,
        "dynamic_power_control": True,
    }
    values.update(overrides)
    return _device(**values)


def _expire_dynamic_yield(loads):
    """Move the initial yield into the past without depending on wall-clock time."""
    loads._dynamic_yield_until["0:sensor.dev"] -= timedelta(days=1)


@pytest.mark.parametrize(
    "activity_state",
    [
        "on",
        "true",
        "1",
        "Charging",
        "Chargement",
        "Cargando",
        "Carregant",
        "Carregando",
        "Laden",
        "Ladend",
        "Caricando",
        "In carica",
        "Laddar",
        "Laddning",
        "Lading",
        "Oplading",
    ],
)
def test_activity_languages_are_shared_by_dynamic_and_no_telemetry(activity_state):
    """Both excluded-load modes must use the same multilingual detector."""
    state = _state(activity_state)
    dynamic_loads = _controller(
        [_dynamic_device(activity_sensor="sensor.ev_state")],
        {"sensor.dev": _state(0), "sensor.ev_state": state},
    )
    no_telemetry_loads = _controller(
        [_ev_device(activity_sensor="sensor.ev_state")],
        {"sensor.ev_state": state},
    )

    assert dynamic_loads.refresh_dynamic_power_control()["charge_blocked"] is True
    assert no_telemetry_loads.check_ev_charger_state() == (True, False)


def test_dynamic_control_first_load_starts_initial_yield():
    loads = _controller(
        [_dynamic_device()],
        {"sensor.dev": _state(3600), "sensor.solar": _state(5000)},
        solar_production_sensor="sensor.solar",
    )

    status = loads.refresh_dynamic_power_control()

    assert status["active"] is True
    assert status["charge_blocked"] is True
    assert status["devices"] == ["sensor.dev"]
    assert status["phases"] == {"sensor.dev": "yielding"}
    assert 0 < status["yield_remaining_s"] <= 30


@pytest.mark.parametrize("activity_state", ["on", "Charging", "Cargando"])
def test_dynamic_control_activity_sensor_blocks_before_power(activity_state):
    loads = _controller(
        [_dynamic_device(activity_sensor="binary_sensor.ev_active")],
        {
            "sensor.dev": _state(0),
            "binary_sensor.ev_active": _state(activity_state),
        },
    )

    status = loads.refresh_dynamic_power_control()

    assert status["active"] is True
    assert status["charge_blocked"] is True
    assert status["phases"] == {"sensor.dev": "waiting_for_load"}


def test_dynamic_control_activity_sensor_yields_when_power_appears():
    states = {
        "sensor.dev": _state(0),
        "binary_sensor.ev_active": _state("on"),
    }
    loads = _controller(
        [_dynamic_device(activity_sensor="binary_sensor.ev_active")],
        states,
    )
    loads.refresh_dynamic_power_control()

    states["sensor.dev"] = _state(1500)
    status = loads.refresh_dynamic_power_control()

    assert status["charge_blocked"] is True
    assert status["phases"] == {"sensor.dev": "yielding"}
    assert 0 < status["yield_remaining_s"] <= 30


def test_dynamic_control_inactive_activity_sensor_releases_immediately():
    states = {
        "sensor.dev": _state(0),
        "binary_sensor.ev_active": _state("on"),
    }
    loads = _controller(
        [_dynamic_device(activity_sensor="binary_sensor.ev_active")],
        states,
    )
    assert loads.refresh_dynamic_power_control()["charge_blocked"] is True

    states["binary_sensor.ev_active"] = _state("off")
    status = loads.refresh_dynamic_power_control()

    assert status["active"] is False
    assert status["charge_blocked"] is False


@pytest.mark.parametrize("overrides", [
    {"dynamic_power_control": False},
    {"allow_solar_surplus": False},
    {"included_in_consumption": False},
    {"enabled": False},
    {"ev_charger_no_telemetry": True},
])
def test_dynamic_control_requires_all_prerequisites(overrides):
    device = _dynamic_device()
    device.update(overrides)
    loads = _controller([device], {"sensor.dev": _state(3600)})

    assert loads.refresh_dynamic_power_control()["active"] is False


def test_dynamic_control_allows_residual_after_initial_yield():
    loads = _controller(
        [_dynamic_device()],
        {"sensor.dev": _state(3600), "sensor.solar": _state(5000)},
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()
    loads._dynamic_yield_until["0:sensor.dev"] = dt_util.utcnow() - timedelta(seconds=1)

    status = loads.refresh_dynamic_power_control()

    assert status["active"] is True
    assert status["charge_blocked"] is False
    assert status["discharge_blocked"] is True
    assert status["discharge_blocked_devices"] == ["sensor.dev"]
    assert status["phases"] == {"sensor.dev": "monitoring_residual"}


def test_dynamic_control_cover_home_allows_residual_discharge():
    loads = _controller(
        [_dynamic_device(cover_home_when_active=True)],
        {"sensor.dev": _state(3600), "sensor.solar": _state(5000)},
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()
    loads._dynamic_yield_until["0:sensor.dev"] = dt_util.utcnow() - timedelta(seconds=1)

    status = loads.refresh_dynamic_power_control()

    assert status["charge_blocked"] is False
    assert status["discharge_blocked"] is False
    assert status["phases"] == {"sensor.dev": "monitoring_residual"}


def test_dynamic_control_solar_rise_starts_new_yield():
    states = {
        "sensor.dev": _state(3600),
        "sensor.solar": _state(5000),
    }
    loads = _controller(
        [_dynamic_device()],
        states,
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()
    _expire_dynamic_yield(loads)
    assert loads.refresh_dynamic_power_control()["charge_blocked"] is False

    states["sensor.solar"] = _state(5250)
    status = loads.refresh_dynamic_power_control()

    assert status["charge_blocked"] is True
    assert status["phases"] == {"sensor.dev": "yielding"}
    assert 0 < status["yield_remaining_s"] <= 20


def test_dynamic_control_re_yields_when_wallbox_power_drops_with_stable_solar():
    states = {
        "sensor.dev": _state(3600),
        "sensor.solar": _state(5000),
    }
    loads = _controller(
        [_dynamic_device()],
        states,
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()
    _expire_dynamic_yield(loads)
    assert loads.refresh_dynamic_power_control()["charge_blocked"] is False

    states["sensor.dev"] = _state(3300)
    status = loads.refresh_dynamic_power_control()

    assert status["charge_blocked"] is True
    assert status["phases"] == {"sensor.dev": "yielding"}
    assert 0 < status["yield_remaining_s"] <= 20


def test_dynamic_control_does_not_reyield_when_solar_and_wallbox_drop_together():
    states = {
        "sensor.dev": _state(3600),
        "sensor.solar": _state(5000),
    }
    loads = _controller(
        [_dynamic_device()],
        states,
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()
    _expire_dynamic_yield(loads)
    assert loads.refresh_dynamic_power_control()["charge_blocked"] is False

    states["sensor.dev"] = _state(3300)
    states["sensor.solar"] = _state(4700)
    status = loads.refresh_dynamic_power_control()

    assert status["charge_blocked"] is False
    assert status["phases"] == {"sensor.dev": "monitoring_residual"}


def test_dynamic_control_does_not_reyield_for_small_margin_rise():
    states = {
        "sensor.dev": _state(3600),
        "sensor.solar": _state(5000),
    }
    loads = _controller(
        [_dynamic_device()],
        states,
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()
    loads._dynamic_yield_until["0:sensor.dev"] = dt_util.utcnow() - timedelta(seconds=1)
    assert loads.refresh_dynamic_power_control()["charge_blocked"] is False

    states["sensor.dev"] = _state(3450)
    status = loads.refresh_dynamic_power_control()

    assert status["charge_blocked"] is False
    assert status["phases"] == {"sensor.dev": "monitoring_residual"}


def test_dynamic_control_zero_power_is_held_for_restart():
    states = {"sensor.dev": _state(3600), "sensor.solar": _state(5000)}
    loads = _controller(
        [_dynamic_device()],
        states,
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()

    states["sensor.dev"] = _state(0)
    status = loads.refresh_dynamic_power_control()

    assert status["active"] is True
    assert status["charge_blocked"] is True
    assert status["discharge_blocked"] is True
    assert status["discharge_blocked_devices"] == ["sensor.dev"]
    assert status["phases"] == {"sensor.dev": "restart_hold"}
    assert 0 < status["hold_remaining_s"] <= 300
    assert 0 < status["yield_remaining_s"] <= 30


def test_dynamic_control_restart_hold_allows_residual_charge_after_grace():
    states = {"sensor.dev": _state(3600), "sensor.solar": _state(5000)}
    loads = _controller(
        [_dynamic_device()],
        states,
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()

    states["sensor.dev"] = _state(0)
    loads.refresh_dynamic_power_control()
    loads._dynamic_yield_until["0:sensor.dev"] = dt_util.utcnow() - timedelta(seconds=1)

    status = loads.refresh_dynamic_power_control()

    assert status["active"] is True
    assert status["charge_blocked"] is False
    assert status["discharge_blocked"] is True
    assert status["discharge_blocked_devices"] == ["sensor.dev"]
    assert status["phases"] == {"sensor.dev": "restart_hold"}
    assert 0 < status["hold_remaining_s"] <= 300


def test_dynamic_control_restart_hold_re_yields_when_solar_rises():
    states = {"sensor.dev": _state(3600), "sensor.solar": _state(5000)}
    loads = _controller(
        [_dynamic_device()],
        states,
        solar_production_sensor="sensor.solar",
    )
    loads.refresh_dynamic_power_control()

    states["sensor.dev"] = _state(0)
    loads.refresh_dynamic_power_control()
    loads._dynamic_yield_until["0:sensor.dev"] = dt_util.utcnow() - timedelta(seconds=1)
    assert loads.refresh_dynamic_power_control()["charge_blocked"] is False

    states["sensor.solar"] = _state(5250)
    status = loads.refresh_dynamic_power_control()

    assert status["charge_blocked"] is True
    assert status["discharge_blocked"] is True
    assert status["phases"] == {"sensor.dev": "restart_hold"}
    assert 0 < status["yield_remaining_s"] <= 20


def test_dynamic_control_restart_hold_expires_to_normal_operation():
    states = {"sensor.dev": _state(3600)}
    loads = _controller([_dynamic_device()], states)
    loads.refresh_dynamic_power_control()
    states["sensor.dev"] = _state(0)
    loads._dynamic_hold_until["0:sensor.dev"] = dt_util.utcnow() - timedelta(seconds=1)

    status = loads.refresh_dynamic_power_control()

    assert status["active"] is False
    assert status["charge_blocked"] is False


def test_dynamic_control_without_solar_uses_periodic_probe():
    loads = _controller([_dynamic_device()], {"sensor.dev": _state(3600)})
    loads.refresh_dynamic_power_control()
    key = "0:sensor.dev"
    loads._dynamic_yield_until[key] = dt_util.utcnow() - timedelta(seconds=1)
    loads._dynamic_next_probe[key] = dt_util.utcnow() - timedelta(seconds=1)

    status = loads.refresh_dynamic_power_control()

    assert status["charge_blocked"] is True
    assert status["phases"] == {"sensor.dev": "yielding"}


def test_controller_registers_dynamic_control_operation_blocks():
    status = {
        "active": True,
        "charge_blocked": True,
        "discharge_blocked": True,
        "devices": ["sensor.dev"],
        "blocked_devices": ["sensor.dev"],
        "discharge_blocked_devices": ["sensor.dev"],
        "phases": {"sensor.dev": "yielding"},
        "hold_remaining_s": 0,
        "yield_remaining_s": 25,
    }
    calls = []
    controller = SimpleNamespace(
        _external_loads=SimpleNamespace(refresh_dynamic_power_control=lambda: status),
        set_charge_block=lambda *args, **kwargs: calls.append(("set", args, kwargs)),
        remove_charge_block=lambda *args, **kwargs: calls.append(("remove", args, kwargs)),
        set_discharge_block=lambda *args, **kwargs: calls.append(("set_discharge", args, kwargs)),
        remove_discharge_block=lambda *args, **kwargs: calls.append(("remove_discharge", args, kwargs)),
    )

    ChargeDischargeController._refresh_dynamic_power_control_block(controller)

    assert calls[0][0] == "set"
    assert calls[0][1][:2] == (
        "excluded_device_dynamic_power_control",
        "dynamic_power_control",
    )
    assert calls[0][1][2]["devices"] == "sensor.dev"
    assert calls[1][0] == "set_discharge"
    assert calls[1][1][:2] == (
        "excluded_device_dynamic_power_control",
        "dynamic_power_control",
    )
    assert calls[1][1][2]["devices"] == "sensor.dev"


def test_controller_allows_charge_while_keeping_discharge_blocked_in_restart_hold():
    status = {
        "active": True,
        "charge_blocked": False,
        "discharge_blocked": True,
        "devices": ["sensor.dev"],
        "blocked_devices": [],
        "discharge_blocked_devices": ["sensor.dev"],
        "phases": {"sensor.dev": "restart_hold"},
        "hold_remaining_s": 250,
        "yield_remaining_s": 0,
    }
    calls = []
    controller = SimpleNamespace(
        _external_loads=SimpleNamespace(refresh_dynamic_power_control=lambda: status),
        set_charge_block=lambda *args, **kwargs: calls.append(("set", args, kwargs)),
        remove_charge_block=lambda *args, **kwargs: calls.append(("remove", args, kwargs)),
        set_discharge_block=lambda *args, **kwargs: calls.append(("set_discharge", args, kwargs)),
        remove_discharge_block=lambda *args, **kwargs: calls.append(("remove_discharge", args, kwargs)),
    )

    ChargeDischargeController._refresh_dynamic_power_control_block(controller)

    assert calls == [
        ("remove", ("excluded_device_dynamic_power_control",), {}),
        (
            "set_discharge",
            (
                "excluded_device_dynamic_power_control",
                "dynamic_power_control",
                {
                    "devices": "sensor.dev",
                    "phases": {"sensor.dev": "restart_hold"},
                    "hold_remaining_s": 250,
                    "yield_remaining_s": 0,
                },
            ),
            {},
        ),
    ]


def test_controller_removes_dynamic_control_operation_blocks_when_idle():
    status = {
        "active": False,
        "charge_blocked": False,
        "discharge_blocked": False,
        "devices": [],
        "blocked_devices": [],
        "discharge_blocked_devices": [],
        "phases": {},
        "hold_remaining_s": 0,
        "yield_remaining_s": 0,
    }
    calls = []
    controller = SimpleNamespace(
        _external_loads=SimpleNamespace(refresh_dynamic_power_control=lambda: status),
        set_charge_block=lambda *args, **kwargs: calls.append(("set", args, kwargs)),
        remove_charge_block=lambda *args, **kwargs: calls.append(("remove", args, kwargs)),
        set_discharge_block=lambda *args, **kwargs: calls.append(("set_discharge", args, kwargs)),
        remove_discharge_block=lambda *args, **kwargs: calls.append(("remove_discharge", args, kwargs)),
    )

    ChargeDischargeController._refresh_dynamic_power_control_block(controller)

    assert calls == [
        ("remove", ("excluded_device_dynamic_power_control",), {}),
        ("remove_discharge", ("excluded_device_dynamic_power_control",), {}),
    ]


def test_operation_log_reason_uses_actual_blocker_keys():
    controller = SimpleNamespace(
        get_charge_blockers=lambda: {
            "excluded_device_dynamic_power_control": {"reason": "dynamic_power_control"},
        },
        get_discharge_blockers=lambda: {
            "time_slot_discharge": {"reason": "time_slot"},
        },
    )

    assert ChargeDischargeController._operation_blockers_for_log(controller, True) == (
        "excluded_device_dynamic_power_control"
    )
    assert ChargeDischargeController._operation_blockers_for_log(controller, False) == (
        "time_slot_discharge"
    )


# ----------------------------------------------------------------------
# check_ev_charger_state  (no-telemetry EV: 5-min pause then discharge-block)
# Time-dependent: a frozen clock drives the pause window. Returns
# (pause_active, ev_charging_active).
# ----------------------------------------------------------------------

def _ev_device(**overrides):
    """A no-telemetry EV charger: a state sensor, not a numeric power sensor."""
    base = {
        "enabled": True,
        "ev_charger_no_telemetry": True,
        "power_sensor": "sensor.ev",
    }
    base.update(overrides)
    return base


def test_ev_start_charging_starts_pause():
    loads = _controller([_ev_device()], {"sensor.ev": _state("charging")})
    # First detection: pause begins, not yet discharge-blocking.
    assert loads.check_ev_charger_state() == (True, False)


def test_ev_pause_stays_active_within_5_min():
    loads = _controller([_ev_device()], {"sensor.ev": _state("charging")})
    loads.check_ev_charger_state()                # pause starts (now + 5 min)
    # An immediate re-check is well within the 5-minute window.
    assert loads.check_ev_charger_state() == (True, False)


def test_ev_charging_active_after_pause_expires():
    loads = _controller([_ev_device()], {"sensor.ev": _state("charging")})
    loads.check_ev_charger_state()                # pause starts
    # Simulate the 5-min pause having elapsed by moving the stored deadline into
    # the past. (freezegun can't patch HA's dt_util.utcnow with the HA pytest
    # plugin disabled, so we manipulate the deadline directly instead.)
    loads._controller._ev_pause_until["sensor.ev"] = dt_util.utcnow() - timedelta(minutes=1)
    assert loads.check_ev_charger_state() == (False, True)


def test_ev_stop_charging_cancels_pause():
    states = {"sensor.ev": _state("charging")}
    loads = _controller([_ev_device()], states)
    loads.check_ev_charger_state()                # pause starts
    states["sensor.ev"] = _state("idle")          # EV stops mid-pause
    assert loads.check_ev_charger_state() == (False, False)
    assert loads._controller._ev_pause_until == {}


def test_ev_idle_does_nothing():
    loads = _controller([_ev_device()], {"sensor.ev": _state("idle")})
    assert loads.check_ev_charger_state() == (False, False)


def test_ev_spanish_cargando_detected():
    loads = _controller([_ev_device()], {"sensor.ev": _state("Cargando")})
    assert loads.check_ev_charger_state() == (True, False)


def test_ev_uses_dedicated_activity_sensor():
    device = _ev_device(
        power_sensor="sensor.legacy_idle",
        activity_sensor="binary_sensor.ev_charging",
    )
    loads = _controller(
        [device],
        {
            "sensor.legacy_idle": _state("idle"),
            "binary_sensor.ev_charging": _state("on"),
        },
    )

    assert loads.check_ev_charger_state() == (True, False)


def test_ev_legacy_power_sensor_state_remains_supported():
    loads = _controller([_ev_device()], {"sensor.ev": _state("charging")})

    assert loads.check_ev_charger_state() == (True, False)


@pytest.mark.parametrize("device", [
    _ev_device(enabled=False),
    _ev_device(power_sensor=None),
    # A numeric excluded device (not a no-telemetry EV) must be ignored here.
    _device(ev_charger_no_telemetry=False, power_sensor="sensor.ev"),
])
def test_ev_skips_non_applicable_devices(device):
    loads = _controller([device], {"sensor.ev": _state("charging")})
    assert loads.check_ev_charger_state() == (False, False)


# ----------------------------------------------------------------------
# claimable_solar_demand_kwh  (excluded-device claim on the solar forecast)
# ----------------------------------------------------------------------

def _claim_device(**overrides):
    """An excluded device that reports its expected remaining demand."""
    overrides.setdefault("remaining_demand_sensor", "sensor.demand")
    return _device(**overrides)


def test_remaining_demand_reads_kwh_unit():
    loads = _controller([_claim_device()],
                        {"sensor.demand": _state(6.5, unit="kWh")})
    assert loads.claimable_solar_demand_kwh() == pytest.approx(6.5)


def test_remaining_demand_converts_wh_to_kwh():
    loads = _controller([_claim_device()],
                        {"sensor.demand": _state(6500, unit="Wh")})
    assert loads.claimable_solar_demand_kwh() == pytest.approx(6.5)


def test_remaining_demand_unit_match_is_case_insensitive():
    loads = _controller([_claim_device()],
                        {"sensor.demand": _state(6500, unit="wh")})
    assert loads.claimable_solar_demand_kwh() == pytest.approx(6.5)


def test_remaining_demand_without_unit_is_read_as_kwh():
    state = SimpleNamespace(state="6.5", attributes={})
    loads = _controller([_claim_device()], {"sensor.demand": state})
    assert loads.claimable_solar_demand_kwh() == pytest.approx(6.5)


@pytest.mark.parametrize("raw", ["unknown", "unavailable", "not-a-number"])
def test_remaining_demand_unusable_state_yields_no_claim(raw):
    loads = _controller([_claim_device()],
                        {"sensor.demand": _state(raw, unit="kWh")})
    assert loads.claimable_solar_demand_kwh() is None


def test_remaining_demand_missing_entity_yields_no_claim():
    loads = _controller([_claim_device()], {})
    assert loads.claimable_solar_demand_kwh() is None


def test_remaining_demand_clamps_negative_to_zero():
    loads = _controller([_claim_device()],
                        {"sensor.demand": _state(-0.01, unit="kWh")})
    assert loads.claimable_solar_demand_kwh() == pytest.approx(0.0)


def test_claim_is_none_without_configured_sensor():
    loads = _controller([_device()], {"sensor.demand": _state(6.5, unit="kWh")})
    assert loads.claimable_solar_demand_kwh() is None


def test_claim_is_none_without_devices():
    assert _controller([]).claimable_solar_demand_kwh() is None


def test_claim_sums_multiple_devices():
    loads = _controller(
        [
            _claim_device(power_sensor="sensor.a"),
            _claim_device(power_sensor="sensor.b",
                          remaining_demand_sensor="sensor.demand_b"),
        ],
        {
            "sensor.demand": _state(6.5, unit="kWh"),
            "sensor.demand_b": _state(2.0, unit="kWh"),
        },
    )
    assert loads.claimable_solar_demand_kwh() == pytest.approx(8.5)


def test_claim_skips_unreadable_device_but_keeps_the_others():
    loads = _controller(
        [
            _claim_device(power_sensor="sensor.a"),
            _claim_device(power_sensor="sensor.b",
                          remaining_demand_sensor="sensor.demand_b"),
        ],
        {
            "sensor.demand": _state("unavailable", unit="kWh"),
            "sensor.demand_b": _state(2.0, unit="kWh"),
        },
    )
    assert loads.claimable_solar_demand_kwh() == pytest.approx(2.0)


def test_claim_skips_disabled_device():
    loads = _controller([_claim_device(enabled=False)],
                        {"sensor.demand": _state(6.5, unit="kWh")})
    assert loads.claimable_solar_demand_kwh() is None


def test_claim_skips_device_not_included_in_consumption():
    # Its load already sits outside the consumption forecast; claiming solar
    # for it would count the same energy twice.
    loads = _controller([_claim_device(included_in_consumption=False)],
                        {"sensor.demand": _state(6.5, unit="kWh")})
    assert loads.claimable_solar_demand_kwh() is None


def test_claim_ignores_exclusion_pct():
    # exclusion_pct governs live power, not a future energy demand.
    loads = _controller([_claim_device(exclusion_pct=50)],
                        {"sensor.demand": _state(6.5, unit="kWh")})
    assert loads.claimable_solar_demand_kwh() == pytest.approx(6.5)
