"""Options flow: an existing battery's MAC tracking survives a pass through Configure.

The opt-in and the MAC have no field in the connection steps; they appear on the
battery_limits form. These tests walk the steps the way the UI does, from the
battery count on, accepting every default, so the form sees only what the flow
carries from one step to the next.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.omnibattery import config_flow as cf
from custom_components.omnibattery.config_flow import OptionsFlowHandler
from custom_components.omnibattery.infra.mac_tracking import CONF_MAC, CONF_TRACK_MAC

MAC = "00:00:5e:00:53:01"

STORED_BATTERY = {
    "brand": "marstek",
    "name": "Venus 1",
    "host": "192.0.2.10",
    "port": 502,
    "serial_port": "",
    "rs485_gateway": False,
    "slave_id": 1,
    "battery_version": "v3",
    "max_charge_power": 2500,
    "max_discharge_power": 2500,
    "max_soc": 100,
    "min_soc": 12,
    "charge_hysteresis_percent": 2,
    "backup_offgrid_threshold": 50,
    CONF_TRACK_MAC: True,
    CONF_MAC: MAC,
}


@pytest.fixture(autouse=True)
def empty_dhcp_cache(monkeypatch):
    """Home Assistant holds no lease for the battery, so only the stored MAC can fill the field."""
    monkeypatch.setattr(cf, "detect_mac", lambda discovered, host: None)


def _options_flow(batteries) -> OptionsFlowHandler:
    entry = SimpleNamespace(entry_id="test-entry", data={"batteries": batteries}, options={})
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: entry if entry_id == entry.entry_id else None
        )
    )
    flow.handler = entry.entry_id
    flow._test_connection = AsyncMock(return_value=True)
    return flow


def _defaults(form) -> dict:
    return {
        marker.schema: marker.default()
        for marker in form["data_schema"].schema
        if callable(marker.default)
    }


async def _walk_to_limits_form(flow):
    """Submit battery_count, battery_brand and battery_connection as they open."""
    form = await flow.async_step_battery_count()
    form = await flow.async_step_battery_count(_defaults(form))
    form = await flow.async_step_battery_brand(_defaults(form))
    return await flow.async_step_battery_connection(_defaults(form))


async def test_limits_form_opens_with_the_stored_mac_tracking():
    flow = _options_flow([dict(STORED_BATTERY)])

    form = await _walk_to_limits_form(flow)

    assert form["step_id"] == "battery_limits"
    defaults = _defaults(form)
    assert defaults[CONF_TRACK_MAC] is True
    assert defaults[CONF_MAC] == MAC


async def test_saving_without_touching_mac_tracking_keeps_it():
    flow = _options_flow([dict(STORED_BATTERY)])
    form = await _walk_to_limits_form(flow)

    with patch.object(
        OptionsFlowHandler, "_save_and_finish", AsyncMock(return_value={"type": "create_entry"})
    ):
        await flow.async_step_battery_limits(_defaults(form))

    saved = flow.config_data["batteries"][0]
    assert saved[CONF_TRACK_MAC] is True
    assert saved[CONF_MAC] == MAC
