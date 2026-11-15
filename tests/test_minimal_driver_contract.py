"""A driver implementing ONLY the abstract base class must run end to end.

Plan (docs/plans/driver-registry/plan.md, criterion 3 and T2.0): the shared code
(coordinator, setup, teardown, platforms) may call any member of ``BatteryDriver``
without a guard. A new brand that implements just the abstract members must start,
poll, take a power command and shut down without an ``AttributeError``.

``MinimalDriver`` below defines nothing but the abstract members. Everything else
(entity definitions, RS485 control, per-pack fields) must come from the base class.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.drivers import registry
from custom_components.omnibattery.drivers.base import (
    BatteryDriver,
    DriverCapabilities,
    DriverContext,
    ReadGroup,
    SetpointResult,
)
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)


class MinimalDriver(BatteryDriver):
    """Implements the abstract members of BatteryDriver and nothing else."""

    def __init__(self) -> None:
        self._connected = False
        self.calls: list[str] = []

    @classmethod
    def from_config(cls, battery_config, context: DriverContext) -> "MinimalDriver":
        return cls()

    @property
    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(
            hardware_soc_cutoff=False,
            has_force_mode=False,
            push_telemetry=False,
            max_charge_power_w=800,
            max_discharge_power_w=800,
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
        )

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def close(self) -> None:
        self._connected = False

    def set_shutting_down(self, value: bool) -> None:
        pass

    @property
    def read_groups(self) -> list[ReadGroup]:
        return [ReadGroup("high", ("battery_soc", "battery_power"))]

    async def read_telemetry(self, keys=None):
        return {"battery_soc": 50, "battery_power": 0}

    async def apply_setpoint(self, net_power_w, *, mode_hint=None, read_back=True):
        self.calls.append(f"apply_setpoint:{net_power_w}")
        return SetpointResult(ok=True, net_power_w=net_power_w, confirmed=False)

    async def write_control(self, key: str, value: int) -> bool:
        return False

    def net_power_from_data(self, data):
        return None

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset()

    async def apply_config(
        self,
        *,
        max_soc_pct,
        min_soc_pct,
        max_charge_power_w,
        max_discharge_power_w,
    ) -> bool:
        self.calls.append("apply_config")
        return True

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        self.calls.append("set_charge_cutoff")
        return False

    async def standby(self) -> bool:
        self.calls.append("standby")
        return True


@pytest.fixture
def build_coordinator(monkeypatch):
    """Build a real coordinator whose driver the registry constructs.

    ``MinimalDriver`` is registered under the brand ``"minimal"``; the coordinator
    does what ``_setup_battery`` does: ``driver_class(brand).from_config(...)``.
    Only the Home Assistant machinery that needs a running instance is stubbed.
    """
    monkeypatch.setitem(registry.DRIVERS, "minimal", MinimalDriver)
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)

    def _build() -> MarstekVenusDataUpdateCoordinator:
        coordinator = MarstekVenusDataUpdateCoordinator(
            MagicMock(),
            {
                "name": "minimal", "host": "192.0.2.1", "port": 502, "brand": "minimal",
                "max_charge_power": 800, "max_discharge_power": 800,
                "max_soc": 100, "min_soc": 12,
            },
            "sensor.grid_power",
        )
        # Set by the stubbed DataUpdateCoordinator.__init__.
        coordinator.hass = MagicMock()
        coordinator.data = {}
        coordinator._alarm_notifier = SimpleNamespace(
            check=lambda *args, **kwargs: _done()
        )
        # No entity is registered or disabled, so nothing is skipped in the poll.
        coordinator._entity_registry = SimpleNamespace(
            async_get_entity_id=lambda *args: None, entities={}
        )
        return coordinator

    return _build


async def _done() -> None:
    return None


def test_minimal_driver_is_instantiable():
    """Only abstract members are needed, so the class must have none left over."""
    assert MinimalDriver.__abstractmethods__ == frozenset()


def test_apply_config_set_charge_cutoff_and_standby_are_abstract():
    assert {"apply_config", "set_charge_cutoff", "standby"} <= (
        BatteryDriver.__abstractmethods__
    )


def test_minimal_driver_has_empty_definitions_and_no_packs():
    driver = MinimalDriver()
    assert driver.sensor_definitions == []
    assert driver.number_definitions == []
    assert driver.select_definitions == []
    assert driver.switch_definitions == []
    assert driver.binary_sensor_definitions == []
    assert driver.button_definitions == []
    assert driver.all_definitions == []
    assert not driver.pack_field_specs


@pytest.mark.asyncio
async def test_minimal_driver_runs_the_coordinator_lifecycle(build_coordinator):
    coordinator = build_coordinator()
    driver = coordinator.driver
    assert type(driver) is MinimalDriver

    # Startup: connect, RS485 re-assert, first read, one-time config write.
    assert await coordinator.connect() is True
    assert await coordinator.set_rs485_control(True) is False
    assert await coordinator.rs485_control_enabled() is None
    assert await coordinator.apply_config(
        max_soc_pct=100,
        min_soc_pct=12,
        max_charge_power_w=800,
        max_discharge_power_w=800,
    ) is True

    # One poll cycle.
    data = await coordinator._async_update_data()
    assert data["battery_soc"] == 50

    # Control: power command, charge cutoff.
    result = await coordinator.apply_power(-300)
    assert result.ok and result.net_power_w == -300
    assert await coordinator.set_charge_cutoff(90) is False

    # Teardown.
    assert await coordinator.standby() is True
    assert await coordinator.set_rs485_control(False) is False

    assert driver.calls == [
        "apply_config",
        "apply_setpoint:-300",
        "set_charge_cutoff",
        "standby",
    ]


@pytest.mark.asyncio
async def test_minimal_driver_reaches_every_platform_definition_loop(monkeypatch, build_coordinator):
    """Each platform reads its definition list (and per-pack fields) off the driver."""
    from custom_components.omnibattery import (
        binary_sensor,
        button,
        number,
        select,
        sensor,
        switch,
    )

    coordinator = build_coordinator()
    coordinator.data = {"battery_soc": 50, "battery_power": 0}
    entry = SimpleNamespace(entry_id="entry", data={}, options={})
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry": {"coordinators": [coordinator]}}}
    monkeypatch.setattr(
        sensor, "_remove_obsolete_solar_entities", lambda *args, **kwargs: None
    )

    for platform in (binary_sensor, button, number, select, sensor, switch):
        added: list = []
        await platform.async_setup_entry(hass, entry, added.extend)
