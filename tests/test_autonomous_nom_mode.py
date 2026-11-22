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
