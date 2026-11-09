"""Every action colour needs real energy behind it (1.6.0b12).

The complaint this file pins down, with the measurement that produced it: the
daily-operation graph painted **purple** quarters at 03:15, 04:15 and 05:30
although the battery had charged almost nothing.  The published diary said so
itself::

    index  time    actual_action_mask  actual_charge_to_battery_kwh
    13     03:15   2 (grid charge)     0.000944
    17     04:15   2                   0.000097
    22     05:30   2                   0.000158

The mask carried the charge bit while the energy behind it was 0.0001-0.0009
kWh: a few watt-hours of spill on a quarter boundary, about three seconds of
flow, shown as a full grid charge.

The rule these tests lock in: a bit may colour a quarter only when the energy
series that owns it clears ``ACTION_ENERGY_DEADBAND_KWH``, and that holds for
green (solar charge), purple (grid charge) and blue (discharge), for the
observed series and for the projected one.  A bit whose energy was never
measured is still honoured — a statement is not a measurement — and a hidden
trickle stays in the diagnostics instead of silently becoming "unknown".
"""
from __future__ import annotations

import copy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController, diagnostics
from custom_components.omnibattery.const import ACTION_ENERGY_DEADBAND_KWH
from custom_components.omnibattery.pricing.daily_timeline import (
    masked_by_energy_deadband,
)
from custom_components.omnibattery.tracking.daily_timeline import (
    ACTION_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_SOLAR_CHARGE,
    DailyOperationTimelineManager,
)

MADRID = ZoneInfo("Europe/Madrid")

# The three quarters from the report, with the energy each one measured.  The
# power is chosen above ``POWER_DEADBAND_W`` so the diary really accumulates the
# energy the way the installation did; only the duration is derived.
REPORTED_CELLS = [
    # hour, minute, index, energy_kwh, charge_power_w
    (3, 15, 13, 0.000944, 56.64),
    (4, 15, 17, 0.000097, 12.0),
    (5, 30, 22, 0.000158, 12.0),
]

COLOURS = [
    # bit, published name, tiny flow (measured), real flow (above the dead-band)
    (ACTION_SOLAR_CHARGE, "solar_charge", "solar_to_battery_kwh", 0.0009, 0.25),
    (ACTION_GRID_CHARGE, "grid_charge", "grid_to_battery_kwh", 0.0009, 0.25),
    (ACTION_DISCHARGE, "discharge", "battery_to_home_kwh", 0.0002, 0.25),
]


class FakeStore:
    """Async Store double retaining exactly what the manager writes."""

    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.writes: list[dict] = []

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.data = copy.deepcopy(data)
        self.writes.append(copy.deepcopy(data))


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _manager(clock: MutableClock, *, mode: str = "dynamic_pricing"):
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid"))
    entry = SimpleNamespace(
        entry_id="entry-one",
        data={"pricing_mode": mode, "household_consumption_sensor": "sensor.house"},
        options={},
    )
    return DailyOperationTimelineManager(
        hass,
        entry,
        SimpleNamespace(pricing_mode=mode),
        store=FakeStore(),
        now_provider=clock,
        debounce_seconds=0.01,
    )


def _runtime_controller(coordinators, *, grid_active=False):
    controller = SimpleNamespace(
        coordinators=coordinators,
        _consumption_tracker=None,
        grid_charging_active=grid_active,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=(
            ChargeDischargeController._coordinator_delivered_power
        ),
        _is_battery_manual_owned=lambda _coordinator: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )
    controller._daily_operation_delay_active = lambda: (
        ChargeDischargeController._daily_operation_delay_active(controller)
    )
    return controller


def _operations(manager) -> dict:
    return manager.build_public_snapshot()["operations"]


# ----------------------------------------------------------------------
# 1. The reported symptom, cell for cell
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("hour", "minute", "index", "energy_kwh", "charge_power_w"),
    REPORTED_CELLS,
)
def test_a_measured_grid_trickle_does_not_colour_the_reported_quarter(
    hour, minute, index, energy_kwh, charge_power_w
):
    """Index 13/17/22 carried the charge bit on 0.0001-0.0009 kWh. Not any more."""
    clock = MutableClock(datetime(2026, 8, 23, hour, minute, tzinfo=MADRID))
    manager = _manager(clock)
    duration_s = energy_kwh * 3_600_000.0 / charge_power_w

    # Exactly what the control loop states: "grid charge", with the measured
    # power and duration that produced the tiny energy. No flow field is
    # supplied, so the value-derived path cannot help — this is the path the
    # graph actually rendered.
    manager.record_runtime_decision(
        {"action_mask": ACTION_GRID_CHARGE},
        duration_s=duration_s,
        charge_power_w=charge_power_w,
    )

    operations = _operations(manager)

    assert operations["actual_charge_to_battery_kwh"][index] == pytest.approx(
        energy_kwh, abs=1e-9
    )
    # The colour is gone...
    assert operations["actual_action_mask"][index] == 0
    # ...and the measurement is not: the diagnostics still count it.
    assert operations["actual_deadband_hidden_mask"][index] == ACTION_GRID_CHARGE
    snapshot = manager.build_public_snapshot()
    assert snapshot["counts"]["by_action"]["grid_charge"] == 0
    assert snapshot["counts"]["raw_by_action"]["grid_charge"] == 1
    assert snapshot["counts"]["below_deadband_cells"] == 1


def test_the_reported_quarter_keeps_its_raw_bit_so_nothing_is_erased():
    """Only the published view is gated; the diary itself is untouched."""
    clock = MutableClock(datetime(2026, 8, 23, 3, 15, tzinfo=MADRID))
    manager = _manager(clock)
    manager.record_runtime_decision(
        {"action_mask": ACTION_GRID_CHARGE},
        duration_s=60.0,
        charge_power_w=56.64,
    )

    assert manager._cells[13].actual_action_mask == ACTION_GRID_CHARGE
    assert _operations(manager)["actual_action_mask"][13] == 0


# ----------------------------------------------------------------------
# 2. All three colours, observed series
# ----------------------------------------------------------------------

@pytest.mark.parametrize(("bit", "name", "field", "tiny", "real"), COLOURS)
def test_a_supplied_mask_is_filtered_on_entry_by_its_own_energy(
    bit, name, field, tiny, real
):
    """A mask from elsewhere may not bypass the display dead-band."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    manager.record_runtime_decision({"action_mask": bit, field: tiny}, duration_s=60)

    assert manager._cells[40].actual_action_mask & bit == 0
    assert manager._cells[40].actual_deadband_hidden_mask & bit == bit
    operations = _operations(manager)
    assert operations["actual_action_mask"][40] & bit == 0
    assert operations["actual_deadband_hidden_mask"][40] & bit == bit

    # The same bit with real energy behind it is honoured.
    other = _manager(MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID)))
    other.record_runtime_decision({"action_mask": bit, field: real}, duration_s=60)

    assert other._cells[40].actual_action_mask & bit == bit
    assert other._cells[40].actual_deadband_hidden_mask & bit == 0
    assert _operations(other)["actual_action_mask"][40] & bit == bit


@pytest.mark.parametrize(("bit", "name", "field", "tiny", "real"), COLOURS)
def test_a_measured_trickle_loses_its_colour_for_every_action(
    bit, name, field, tiny, real
):
    """Green, purple and blue are judged by one rule on the published gate."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    duration_s = tiny * 3_600_000.0 / 12.0
    power_key = "charge_power_w" if bit != ACTION_DISCHARGE else "discharge_power_w"

    manager.record_runtime_decision(
        {"action_mask": bit}, duration_s=duration_s, **{power_key: 12.0}
    )

    snapshot = manager.build_public_snapshot()
    assert snapshot["operations"]["actual_action_mask"][40] == 0
    assert snapshot["operations"]["actual_deadband_hidden_mask"][40] == bit
    assert snapshot["counts"]["by_action"][name] == 0
    assert snapshot["counts"]["raw_by_action"][name] == 1


@pytest.mark.parametrize(("bit", "name", "field", "tiny", "real"), COLOURS)
def test_a_real_flow_above_the_deadband_still_colours_the_quarter(
    bit, name, field, tiny, real
):
    """The rule must not turn a real charge or discharge grey."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    duration_s = real * 3_600_000.0 / 500.0
    power_key = "charge_power_w" if bit != ACTION_DISCHARGE else "discharge_power_w"

    manager.record_runtime_decision(
        {"action_mask": bit}, duration_s=duration_s, **{power_key: 500.0}
    )

    snapshot = manager.build_public_snapshot()
    assert snapshot["operations"]["actual_action_mask"][40] == bit
    assert snapshot["operations"]["actual_deadband_hidden_mask"][40] == 0
    assert snapshot["counts"]["by_action"][name] == 1
    assert snapshot["counts"]["below_deadband_cells"] == 0


@pytest.mark.parametrize(("bit", "name", "field", "tiny", "real"), COLOURS)
def test_a_decision_without_a_mask_is_derived_by_the_same_threshold(
    bit, name, field, tiny, real
):
    """The value-derived path (no supplied mask) uses the one threshold too."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    # No action_mask anywhere: the bit can only come from the flow.
    manager.record_runtime_decision({field: tiny}, duration_s=60)

    assert manager._cells[40].actual_action_mask & bit == 0
    assert _operations(manager)["actual_action_mask"][40] & bit == 0

    other = _manager(MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID)))
    other.record_runtime_decision({field: real}, duration_s=60)

    assert other._cells[40].actual_action_mask & bit == bit
    assert _operations(other)["actual_action_mask"][40] & bit == bit


def test_a_real_discharge_beside_a_negligible_solar_charge_shows_only_blue():
    """A combination may not keep a second colour the energy does not support."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    manager.record_runtime_decision(
        {
            "action_mask": ACTION_SOLAR_CHARGE | ACTION_DISCHARGE,
            "solar_to_battery_kwh": 0.0001,
            "battery_to_home_kwh": 0.4,
        },
        duration_s=60,
        simultaneous=True,
    )

    operations = _operations(manager)
    assert operations["actual_action_mask"][40] == ACTION_DISCHARGE
    assert operations["actual_deadband_hidden_mask"][40] == ACTION_SOLAR_CHARGE
    # Coexistence can never claim a colour the cell no longer has.
    assert operations["actual_coexistence_mask"][40] == ACTION_DISCHARGE


# ----------------------------------------------------------------------
# 3. The projected sister site has to pass the same gate
# ----------------------------------------------------------------------

@pytest.mark.parametrize(("bit", "name", "field", "tiny", "real"), COLOURS)
def test_a_projected_trickle_does_not_colour_a_future_quarter(
    bit, name, field, tiny, real
):
    """The plan names its own mask; it may not bypass the dead-band either."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    manager.rebuild_future_projection(
        [{"index": 41, "action_mask": bit, field: tiny}], mode="dynamic_pricing"
    )

    snapshot = manager.build_public_snapshot()
    assert snapshot["operations"]["planned_action_mask"][41] == 0
    assert snapshot["operations"]["planned_deadband_hidden_mask"][41] == bit
    assert snapshot["counts"]["by_action"][name] == 0
    assert snapshot["counts"]["raw_by_action"][name] == 1

    other = _manager(MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID)))
    other.rebuild_future_projection(
        [{"index": 41, "action_mask": bit, field: real}], mode="dynamic_pricing"
    )

    snapshot = other.build_public_snapshot()
    assert snapshot["operations"]["planned_action_mask"][41] == bit
    assert snapshot["operations"]["planned_deadband_hidden_mask"][41] == 0


@pytest.mark.parametrize(("bit", "name", "field", "tiny", "real"), COLOURS)
def test_the_pricing_projection_composes_the_same_deadband(
    bit, name, field, tiny, real
):
    """The pure projection layer grants a bit on the same one threshold."""
    assert masked_by_energy_deadband(**{name: tiny}) == 0
    assert masked_by_energy_deadband(**{name: real}) == bit
    # Exactly at the dead-band is still "nothing": the comparison is strict.
    assert masked_by_energy_deadband(**{name: ACTION_ENERGY_DEADBAND_KWH}) == 0
    assert masked_by_energy_deadband(
        **{name: ACTION_ENERGY_DEADBAND_KWH + 1e-6}
    ) == bit


# ----------------------------------------------------------------------
# 4. Only the display changed: control and planning are untouched
# ----------------------------------------------------------------------

def test_the_control_decision_keeps_stating_what_the_graph_may_not_show():
    """The dead-band is a display rule; the controller's own mask is unchanged."""
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"battery_power": 12},
    )
    controller = _runtime_controller([coordinator], grid_active=True)
    now = datetime(2026, 8, 23, 10, 7, tzinfo=MADRID)

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, now
    )

    # The control loop still classifies 12 W of grid charging as a grid charge.
    assert decision["action_mask"] == ACTION_GRID_CHARGE
    assert decision["charge_power_w"] == pytest.approx(12.0)
    assert decision["grid_charge_decision"] == "scheduled"

    manager = _manager(MutableClock(now))
    manager.record_runtime_decision(decision, at=now, duration_s=60.0)

    snapshot = manager.build_public_snapshot()
    # The diary keeps the decision verbatim...
    assert manager._cells[40].actual_action_mask == ACTION_GRID_CHARGE
    assert snapshot["operations"]["actual_charge_power_w"][40] == pytest.approx(12.0)
    # ...and only the graph withholds the colour for 0.0002 kWh.
    assert snapshot["operations"]["actual_action_mask"][40] == 0
    assert (
        snapshot["operations"]["actual_deadband_hidden_mask"][40]
        == ACTION_GRID_CHARGE
    )


def test_the_projected_energy_is_published_even_when_its_colour_is_not():
    """Hiding a colour must never hide the kWh the plan is built on."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    manager.rebuild_future_projection(
        [
            {
                "index": 41,
                "action_mask": ACTION_GRID_CHARGE,
                "grid_to_battery_kwh": 0.0009,
                "soc_end_pct": 51.0,
            }
        ],
        mode="dynamic_pricing",
    )

    operations = _operations(manager)
    assert operations["grid_to_battery_kwh"][41] == pytest.approx(0.0009)
    assert operations["planned_action_mask"][41] == 0
    assert operations["planned_soc_pct"][41] == pytest.approx(51.0)


# ----------------------------------------------------------------------
# 5. Diagnostics keep the hidden energy visible
# ----------------------------------------------------------------------

def test_diagnostics_count_the_measured_action_behind_a_hidden_colour():
    """``raw_by_action``/``below_deadband_cells`` expose what the graph hides."""
    clock = MutableClock(datetime(2026, 8, 23, 3, 15, tzinfo=MADRID))
    manager = _manager(clock)
    manager.record_runtime_decision(
        {"action_mask": ACTION_GRID_CHARGE},
        duration_s=60.0,
        charge_power_w=56.64,
    )

    controller = SimpleNamespace(_daily_operation_timeline=manager)
    summary = diagnostics._daily_operation_timeline_summary(controller)

    assert summary["counts"]["by_action"]["grid_charge"] == 0
    assert summary["counts"]["raw_by_action"]["grid_charge"] == 1
    assert summary["counts"]["below_deadband_cells"] == 1


def test_the_shared_threshold_is_the_only_number_involved():
    """One definition, in const, used by both layers."""
    from custom_components.omnibattery.pricing import daily_timeline as pricing_timeline

    assert ACTION_ENERGY_DEADBAND_KWH == 0.01
    assert pricing_timeline.ACTION_ENERGY_DEADBAND_KWH is ACTION_ENERGY_DEADBAND_KWH
    assert "masked_by_energy_deadband" in pricing_timeline.__all__
    # The pricing list maps each of the three bits to its own energy series.
    assert {bit for bit, _keyword, _field in pricing_timeline._ACTION_ENERGY_KEYS} == {
        ACTION_SOLAR_CHARGE,
        ACTION_GRID_CHARGE,
        ACTION_DISCHARGE,
    }
    # The tracking sister list covers the same three bits.
    from custom_components.omnibattery.tracking import daily_timeline as tracking_timeline

    assert {
        bit for bit, _keys in tracking_timeline._ACTION_ENERGY_KEYS
    } == {ACTION_SOLAR_CHARGE, ACTION_GRID_CHARGE, ACTION_DISCHARGE}
    assert (
        tracking_timeline._ACTION_ENERGY_DEADBAND_KWH is ACTION_ENERGY_DEADBAND_KWH
    )
