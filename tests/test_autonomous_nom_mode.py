"""The batteries that regulate on their own: what they are asked for and when.

The manager is the real one, with a fake device behind it and a clock the test
moves by hand. ``Rig.cycle`` is one control cycle: reconcile, then let the
background write finish.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.control import autonomous_mode as mod
from custom_components.omnibattery.control.autonomous_mode import (
    AutonomousBatteryModeManager,
)
from custom_components.omnibattery.drivers.base import (
    BatteryDriver,
    NativeOperatingMode,
    OperatingModeResult,
)

NOM = NativeOperatingMode.NOM
MANUAL = NativeOperatingMode.MANUAL


class FakeNomDriver:
    """A device with a native mode. ``obeys`` False accepts writes and ignores them."""

    ac_contribution_w = BatteryDriver.ac_contribution_w
    is_self_managed = BatteryDriver.is_self_managed
    on_backup_power = BatteryDriver.on_backup_power
    dc_coupled = False

    def __init__(self, mode=MANUAL):
        self.mode = mode
        self.writes: list[NativeOperatingMode] = []
        self.obeys = True
        self.ok = True

    def ac_register_power_w(self, data):
        return data.get("ac_power")

    def operating_mode_from_data(self, data):
        return self.mode

    async def apply_operating_mode(self, mode, *, read_back=True):
        self.writes.append(mode)
        if self.ok and self.obeys:
            self.mode = mode
        return OperatingModeResult(
            ok=self.ok, confirmed=False, requested_mode=mode,
            failure_reason=None if self.ok else "boom",
        )


class Battery:
    """Coordinator stand-in of a battery with a native mode (hashable)."""

    def __init__(self, name="NOM", mode=MANUAL, requested="manual", power=0.0, nom=True):
        self.name = name
        self.device_key = f"{name.lower()}_0"
        self.driver = FakeNomDriver(mode)
        self.capabilities = SimpleNamespace(
            supports_nom_mode=nom, supports_power_setpoint=False, has_mppt_pv=False,
            max_charge_power_w=0, max_discharge_power_w=0,
        )
        self.autonomous_requested_mode = requested
        self.battery_manual_mode_enabled = False
        self.data = {"battery_soc": 50, "battery_power": power}
        self.is_available = True
        self._is_shutting_down = False
        self.rs485_user_disabled = False
        self.persisted: dict = {}

    def persist_battery_config(self, key, value):
        self.persisted[key] = value

    async def apply_operating_mode(self, mode):
        return await self.driver.apply_operating_mode(mode, read_back=False)


def record_issues(manager) -> set[str]:
    """Keep the manager's repairs in a set of their translation keys."""
    raised: set[str] = set()
    manager._create_issue = lambda issue_id, key, coordinator: raised.add(key)
    manager._delete_issue = lambda issue_id: raised.discard(
        next((key for key in raised if issue_id.startswith(key)), None)
    )
    return raised


class Rig:
    """A manager, its batteries and a clock."""

    def __init__(self, *batteries, **controller):
        self.batteries = list(batteries)
        self.controller = SimpleNamespace(
            coordinators=self.batteries,
            manual_mode_enabled=False,
            config_entry=SimpleNamespace(entry_id="e1"),
            **controller,
        )
        self.manager = AutonomousBatteryModeManager(
            SimpleNamespace(), self.controller.config_entry, self.controller
        )
        self.now = 1000.0
        self.manager._clock = lambda: self.now
        self.issues = record_issues(self.manager)
        self.causes: dict[str, dict] = {}

    async def cycle(self, seconds=2.0):
        self.now += seconds
        for source, details in self.causes.items():
            self.manager.assert_cause(source, **details)
        await self.manager.async_reconcile()
        for state in self.manager._states.values():
            if state.task is not None:
                await state.task

    async def run(self, seconds, step=2.0):
        elapsed = 0.0
        while elapsed < seconds:
            await self.cycle(step)
            elapsed += step


def nom_battery(**kwargs):
    """A battery whose user wants NOM and which is in it."""
    return Battery(requested="nom", mode=NOM, **kwargs)


# ------------------------------------------------------------- preference

@pytest.mark.asyncio
async def test_a_battery_nobody_chose_for_goes_to_manual():
    battery = Battery(mode=NOM)
    del battery.autonomous_requested_mode
    rig = Rig(battery)
    await rig.cycle()
    assert battery.driver.writes == [MANUAL]
    await rig.cycle()
    assert rig.manager.status(battery) == "manual"


@pytest.mark.asyncio
async def test_choosing_nom_is_stored_and_applied():
    battery = Battery()
    rig = Rig(battery)
    await rig.run(40)                       # nothing has held it off for 30 s
    await rig.manager.async_set_requested_mode(battery, "nom")
    assert battery.persisted == {"autonomous_requested_mode": "nom"}
    await rig.cycle()
    assert battery.driver.mode is NOM
    await rig.cycle()
    assert rig.manager.status(battery) == "nom_active"
    assert battery.battery_manual_mode_enabled is False    # an unrelated thing


@pytest.mark.asyncio
async def test_choosing_manual_is_stored_before_anything_is_written():
    battery = nom_battery()
    rig = Rig(battery)
    await rig.cycle()
    order = []
    battery.persist_battery_config = lambda k, v: order.append("stored")

    async def _apply(mode):
        order.append("written")
        return await Battery.apply_operating_mode(battery, mode)

    battery.apply_operating_mode = _apply
    await rig.manager.async_set_requested_mode(battery, "manual")
    await rig.cycle()
    assert order == ["stored", "written"]


@pytest.mark.asyncio
async def test_a_battery_without_a_mode_is_never_touched():
    observed = Battery(nom=False, mode=NOM)
    commandable = SimpleNamespace(name="Marstek")       # no capabilities at all
    rig = Rig(observed, commandable)
    assert rig.manager.batteries == []
    await rig.run(60)
    assert observed.driver.writes == [] and rig.manager._states == {}


@pytest.mark.asyncio
async def test_a_battery_already_in_the_wanted_mode_is_not_written():
    """A restart with everything in order writes nothing."""
    running, resting = nom_battery(), Battery("rest")
    rig = Rig(running, resting)
    await rig.run(120)
    assert running.driver.writes == [] and resting.driver.writes == []


# ------------------------------------------------------------- suspension

@pytest.mark.asyncio
async def test_a_cause_suspends_nom_and_keeps_the_preference():
    battery = nom_battery()
    rig = Rig(battery)
    await rig.cycle()
    rig.causes["grid_charge:predictive"] = {}
    await rig.cycle()
    await rig.cycle()
    assert battery.driver.mode is MANUAL
    snapshot = rig.manager.snapshot(battery)
    assert snapshot["requested_mode"] == "nom" and snapshot["effective_mode"] == "manual"
    assert snapshot["suspended"] is True
    assert list(snapshot["suspension_causes"]) == ["grid_charge:predictive"]
    assert snapshot["status"] == "nom_suspended_grid_charge"
    assert battery.persisted == {}                      # causes never reach disk


@pytest.mark.asyncio
async def test_nom_waits_for_the_last_cause_and_thirty_seconds():
    battery = nom_battery()
    rig = Rig(battery)
    rig.causes = {"grid_charge:predictive": {}, "excluded_device:x": {}}
    await rig.run(20)
    del rig.causes["grid_charge:predictive"]
    await rig.run(60)
    assert battery.driver.mode is MANUAL, "another cause is still there"
    del rig.causes["excluded_device:x"]
    await rig.run(mod.CAUSE_TTL_S + mod.RESTORE_STABLE_S - 2)
    assert battery.driver.mode is MANUAL
    await rig.run(8)
    assert battery.driver.mode is NOM
    assert battery.driver.writes == [MANUAL, NOM]


@pytest.mark.asyncio
async def test_a_flickering_cause_does_not_flap_the_mode():
    battery = nom_battery()
    rig = Rig(battery)
    for _ in range(10):
        rig.causes["excluded_device:x"] = {}
        await rig.run(6)
        rig.causes.clear()
        await rig.run(24)                   # gone, but never for 30 s
    assert battery.driver.writes == [MANUAL]


@pytest.mark.asyncio
async def test_back_to_nom_respects_the_dwell_and_manual_does_not():
    """The device loses its mode by itself seconds after each write."""
    battery = nom_battery()
    rig = Rig(battery)
    await rig.run(40)
    battery.driver.mode = MANUAL            # the vendor app switched it
    await rig.run(4)
    assert battery.driver.mode is NOM
    battery.driver.mode = MANUAL
    await rig.run(20)
    assert battery.driver.writes == [NOM], "the second NOM waits for the dwell"
    await rig.run(14)
    assert battery.driver.writes == [NOM, NOM]
    rig.causes["global_manual_mode"] = {}
    await rig.cycle()
    assert battery.driver.writes[-1] is MANUAL


@pytest.mark.asyncio
async def test_two_batteries_are_independent():
    first, second = nom_battery(), Battery("second")
    rig = Rig(first, second)
    await rig.run(40)
    await rig.manager.async_set_requested_mode(second, "nom")
    await rig.run(4)
    await rig.manager.async_set_requested_mode(first, "manual")
    await rig.run(4)
    assert first.driver.mode is MANUAL and second.driver.mode is NOM


# ------------------------------------------------------------- failures

@pytest.mark.asyncio
async def test_a_refused_manual_is_retried_with_backoff_and_shown():
    battery = nom_battery()
    battery.driver.ok = False
    rig = Rig(battery)
    rig.causes["grid_charge:predictive"] = {}
    await rig.run(90)
    # 5, 10, 20, 40 s between tries: not one per cycle, and it never gives up.
    assert 4 <= len(battery.driver.writes) <= 6
    snapshot = rig.manager.snapshot(battery)
    assert snapshot["status"] == "degraded"
    assert snapshot["requested_mode"] == "nom"
    assert snapshot["effective_mode"] == "nom", "never claims to be stopped"
    assert snapshot["last_error"] == "boom"

    battery.driver.ok = True
    await rig.run(70)
    snapshot = rig.manager.snapshot(battery)
    assert snapshot["status"] == "nom_suspended_grid_charge"
    assert snapshot["last_error"] is None and snapshot["failed_attempts"] == 0


@pytest.mark.asyncio
async def test_an_accepted_write_the_device_ignores_is_a_failure_too():
    battery = nom_battery()
    battery.driver.obeys = False
    rig = Rig(battery)
    rig.causes["global_manual_mode"] = {}
    await rig.run(90)
    assert rig.manager.snapshot(battery)["last_error"] == "not_confirmed"
    assert rig.manager.status(battery) == "degraded"
    assert len(battery.driver.writes) >= 2


@pytest.mark.asyncio
async def test_a_nom_that_does_not_take_stays_in_manual():
    battery = Battery(requested="nom")
    battery.driver.ok = False
    rig = Rig(battery)
    await rig.run(120)
    assert rig.manager.effective(battery) is MANUAL
    assert rig.manager.status(battery) == "degraded"
    assert set(battery.driver.writes) == {NOM}


@pytest.mark.asyncio
async def test_a_requested_manual_that_fails_is_never_reported_as_nom_or_stopped():
    battery = nom_battery()
    battery.driver.obeys = False
    rig = Rig(battery)
    await rig.cycle()
    await rig.manager.async_set_requested_mode(battery, "manual")
    await rig.run(70)
    snapshot = rig.manager.snapshot(battery)
    assert snapshot["requested_mode"] == "manual"
    assert snapshot["status"] == "degraded" and snapshot["effective_mode"] == "nom"


@pytest.mark.asyncio
async def test_an_unmapped_or_unreadable_mode_is_not_assumed():
    battery = nom_battery()
    battery.driver.mode = None
    battery.driver.obeys = False
    rig = Rig(battery)
    await rig.run(70)
    assert rig.manager.snapshot(battery)["effective_mode"] is None
    assert rig.manager.status(battery) == "degraded"


@pytest.mark.asyncio
async def test_a_hanging_service_does_not_hold_the_cycle():
    battery = nom_battery()
    release = asyncio.Event()

    async def _slow(mode):
        await release.wait()
        return await Battery.apply_operating_mode(battery, mode)

    battery.apply_operating_mode = _slow
    rig = Rig(battery)
    rig.manager.assert_cause("global_manual_mode")
    await asyncio.wait_for(rig.manager.async_reconcile(), 1)
    # ... and while it hangs there is one write, not one per cycle.
    rig.now += 100
    await asyncio.wait_for(rig.manager.async_reconcile(), 1)
    assert battery.driver.writes == []
    release.set()
    await rig.manager._state(battery).task
    assert battery.driver.writes == [MANUAL]


@pytest.mark.asyncio
async def test_the_repair_of_an_unconfirmed_manual_clears_itself():
    battery = nom_battery()
    battery.driver.obeys = False
    rig = Rig(battery)
    rig.causes["global_manual_mode"] = {}
    await rig.run(50)
    assert rig.issues == set(), "a slow device is not a failure yet"
    await rig.run(20)
    assert rig.issues == {"nom_manual_unconfirmed"}
    battery.driver.mode = MANUAL
    await rig.cycle()
    assert rig.issues == set()


@pytest.mark.asyncio
async def test_a_repair_left_by_an_earlier_run_is_cleared_on_the_first_look():
    rig = Rig(Battery())
    deleted = []
    rig.manager._delete_issue = deleted.append
    await rig.run(10)
    assert "nom_manual_unconfirmed_e1_nom_0" in deleted
    assert len(deleted) == len(set(deleted)), "once, not on every cycle"


# ------------------------------------------------------------- global manual

@pytest.mark.asyncio
async def test_global_manual_suspends_and_gives_back():
    battery = nom_battery()
    rig = Rig(battery)
    await rig.cycle()
    rig.controller.manual_mode_enabled = True
    await rig.cycle()
    assert battery.driver.mode is MANUAL
    assert rig.manager.status(battery) == "nom_suspended_global_manual"
    rig.controller.manual_mode_enabled = False
    await rig.run(40)
    assert battery.driver.mode is NOM
    assert battery.persisted == {}


@pytest.mark.asyncio
async def test_a_manual_charge_of_another_battery_suspends_nom():
    battery = nom_battery()
    other = SimpleNamespace(
        name="Marstek", battery_manual_mode_enabled=True,
        manual_force_mode="Charge", manual_set_charge_power=1500,
    )
    rig = Rig(battery, other)
    await rig.cycle()
    assert battery.driver.mode is MANUAL
    other.manual_force_mode = "None"
    await rig.run(40)
    assert battery.driver.mode is NOM


# ------------------------------------------------------------- excluded devices

def excluded(*devices, states=None, status=None):
    """The real ``ExternalLoads`` over a table of entity states."""
    from custom_components.omnibattery.infra.external_loads import ExternalLoads

    table = states if states is not None else {}
    loads = ExternalLoads(
        SimpleNamespace(states=SimpleNamespace(get=table.get)),
        SimpleNamespace(data={"excluded_devices": list(devices)}),
        SimpleNamespace(),
    )
    if status is not None:
        loads.dynamic_power_control_status = status
    return loads


def reading(value):
    return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": "W"})


WASHER = {"power_sensor": "sensor.washer", "included_in_consumption": True}
CAR = {"power_sensor": "sensor.car_state", "ev_charger_no_telemetry": True}


async def _suspended(device, states, status=None) -> bool:
    battery = nom_battery()
    rig = Rig(battery, _external_loads=excluded(device, states=states, status=status))
    await rig.run(6)
    return battery.driver.mode is MANUAL


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {}, {"exclusion_pct": 30}, {"allow_solar_surplus": True},
    {"allow_solar_surplus": True, "cover_home_when_active": True},
])
async def test_a_running_excluded_device_suspends_nom(extra):
    assert await _suspended({**WASHER, **extra}, {"sensor.washer": reading(800)})
    assert not await _suspended({**WASHER, **extra}, {"sensor.washer": reading(2)})


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"included_in_consumption": False}, {"exclusion_pct": 0}, {"enabled": False},
])
async def test_a_device_the_batteries_do_cover_does_not(extra):
    assert not await _suspended({**WASHER, **extra}, {"sensor.washer": reading(800)})


@pytest.mark.asyncio
async def test_an_ev_without_telemetry_suspends_while_it_charges():
    assert await _suspended(CAR, {"sensor.car_state": reading("charging")})
    assert not await _suspended(CAR, {"sensor.car_state": reading("idle")})


@pytest.mark.asyncio
async def test_an_activity_sensor_suspends_before_there_is_any_power():
    device = {**WASHER, "activity_sensor": "binary_sensor.washer"}
    states = {"sensor.washer": reading(0), "binary_sensor.washer": reading("on")}
    assert await _suspended(device, states)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["waiting_for_load", "yielding", "restart_hold"])
async def test_dynamic_power_control_suspends_in_every_phase(phase):
    device = {**WASHER, "allow_solar_surplus": True, "dynamic_power_control": True}
    status = {"devices": ["sensor.washer"], "phases": {"sensor.washer": phase}}
    assert await _suspended(device, {"sensor.washer": reading(0)}, status)


@pytest.mark.asyncio
async def test_nom_comes_back_once_after_the_device_stops():
    battery = nom_battery()
    states = {"sensor.washer": reading(800)}
    rig = Rig(battery, _external_loads=excluded(WASHER, states=states))
    await rig.run(60)
    assert rig.manager.status(battery) == "nom_suspended_excluded_load"
    states["sensor.washer"] = reading(0)
    await rig.run(28)
    assert battery.driver.mode is MANUAL
    await rig.run(120)
    assert battery.driver.writes == [MANUAL, NOM]


@pytest.mark.asyncio
async def test_a_running_device_that_goes_unavailable_still_counts():
    battery = nom_battery()
    states = {"sensor.washer": reading(800)}
    rig = Rig(battery, _external_loads=excluded(WASHER, states=states))
    await rig.run(10)
    states["sensor.washer"] = reading("unavailable")
    await rig.run(300)
    assert battery.driver.mode is MANUAL
    states["sensor.washer"] = reading(0)
    await rig.run(40)
    assert battery.driver.mode is NOM
    # One that was off when it was lost does not suspend.
    states["sensor.washer"] = reading("unavailable")
    await rig.run(60)
    assert battery.driver.mode is NOM


@pytest.mark.asyncio
async def test_at_startup_nom_waits_for_every_excluded_device_to_be_readable():
    battery = Battery(requested="nom")
    states = {"sensor.washer": reading("unknown")}
    rig = Rig(battery, _external_loads=excluded(WASHER, states=states))
    await rig.run(120)
    assert battery.driver.writes == []
    states["sensor.washer"] = reading(0)
    await rig.run(40)
    assert battery.driver.mode is NOM


# ------------------------------------------------------------- grid charge

@pytest.mark.asyncio
async def test_a_grid_charge_waits_for_manual_and_holds_it():
    battery = nom_battery()
    rig = Rig(battery)
    await rig.cycle()
    assert rig.manager.grid_charge_ready("predictive") is False
    for state in rig.manager._states.values():
        await state.task                    # the switch started in that call
    assert battery.driver.mode is MANUAL
    assert rig.manager.grid_charge_ready("predictive") is True
    for _ in range(100):
        await rig.cycle()
        assert rig.manager.grid_charge_ready("predictive") is True
    assert battery.driver.writes == [MANUAL]
    await rig.run(60)                       # nobody asks any more
    assert battery.driver.mode is NOM


@pytest.mark.asyncio
async def test_without_autonomous_batteries_a_grid_charge_never_waits():
    rig = Rig(Battery(nom=False, mode=NOM))
    assert rig.manager.grid_charge_ready("predictive") is True
    assert rig.manager._asserted == {} and rig.manager._charges == {}


async def _charge(rig, seconds):
    """Ask on every cycle; returns the answers."""
    answers = []
    for _ in range(int(seconds / 2)):
        await rig.cycle()
        answers.append(rig.manager.grid_charge_ready("predictive"))
    return answers


@pytest.mark.asyncio
async def test_the_wait_is_sixty_seconds_once_per_charge():
    battery = nom_battery()
    battery.driver.obeys = False
    rig = Rig(battery)
    answers = await _charge(rig, 80)
    assert answers.index(True) * 2 == pytest.approx(mod.GRID_CHARGE_WAIT_S, abs=2)
    assert all(answers[answers.index(True):])
    await rig.cycle()
    assert rig.issues == {"nom_manual_unconfirmed"}
    assert rig.manager.status(battery) == "degraded"
    tries = len(battery.driver.writes)
    # The flag behind the charge drops and comes back: no second wait.
    await rig.run(20)
    assert all(await _charge(rig, 60))
    assert len(battery.driver.writes) > tries, "keeps trying during the charge"
    battery.driver.mode = MANUAL
    assert all(await _charge(rig, 4))
    assert rig.issues == set() and rig.manager.status(battery) == "nom_suspended_grid_charge"
    # A later charge is another charge.
    await rig.run(120)
    battery.driver.mode = NOM
    assert (await _charge(rig, 4))[0] is False


@pytest.mark.asyncio
async def test_a_charge_that_started_is_not_stopped_by_the_battery_waking_up():
    battery = nom_battery()
    rig = Rig(battery)
    await _charge(rig, 10)
    battery.driver.mode = NOM               # the vendor app
    assert all(await _charge(rig, 10))
    assert battery.driver.mode is MANUAL


# ------------------------------------------------------------- entity

def test_the_select_shows_the_request_and_explains_the_rest():
    from custom_components.omnibattery.select import BatteryAutonomousModeSelect

    battery = Battery(requested="nom")
    battery.battery_device_info = {"identifiers": {("omnibattery", "x")}}
    rig = Rig(battery)
    select = BatteryAutonomousModeSelect(battery, rig.manager)
    assert select.options == ["nom", "manual"]
    assert select.current_option == "nom"
    assert select.unique_id == f"{battery.device_key}_autonomous_operating_mode"
    attrs = select.extra_state_attributes
    assert {"effective_mode", "suspended", "suspension_causes", "last_change",
            "last_error", "status"} <= set(attrs)
    assert attrs["effective_mode"] == "manual"


@pytest.mark.asyncio
@pytest.mark.parametrize("nom", [True, False])
async def test_the_select_exists_only_for_a_battery_with_a_mode(nom):
    from unittest.mock import MagicMock

    from custom_components.omnibattery import select as platform
    from custom_components.omnibattery import switch as switch_platform
    from custom_components.omnibattery.const import DOMAIN

    battery = Battery(nom=nom)
    battery.select_definitions = []
    battery.switch_definitions = []
    rig = Rig(battery)
    rig.controller._autonomous_mode_mgr = rig.manager
    entry = SimpleNamespace(entry_id="e1", data={"batteries": [{}]})
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": {"coordinators": [battery], "controller": rig.controller}}}
    added = []
    await platform.async_setup_entry(hass, entry, lambda ents, *a, **k: added.extend(ents))
    names = [type(e).__name__ for e in added]
    assert names.count("BatteryAutonomousModeSelect") == (1 if nom else 0)
    # No control of a battery that takes setpoints.
    assert "ManualForceModeSelect" not in names and "BatteryPhaseSelect" not in names

    source = __import__("inspect").getsource(switch_platform.async_setup_entry)
    assert "if controller and can_take_setpoint(coordinator):" in source
