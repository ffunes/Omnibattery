"""State class of the consumption profile capture sensor (#956)."""
from __future__ import annotations

from types import SimpleNamespace

from homeassistant.components.sensor import SensorStateClass

from custom_components.omnibattery.sensor import ConsumptionProfileCaptureSensor


class _Profile:
    def __init__(self) -> None:
        self.energy_kwh = 3.786

    def current_day_capture(self):
        return {"energy_kwh": self.energy_kwh}


def test_capture_can_drop_so_state_class_is_total():
    profile = _Profile()
    controller = SimpleNamespace(
        _consumption_tracker=SimpleNamespace(consumption_profile=profile)
    )
    sensor = ConsumptionProfileCaptureSensor(controller)

    assert sensor.native_value == 3.786
    # A better-covered bin replaces captured energy: the total legitimately drops.
    profile.energy_kwh = 3.735
    assert sensor.native_value == 3.735

    assert sensor.state_class == SensorStateClass.TOTAL
    assert sensor.state_class != SensorStateClass.TOTAL_INCREASING
