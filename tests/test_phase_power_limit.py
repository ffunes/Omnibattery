from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    CONF_METER_INVERTED,
    CONF_PHASE_1_FUSE_SIZE,
    CONF_PHASE_1_CURRENT_SENSOR,
    CONF_PHASE_2_FUSE_SIZE,
    CONF_PHASE_2_CURRENT_SENSOR,
    CONF_PHASE_3_FUSE_SIZE,
    CONF_PHASE_3_CURRENT_SENSOR,
    CONF_SLOT_ENABLED,
    CONF_SLOT_MODE,
    CONF_TIME_SLOTS,
    CONF_THREE_PHASE_ENABLED,
    PHASE_L1,
    PHASE_L2,
    PHASE_L3,
    PHASE_UNASSIGNED,
    SLOT_MODE_MANUAL,
)
from custom_components.omnibattery.control import (
    phase_power_limit as phase_power_limit_module,
)
from custom_components.omnibattery.control.phase_power_limit import (
    PhasePowerLimiter,
    calculate_phase_budgets,
    normalize_current_sensor_state,
)
from custom_components.omnibattery.config_flow import (
    _phase_protection_schema,
    _validate_phase_protection,
)
from custom_components.omnibattery.sensor import ThreePhaseProtectionSensor


class FakeStates:
    def __init__(self, states: dict[str, object]):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


class FakeCoordinator:
    def __init__(self, name: str, phase: str, max_power: int = 4000):
        self.name = name
        self.phase = phase
        self.data = {"battery_power": 0}
        self.max_charge_power = max_power
        self.max_discharge_power = max_power
        self.commanded_charge_power = 0
        self.commanded_discharge_power = 0


class FakeController:
    def __init__(self, coordinators):
        self.coordinators = coordinators
        self._active_charge_batteries = []
        self._active_discharge_batteries = []

    @staticmethod
    def _coordinator_delivered_power(coordinator):
        return float(coordinator.data.get("battery_power", 0))

    @staticmethod
    def _battery_power_limit(coordinator, is_charging):
        return coordinator.max_charge_power if is_charging else coordinator.max_discharge_power

    @staticmethod
    def _clamp_to_system_capacity(total, _batteries, _is_charging):
        return total


def _state(value, unit="A", now=None, age_s=0):
    now = now or datetime.now(timezone.utc)
    return SimpleNamespace(
        state=str(value),
        attributes={"unit_of_measurement": unit},
        last_reported=now - timedelta(seconds=age_s),
        last_updated=now - timedelta(seconds=age_s),
    )


def _limiter(states, coordinators, *, now=None, configured_phases=None):
    entry = SimpleNamespace(
        data={
            CONF_THREE_PHASE_ENABLED: True,
            CONF_METER_INVERTED: False,
            CONF_PHASE_1_CURRENT_SENSOR: "sensor.l1",
            CONF_PHASE_2_CURRENT_SENSOR: "sensor.l2",
            CONF_PHASE_3_CURRENT_SENSOR: "sensor.l3",
            CONF_PHASE_1_FUSE_SIZE: 25,
            CONF_PHASE_2_FUSE_SIZE: 25,
            CONF_PHASE_3_FUSE_SIZE: 25,
        }
    )
    if configured_phases is not None:
        phase_fields = {
            PHASE_L1: (CONF_PHASE_1_CURRENT_SENSOR, CONF_PHASE_1_FUSE_SIZE),
            PHASE_L2: (CONF_PHASE_2_CURRENT_SENSOR, CONF_PHASE_2_FUSE_SIZE),
            PHASE_L3: (CONF_PHASE_3_CURRENT_SENSOR, CONF_PHASE_3_FUSE_SIZE),
        }
        for phase, (sensor_key, limit_key) in phase_fields.items():
            if phase not in configured_phases:
                entry.data[sensor_key] = None
                entry.data[limit_key] = None
    controller = FakeController(coordinators)
    hass = SimpleNamespace(states=FakeStates(states))
    return PhasePowerLimiter(
        hass,
        entry,
        controller,
        max_age_s=65,
    )


def _warning_limiter(*, phase_enabled=True, slots=None):
    entry = SimpleNamespace(
        data={
            CONF_THREE_PHASE_ENABLED: phase_enabled,
            CONF_TIME_SLOTS: slots or [],
        }
    )
    return PhasePowerLimiter(SimpleNamespace(), entry)


def _capture_warning_repairs(monkeypatch):
    created = []
    deleted = []
    monkeypatch.setattr(
        phase_power_limit_module.ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )
    monkeypatch.setattr(
        phase_power_limit_module.ir,
        "async_delete_issue",
        lambda *args, **kwargs: deleted.append((args, kwargs)),
    )
    return created, deleted


def test_manual_warning_is_cleared_without_a_manual_bypass(monkeypatch):
    created, deleted = _capture_warning_repairs(monkeypatch)
    limiter = _warning_limiter()

    limiter.update_manual_mode_warning("entry", False)

    assert created == []
    assert deleted[0][0][2] == "three_phase_manual_mode_entry"
    assert limiter._manual_warning_created is False


def test_manual_warning_covers_manual_mode_and_manual_slots(monkeypatch):
    created, deleted = _capture_warning_repairs(monkeypatch)
    limiter = _warning_limiter()

    limiter.update_manual_mode_warning("entry", True)
    assert len(created) == 1

    limiter.update_manual_mode_warning("entry", False)
    assert len(deleted) == 1

    limiter.config_entry.data[CONF_TIME_SLOTS] = [
        {CONF_SLOT_ENABLED: True, CONF_SLOT_MODE: SLOT_MODE_MANUAL}
    ]
    limiter.update_manual_mode_warning("entry", False)
    assert len(created) == 2

    limiter.config_entry.data[CONF_TIME_SLOTS][0][CONF_SLOT_ENABLED] = False
    limiter.update_manual_mode_warning("entry", False)
    assert len(deleted) == 2


def test_manual_warning_stays_cleared_when_three_phase_protection_is_disabled(
    monkeypatch,
):
    created, deleted = _capture_warning_repairs(monkeypatch)
    limiter = _warning_limiter(phase_enabled=False)

    limiter.update_manual_mode_warning("entry", True)

    assert created == []
    assert deleted[0][0][2] == "three_phase_manual_mode_entry"


def test_phase_budget_uses_controller_battery_sign():
    no_battery = calculate_phase_budgets(20, 0, 25)
    assert no_battery["charge_budget_a"] == 5
    assert no_battery["discharge_budget_a"] == 25

    budgets = calculate_phase_budgets(20, 5, 25)

    assert budgets == {
        "base_a": 15,
        "charge_budget_a": 10,
        "discharge_budget_a": 25,
    }

    export = calculate_phase_budgets(-20, -5, 25)
    assert export["base_a"] == -15
    assert export["charge_budget_a"] == 25
    assert export["discharge_budget_a"] == 10


@pytest.mark.parametrize(
    ("grid_charging_active", "expected"),
    [(False, 1340), (True, -1340)],
)
def test_phase_safety_review_preserves_active_power_sign_convention(
    grid_charging_active, expected
):
    battery = FakeCoordinator("L1 battery", PHASE_L1)
    battery.commanded_charge_power = 1340
    calls = []

    async def _set_battery_power(coordinator, charge, discharge):
        calls.append((coordinator, charge, discharge))

    controller = SimpleNamespace(
        coordinators=[battery],
        _phase_power_limiter=SimpleNamespace(enabled=True),
        _power_distribution=SimpleNamespace(
            _distribute_power_by_limits=lambda total, selected, is_charging: {
                selected[0]: total
            },
        ),
        _coordinator_delivered_power=ChargeDischargeController._coordinator_delivered_power,
        _set_battery_power=_set_battery_power,
        _phase_safety_pending=True,
        grid_charging_active=grid_charging_active,
    )
    controller._signed_power_from_allocations = (
        lambda charging, discharging: ChargeDischargeController._signed_power_from_allocations(
            controller, charging, discharging
        )
    )

    asyncio.run(ChargeDischargeController._apply_phase_safety_review(controller))

    assert controller.previous_power == expected
    assert calls == [(battery, 1340, 0)]
    assert controller._phase_safety_pending is False


def test_phase_budgets_never_exceed_configured_current_limit():
    budgets = calculate_phase_budgets(2.82, -3.275362318840579, 9)

    assert budgets["base_a"] == pytest.approx(6.095362318840579)
    assert budgets["charge_budget_a"] == pytest.approx(2.9046376811594206)
    assert budgets["discharge_budget_a"] == 9

    export_base = calculate_phase_budgets(-6, 0, 9)
    assert export_base["charge_budget_a"] == 9
    assert export_base["discharge_budget_a"] == 3


def test_direct_discharge_command_is_capped_by_phase_current_limit():
    now = datetime.now(timezone.utc)
    battery = FakeCoordinator("L1 battery", PHASE_L1, max_power=6000)
    limiter = _limiter(
        {
            "sensor.l1": _state(0, now=now),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [battery],
    )

    assert limiter.limit_single_command(battery, 0, 6000) == (0, 5175)


def test_sensor_normalization_handles_ma_inversion_and_staleness():
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)

    assert normalize_current_sensor_state(
        _state(1.5, "mA", now), now=now
    ).value_a == 0.0015
    assert normalize_current_sensor_state(
        _state(1500, "A", now), meter_inverted=True, now=now
    ).value_a == -1500
    stale = normalize_current_sensor_state(
        _state(1500, "A", now, age_s=66), now=now
    )
    assert stale.value_a is None
    assert stale.reason == "sensor_stale"


def test_allocation_caps_phase_and_moves_overflow_to_healthy_phase():
    now = datetime.now(timezone.utc)
    b1 = FakeCoordinator("L1 battery", PHASE_L1)
    b2 = FakeCoordinator("L2 battery", PHASE_L2)
    limiter = _limiter(
        {
            "sensor.l1": _state(20, now=now),
            "sensor.l2": _state(5, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [b1, b2],
    )

    allocation = limiter.limit_allocation({b1: 2000, b2: 2000}, True, [b1, b2])

    assert allocation[b1] == 1035
    assert allocation[b2] == 2965
    assert sum(allocation.values()) == 4000

    # If L1 telemetry is unavailable, its battery is held at zero while L2
    # accepts the rejected portion up to its own safe capacity.
    limiter.hass.states._states["sensor.l1"] = _state("unavailable", now=now)
    limiter.begin_cycle()
    allocation = limiter.limit_allocation({b1: 2000, b2: 2000}, True, [b1, b2])
    assert allocation[b1] == 0
    assert allocation[b2] == 4000


def test_allocation_never_adds_an_unselected_battery():
    now = datetime.now(timezone.utc)
    selected = FakeCoordinator("Selected", PHASE_L1)
    unselected = FakeCoordinator("Unselected", PHASE_L1)
    limiter = _limiter(
        {
            "sensor.l1": _state(0, now=now),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [selected, unselected],
    )

    allocation = limiter.limit_allocation(
        {selected: 100},
        True,
        [selected, unselected],
    )

    assert allocation[selected] == 100
    assert allocation[unselected] == 0


def test_overflow_activates_fallback_only_after_selected_phase_is_capped():
    now = datetime.now(timezone.utc)
    selected = FakeCoordinator("Selected L1", PHASE_L1)
    fallback = FakeCoordinator("Fallback L2", PHASE_L2)
    limiter = _limiter(
        {
            "sensor.l1": _state(20, now=now),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [selected, fallback],
    )

    allocation = limiter.limit_allocation(
        {selected: 2000},
        True,
        [selected, fallback],
    )

    assert allocation == {selected: 1035, fallback: 965}


def test_overflow_can_use_an_unconfigured_phase_without_a_phase_cap():
    now = datetime.now(timezone.utc)
    selected = FakeCoordinator("Selected L1", PHASE_L1)
    fallback = FakeCoordinator("Fallback L2", PHASE_L2)
    limiter = _limiter(
        {
            "sensor.l1": _state(20, now=now),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [selected, fallback],
        configured_phases={PHASE_L1},
    )

    allocation = limiter.limit_allocation(
        {selected: 2000},
        True,
        [selected, fallback],
    )

    assert allocation == {selected: 1035, fallback: 965}


def test_allocation_preserves_normal_proportional_split_below_phase_cap():
    now = datetime.now(timezone.utc)
    b1 = FakeCoordinator("First", PHASE_L1)
    b2 = FakeCoordinator("Second", PHASE_L1)
    limiter = _limiter(
        {
            "sensor.l1": _state(0, now=now),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [b1, b2],
    )

    assert limiter.limit_allocation({b1: 750, b2: 250}, True, [b1, b2]) == {
        b1: 750,
        b2: 250,
    }


def test_degraded_phase_is_detected_without_a_new_sensor_event():
    now = datetime.now(timezone.utc)
    battery = FakeCoordinator("L1 battery", PHASE_L1)
    limiter = _limiter(
        {
            "sensor.l1": _state(0, now=now, age_s=66),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [battery],
    )

    assert limiter.has_degraded_phase() is True
    assert limiter.limit_allocation({battery: 500}, True) == {battery: 0}


def test_unconfigured_phases_are_optional_and_unlimited():
    now = datetime.now(timezone.utc)
    l1_battery = FakeCoordinator("L1 battery", PHASE_L1)
    limiter = _limiter(
        {
            "sensor.l1": _state(0, now=now),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [l1_battery],
        configured_phases={PHASE_L1},
    )

    assert limiter.has_degraded_phase() is False
    assert limiter.phase_snapshot(PHASE_L2)["reason"] == "not_configured"

    l2_battery = FakeCoordinator("L2 battery", PHASE_L2)
    limiter.controller.coordinators.append(l2_battery)
    limiter.begin_cycle()

    assert limiter.has_degraded_phase() is False
    assert limiter.limit_allocation({l2_battery: 500}, True) == {l2_battery: 500}
    assert limiter.limit_single_command(l2_battery, 1000, 0) == (1000, 0)


def test_unassigned_battery_is_exempt_and_valid_phase_is_capped():
    now = datetime.now(timezone.utc)
    b1 = FakeCoordinator("L1 battery", PHASE_L1)
    unassigned = FakeCoordinator("Unassigned", PHASE_UNASSIGNED)
    limiter = _limiter(
        {
            "sensor.l1": _state(20, now=now),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [b1, unassigned],
    )

    assert limiter.limit_single_command(b1, 2000, 0) == (1035, 0)
    assert limiter.limit_single_command(unassigned, 1000, 0) == (1000, 0)
    assert limiter.limit_allocation({unassigned: 1500}, True) == {unassigned: 1500}
    assert limiter.has_degraded_phase() is False


def test_protection_diagnostics_report_limited_and_unassigned_batteries():
    now = datetime.now(timezone.utc)
    l1_battery = FakeCoordinator("L1 battery", PHASE_L1)
    l2_battery = FakeCoordinator("L2 battery", PHASE_L2)
    unassigned = FakeCoordinator("Unassigned", PHASE_UNASSIGNED)
    limiter = _limiter(
        {
            "sensor.l1": _state(20, now=now),
            "sensor.l2": _state(5, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [l1_battery, l2_battery, unassigned],
    )

    limiter.limit_allocation(
        {l1_battery: 2000, l2_battery: 2000},
        True,
        [l1_battery, l2_battery],
    )
    details = limiter.diagnostics()

    assert details["state"] == "limiting"
    assert details["protection_enabled"] is True
    assert details["limited_batteries"] == ["L1 battery"]
    assert details["limited_battery_details"] == [
        {
            "battery": "L1 battery",
            "phase": PHASE_L1,
            "direction": "charging",
            "requested_power_w": 2000,
            "assigned_power_w": 1035,
            "limited_power_w": 965,
            "reason": "phase_limit",
        }
    ]
    assert details["phases"][PHASE_L1]["batteries"] == ["L1 battery"]
    assert details["unassigned_batteries"] == ["Unassigned"]

    limiter.begin_cycle()
    limiter.limit_allocation({l1_battery: 100}, True, [l1_battery])
    assert limiter.diagnostics()["state"] == "active"

    limiter.enabled = False
    disabled = limiter.diagnostics()
    assert disabled["state"] == "disabled"
    assert disabled["limited_batteries"] == []


def test_three_phase_protection_sensor_exposes_status_and_attributes():
    now = datetime.now(timezone.utc)
    battery = FakeCoordinator("L1 battery", PHASE_L1)
    limiter = _limiter(
        {
            "sensor.l1": _state(20, now=now),
            "sensor.l2": _state(0, now=now),
            "sensor.l3": _state(0, now=now),
        },
        [battery],
    )
    limiter.limit_single_command(battery, 2000, 0)
    limiter.controller._phase_power_limiter = limiter
    sensor = ThreePhaseProtectionSensor(
        limiter.hass,
        limiter.config_entry,
        limiter.controller,
    )

    assert sensor.entity_id == "sensor.omnibattery_three_phase_protection_status"
    assert sensor.native_value == "limiting"
    assert sensor.extra_state_attributes["limited_batteries"] == ["L1 battery"]
    assert sensor.extra_state_attributes["phases"][PHASE_L1]["reading_a"] == 20


def test_config_validation_rejects_duplicate_sensors_and_bad_units():
    hass = SimpleNamespace(
        states=FakeStates(
            {
                "sensor.l1": _state(10, "A"),
                "sensor.l2": _state(10, "mA"),
                "sensor.l3": _state(10, "A"),
            }
        )
    )
    valid = {
        CONF_PHASE_1_CURRENT_SENSOR: "sensor.l1",
        CONF_PHASE_2_CURRENT_SENSOR: "sensor.l2",
        CONF_PHASE_3_CURRENT_SENSOR: "sensor.l3",
        CONF_PHASE_1_FUSE_SIZE: 25,
        CONF_PHASE_2_FUSE_SIZE: 25,
        CONF_PHASE_3_FUSE_SIZE: 25,
    }
    assert _validate_phase_protection(hass, valid) == {}

    two_phase = {
        key: valid[key]
        for key in (
            CONF_PHASE_1_CURRENT_SENSOR,
            CONF_PHASE_1_FUSE_SIZE,
            CONF_PHASE_2_CURRENT_SENSOR,
            CONF_PHASE_2_FUSE_SIZE,
        )
    }
    assert _validate_phase_protection(hass, two_phase) == {}

    invalid = {**valid, CONF_PHASE_2_CURRENT_SENSOR: "sensor.l1", CONF_PHASE_3_FUSE_SIZE: 0}
    errors = _validate_phase_protection(hass, invalid)
    assert errors[CONF_PHASE_1_CURRENT_SENSOR] == "phase_sensors_must_differ"
    assert errors[CONF_PHASE_2_CURRENT_SENSOR] == "phase_sensors_must_differ"
    assert errors[CONF_PHASE_3_FUSE_SIZE] == "phase_limit_must_be_positive"

    missing = {**valid}
    for key in (
        CONF_PHASE_1_CURRENT_SENSOR,
        CONF_PHASE_1_FUSE_SIZE,
        CONF_PHASE_2_CURRENT_SENSOR,
        CONF_PHASE_2_FUSE_SIZE,
    ):
        missing.pop(key)
    missing_errors = _validate_phase_protection(hass, missing)
    assert missing_errors == {}

    partial = {
        CONF_PHASE_1_CURRENT_SENSOR: "sensor.l1",
        CONF_PHASE_1_FUSE_SIZE: None,
    }
    partial_errors = _validate_phase_protection(hass, partial)
    assert partial_errors[CONF_PHASE_1_FUSE_SIZE] == "phase_sensor_and_limit_required"

    orphan_limit_errors = _validate_phase_protection(
        hass,
        {CONF_PHASE_1_FUSE_SIZE: 25},
    )
    assert orphan_limit_errors[CONF_PHASE_1_CURRENT_SENSOR] == (
        "phase_sensor_and_limit_required"
    )


def test_phase_form_accepts_a_single_configured_phase():
    assert _phase_protection_schema()(
        {
            CONF_PHASE_1_CURRENT_SENSOR: "sensor.l1",
            CONF_PHASE_1_FUSE_SIZE: 25,
        }
    ) == {
        CONF_PHASE_1_CURRENT_SENSOR: "sensor.l1",
        CONF_PHASE_1_FUSE_SIZE: 25.0,
    }


def test_phase_form_allows_clearing_a_saved_phase_pair():
    defaults = {
        CONF_PHASE_1_CURRENT_SENSOR: "sensor.l1",
        CONF_PHASE_1_FUSE_SIZE: 25,
        CONF_PHASE_2_CURRENT_SENSOR: "sensor.l2",
        CONF_PHASE_2_FUSE_SIZE: 25,
        CONF_PHASE_3_CURRENT_SENSOR: "sensor.l3",
        CONF_PHASE_3_FUSE_SIZE: 25,
    }

    assert _phase_protection_schema(defaults)(
        {
            CONF_PHASE_1_CURRENT_SENSOR: "sensor.l1",
            CONF_PHASE_1_FUSE_SIZE: 25,
            CONF_PHASE_2_CURRENT_SENSOR: "sensor.l2",
            CONF_PHASE_2_FUSE_SIZE: 25,
        }
    ) == {
        CONF_PHASE_1_CURRENT_SENSOR: "sensor.l1",
        CONF_PHASE_1_FUSE_SIZE: 25.0,
        CONF_PHASE_2_CURRENT_SENSOR: "sensor.l2",
        CONF_PHASE_2_FUSE_SIZE: 25.0,
    }
