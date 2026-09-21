"""Pre-discharge sliders after the export knob was dropped.

The two remaining sliders used to share a setter that pushed every value
through the export normalizer, which clamped the negative injection threshold
to zero and rewrote the export mode on every reserve change. And a 0% reserve
lets anti-curtailment empty the fleet down to each battery's own min SOC for a
forecast that may never arrive, so an unset reserve must read 20%, not 0%.
"""

import asyncio
from types import SimpleNamespace

from custom_components.omnibattery.number import SmartPredischargeNumber
from custom_components.omnibattery.const import (
    DOMAIN,
    CONF_NEGATIVE_INJECTION_THRESHOLD,
    CONF_PREDISCHARGE_RESERVE_SOC,
    DEFAULT_PREDISCHARGE_RESERVE_SOC,
)


class _Entries:
    def __init__(self, entry):
        self.entry = entry

    def async_update_entry(self, entry, data):
        entry.data = data


def _number(kind, data):
    entry = SimpleNamespace(entry_id="e", data=dict(data))
    hass = SimpleNamespace(
        config_entries=_Entries(entry),
        data={DOMAIN: {"e": {}}},
    )
    number = SmartPredischargeNumber(hass, entry, kind)
    number.async_write_ha_state = lambda: None  # not added to a real hass
    return number, entry


def test_unset_reserve_reads_the_twenty_percent_default():
    number, _ = _number("reserve", {})

    assert number.native_value == DEFAULT_PREDISCHARGE_RESERVE_SOC == 20.0


def test_negative_threshold_keeps_its_sign():
    number, entry = _number("threshold", {})

    asyncio.run(number.async_set_native_value(-0.05))

    assert entry.data[CONF_NEGATIVE_INJECTION_THRESHOLD] == -0.05
    assert number.native_value == -0.05


def test_writing_the_reserve_touches_only_the_reserve():
    number, entry = _number("reserve", {CONF_NEGATIVE_INJECTION_THRESHOLD: -0.02})

    asyncio.run(number.async_set_native_value(35))

    assert entry.data == {
        CONF_NEGATIVE_INJECTION_THRESHOLD: -0.02,
        CONF_PREDISCHARGE_RESERVE_SOC: 35,
    }
