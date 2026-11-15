"""The coordinator calls ``driver.reconcile`` at four moments and applies the result.

Plan T4.1 (docs/plans/driver-registry/plan.md, section 3.8). The moments are the
ones the per-brand ``_sync_*`` methods ran at before the hook existed:

* ``init``       end of the constructor's power-limit setup
* ``connect``    after ``connect()`` and after ``async_reconnect_fresh()``
* ``read_group`` after each telemetry group read
* ``poll``       after each poll merged its data

A recording driver stands in for every brand: the coordinator has no brand branch,
so what it does with a ``DriverReconciliation`` is the same for all of them.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery.drivers import registry
from custom_components.omnibattery.drivers.base import (
    DriverReconciliation,
    ReconcileState,
)
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from tests.test_minimal_driver_contract import MinimalDriver, _done


class RecordingDriver(MinimalDriver):
    """Minimal driver that records each ``reconcile`` call and returns a canned result."""

    results: dict = {}
    calls: list = []

    @classmethod
    def from_config(cls, battery_config, context):
        return cls()

    def coordinator_attributes(self):
        return {"mirrored": None}

    @property
    def all_definitions(self):
        return [{"key": "battery_soc"}, {"key": "added_by_reconcile"}]

    def reconcile(self, data, *, reason, state=None):
        type(self).calls.append((reason, dict(data), state))
        return type(self).results.get(reason)


@pytest.fixture
def build(monkeypatch):
    RecordingDriver.results = {}
    RecordingDriver.calls = []
    monkeypatch.setitem(registry.DRIVERS, "recording", RecordingDriver)
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)

    def _build():
        coordinator = MarstekVenusDataUpdateCoordinator(
            MagicMock(),
            {
                "name": "rec", "host": "192.0.2.1", "port": 502, "brand": "recording",
                "max_charge_power": 600, "max_discharge_power": 700,
                "max_soc": 100, "min_soc": 12,
            },
            "sensor.grid_power",
        )
        coordinator.hass = MagicMock()
        coordinator.data = {}
        coordinator.persist_battery_config = MagicMock()
        coordinator._alarm_notifier = SimpleNamespace(
            check=lambda *args, **kwargs: _done()
        )
        coordinator._entity_registry = SimpleNamespace(
            async_get_entity_id=lambda *args: None, entities={}
        )
        return coordinator

    return _build


def _reasons():
    return [reason for reason, _data, _state in RecordingDriver.calls]


def test_init_reconciles_at_construction_and_applies_the_limits(build):
    RecordingDriver.results = {
        "init": DriverReconciliation(
            device_max_charge_power=500, device_max_discharge_power=450
        )
    }
    coordinator = build()

    assert _reasons() == ["init"]
    _reason, _data, state = RecordingDriver.calls[0]
    assert isinstance(state, ReconcileState)
    assert state.configured_max_charge_power == 600
    assert state.configured_max_discharge_power == 700
    assert state.attributes == {"mirrored": None}
    assert coordinator.device_max_charge_power == 500
    assert coordinator.device_max_discharge_power == 450
    assert coordinator.effective_max_charge_power == 500
    assert coordinator.effective_max_discharge_power == 450


@pytest.mark.asyncio
async def test_connect_reconciles_and_applies_everything_in_order(build):
    coordinator = build()
    RecordingDriver.calls.clear()
    coordinator.data = {"solar_power": 10, "kept": 1}
    RecordingDriver.results = {
        "connect": DriverReconciliation(
            device_max_charge_power=900,
            device_max_discharge_power=800,
            configured_max_charge_power=300,
            configured_max_discharge_power=250,
            max_soc=90,
            min_soc=15,
            attributes={"mirrored": "new"},
            persist={"model": "x", "device_max_charge_power": 900, "max_soc": 90},
            drop_keys=("solar_power", "absent"),
            definitions_changed=True,
            message="hello",
        )
    }

    assert await coordinator.connect() is True

    assert _reasons() == ["connect"]
    assert coordinator.mirrored == "new"
    assert coordinator.data == {"kept": 1}
    assert set(coordinator._def_by_key) == {"battery_soc", "added_by_reconcile"}
    assert coordinator.device_max_charge_power == 900
    assert coordinator.device_max_discharge_power == 800
    assert coordinator.configured_max_charge_power == 300
    assert coordinator.configured_max_discharge_power == 250
    assert coordinator.max_soc == 90
    assert coordinator.min_soc == 15
    assert coordinator.persist_battery_config.call_args_list == [
        call("model", "x"),
        call("device_max_charge_power", 900),
        call("max_soc", 90),
    ]


@pytest.mark.asyncio
async def test_fresh_reconnect_reconciles_with_reason_connect(build):
    coordinator = build()
    RecordingDriver.calls.clear()
    RecordingDriver.results = {
        "connect": DriverReconciliation(persist={"model": "y"})
    }

    assert await coordinator.async_reconnect_fresh() is True

    # The reconnect probe is a plain read: only the connect moment reconciles.
    assert _reasons() == ["connect"]
    coordinator.persist_battery_config.assert_called_once_with("model", "y")


@pytest.mark.asyncio
async def test_read_group_reconciles_after_each_group_and_before_poll(build):
    coordinator = build()
    RecordingDriver.calls.clear()
    RecordingDriver.results = {
        "read_group": DriverReconciliation(drop_keys=("stale",)),
    }
    coordinator.data = {"stale": 1}

    await coordinator._async_update_data()

    reasons = _reasons()
    assert reasons.count("read_group") == 1  # MinimalDriver has one read group
    assert reasons.count("poll") == 1
    assert reasons.index("read_group") < reasons.index("poll")
    assert "stale" not in coordinator.data


@pytest.mark.asyncio
async def test_poll_reconciles_with_merged_data_and_adopts_the_soc_limits(build):
    coordinator = build()
    RecordingDriver.calls.clear()
    RecordingDriver.results = {
        "poll": DriverReconciliation(
            configured_max_discharge_power=400,
            max_soc=77,
            min_soc=11,
            persist={"max_discharge_power": 400},
            message="synced",
        )
    }

    await coordinator._async_update_data()

    poll_calls = [c for c in RecordingDriver.calls if c[0] == "poll"]
    assert len(poll_calls) == 1
    _reason, data, state = poll_calls[0]
    assert data["battery_soc"] == 50  # the poll's own readings are already merged
    assert state.configured_max_discharge_power == 700
    assert coordinator.configured_max_discharge_power == 400
    assert coordinator.max_soc == 77
    assert coordinator.min_soc == 11
    coordinator.persist_battery_config.assert_called_once_with(
        "max_discharge_power", 400
    )


@pytest.mark.asyncio
async def test_a_driver_without_reconcile_changes_nothing(build):
    RecordingDriver.results = {}
    coordinator = build()
    before = (
        coordinator.device_max_charge_power,
        coordinator.configured_max_charge_power,
        coordinator.max_soc,
        coordinator.min_soc,
    )

    await coordinator.connect()
    await coordinator._async_update_data()

    assert (
        coordinator.device_max_charge_power,
        coordinator.configured_max_charge_power,
        coordinator.max_soc,
        coordinator.min_soc,
    ) == before
    coordinator.persist_battery_config.assert_not_called()


# ---------------------------------------------------------------------------
# Brand drivers: what each returns at each moment (the coordinator side is above)
# ---------------------------------------------------------------------------


def _state(**overrides):
    values = dict(
        device_max_charge_power=2400,
        device_max_discharge_power=2400,
        configured_max_charge_power=1200,
        configured_max_discharge_power=1200,
    )
    values.update(overrides)
    return ReconcileState(**values)


@pytest.mark.parametrize("reason", ["connect", "read_group"])
def test_anker_without_independent_pv_drops_the_pv_keys(reason):
    from custom_components.omnibattery.drivers.anker import AnkerModbusDriver

    driver = AnkerModbusDriver("1.2.3.4", 502, 1, client=MagicMock())
    assert driver.has_independent_pv is False

    result = driver.reconcile({}, reason=reason)

    assert result.drop_keys == ("solar_power", "pv_power", "third_party_pv_power")
    assert driver.reconcile({}, reason="poll") is None
    assert driver.reconcile({}, reason="init") is None


def test_anker_with_independent_pv_keeps_its_keys():
    from custom_components.omnibattery.drivers.anker import AnkerModbusDriver

    driver = AnkerModbusDriver("1.2.3.4", 502, 1, client=MagicMock())
    driver._product_code = "DPM4"

    assert driver.reconcile({}, reason="connect") is None
    assert driver.reconcile({}, reason="read_group") is None


def test_zendure_poll_adopts_soc_set_and_min_soc_only_at_poll():
    from custom_components.omnibattery.drivers.zendure import ZendureLocalDriver

    driver = ZendureLocalDriver("1.2.3.4")
    data = {"soc_set": 85, "min_soc": 10}

    result = driver.reconcile(data, reason="poll", state=_state())

    assert (result.max_soc, result.min_soc) == (85, 10)
    assert result.persist == {}
    assert driver.reconcile(data, reason="read_group", state=_state()) is None
    assert driver.reconcile({}, reason="poll", state=_state()) is None


def test_marstek_vd_clamps_at_init_and_follows_the_ems_at_poll():
    from unittest.mock import AsyncMock

    from custom_components.omnibattery.drivers.marstek import MarstekModbusDriver

    driver = MarstekModbusDriver("1.2.3.4", 502, "vD", client=AsyncMock(), ems_version=149)
    ceiling = driver.capabilities.max_charge_power_w
    state = _state(device_max_charge_power=2500, device_max_discharge_power=2500)

    init = driver.reconcile({}, reason="init", state=state)
    assert init.device_max_charge_power == ceiling
    assert init.persist == {}

    poll = driver.reconcile({"ems_version": 147}, reason="poll", state=state)
    assert poll.attributes == {"ems_version": 147}
    assert list(poll.persist)[:3] == ["ems_version", "device_max_charge_power", "device_max_discharge_power"]
    assert driver.reconcile({"ems_version": 147}, reason="connect", state=state) is None
