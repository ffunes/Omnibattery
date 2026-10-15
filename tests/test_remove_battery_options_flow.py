"""Options flow: remove one specific battery, not just the last one."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.omnibattery.config_flow import OptionsFlowHandler
from custom_components.omnibattery.const import DOMAIN


def _entry(batteries):
    return SimpleNamespace(
        entry_id="test-entry",
        data={"batteries": batteries, "primary_battery": "Venus 1"},
    )


def _options_flow(entry) -> OptionsFlowHandler:
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: entry if entry_id == entry.entry_id else None
        )
    )
    flow.handler = entry.entry_id
    return flow


async def test_single_battery_skips_submenu_to_count_form():
    flow = _options_flow(_entry([{"name": "Venus 1"}]))
    flow.async_show_form = lambda **kw: kw
    assert (await flow.async_step_batteries())["step_id"] == "battery_count"


async def test_removes_first_battery_and_its_device():
    batteries = [
        {"name": "Venus 1", "host": "10.0.0.1", "port": 502},
        {"name": "Zendure", "host": "10.0.0.2", "port": 80},
        {"name": "Venus 2", "host": "10.0.0.3", "port": 502, "slave_id": 2},
    ]
    flow = _options_flow(_entry(batteries))
    assert (await flow.async_step_batteries())["menu_options"] == ["battery_count", "remove_battery"]

    dev_reg = MagicMock()
    dev_reg.async_get_device_by_identifier.return_value = SimpleNamespace(id="dev-venus-1")
    with (
        patch.object(OptionsFlowHandler, "_save_and_finish", AsyncMock(return_value={"type": "create_entry"})),
        patch("custom_components.omnibattery.config_flow.dr.async_get", return_value=dev_reg),
    ):
        await flow.async_step_remove_battery({"battery": "0"})

    assert [b["name"] for b in flow.config_data["batteries"]] == ["Zendure", "Venus 2"]
    assert flow.config_data["primary_battery"] == ""
    dev_reg.async_get_device_by_identifier.assert_called_once_with(
        (DOMAIN, "10.0.0.1_502"), "test-entry"
    )
    dev_reg.async_get_device.assert_not_called()
    dev_reg.async_remove_device.assert_called_once_with("dev-venus-1")
