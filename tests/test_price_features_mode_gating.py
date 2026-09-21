"""Price-only features must not surface on time-slot installs.

Their enable keys are backfilled on every entry, so presence says nothing about
the active predictive mode. The control side (control/high_price_discharge.py,
surplus_price_hold.py, discharge_reserve.py) bails out unless the mode is
dynamic pricing, so on a time-slot install these switches and sliders were dead
rows on the dashboard.
"""
import asyncio
from types import SimpleNamespace

from custom_components.omnibattery.const import (
    CONF_DISCHARGE_RESERVE_ENABLED,
    CONF_DISCHARGE_RESERVE_MIN_SAVING,
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_HIGH_PRICE_DISCHARGE_ENABLED,
    CONF_PREDICTIVE_CHARGING_MODE,
    CONF_SURPLUS_HOLD_MIN_SAVING,
    CONF_SURPLUS_PRICE_HOLD_ENABLED,
    DOMAIN,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.number import (
    async_setup_entry as async_setup_numbers,
)
from custom_components.omnibattery.switch import (
    DischargeReserveSwitch,
    HighPriceDischargeSwitch,
    SurplusPriceHoldSwitch,
    async_setup_entry as async_setup_switches,
)

PRICE_SWITCHES = (
    HighPriceDischargeSwitch,
    SurplusPriceHoldSwitch,
    DischargeReserveSwitch,
)
PRICE_SLIDERS = {
    CONF_SURPLUS_HOLD_MIN_SAVING,
    CONF_DISCHARGE_RESERVE_MIN_SAVING,
}


def _setup(mode):
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={
            CONF_ENABLE_PREDICTIVE_CHARGING: True,
            CONF_PREDICTIVE_CHARGING_MODE: mode,
            # Backfilled on every entry, whatever the mode.
            CONF_HIGH_PRICE_DISCHARGE_ENABLED: False,
            CONF_SURPLUS_PRICE_HOLD_ENABLED: False,
            CONF_DISCHARGE_RESERVE_ENABLED: False,
        },
    )
    controller = SimpleNamespace(
        predictive_charging_enabled=True,
        predictive_charging_mode=mode,
        weekly_full_charge_enabled=False,
    )
    hass = SimpleNamespace(
        data={DOMAIN: {entry.entry_id: {"coordinators": [], "controller": controller}}}
    )
    switches: list = []
    numbers: list = []
    asyncio.run(async_setup_switches(hass, entry, lambda e: switches.extend(e)))
    asyncio.run(async_setup_numbers(hass, entry, lambda e: numbers.extend(e)))
    return switches, numbers


def _slider_keys(numbers):
    return {getattr(n, "_key", None) for n in numbers}


def test_time_slot_install_gets_no_price_controls():
    switches, numbers = _setup(PREDICTIVE_MODE_TIME_SLOT)

    assert not [s for s in switches if isinstance(s, PRICE_SWITCHES)]
    assert not (_slider_keys(numbers) & PRICE_SLIDERS)


def test_dynamic_pricing_install_keeps_them():
    switches, numbers = _setup(PREDICTIVE_MODE_DYNAMIC_PRICING)

    for cls in PRICE_SWITCHES:
        assert any(isinstance(s, cls) for s in switches), cls.__name__
    assert _slider_keys(numbers) >= PRICE_SLIDERS
