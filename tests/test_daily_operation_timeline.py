"""Unit tests for the pure daily operation timeline contract."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.daily_timeline import (
    ACTION_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_SOLAR_CHARGE,
    CONTEXT_CHARGE_DELAY,
    CONTEXT_DYNAMIC_PRICE,
    CONTEXT_HOURLY_BALANCE,
    CONTEXT_SETPOINT,
    DST_REPEATED,
    DST_SKIPPED,
    GRID_CHARGE_NOT_NEEDED,
    GRID_CHARGE_SCHEDULED,
    GRID_CHARGE_UNKNOWN,
    STATE_CURRENT,
    BatteryProjectionInput,
    ProjectedIntervalFlow,
    ProjectionIntervalInput,
    build_daily_operation_snapshot,
    build_local_grid,
    compose_action_mask,
    compose_context_mask,
    json_safe,
    project_charge_delay,
    simulate_battery_projection,
)

UTC = timezone.utc
MADRID = ZoneInfo("Europe/Madrid")
BASE = datetime(2026, 8, 23, tzinfo=UTC)


def _interval(
    index: int,
    *,
    consumption: float = 0.0,
    solar: float = 0.0,
    state: str = "future",
    coverage_seconds: float | None = None,
) -> ProjectionIntervalInput:
    start = BASE + timedelta(minutes=index * 15)
    return ProjectionIntervalInput(
        start=start,
        end=start + timedelta(minutes=15),
        consumption_kwh=consumption,
        solar_kwh=solar,
        state=state,
        coverage_seconds=coverage_seconds,
    )


def _battery(
    *,
    stored: float = 2.0,
    capacity: float = 10.0,
    min_soc: float = 0.0,
    max_soc: float = 100.0,
    charge_power: float = 4000.0,
    discharge_power: float = 4000.0,
) -> BatteryProjectionInput:
    return BatteryProjectionInput(
        key="battery-a",
        stored_kwh=stored,
        capacity_kwh=capacity,
        min_soc_pct=min_soc,
        max_soc_pct=max_soc,
        charge_power_w=charge_power,
        discharge_power_w=discharge_power,
    )


def test_normal_day_has_96_wall_clock_indices_and_current_interval():
    now = datetime(2026, 8, 23, 12, 37, tzinfo=MADRID)
    grid = build_local_grid(now.date(), MADRID, now=now)

    assert len(grid) == 96
    assert [grid[index].label for index in (0, 1, 50, 95)] == [
        "00:00",
        "00:15",
        "12:30",
        "23:45",
    ]
    assert grid[50].state == STATE_CURRENT
    assert grid[49].state == "past"
    assert grid[51].state == "future"
    assert all(item.duration_seconds == 900 for item in grid)


def test_partial_current_interval_is_kept_as_real_coverage():
    now = datetime(2026, 8, 23, 12, 37, tzinfo=MADRID)
    grid = build_local_grid(now.date(), MADRID, now=now)
    flows = [None] * 96
    flows[50] = ProjectedIntervalFlow(
        start=grid[50].start,
        end=grid[50].end,
        solar_kwh=0.12,
        consumption_kwh=0.31,
        solar_to_battery_kwh=0.0,
        grid_to_battery_kwh=0.0,
        battery_to_home_kwh=0.0,
        grid_to_home_kwh=0.19,
        stored_energy_end_kwh=2.0,
        coverage_seconds=7 * 60,
        projected=False,
    )

    snapshot = build_daily_operation_snapshot(
        grid,
        flows,
        current_progress=7 / 15,
    )
    payload = snapshot.to_dict()

    assert payload["interval_count"] == 96
    assert payload["current_index"] == 50
    assert payload["current_progress"] == pytest.approx(7 / 15)
    assert payload["series"]["actual_coverage_s"][50] == 420
    assert payload["series"]["consumption_actual_kwh"][50] == pytest.approx(0.31)
    assert payload["series"]["consumption_actual_kwh"][51] is None


@pytest.mark.parametrize(
    ("local_date", "expected_duration", "dst_status", "expected_cells"),
    [
        (datetime(2026, 3, 29, tzinfo=MADRID), 23 * 3600, DST_SKIPPED, 4),
        (datetime(2026, 10, 25, tzinfo=MADRID), 25 * 3600, DST_REPEATED, 4),
    ],
)
def test_dst_keeps_96_cells_without_inventing_physical_intervals(
    local_date: datetime,
    expected_duration: int,
    dst_status: str,
    expected_cells: int,
):
    grid = build_local_grid(local_date.date(), MADRID, now=local_date)

    assert len(grid) == 96
    assert sum(item.duration_seconds for item in grid) == expected_duration
    assert sum(item.dst_status == dst_status for item in grid) == expected_cells
    if dst_status == DST_SKIPPED:
        assert all(
            item.start is None and item.end is None for item in grid if item.dst_skipped
        )
        assert all(item.duration_seconds == 0 for item in grid if item.dst_skipped)
    else:
        repeated = [item for item in grid if item.dst_repeated]
        assert all(len(item.occurrences) == 2 for item in repeated)
        assert all(item.duration_seconds == 1800 for item in repeated)


def test_repeated_wall_cell_aggregates_energy_and_masks():
    local_date = datetime(2026, 10, 25, tzinfo=MADRID)
    grid = build_local_grid(local_date.date(), MADRID, now=local_date)
    occurrences = [occurrence for cell in grid for occurrence in cell.occurrences]
    occurrences.sort(key=lambda item: item[0].timestamp())
    flows = [
        ProjectedIntervalFlow(
            start=start,
            end=end,
            solar_kwh=0.25,
            consumption_kwh=0.1,
            solar_to_battery_kwh=0.15,
            grid_to_battery_kwh=0.0,
            battery_to_home_kwh=0.0,
            grid_to_home_kwh=0.0,
            stored_energy_end_kwh=1.0,
            action_mask=ACTION_SOLAR_CHARGE,
            grid_charge_decision=GRID_CHARGE_NOT_NEEDED,
        )
        for start, end in occurrences
    ]
    # Make the second physical occurrence of the first repeated cell a
    # grid-charge example.
    repeated_index = next(item.index for item in grid if item.dst_repeated)
    repeated_occurrence_positions = [
        index
        for index, occurrence in enumerate(occurrences)
        if occurrence in grid[repeated_index].occurrences
    ]
    second_occurrence = repeated_occurrence_positions[1]
    flows[second_occurrence] = ProjectedIntervalFlow(
        start=flows[second_occurrence].start,
        end=flows[second_occurrence].end,
        solar_kwh=0.25,
        consumption_kwh=0.1,
        solar_to_battery_kwh=0.0,
        grid_to_battery_kwh=0.15,
        battery_to_home_kwh=0.0,
        grid_to_home_kwh=0.0,
        stored_energy_end_kwh=1.1,
        action_mask=ACTION_GRID_CHARGE,
        grid_charge_decision=GRID_CHARGE_SCHEDULED,
    )

    snapshot = build_daily_operation_snapshot(grid, flows)
    cell = snapshot.intervals[repeated_index]
    assert cell.flow is not None
    assert cell.flow.solar_kwh == pytest.approx(0.5)
    assert cell.flow.action_mask == ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE
    assert cell.flow.grid_charge_decision == GRID_CHARGE_SCHEDULED
    assert cell.flow.duration_seconds == 1800


def test_masks_are_composable_and_grid_decision_is_independent():
    action_mask = compose_action_mask(
        ACTION_SOLAR_CHARGE,
        ACTION_GRID_CHARGE,
        ACTION_DISCHARGE,
    )
    context_mask = compose_context_mask(
        CONTEXT_SETPOINT,
        CONTEXT_CHARGE_DELAY,
        CONTEXT_DYNAMIC_PRICE,
    )
    flow = ProjectedIntervalFlow(
        BASE,
        BASE + timedelta(minutes=15),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        action_mask=action_mask,
        context_mask=context_mask,
        grid_charge_decision=GRID_CHARGE_NOT_NEEDED,
    )

    assert flow.action_mask == 7
    assert flow.context_mask == 7
    assert flow.actions == ("solar_charge", "grid_charge", "discharge")
    assert flow.grid_charge_decision == GRID_CHARGE_NOT_NEEDED
    assert (
        ProjectedIntervalFlow(
            BASE,
            BASE + timedelta(minutes=15),
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            grid_charge_decision="bad-value",
        ).grid_charge_decision
        == GRID_CHARGE_UNKNOWN
    )


def test_hourly_balance_context_alias_is_composable():
    assert compose_context_mask("hourly_net_balance") == CONTEXT_HOURLY_BALANCE
    assert compose_context_mask("net_balance") == CONTEXT_HOURLY_BALANCE


def test_simulation_preserves_energy_balance_and_efficiency():
    intervals = [
        _interval(0, consumption=1.0, solar=3.0),
        _interval(1, consumption=3.0, solar=0.0),
    ]
    result = simulate_battery_projection(
        intervals,
        [_battery(stored=2.0)],
        charge_efficiency=0.8,
        discharge_efficiency=1.0,
    )

    first, second = result.intervals
    assert first.solar_to_battery_kwh == pytest.approx(1.0)
    assert first.stored_energy_end_kwh == pytest.approx(2.8)
    assert second.battery_to_home_kwh == pytest.approx(1.0)
    assert second.grid_to_home_kwh == pytest.approx(2.0)
    assert second.stored_energy_end_kwh == pytest.approx(1.8)
    for flow in result.intervals:
        assert flow.solar_to_battery_kwh <= flow.solar_kwh + 1e-9
        assert (
            flow.battery_to_home_kwh + flow.grid_to_home_kwh
            <= flow.consumption_kwh + 1e-9
        )
        assert flow.stored_energy_end_kwh >= -1e-9
        assert flow.stored_energy_end_kwh == pytest.approx(
            (flow.stored_energy_start_kwh or 0.0)
            + (flow.stored_energy_charged_kwh or 0.0)
            - (flow.stored_energy_discharged_kwh or 0.0)
        )


def test_global_allocation_is_capped_once_and_soc_power_limits_apply():
    result = simulate_battery_projection(
        [_interval(0)],
        [
            _battery(stored=0.0),
            BatteryProjectionInput("battery-b", 0.0, 10.0, 0, 100, 4000, 4000),
        ],
        allocations=[1.5],
        charge_efficiency=1.0,
    )
    flow = result.intervals[0]
    assert flow.grid_to_battery_kwh == pytest.approx(1.5)
    assert flow.action_mask == ACTION_GRID_CHARGE
    assert sum(result.final_stored_kwh_by_battery.values()) == pytest.approx(1.5)

    limited = simulate_battery_projection(
        [_interval(0)],
        [_battery(stored=7.0, max_soc=80.0)],
        allocations=[5.0],
        charge_efficiency=0.85,
    ).intervals[0]
    assert limited.grid_to_battery_kwh == pytest.approx(1.0)
    assert limited.stored_energy_end_kwh <= 8.0 + 1e-9


def test_same_battery_never_charges_and_discharges_in_one_interval():
    result = simulate_battery_projection(
        [_interval(0, consumption=0.5)],
        [_battery(stored=5.0)],
        allocations=[0.5],
        charge_efficiency=1.0,
    )

    flow = result.intervals[0]
    battery_flow = result.battery_flows["battery-a"][0]
    assert flow.grid_to_battery_kwh == pytest.approx(0.5)
    assert flow.battery_to_home_kwh == 0.0
    assert flow.action_mask == ACTION_GRID_CHARGE
    assert battery_flow.grid_to_battery_kwh == pytest.approx(0.5)
    assert battery_flow.battery_to_home_kwh == 0.0


def test_system_power_limits_cap_aggregate_charge_and_discharge():
    batteries = [
        _battery(stored=2.0),
        BatteryProjectionInput("battery-b", 2.0, 10.0, 0, 100, 4000, 4000),
    ]
    result = simulate_battery_projection(
        [_interval(0), _interval(1, consumption=2.0)],
        batteries,
        allocations=[2.0, 0.0],
        charge_efficiency=1.0,
        system_charge_power_w=2000.0,
        system_discharge_power_w=2000.0,
    )

    charge, discharge = result.intervals
    assert charge.grid_to_battery_kwh == pytest.approx(0.5)
    assert charge.charge_power_w == pytest.approx(2000.0)
    assert discharge.battery_to_home_kwh == pytest.approx(0.5)
    assert discharge.discharge_power_w == pytest.approx(2000.0)
    assert discharge.grid_to_home_kwh == pytest.approx(1.5)


def test_charge_availability_blocks_and_prorates_interval_charging():
    intervals = [
        _interval(0, consumption=0.1, solar=1.1),
        _interval(1, consumption=0.1, solar=1.1),
        _interval(2, consumption=0.1, solar=1.1),
    ]
    result = simulate_battery_projection(
        intervals,
        [_battery(stored=2.0)],
        charge_efficiency=1.0,
        charge_availability=[0.0, 0.5, 1.0],
    )

    blocked, partial, available = result.intervals
    assert blocked.solar_to_battery_kwh == 0.0
    assert blocked.action_mask & ACTION_SOLAR_CHARGE == 0
    assert partial.solar_to_battery_kwh == pytest.approx(0.5)
    assert available.solar_to_battery_kwh == pytest.approx(1.0)
    assert available.stored_energy_end_kwh == pytest.approx(3.5)


def test_charge_delay_projection_is_pure_and_does_not_mutate_inputs():
    battery = _battery(stored=0.0, capacity=2.0, charge_power=4000.0)
    intervals = [
        _interval(0),
        _interval(1, consumption=1.0),
    ]
    projection = project_charge_delay(
        intervals,
        [battery],
        setpoint_soc_pct=50.0,
        allocations=[1.0, 0.0],
        charge_efficiency=1.0,
        now=BASE,
    )

    assert projection.setpoint_reached_at == intervals[0].end
    assert projection.delay_starts_at == intervals[0].end
    assert projection.estimated_unlock_at == intervals[1].start
    assert battery.stored_kwh == 0.0
    assert intervals[0].consumption_kwh == 0.0


def test_snapshot_serialization_replaces_non_finite_values_and_is_json_safe():
    grid = build_local_grid(BASE.date(), UTC, now=BASE)
    flow = ProjectedIntervalFlow(
        grid[0].start,
        grid[0].end,
        float("nan"),
        float("inf"),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        coverage_seconds=float("nan"),
    )
    snapshot = build_daily_operation_snapshot(grid, {0: flow})
    payload = snapshot.to_dict()
    encoded = snapshot.to_json()

    assert payload["interval_count"] == 96
    assert payload["intervals"][0]["flow"]["solar_kwh"] is None
    assert payload["intervals"][0]["flow"]["consumption_kwh"] is None
    assert json.loads(encoded)["interval_count"] == 96
    assert json_safe({"bad": float("-inf")}) == {"bad": None}
