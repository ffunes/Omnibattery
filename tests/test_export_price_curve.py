"""Tests for the optional export/feed-in price curve.

The export curve reuses the import parsers but must stay isolated from the
import health bookkeeping: a flaky export sensor may not raise the import-price
repair issue, which gates load-bearing pricing features.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    PRICE_INTEGRATION_CKW,
    PRICE_INTEGRATION_ENTSOE,
    PRICE_INTEGRATION_EPEX,
    PRICE_INTEGRATION_NORDPOOL,
    PRICE_INTEGRATION_PVPC,
    PRICE_INTEGRATION_TIBBER,
)
from custom_components.omnibattery.config_flow import (
    _price_integration_export_options,
    _validate_export_price_input,
)
from custom_components.omnibattery.pricing import calculations
from custom_components.omnibattery.pricing.engine import PricingManager


IMPORT_SENSOR = "sensor.import_price"
EXPORT_SENSOR = "sensor.export_price"


def _raw_today(prices: list[float]) -> dict:
    """Build a Nordpool-style attribute payload for today."""
    start = datetime.now().replace(minute=0, second=0, microsecond=0)
    return {
        "raw_today": [
            {
                "start": start + timedelta(hours=index),
                "end": start + timedelta(hours=index + 1),
                "value": price,
            }
            for index, price in enumerate(prices)
        ]
    }


def _hass(states: dict):
    return SimpleNamespace(
        states=SimpleNamespace(get=lambda entity_id: states.get(entity_id))
    )


def _controller(**overrides):
    base = dict(
        price_sensor=IMPORT_SENSOR,
        price_integration_type=PRICE_INTEGRATION_NORDPOOL,
        export_price_sensor=None,
        export_price_integration_type=None,
        _price_data_status="unset",
        _tibber_price_slots=[],
        _nordpool_price_slots=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ----------------------------------------------------------------------
# Provider dispatch extraction
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "integration_type, attrs, expected",
    [
        (PRICE_INTEGRATION_PVPC, {f"price_{h:02d}h": 0.1 for h in range(24)}, 24),
        (PRICE_INTEGRATION_NORDPOOL, _raw_today([0.1, 0.2]), 2),
    ],
)
def test_dispatch_matches_the_individual_parsers(integration_type, attrs, expected):
    assert len(calculations.parse_prices_for_integration(integration_type, attrs)) == expected


def test_dispatch_falls_back_to_nordpool_for_an_unset_type():
    attrs = _raw_today([0.1, 0.2, 0.3])

    assert len(calculations.parse_prices_for_integration(None, attrs)) == 3


@pytest.mark.parametrize(
    "integration_type, key",
    [
        (PRICE_INTEGRATION_NORDPOOL, "raw_today"),
        (PRICE_INTEGRATION_CKW, "prices"),
        (PRICE_INTEGRATION_EPEX, "data"),
        (PRICE_INTEGRATION_ENTSOE, "prices_today"),
    ],
)
def test_stringified_attributes_are_detected_per_provider(integration_type, key):
    assert calculations.stringified_price_attrs(integration_type, {key: "[]"}) == [key]


def test_pvpc_has_no_list_attributes_to_guard():
    assert calculations.stringified_price_attrs(PRICE_INTEGRATION_PVPC, {"x": "[]"}) == []


# ----------------------------------------------------------------------
# Export curve
# ----------------------------------------------------------------------


def test_export_curve_falls_back_to_the_import_curve_when_unset():
    states = {IMPORT_SENSOR: SimpleNamespace(state="0.1", attributes=_raw_today([0.1, 0.2]))}
    manager = PricingManager(_hass(states), _controller())

    horizon = datetime.now() + timedelta(hours=12)
    assert manager.get_future_export_price_slots(horizon) == manager.get_future_price_slots(horizon)


def test_export_curve_reads_its_own_sensor():
    states = {
        IMPORT_SENSOR: SimpleNamespace(state="0.30", attributes=_raw_today([0.30, 0.30])),
        EXPORT_SENSOR: SimpleNamespace(state="0.10", attributes=_raw_today([0.10, 0.10])),
    }
    manager = PricingManager(_hass(states), _controller(export_price_sensor=EXPORT_SENSOR))

    slots = manager.get_future_export_price_slots(datetime.now() + timedelta(hours=12))

    assert slots
    assert {slot.price for slot in slots} == {0.10}


def test_export_parsing_never_touches_the_import_health_status():
    """A broken export sensor must not raise the import-price repair issue."""
    states = {IMPORT_SENSOR: SimpleNamespace(state="0.1", attributes=_raw_today([0.1]))}
    controller = _controller(export_price_sensor=EXPORT_SENSOR)
    manager = PricingManager(_hass(states), controller)

    # Prime the import status, then read a missing export sensor.
    manager.get_future_price_slots()
    healthy_status = controller._price_data_status
    assert healthy_status.startswith("ok")

    assert manager.get_future_export_price_slots() == []
    assert controller._price_data_status == healthy_status


def test_unavailable_export_sensor_degrades_to_an_empty_curve():
    states = {
        IMPORT_SENSOR: SimpleNamespace(state="0.1", attributes=_raw_today([0.1])),
        EXPORT_SENSOR: SimpleNamespace(state="unavailable", attributes={}),
    }
    manager = PricingManager(_hass(states), _controller(export_price_sensor=EXPORT_SENSOR))

    assert manager.get_future_export_price_slots() == []


def test_export_curve_uses_the_import_provider_type_when_unset():
    states = {
        IMPORT_SENSOR: SimpleNamespace(state="0.1", attributes=_raw_today([0.1])),
        EXPORT_SENSOR: SimpleNamespace(state="0.05", attributes=_raw_today([0.05, 0.05])),
    }
    controller = _controller(
        export_price_sensor=EXPORT_SENSOR, export_price_integration_type=None
    )
    manager = PricingManager(_hass(states), controller)

    slots = manager.get_future_export_price_slots(datetime.now() + timedelta(hours=12))

    assert {slot.price for slot in slots} == {0.05}


# ----------------------------------------------------------------------
# Config flow validation
# ----------------------------------------------------------------------


def test_tibber_is_not_offered_as_an_export_provider():
    assert PRICE_INTEGRATION_TIBBER not in _price_integration_export_options()
    assert PRICE_INTEGRATION_NORDPOOL in _price_integration_export_options()


def test_no_export_sensor_is_valid():
    assert _validate_export_price_input(_hass({}), {}, PRICE_INTEGRATION_NORDPOOL) == {}


def test_tibber_is_rejected_for_the_export_curve():
    errors = _validate_export_price_input(
        _hass({}),
        {
            "export_price_sensor": EXPORT_SENSOR,
            "export_price_integration_type": PRICE_INTEGRATION_TIBBER,
        },
        PRICE_INTEGRATION_NORDPOOL,
    )

    assert errors == {"export_price_integration_type": "export_tibber_unsupported"}


def test_export_type_is_required_when_the_import_curve_is_service_based():
    errors = _validate_export_price_input(
        _hass({}),
        {"export_price_sensor": EXPORT_SENSOR},
        PRICE_INTEGRATION_TIBBER,
    )

    assert errors == {"export_price_integration_type": "export_type_required"}


def test_export_sensor_without_price_data_is_rejected():
    states = {EXPORT_SENSOR: SimpleNamespace(state="0.1", attributes={})}
    errors = _validate_export_price_input(
        _hass(states),
        {"export_price_sensor": EXPORT_SENSOR},
        PRICE_INTEGRATION_NORDPOOL,
    )

    assert errors == {"export_price_sensor": "no_price_data"}


def test_missing_export_sensor_entity_is_rejected():
    errors = _validate_export_price_input(
        _hass({}),
        {"export_price_sensor": EXPORT_SENSOR},
        PRICE_INTEGRATION_NORDPOOL,
    )

    assert errors == {"export_price_sensor": "sensor_not_found"}


def test_valid_export_sensor_passes():
    states = {EXPORT_SENSOR: SimpleNamespace(state="0.1", attributes=_raw_today([0.1]))}
    errors = _validate_export_price_input(
        _hass(states),
        {"export_price_sensor": EXPORT_SENSOR},
        PRICE_INTEGRATION_NORDPOOL,
    )

    assert errors == {}


def test_an_official_nordpool_sensor_is_rejected_for_the_export_curve():
    """The service cache belongs to the import curve; the parser needs attributes."""
    import custom_components.omnibattery.config_flow as config_flow

    states = {EXPORT_SENSOR: SimpleNamespace(state="0.1", attributes={})}
    original = config_flow.is_official_nordpool_sensor
    config_flow.is_official_nordpool_sensor = lambda *_args, **_kwargs: True
    try:
        errors = _validate_export_price_input(
            _hass(states),
            {"export_price_sensor": EXPORT_SENSOR},
            PRICE_INTEGRATION_NORDPOOL,
        )
        # The import curve still accepts it.
        import_error = config_flow._validate_price_sensor(
            _hass(states), EXPORT_SENSOR, PRICE_INTEGRATION_NORDPOOL
        )
    finally:
        config_flow.is_official_nordpool_sensor = original

    assert errors == {"export_price_sensor": "no_price_data"}
    assert import_error is None


def test_the_export_fallback_leaves_the_import_health_status_alone():
    """A short horizon that empties after sunset must not flap the diagnostic."""
    states = {IMPORT_SENSOR: SimpleNamespace(state="0.1", attributes=_raw_today([0.1]))}
    controller = _controller()
    manager = PricingManager(_hass(states), controller)

    manager.get_future_price_slots()
    healthy_status = controller._price_data_status
    assert healthy_status.startswith("ok")

    # A horizon in the past yields no slots but says nothing about feed health.
    assert manager.get_future_export_price_slots(datetime.now() - timedelta(days=1)) == []
    assert controller._price_data_status == healthy_status


def test_the_export_fallback_still_reports_a_real_import_fault():
    """Only the empty-horizon artifact is masked, never a broken feed."""
    states = {IMPORT_SENSOR: SimpleNamespace(state="unavailable", attributes={})}
    controller = _controller(_price_data_status="ok (24 slots)")
    manager = PricingManager(_hass(states), controller)

    assert manager.get_future_export_price_slots(datetime.now() + timedelta(hours=12)) == []
    assert controller._price_data_status != "ok (24 slots)"
