"""Snapshots of ``entry.data["batteries"][i]`` produced by the REAL config flows.

Characterisation tests for the driver-registry refactor (plan T1.2, part 1/2):
for each brand, the stored battery dict that comes out of

  a) the config flow, add path (``user`` -> ... -> entry created),
  b) the options flow, editing the battery in slot 0,
  c) the config flow, reconfigure path,

is compared with a JSON file in ``tests/snapshots/config_entry/<path>_<case>.json``.
The shape is frozen by plan section 1 (restriction 2): the refactor must not change it.
Today's behaviour is pinned as is, including oddities (listed in the plan's section 8).

The step ids visited are asserted too: they are the other frozen contract
(restriction 3: strings.json and the translations key on them).

Run with the HA plugin (the plain ``pytest`` skips these)::

    .venv-test/Scripts/python -m pytest -q -o addopts="" tests/test_config_entry_snapshots.py

Regenerate (and then review the diff by eye)::

    OMNIBATTERY_UPDATE_SNAPSHOTS=1 .venv-test/Scripts/python -m pytest -q -o addopts="" tests/test_config_entry_snapshots.py
"""
from __future__ import annotations

import pytest

from tests.flow_harness import (  # noqa: F401  (fixtures are used by name)
    CASES,
    BrandCase,
    assert_snapshot,
    flow_env,
    probes,
)

CASE_IDS = list(CASES)


def _add_steps(case: BrandCase) -> list[str]:
    return [
        "user", "batteries", "battery_brand", case.connection_step,
        *case.extra_add_steps, "battery_limits",
        "time_slots", "excluded_devices", "predictive_charging",
    ]


def _options_steps(case: BrandCase) -> list[str]:
    return [
        "init", "battery_count", "battery_brand", case.connection_step,
        *case.extra_add_steps, "battery_limits",
    ]


def _reconfigure_steps(case: BrandCase) -> list[str]:
    return [case.reconfigure_step, *case.extra_reconfigure_steps]


@pytest.mark.parametrize("case_id", CASE_IDS)
async def test_add_via_config_flow(flow_env, case_id):
    """CF add: the battery as stored after the whole wizard with default limits."""
    case = CASES[case_id]
    run = await flow_env.add_via_config_flow(case)

    assert run.steps == _add_steps(case)
    assert len(run.batteries) == 1
    assert_snapshot(f"add_{case_id}", run.batteries[0])


@pytest.mark.parametrize("case_id", CASE_IDS)
async def test_edit_via_options_flow(flow_env, case_id):
    """OF: edit slot 0 of an entry that already holds a battery of this brand."""
    case = CASES[case_id]
    run = await flow_env.edit_via_options_flow(case)

    # `batteries` has fewer than two stored, so it goes straight to battery_count
    # (the menu step is visited as "init" only).
    assert run.steps == _options_steps(case)
    assert len(run.batteries) == 1
    assert_snapshot(f"options_{case_id}", run.batteries[0])


@pytest.mark.parametrize("case_id", CASE_IDS)
async def test_reconfigure_battery(flow_env, case_id):
    """CF reconfigure: connection fields rewritten, the rest of the stored battery kept."""
    case = CASES[case_id]
    run = await flow_env.reconfigure(case)

    assert run.steps == _reconfigure_steps(case)
    assert len(run.batteries) == 1
    assert_snapshot(f"reconfigure_{case_id}", run.batteries[0])
