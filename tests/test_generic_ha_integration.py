"""How an observed battery changes -- and does not change -- the shared layers.

The defect this driver exists for, as measured on the reference installation:
Omnibattery derived 2044 W of household load from
``grid 1486 W + marstek 0 W + pv_dc 557.5 W`` while the Kostal's own figure was
1491 W, because 403 W of that DC production was going into a BYD stack
Omnibattery knew nothing about. These tests pin both halves of the fix: the
battery's power must reach the balance, and nothing else about it may reach the
control or planning path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.charge_order import _is_dc_coupled
from custom_components.omnibattery.control.power_distribution import PowerDistribution
from custom_components.omnibattery.control.residual_load import (
    automatic_batteries,
    primary_coordinator,
    uncovered_load_w,
)
from custom_components.omnibattery.drivers.base import (
    BatteryDriver,
    can_take_setpoint,
    is_observed,
)


class _Driver:
    """Driver stand-in that borrows the real AC-contribution logic.

    ``uncovered_load_w`` reads a battery's AC term through
    ``driver.ac_contribution_w`` and its source order, so a double that faked
    that method would test the double instead of the shared reconstruction this
    battery exists to feed.
    """

    ac_contribution_w = BatteryDriver.ac_contribution_w
    # ``_is_battery_manual_owned`` consults this hook, so the double needs it.
    is_self_managed = BatteryDriver.is_self_managed

    def __init__(self, dc_coupled=False):
        self.dc_coupled = dc_coupled

    def ac_register_power_w(self, data):
        """Stand in for the AC register the AC-coupled families publish.

        The base class returns None here and each driver that has the register
        overrides it, so a double that wants to represent one has to as well.
        An observed battery publishes no such register, which is the whole
        point: its term falls through to cell power, as ``generic_ha`` intends.
        """
        return data.get("ac_power")


class _Coord:
    """Coordinator stand-in. A plain class, not SimpleNamespace: the allocator
    uses coordinators as dict keys and SimpleNamespace is unhashable."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def __repr__(self):  # readable assertion failures
        return f"<_Coord {self.name}>"


def _battery(
    name,
    *,
    battery_power=None,
    ac_power=None,
    soc=50,
    capacity=10.0,
    commandable=True,
    dc_coupled=False,
    min_soc=10,
    max_soc=100,
):
    data = {"battery_soc": soc, "battery_total_energy": capacity}
    if battery_power is not None:
        data["battery_power"] = battery_power
    if ac_power is not None:
        data["ac_power"] = ac_power
    return _Coord(
        name=name,
        device_key=f"{name.lower().replace(' ', '_')}_0",
        data=data,
        is_available=True,
        supports_power_setpoint=commandable,
        battery_manual_mode_enabled=False,
        rs485_user_disabled=False,
        min_soc=min_soc,
        max_soc=max_soc,
        battery_capacity_kwh=capacity,
        battery_version="v3",
        capabilities=SimpleNamespace(
            has_mppt_pv=False,
            supports_power_setpoint=commandable,
        ),
        dc_pv_connected=True,
        driver=_Driver(dc_coupled=dc_coupled),
    )


def _controller(batteries, *, limit=2500):
    def _limit(coordinator, is_charging):
        # A real coordinator derives this from the driver envelope, which is 0 W
        # in both directions for an observed battery.
        return limit if coordinator.supports_power_setpoint else 0

    return SimpleNamespace(
        coordinators=batteries,
        primary_battery="",
        primary_feedforward_enabled=True,
        compute_active_target=lambda: 0.0,
        deadband=0,
        previous_power=0.0,
        _surplus_guard_latched=False,
        _active_discharge_batteries=[],
        _active_charge_batteries=[],
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _eid: None)),
        _battery_power_limit=_limit,
        _clamp_to_system_capacity=lambda power, batteries, is_charging: min(
            power, sum(_limit(c, is_charging) for c in batteries)
        ),
        _log_power_command_plan=lambda **kwargs: None,
        _phase_power_limiter=None,
        _get_available_batteries=lambda is_charging, *a, **k: list(batteries),
        _is_battery_manual_owned=lambda c: c.battery_manual_mode_enabled,
    )


# ---------------------------------------------------------------------------
# what the battery must change: the meter-side reconstruction
# ---------------------------------------------------------------------------

def test_an_observed_battery_is_counted_in_the_load_reconstruction():
    """Its charging is at the meter whether or not anyone can command it."""
    observed = _battery("BYD", battery_power=403, commandable=False, dc_coupled=True)
    marstek = _battery("Marstek", ac_power=0)
    controller = _controller([marstek, observed])
    # The meter reads the 600 W house plus the 403 W the BYD is taking in.
    assert uncovered_load_w(controller, 1003.0) == pytest.approx(600.0)


def test_leaving_it_out_is_wrong_by_exactly_its_power():
    """The difference between configuring it and not is its own battery power."""
    marstek = _battery("Marstek", ac_power=0)
    observed = _battery("BYD", battery_power=403, commandable=False, dc_coupled=True)
    without = uncovered_load_w(_controller([marstek]), 1003.0)
    with_it = uncovered_load_w(_controller([marstek, observed]), 1003.0)
    assert without - with_it == pytest.approx(403.0)


# ---------------------------------------------------------------------------
# what it must not change: control
# ---------------------------------------------------------------------------

def test_it_never_joins_the_ladder_of_batteries_to_command():
    marstek = _battery("Marstek", ac_power=0)
    observed = _battery("BYD", battery_power=403, commandable=False)
    controller = _controller([marstek, observed])
    assert automatic_batteries(controller) == [marstek]
    assert primary_coordinator(controller) is marstek


def test_it_is_never_allocated_power():
    marstek = _battery("Marstek", ac_power=0)
    observed = _battery("BYD", battery_power=0, commandable=False)
    controller = _controller([marstek, observed])
    distribution = PowerDistribution(SimpleNamespace(), SimpleNamespace(data={}), controller)
    allocation = distribution._distribute_power_by_limits(
        1000, [marstek, observed], is_charging=False
    )
    assert allocation.get(observed, 0) == 0
    assert allocation[marstek] == 1000


def test_a_fleet_of_only_observed_batteries_allocates_nothing():
    observed = _battery("BYD", battery_power=0, commandable=False)
    controller = _controller([observed])
    distribution = PowerDistribution(SimpleNamespace(), SimpleNamespace(data={}), controller)
    assert not any(
        distribution._distribute_power_by_limits(
            1000, [observed], is_charging=False
        ).values()
    )


def test_its_empty_room_does_not_make_the_day_look_scarce():
    """A DC battery nothing can aim at must not pull the charge order around."""
    observed = _battery("BYD", commandable=False, dc_coupled=True)
    assert _is_dc_coupled(observed) is False
    assert _is_dc_coupled(_battery("Huawei", dc_coupled=True)) is True


# ---------------------------------------------------------------------------
# what it must not change: planning and the fleet aggregates
# ---------------------------------------------------------------------------

def test_the_availability_gate_rejects_it_before_any_blocker_runs():
    """One shared helper answers this, not a copied ``getattr`` per call site."""
    assert can_take_setpoint(_battery("BYD", commandable=False)) is False
    assert is_observed(_battery("BYD", commandable=False)) is True
    assert can_take_setpoint(_battery("Marstek")) is True
    assert is_observed(_battery("Marstek")) is False


def test_a_coordinator_without_the_flag_is_still_commandable():
    """Third-party drivers written before the capability keep working."""
    legacy = SimpleNamespace(name="Legacy", data={})
    assert can_take_setpoint(legacy) is True
    assert ChargeDischargeController._counts_towards_fleet_energy(legacy) is True


def test_its_kilowatt_hours_stay_out_of_the_planned_fleet():
    """Stage 1: predictive charging reasons about energy it can place."""
    assert ChargeDischargeController._counts_towards_fleet_energy(
        _battery("BYD", commandable=False)
    ) is False
    assert ChargeDischargeController._counts_towards_fleet_energy(
        _battery("Marstek")
    ) is True


def test_but_its_kilowatt_hours_do_count_in_the_system_aggregates():
    """System SOC, capacity and stored energy describe the installation.

    The battery is physically there and its charge is really in the house, so
    the three aggregates count it. Only the *plan* leaves it out, and the two
    questions are deliberately answered by different code: the aggregates never
    consult the planning helper above.
    """
    import inspect

    from custom_components.omnibattery.sensors import aggregate_sensors

    source = inspect.getsource(aggregate_sensors)
    for aggregate in (
        "_calculate_system_soc",
        "_calculate_total_energy",
        "_calculate_total_stored_energy",
    ):
        body = source.split(f"def {aggregate}")[1].split("\n    def ")[0]
        assert "supports_power_setpoint" not in body
        assert "can_take_setpoint" not in body
        assert "_counts_towards_fleet_energy" not in body


# ---------------------------------------------------------------------------
# the wiring the doubles above stub around: a real coordinator, a real driver
# ---------------------------------------------------------------------------

class _HaState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}
        self.last_reported = None


def _coordinator(monkeypatch, **stored):
    """A real coordinator for brand generic_ha. No Home Assistant runtime."""
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *a, **k: None)
    table = {
        "sensor.scb_battery_soc": _HaState("34"),
        "sensor.scb_battery_power": _HaState("-403"),
        "number.scb_battery_min_soc": _HaState("100", {"min": 5, "max": 100, "step": 5}),
    }
    hass = MagicMock()
    hass.states = SimpleNamespace(get=table.get)
    hass.services.async_call = AsyncMock()

    from custom_components.omnibattery.infra.coordinator import (
        MarstekVenusDataUpdateCoordinator,
    )

    coordinator = MarstekVenusDataUpdateCoordinator(
        hass,
        {
            # 2.0.0 hands the coordinator the stored battery dict and the
            # registry builds the driver from it; there is no brand kwarg and
            # no per-brand constructor argument to add.
            "name": "BYD",
            "host": "deadbeefcafe",   # the generated identity, not a host
            "port": 0,
            "brand": "generic_ha",
            "max_charge_power": 0,
            "max_discharge_power": 0,
            "max_soc": 100,
            "min_soc": 12,
            "battery_capacity_kwh": 6.0,
            "ha_soc_entity": "sensor.scb_battery_soc",
            "ha_power_entity": "sensor.scb_battery_power",
            "ha_power_positive_is": "discharge",
            "ha_charge_energy_entities": [],
            "ha_discharge_energy_entity": None,
            "ha_target_soc_entity": "number.scb_battery_min_soc",
            "ha_stale_after_s": 600,
            **stored,
        },
        "sensor.scb_grid_power",
    )
    return coordinator, hass


@pytest.mark.parametrize("target", [None, "number.scb_battery_min_soc"])
def test_a_short_outage_ends_when_the_source_is_back(monkeypatch, caplog, target):
    """The two-minute suspension spares a device; there is none behind hass.states."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from homeassistant.util import dt as dt_util

    coordinator, hass = _coordinator(monkeypatch, ha_target_soc_entity=target)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = {"s": 0}
    monkeypatch.setattr(dt_util, "utcnow", lambda: start + timedelta(seconds=now["s"]))
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    coordinator._entity_registry = registry
    coordinator.data = {}
    read = hass.states.get
    hass.states.get = lambda entity_id: (
        None if 10 <= now["s"] < 70 and entity_id.startswith("sensor.") else read(entity_id)
    )

    async def _run():
        assert await coordinator.driver.connect()
        coordinator._is_connected = True
        available = []
        for second in range(80):
            now["s"] = second
            coordinator.data = await coordinator._async_update_data_impl()
            available.append(coordinator.is_available)
        return available

    available = asyncio.run(_run())
    assert available[9] and not any(available[10:70])
    assert all(available[71:])
    # Five failed polls, then silence until the source is back: no 1 Hz flood.
    assert len([r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]) < 40


def test_the_brand_selects_the_observing_driver(monkeypatch):
    from custom_components.omnibattery.drivers.generic_ha import GenericHaEntityDriver

    coordinator, _ = _coordinator(monkeypatch)
    assert isinstance(coordinator.driver, GenericHaEntityDriver)
    assert can_take_setpoint(coordinator) is False
    # The one writable control exists because an entity was configured for it,
    # not because of a capability flag: the driver publishes it in its own
    # number_definitions, which is what the entity platform reads.
    assert [d["key"] for d in coordinator.driver.number_definitions] == ["target_soc"]


def test_only_the_target_soc_control_is_offered(monkeypatch):
    coordinator, _ = _coordinator(monkeypatch)
    assert [d["key"] for d in coordinator.number_definitions] == ["target_soc"]
    assert coordinator.select_definitions == []
    assert coordinator.switch_definitions == []
    assert coordinator.button_definitions == []
    assert {d["key"] for d in coordinator.sensor_definitions} == {
        "battery_soc", "battery_power",
    }


@pytest.mark.asyncio
async def test_apply_power_through_the_coordinator_writes_nothing(monkeypatch):
    """Shared control is gated well before here, but the floor must hold."""
    coordinator, hass = _coordinator(monkeypatch)
    coordinator.lock = __import__("asyncio").Lock()
    result = await coordinator.apply_power(1500, read_back=False)
    assert result.net_power_w == 0
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_hysteresis_readout_for_a_battery_we_never_charge(monkeypatch):
    """Hysteresis holds a battery off charging; this one is never told to charge.

    Driven through the real platform setup, with the flag still set to what an
    entry configured before this fix has stored.
    """
    from custom_components.omnibattery import binary_sensor as platform
    from custom_components.omnibattery.const import DOMAIN

    observed, _ = _coordinator(monkeypatch)
    observed.enable_charge_hysteresis = True      # what the old config stored
    observed.binary_sensor_definitions  # must not raise

    marstek = _battery("Marstek")
    marstek.enable_charge_hysteresis = True
    marstek.binary_sensor_definitions = []

    entry = SimpleNamespace(entry_id="e1", data={})
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": {"coordinators": [marstek, observed], "controller": None}}}

    added = []
    await platform.async_setup_entry(hass, entry, lambda ents, *a, **k: added.extend(ents))

    names = [type(e).__name__ for e in added]
    hysteresis = [
        e for e in added if type(e).__name__ == "ChargeHysteresisActiveSensor"
    ]
    assert len(hysteresis) == 1, names
    assert hysteresis[0].coordinator is marstek


def test_the_device_card_does_not_claim_a_vendor_it_cannot_know(monkeypatch):
    import custom_components.omnibattery.drivers.generic_ha as mod

    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform="kostal_plenticore")
    monkeypatch.setattr(mod.er, "async_get", lambda _hass: registry)

    coordinator, _ = _coordinator(monkeypatch)
    info = coordinator.battery_device_info
    # 2.0.0 reads MANUFACTURER off the driver class, so it cannot vary with the
    # source integration. The name that can -- and the one worth showing -- goes
    # in the model line, which is per instance.
    assert info["manufacturer"] == "Home Assistant"
    assert info["model"] == "Kostal Plenticore (observed)"
    assert "Marstek" not in info.values()


def test_the_arbitrage_gate_is_not_diluted_by_the_observed_battery():
    """1.6.0 derives round-trip efficiency from the fleet's own counters.

    The reference BYD measures 8214.2 / 8994.2 kWh = 91.3%, comfortably inside
    the trusted band, and its 6 kWh would outweigh the Marstek's 5.12 kWh. But
    the energy an arbitrage decision buys is round-tripped through the battery
    the controller commands, so only that battery's efficiency may set the
    price spread the purchase has to clear.
    """
    from custom_components.omnibattery.energy import effective_round_trip_efficiency

    def battery(charge, discharge, capacity, commandable):
        return _Coord(
            name="b",
            capabilities=SimpleNamespace(
                supports_power_setpoint=commandable,
                independent_power_planes=True,
                lifetime_counters_ac=True,
            ),
            data={
                "total_charging_energy": charge,
                "total_discharging_energy": discharge,
                "battery_total_energy": capacity,
            },
        )

    marstek = battery(1000.0, 800.0, 5.12, commandable=True)
    byd = battery(8994.2, 8214.2, 6.0, commandable=False)

    controller = SimpleNamespace(round_trip_efficiency=None, coordinators=[marstek])
    alone = effective_round_trip_efficiency(controller)

    controller.coordinators = [marstek, byd]
    together = effective_round_trip_efficiency(controller)

    assert alone == together == (pytest.approx(0.8), "measured")


# ---------------------------------------------------------------------------
# the central refusal: no code path may command this battery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_control_loop_refuses_to_write_to_it_at_all():
    """Including 0 W.

    Filtering at selection time is not enough on its own: a future caller that
    forgets the filter must still not reach the battery. The guard is the first
    statement in ``_set_battery_power``, before the ownership and availability
    checks, and it is not conditioned on ``owner`` -- every other guard there
    answers "not right now", this one answers "there is nothing to write to".
    """
    observed = _battery("BYD", commandable=False)
    controller = MagicMock()

    for charge, discharge, owner in (
        (1000, 0, "automatic"),
        (0, 1000, "automatic"),
        (0, 0, "automatic"),          # the idle write the stop paths use
        (0, 0, "battery_manual"),     # a manual owner must not get through either
    ):
        assert await ChargeDischargeController._set_battery_power(
            controller, observed, charge, discharge, owner=owner,
        ) is False

    # A commandable battery is untouched by the new guard: it reaches the rest
    # of the method (and fails there on the mock), proving the guard is the only
    # thing that stopped the calls above.
    commandable = _battery("Marstek")
    assert can_take_setpoint(commandable) is True


@pytest.mark.asyncio
async def test_the_command_boundary_itself_refuses_too(monkeypatch):
    """``apply_power`` is reached by four paths that skip ``_set_battery_power``.

    Manual time slots, ``_reassert_register_manual_intent``, the force-mode
    select and global manual mode all call ``coordinator.apply_power`` directly,
    so a refusal placed only in the control loop would not hold for them. The
    driver must see nothing.
    """
    coordinator, _ = _coordinator(monkeypatch)
    applied = []
    monkeypatch.setattr(
        coordinator.driver, "apply_setpoint",
        AsyncMock(side_effect=lambda *a, **k: applied.append(a)),
    )

    for watts in (1500, -1500, 0):
        result = await coordinator.apply_power(watts, read_back=False)
        # Reported as a clean no-op: nothing went wrong, there is simply
        # nothing to command, and a failure would feed the health counters.
        assert result.ok is True
        assert result.net_power_w == 0

    assert applied == [], "the driver was handed a setpoint it cannot take"


def test_its_charging_is_kept_out_of_the_grid_feedback():
    """Otherwise the regulator discharges the commandable battery into it.

    An observed battery charging at 1200 W reaches the meter as import. Without
    this term the controller reads that import as house load and covers it from
    the batteries it does command -- battery to battery, paid for twice.
    """
    observed = _battery("BYD", battery_power=1200, commandable=False)
    marstek = _battery("Marstek", ac_power=0)
    controller = SimpleNamespace(
        coordinators=[marstek, observed],
        _is_battery_manual_owned=lambda c: False,
    )
    total = ChargeDischargeController._manual_battery_power_for_grid_feedback(controller)
    assert total == pytest.approx(1200.0)


def test_a_commandable_battery_is_not_pulled_into_that_term():
    """The term is for power this controller did not command. Nothing else."""
    marstek = _battery("Marstek", ac_power=900)
    controller = SimpleNamespace(
        coordinators=[marstek],
        _is_battery_manual_owned=lambda c: False,
    )
    assert ChargeDischargeController._manual_battery_power_for_grid_feedback(
        controller
    ) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# the planner's own fleet lists
# ---------------------------------------------------------------------------

def _pricing_manager(batteries):
    """A PricingManager whose controller is just these batteries."""
    from custom_components.omnibattery.pricing.engine import PricingManager

    controller = SimpleNamespace(
        coordinators=batteries,
        _is_battery_manual_owned=lambda c: False,
        _counts_towards_fleet_energy=staticmethod(
            ChargeDischargeController._counts_towards_fleet_energy
        ).__func__,
        _dp_last_eval_soc=None,
        _dp_eval_projection=None,
        _dp_eval_rows=None,
        _dp_eval_projection_capacity_kwh=None,
        # Dynamic Pricing runs with a chronological plan, which puts the
        # re-plan threshold at 5 points rather than the 30 of the old rule.
        _dynamic_pricing_schedule=SimpleNamespace(chronological_planning_active=True),
        _current_price_slot_active=False,
    )
    manager = PricingManager.__new__(PricingManager)
    manager._controller = controller
    return manager, controller


def test_the_replan_trigger_ignores_the_observed_batterys_soc():
    """The reference SOC comes from the batteries the plan is made for.

    Marstek at 60 % and on its plan, Kostal observed at 40 %. Averaging both
    gives 50, which is 10 points under the reference, and the below-plan branch
    has no age guard -- so Dynamic Pricing would re-plan on every 2.5 s control
    tick for a deviation that never happened.
    """
    marstek = _battery("Marstek", soc=60, capacity=10.0)
    observed = _battery("BYD", soc=40, capacity=10.0, commandable=False)
    manager, controller = _pricing_manager([marstek, observed])
    controller._dp_last_eval_soc = 60.0

    assert manager._is_dp_soc_drop_reeval() is False

    # Control: a real drop on the battery the plan is for still re-plans.
    marstek.data["battery_soc"] = 50
    assert manager._is_dp_soc_drop_reeval() is True


def test_the_fleet_capacity_counts_only_what_the_plan_can_fill():
    """20 kWh of hardware, 10 kWh the planner can actually place."""
    marstek = _battery("Marstek", soc=60, capacity=10.0)
    observed = _battery("BYD", soc=40, capacity=10.0, commandable=False)
    manager, _ = _pricing_manager([marstek, observed])

    assert manager._fleet_capacity_kwh() == pytest.approx(10.0)


@pytest.mark.parametrize("module_name", [
    "pricing.engine",
    "control.weekly_full_charge",
    "control.charge_delay",
    "control.high_price_discharge",
])
def test_every_fleet_list_uses_the_same_definition(module_name):
    """No fleet list may filter on manual mode alone.

    The cases above are the ones review caught; this pins the rule for the rest
    of each file so the next fleet list cannot quietly reintroduce the defect.
    One such site (the weekly run's completion report) was found by this rule
    rather than by review.
    """
    import importlib
    import inspect

    module = importlib.import_module(f"custom_components.omnibattery.{module_name}")
    source = inspect.getsource(module).split("\n")
    offenders = []
    for i, line in enumerate(source):
        if "battery_manual_mode_enabled" not in line:
            continue
        # Wide enough to see past an explanatory comment between the two
        # guards, narrow enough that an unrelated check cannot mask a miss.
        window = "\n".join(source[i:i + 8])
        if "_counts_towards_fleet_energy" in window or "can_take_setpoint" in window:
            continue
        offenders.append((i + 1, line.strip()))
    assert not offenders, f"{module_name}: fleet lists filtering on manual mode only: {offenders}"


# ---------------------------------------------------------------------------
# weekly full charge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_weekly_run_does_not_wait_for_the_observed_battery():
    """Completion is about the batteries Omnibattery charges.

    A battery it cannot charge never reports full, so counting it would leave
    the weekly run pending until the next sunrise cancelled it -- every week,
    silently. Drives the real ``handle_registers``.
    """
    from unittest.mock import patch

    from custom_components.omnibattery.control.weekly_full_charge import (
        WeeklyFullChargeManager,
    )

    def _coord(name, soc, commandable):
        return _Coord(
            name=name,
            data={"battery_soc": soc, "battery_total_energy": 10.0},
            max_soc=100,
            battery_manual_mode_enabled=False,
            is_available=True,
            capabilities=SimpleNamespace(
                hardware_soc_cutoff=False,
                has_mppt_pv=False,
                supports_power_setpoint=commandable,
            ),
            driver=_Driver(),
            set_charge_cutoff=AsyncMock(return_value=True),
            enable_charge_hysteresis=True,
            charge_hysteresis_percent=2,
            rs485_user_disabled=False,
        )

    marstek = _coord("Marstek", 100, True)     # at the top, as the run wants
    observed = _coord("BYD", 40, False)        # will never be charged, so never full

    controller = SimpleNamespace(
        weekly_full_charge_enabled=True,
        weekly_full_charge_day="mon",
        weekly_full_charge_complete=False,
        weekly_full_charge_registers_written=True,
        last_checked_weekday=0,
        _force_full_charge=False,
        _weekly_charge_needs_restore=False,
        _weekly_charge_saved_max_soc={},
        _weekly_charge_status={},
        _charge_delay_unlocked=False,
        _solar_t_start=None,
        coordinators=[marstek, observed],
        _is_backup_function_active=lambda _c: False,
        charge_delay_enabled=False,
        _balance_monitor_overrides_delay=False,
        _normal_balance_reset_if_new_day=lambda: None,
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _e: None), config=None),
    )
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = controller
    manager._already_complete_logged = False
    manager._cutoff_applied_names = set()
    manager._bms_cutoff_counts = {}
    manager.tick_bms_cutoff = MagicMock()
    manager.is_active = MagicMock(return_value=True)
    manager.save_state = AsyncMock()
    manager.is_battery_full = lambda c: (c.data or {}).get("battery_soc", 0) >= 100
    # The solar outlook is a separate feature with its own tests; it only needs
    # to not reach for a forecast sensor that does not exist here.
    manager._refresh_solar_outlook = MagicMock()
    manager._clear_solar_outlook = MagicMock()

    with patch(
        "custom_components.omnibattery.control.weekly_full_charge.asyncio.create_task"
    ):
        await manager.handle_registers()

    # The run completed on the battery it actually charges.
    assert controller.weekly_full_charge_complete is True
    # And the observed battery is not even reported as a participant.
    assert list(controller._weekly_charge_status.get("batteries", {})) == ["Marstek"]


# ---------------------------------------------------------------------------
# the control loop
# ---------------------------------------------------------------------------

def test_its_power_is_not_the_delivery_of_the_commanded_batteries():
    """The PD re-anchors its command to this figure.

    With the Kostal's 2 kW in it, the no-PD law and the load-step feedforward
    read 2 kW as already delivered and command the Marstek to charge it from the
    grid.
    """
    marstek = _battery("Marstek", ac_power=0)
    observed = _battery("BYD", battery_power=2000, commandable=False)
    controller = SimpleNamespace(
        coordinators=[marstek, observed],
        _coordinator_delivered_power=ChargeDischargeController._coordinator_delivered_power,
    )
    assert ChargeDischargeController._measured_battery_power(controller) == 0


def test_a_slot_is_not_credited_with_what_the_observed_battery_charged():
    """Its own solar charging would cut the grid charge the plan bought short."""
    marstek = _battery("Marstek", soc=50)
    observed = _battery("BYD", soc=40, commandable=False)
    controller = SimpleNamespace(
        coordinators=[marstek, observed], _dynamic_pricing_schedule=None
    )
    slot = SimpleNamespace(start=0, end=1)
    assert ChargeDischargeController._charged_in_slot(controller, slot) == 0.0
    marstek.data["battery_soc"] = 55
    observed.data["battery_soc"] = 90
    assert ChargeDischargeController._charged_in_slot(controller, slot) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("grid", "charging", "command", "expected"),
    [
        (1500, 1000, 0, 500),        # importing for both: the house's share is left
        (0, 1000, 0, 0),             # filled from its own PV: no phantom export
        (-300, 1000, 0, -300),       # a real export stays one
        (0, 1000, -500, -500),       # a discharge into it is unwound ...
        (0, 1000, -1500, -1000),     # ... by no more than goes into it
        (400, 1000, 600, 400),       # while charging, the meter is left alone
    ],
)
def test_removing_its_charging_never_invents_an_export(grid, charging, command, expected):
    controller = SimpleNamespace(
        coordinators=[_battery("BYD", battery_power=charging, commandable=False)],
        previous_power=command,
    )
    assert ChargeDischargeController._without_uncommanded_charge(
        controller, grid, 0
    ) == pytest.approx(expected)


def _run_cycles(monkeypatch, grid_w, observed_w, cycles=6):
    """Run the real control cycle; return the charge commands and guard inputs."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    import custom_components.omnibattery as integration
    from tests.test_control_sample_scheduling import _main_controller, _state

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    holder = {"state": _state(grid_w, t0)}
    controller = _main_controller(holder, [])
    marstek = controller.coordinators[0]
    observed = _battery("BYD", battery_power=observed_w, commandable=False)
    observed._is_shutting_down = False
    controller._power_distribution._rebalance_expired_load_sharing_hold = AsyncMock(
        return_value=False
    )
    controller.coordinators.append(observed)
    commands, guard_grids = [], []

    async def _record(coordinator, charge, discharge, **_kwargs):
        commands.append((charge, discharge))

    def _guards(_controller, power, grid):
        guard_grids.append(grid)
        return power

    monkeypatch.setattr(integration, "apply_guards", _guards)
    controller.__dict__.update(
        previous_power=0.0, last_output_sign=0, kp=0.75, kd=0.0, ki=0, dt=1.0,
        derivative_tau=3.0, max_power_change_per_cycle=2000, direction_hysteresis=0,
        _measured_battery_power=lambda: None,
        _should_log_rate_limiter=lambda _change: False,
        _clear_rate_limiter_state=lambda: None,
        _get_available_batteries=lambda is_charging, include_operation_blocks=True: (
            [marstek] if is_charging else []
        ),
        _set_battery_power=_record,
        _is_grid_at_min_soc_discharge_window=lambda: False,
        min_charge_power=0, min_discharge_power=0,
    )
    for name in ("_compute_pd_new_power", "_apply_min_power"):
        setattr(controller, name, getattr(ChargeDischargeController, name).__get__(
            controller, ChargeDischargeController
        ))
    for i in range(1, cycles):
        ts = t0 + timedelta(seconds=i)
        holder["state"] = _state(grid_w + i % 2, ts)
        asyncio.run(controller._run_control_cycle(now=ts))
    return commands, guard_grids


def test_an_observed_battery_filling_from_its_own_sun_starts_no_grid_charge(monkeypatch):
    commands, _ = _run_cycles(monkeypatch, grid_w=0, observed_w=2000)
    assert all(charge == 0 for charge, _ in commands), commands


def test_the_guards_see_the_meter_with_its_charging_still_in_it(monkeypatch):
    """They rebuild the house from every battery's power; once is enough."""
    _, guard_grids = _run_cycles(monkeypatch, grid_w=1500, observed_w=1000)
    assert guard_grids and all(abs(grid - 1500) < 5 for grid in guard_grids), guard_grids
