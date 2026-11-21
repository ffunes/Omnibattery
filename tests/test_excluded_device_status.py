"""Status per excluded device, from the last control cycle.

calculate_adjustment() already decided per device whether the battery skips
it, lets it run on PV surplus or covers it; only the total survived. It now
keeps each device's result, and device_state() turns that and the dynamic
power control status into one state per device.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery.infra.external_loads import ExternalLoads
from custom_components.omnibattery.sensor import EXCLUDED_DEVICE_STATES, ExcludedDeviceStatusSensor


def _loads(device, power_w, *, solar_w=None, home_w=None):
    states = {"sensor.device_power": power_w}
    if solar_w is not None:
        states["sensor.solar"] = solar_w
    if home_w is not None:
        states["sensor.home"] = home_w

    def get(entity_id):
        if entity_id not in states or states[entity_id] is None:
            return None
        return SimpleNamespace(state=str(states[entity_id]), attributes={"unit_of_measurement": "W"})

    hass = SimpleNamespace(states=SimpleNamespace(get=get))
    entry = SimpleNamespace(data={"excluded_devices": [{"power_sensor": "sensor.device_power", **device}]})
    controller = SimpleNamespace(
        solar_production_sensor="sensor.solar" if solar_w is not None else None,
        home_consumption_sensor="sensor.home" if home_w is not None else None,
    )
    loads = ExternalLoads(hass, entry, controller)
    return loads


@pytest.mark.parametrize(
    ("device", "power", "kw", "state"),
    [
        ({"enabled": False}, 1500, {}, "paused"),
        ({}, None, {}, "unmeasured"),
        ({}, 5, {}, "idle"),
        ({}, 1500, {}, "excluded"),
        ({"included_in_consumption": False}, 800, {}, "covered"),
        ({"allow_solar_surplus": True}, 1500, {"solar_w": 3000, "home_w": 2000}, "solar_surplus"),
        ({"allow_solar_surplus": True}, 1500, {"solar_w": 200, "home_w": 1700}, "excluded"),
    ],
)
def test_each_rule_has_its_state(device, power, kw, state):
    loads = _loads(device, power, **kw)
    loads.calculate_adjustment()

    assert loads.device_state(0)[0] == state


def test_a_reading_that_is_not_a_number_is_unmeasured():
    # It fell through to idle: a device drawing power looked switched off.
    loads = _loads({}, "n/a")
    loads.calculate_adjustment()

    assert loads.device_state(0)[0] == "unmeasured"


def _charger(activity, *, pause_until=None):
    """An EV charger without telemetry: an activity sensor, no power figure."""
    states = {"sensor.wallbox_state": activity}

    def get(entity_id):
        if states.get(entity_id) is None:
            return None
        return SimpleNamespace(state=states[entity_id], attributes={})

    hass = SimpleNamespace(states=SimpleNamespace(get=get))
    device = {"ev_charger_no_telemetry": True, "activity_sensor": "sensor.wallbox_state"}
    entry = SimpleNamespace(data={"excluded_devices": [device]})
    controller = SimpleNamespace(
        solar_production_sensor=None,
        home_consumption_sensor=None,
        _ev_pause_until={} if pause_until is None else {"sensor.wallbox_state": pause_until},
    )
    loads = ExternalLoads(hass, entry, controller)
    loads.calculate_adjustment()
    return loads.device_state(0)


@pytest.mark.parametrize(
    ("activity", "state"),
    [("charging", "excluded"), ("on", "excluded"), ("idle", "idle"), ("off", "idle"),
     ("unavailable", "unmeasured"), (None, "unmeasured")],
)
def test_a_charger_without_telemetry_follows_its_activity_sensor(activity, state):
    # It read unmeasured while it charged: the one moment its state matters.
    assert _charger(activity)[0] == state


def test_a_charging_charger_blocks_discharge_and_charge_only_during_its_pause():
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    _state, attrs = _charger("charging")
    assert attrs["discharge_blocked"] is True and attrs["charge_blocked"] is False
    assert attrs["power_w"] is None

    _state, attrs = _charger("charging", pause_until=dt_util.utcnow() + timedelta(minutes=3))
    assert attrs["charge_blocked"] is True


def test_dynamic_power_control_priority_wins():
    loads = _loads({}, 1500)
    loads.calculate_adjustment()
    loads.dynamic_power_control_status = {
        "devices": ["sensor.device_power"],
        "blocked_devices": ["sensor.device_power"],
        "phases": {"sensor.device_power": "first_detection"},
    }

    state, attrs = loads.device_state(0)
    assert state == "priority"
    assert attrs["phase"] == "first_detection"
    assert attrs["charge_blocked"] is True


def test_the_attributes_carry_power_and_adjustment():
    loads = _loads({"included_in_consumption": False}, 800)
    loads.calculate_adjustment()

    _state, attrs = loads.device_state(0)
    assert attrs["power_w"] == 800.0
    assert attrs["adjustment_w"] == -800.0


def test_before_the_first_cycle_it_is_idle():
    assert _loads({}, 1500).device_state(0)[0] == "idle"


def test_the_sensor_reports_the_state_and_groups_with_its_device():
    loads = _loads({"included_in_consumption": False}, 800)
    loads.calculate_adjustment()
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _e: SimpleNamespace(attributes={"friendly_name": "Heat pump"}))
    )
    entry = SimpleNamespace(data={"excluded_devices": [{"power_sensor": "sensor.device_power", "included_in_consumption": False}]})
    sensor = ExcludedDeviceStatusSensor(hass, entry, SimpleNamespace(_external_loads=loads), 0)

    assert sensor.native_value == "covered"
    assert sensor.native_value in EXCLUDED_DEVICE_STATES
    attrs = sensor.extra_state_attributes
    assert attrs["excluded_device_index"] == 0
    assert attrs["type"] == "additional"
    assert attrs["power_w"] == 800.0


def test_a_device_switched_off_while_the_cycle_returns_early_reads_idle():
    # calculate_adjustment() is skipped during a grid charge or manual mode;
    # the state must not stay "excluded, 2000 W" for hours.
    loads = _loads({}, 2000)
    loads.calculate_adjustment()
    assert loads.device_state(0)[0] == "excluded"

    loads._hass.states.get = lambda entity_id: SimpleNamespace(
        state="0", attributes={"unit_of_measurement": "W"}
    )
    state, attrs = loads.device_state(0)
    assert state == "idle"
    assert attrs["power_w"] == 0.0

    loads._hass.states.get = lambda entity_id: None
    assert loads.device_state(0)[0] == "unmeasured"
