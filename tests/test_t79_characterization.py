"""Characterization for T7.9: discharge-limit alias, pack fill and balance baselines.

Pinned on 9638a55d (before ``canonical_key``, ``sequential_pack_fill`` and
``balance_baseline`` existed) with the real eight drivers, Marstek in its four
versions, so the three moves into the driver can be checked against literals:

* #23  which drivers report ``needs_software_max_discharge`` and how the number
       entity of the discharge-limit definition writes (Zendure's key is
       ``inverse_max_power`` and its entity id / unique_id must not change);
* #46  what ``control_vmax`` decides with and without per-pack SOC;
* #47  the balance monitor's status, trend gate and notification for every driver.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.omnibattery.control.pack_soc import control_vmax
from custom_components.omnibattery.number import MarstekVenusNumber
from custom_components.omnibattery.tracking.balance_monitor import BalanceMonitor
from tests.test_cell_voltage_profile_characterization import (
    CASES,
    IDS,
    PUBLISHERS,
    _coordinator,
    _no_ha_runtime,  # noqa: F401 (autouse: stub DataUpdateCoordinator.__init__)
)

# ---------------------------------------------------------------------------
# #23 discharge-limit alias
# ---------------------------------------------------------------------------

#: ``needs_software_max_discharge`` as observed per case.
NEEDS_SOFTWARE_MAX_DISCHARGE = {
    "marstek": False, "zendure": False, "esphome": False, "anker": True,
    "sessy": True, "indevolt": False, "huawei": True, "hoymiles": True,
}
#: Key of the writable discharge-limit number, None = the driver has none.
DISCHARGE_LIMIT_KEY = {
    "marstek": "max_discharge_power", "zendure": "inverse_max_power",
    "esphome": "max_discharge_power", "anker": None, "sessy": None,
    "indevolt": "max_discharge_power", "huawei": None, "hoymiles": None,
}


@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_needs_software_max_discharge(brand, version):
    coord = _coordinator(brand, version, 3.3)
    assert coord.needs_software_max_discharge is NEEDS_SOFTWARE_MAX_DISCHARGE[brand]


@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_discharge_limit_number_writes_through_the_coordinator(brand, version):
    coord = _coordinator(brand, version, 3.3)
    expected_key = DISCHARGE_LIMIT_KEY[brand]
    candidates = [
        d for d in coord.number_definitions
        if d["key"] in ("max_discharge_power", "inverse_max_power")
    ]
    assert [d["key"] for d in candidates] == ([expected_key] if expected_key else [])
    for definition in candidates:
        entity = MarstekVenusNumber(coord, definition)
        # Entity identity is the definition's own key.
        assert entity.unique_id == f"{coord.device_key}_{definition['key']}"
        entity.hass = SimpleNamespace(data={})
        entity.async_write_ha_state = Mock()
        coord.write_control = AsyncMock(return_value=False)
        coord.persist_battery_config = Mock()
        coord._config_entry = None

        await entity.async_set_native_value(1234)

        assert coord.configured_max_discharge_power == 1234
        # Venus E v2/v3 also persist the user cap (software power cap path).
        coord.persist_battery_config.assert_any_call("max_discharge_power", 1234)
        coord.write_control.assert_awaited_once_with(definition["key"], 1234, do_refresh=True)


async def test_zendure_keeps_its_entity_key_for_the_discharge_limit():
    coord = _coordinator("zendure", None, 3.3)
    keys = {d["key"] for d in coord.number_definitions}
    assert "inverse_max_power" in keys
    assert "max_discharge_power" not in keys


@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_other_numbers_leave_the_discharge_limit_alone(brand, version):
    coord = _coordinator(brand, version, 3.3)
    others = [
        d for d in coord.number_definitions
        if d["key"] in ("soc_set", "min_soc", "max_charge_power") and "min" in d
    ]
    for definition in others:
        entity = MarstekVenusNumber(coord, definition)
        entity.hass = SimpleNamespace(data={})
        entity.async_write_ha_state = Mock()
        coord.write_control = AsyncMock(return_value=False)
        coord.persist_battery_config = Mock()
        before = coord.configured_max_discharge_power

        await entity.async_set_native_value(definition["min"])

        assert coord.configured_max_discharge_power == before
        assert ("max_discharge_power", definition["min"]) not in [
            c.args for c in coord.persist_battery_config.call_args_list
        ]


# ---------------------------------------------------------------------------
# #46 sequential pack fill
# ---------------------------------------------------------------------------

PACK_VERSIONS = {"vA", "vD"}
PACK_SETS = {
    "none": {},
    "all_full": {"battery_soc_pack_1": 100, "battery_soc_pack_2": 100},
    "one_lagging": {"battery_soc_pack_1": 100, "battery_soc_pack_2": 98},
    "at_top": {"battery_soc_pack_1": 99, "battery_soc_pack_2": 99},
    "half": {"battery_soc_pack_1": 50},
    "out_of_range_ignored": {"battery_soc_pack_1": 100, "battery_soc_pack_2": 250},
}


@pytest.mark.parametrize("packs", PACK_SETS, ids=PACK_SETS)
@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_control_vmax(brand, version, packs):
    has_packs = version in PACK_VERSIONS
    coord = _coordinator(brand, version, 3.52)
    if has_packs:
        coord.data = {**coord.data, **PACK_SETS[packs]}
    elif packs != "none":
        pytest.skip("only the coupled-pack Venus A/D publish per-pack SOC")

    publishes_vmax = brand in PUBLISHERS
    lagging = packs in ("one_lagging", "half")
    if not publishes_vmax or (has_packs and lagging):
        assert control_vmax(coord) is None
    else:
        assert control_vmax(coord) == 3.52


# ---------------------------------------------------------------------------
# #47 balance baseline
# ---------------------------------------------------------------------------

DELTAS = [0, 150, 199, 200, 229, 230, 249, 250, 400]
# Status the measurement itself reports: the raw delta against 200/230/250 mV.
STATUS = {
    0: "green", 150: "green", 199: "green", 200: "yellow", 229: "yellow",
    230: "orange", 249: "orange", 250: "red", 400: "red",
}
# Status of a stored reading: the delta minus the 180 mV factory baseline.
STORED_STATUS = {
    0: "green", 379: "green", 380: "yellow", 409: "yellow", 410: "orange",
    429: "orange", 430: "red", 600: "red",
}


def _monitor(coord):
    sent = []
    monitor = BalanceMonitor.__new__(BalanceMonitor)
    monitor._hass = SimpleNamespace()
    monitor._controller = SimpleNamespace(
        _create_entry_background_task=lambda coro, name: sent.append(coro)
    )
    monitor._store = SimpleNamespace(async_save=AsyncMock())
    monitor._data = {}
    monitor._states = {}
    monitor._sensor_groups = {}
    notifications = []

    async def _notify(notification_id, title, message):
        notifications.append((notification_id, title, message))

    monitor._notify = _notify
    return monitor, sent, notifications


@pytest.mark.parametrize("delta", DELTAS)
@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_balance_status_per_delta(brand, version, delta):
    coord = _coordinator(brand, version, 3.52)
    monitor, _sent, _notes = _monitor(coord)
    status = await monitor._save_reading(
        coord.device_key, delta, 3.5, 3.4, 99, "top_balance_measurement", coord
    )
    for coro in _sent:
        coro.close()
    assert status == STATUS[delta]


@pytest.mark.parametrize("delta", sorted(STORED_STATUS))
@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_stored_reading_status_per_delta(brand, version, delta):
    coord = _coordinator(brand, version, 3.52)
    monitor, _sent, _notes = _monitor(coord)
    await monitor._save_reading(
        coord.device_key, delta, 3.5, 3.4, 99, "top_balance_measurement", coord
    )
    for coro in _sent:
        coro.close()
    assert monitor.get_initial_state(
        coord.device_key, coord.capabilities.balance_baseline
    )["status"] == STORED_STATUS[delta]


@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_balance_notification(brand, version):
    coord = _coordinator(brand, version, 3.52)
    monitor, sent, notes = _monitor(coord)
    # One red reading already on record: this one is the second consecutive.
    monitor._data[coord.device_key] = {"readings": [], "consecutive_red": 1}
    await monitor._save_reading(
        coord.device_key, 400, 3.6, 3.2, 99, "top_balance_measurement", coord
    )
    assert len(sent) == 1
    await sent[0]
    (notification_id, title, message), = notes
    assert notification_id == f"marstek_venus_marstek_balance_{coord.device_key}"
    assert title == f"🔴 Cell Balance: {coord.name}"
    lines = message.split(chr(10))
    assert lines[0] == "• Delta: 400 mV — high cell imbalance."
    assert len(lines) == 2
    assert lines[1].startswith("• High cell imbalance (400 mV) for 2 consecutive full charges.")
    assert monitor._data[coord.device_key]["consecutive_red"] == 2


@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_balance_trend_gate_uses_the_baseline(brand, version):
    coord = _coordinator(brand, version, 3.52)
    monitor, sent, notes = _monitor(coord)
    # Rising, but the average stays under baseline + 40 mV (220 mV): no alert.
    for delta in (170, 180, 190, 200, 210):
        await monitor._save_reading(
            coord.device_key, delta, 3.5, 3.4, 99, "top_balance_measurement", coord
        )
    assert sent == []

    monitor2, sent2, notes2 = _monitor(coord)
    # Rising and the average clears 220 mV: one trend alert.
    for delta in (214, 219, 224, 229):
        await monitor2._save_reading(
            coord.device_key, delta, 3.5, 3.4, 99, "top_balance_measurement", coord
        )
    assert len(sent2) == 1
    await sent2[0]
    assert notes2[0][2].startswith("• Rising imbalance trend:")
