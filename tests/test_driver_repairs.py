"""The Repairs fix flow that lets a battery's driver fix an issue it raised."""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.data_entry_flow import FlowResultType

from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.drivers.base import BatteryDriver
from custom_components.omnibattery.repairs import async_create_fix_flow

ISSUE = "indevolt_flash_writes_paused_192.0.2.1_8080"


class FixingDriver:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.fixed: list[str] = []

    async def async_fix_issue(self, issue_id: str) -> bool:
        self.fixed.append(issue_id)
        return self.result


def _hass(*coordinators):
    return SimpleNamespace(data={DOMAIN: {
        "some_other_key": object(),
        "entry": {"coordinators": list(coordinators)},
    }})


async def _flow(hass, device_key="192.0.2.1_8080"):
    flow = await async_create_fix_flow(hass, ISSUE, {"device_key": device_key})
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "flow"
    flow.context = {"issue_id": ISSUE}
    return flow


async def test_submitting_lets_the_batterys_driver_fix_the_issue():
    driver = FixingDriver()
    other = FixingDriver()
    hass = _hass(SimpleNamespace(device_key="192.0.2.9_8080", driver=other),
                 SimpleNamespace(device_key="192.0.2.1_8080", driver=driver))
    result = await (await _flow(hass)).async_step_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert driver.fixed == [ISSUE] and other.fixed == []


async def test_the_issue_stays_when_no_loaded_driver_fixes_it():
    for hass in (_hass(), _hass(SimpleNamespace(device_key="192.0.2.1_8080",
                                                driver=FixingDriver(result=False)))):
        result = await (await _flow(hass)).async_step_confirm({})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "battery_unavailable"


async def test_other_drivers_fix_nothing_by_default():
    class Plain(BatteryDriver):
        pass

    for name in list(getattr(Plain, "__abstractmethods__", ())):
        setattr(Plain, name, lambda *a, **k: None)
    Plain.__abstractmethods__ = frozenset()
    assert await Plain().async_fix_issue(ISSUE) is False
