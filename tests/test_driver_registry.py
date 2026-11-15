"""Driver registry: ordering, brand ids, fallback and the redaction union."""
from __future__ import annotations

import logging

from custom_components.omnibattery.diagnostics import TO_REDACT
from custom_components.omnibattery.drivers.base import BatteryDriver
from custom_components.omnibattery.drivers.marstek import MarstekModbusDriver
from custom_components.omnibattery.drivers.registry import (
    DEFAULT_BRAND,
    DRIVERS,
    driver_class,
    redact_keys,
)


def test_selector_order_is_the_registry_order():
    assert list(DRIVERS)[:8] == [  # a new brand is appended after these
        "marstek", "zendure", "esphome", "anker",
        "sessy", "indevolt", "hoymiles", "huawei",
    ]


def test_each_class_declares_its_own_brand_and_identity():
    for brand, cls in DRIVERS.items():
        assert cls.BRAND == brand
        assert cls.LABEL and cls.MANUFACTURER and cls.DEFAULT_MODEL


def test_unknown_or_missing_brand_falls_back_to_default(caplog):
    with caplog.at_level(logging.WARNING):
        assert driver_class("nope") is MarstekModbusDriver
        assert driver_class(None) is MarstekModbusDriver
    assert DEFAULT_BRAND == "marstek"
    assert "Unknown battery brand" in caplog.text


def test_diagnostics_redaction_set_is_pinned():
    expected = {
        "host", "username", "password", "serial", "serial_port",
        "ip_address", "mac",
    }
    assert set(TO_REDACT) == expected
    assert redact_keys() == frozenset(expected)
    assert BatteryDriver.REDACT_KEYS < redact_keys()
