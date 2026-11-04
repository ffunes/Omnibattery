"""Zero capacity selects automatic detection, including in existing entries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow, OptionsFlowHandler


@pytest.mark.parametrize("options", [False, True])
@pytest.mark.parametrize("brand", ["zendure", "hoymiles"])
async def test_zero_capacity_validates_and_persists(options, brand):
    battery = {"brand": brand, "battery_capacity_kwh": 5.0}
    if options:
        entry = SimpleNamespace(entry_id="capacity-test", data={"batteries": [battery]})
        flow = OptionsFlowHandler(entry)
        flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_known_entry=lambda _: entry))
        flow.handler = entry.entry_id
        flow._save_and_finish = AsyncMock(return_value={"type": "create_entry"})
    else:
        flow = MarstekVenusConfigFlow()
        flow.async_step_time_slots = AsyncMock(return_value={"type": "form"})
    flow.config_data = {"num_batteries": 1}
    flow._current_battery_data = battery
    form = await flow.async_step_battery_limits()
    schema = form["data_schema"]
    inputs = {"max_charge_power": 800, "max_discharge_power": 800,
              "max_soc": 100, "min_soc": 10, "battery_capacity_kwh": 0}
    validated = schema(inputs)
    with pytest.raises(vol.Invalid):
        schema({**inputs, "battery_capacity_kwh": -1})
    await flow.async_step_battery_limits(validated)
    assert flow.battery_configs[0]["battery_capacity_kwh"] == 0
