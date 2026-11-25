"""Registry of battery drivers: the one place a new brand is listed.

The dict order is the order of the config flow's brand selector. A brand the
registry does not know falls back to ``DEFAULT_BRAND``, which is what the
coordinator has always done with an unrecognised ``brand`` value.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from .anker import AnkerModbusDriver
from .apsystems_ezhi import ApsystemsEzhiDriver
from .base import BatteryDriver, DriverTraits
from .esphome import EsphomeEntityDriver
from .generic_ha import GenericHaEntityDriver
from .hoymiles import HoymilesMqttDriver
from .huawei import HuaweiSolarDriver
from .indevolt import IndevoltDriver
from .marstek import MarstekModbusDriver
from .sessy import SessyLocalDriver
from .zendure import ZendureLocalDriver

_LOGGER = logging.getLogger(__name__)

DRIVERS: dict[str, type[BatteryDriver]] = {
    "marstek": MarstekModbusDriver,
    "zendure": ZendureLocalDriver,
    "esphome": EsphomeEntityDriver,
    "anker": AnkerModbusDriver,
    "sessy": SessyLocalDriver,
    "indevolt": IndevoltDriver,
    "hoymiles": HoymilesMqttDriver,
    "huawei": HuaweiSolarDriver,
    "generic_ha": GenericHaEntityDriver,
    "apsystems_ezhi": ApsystemsEzhiDriver,
}
DEFAULT_BRAND = "marstek"


def driver_class(brand: str | None) -> type[BatteryDriver]:
    """Return the driver class for ``brand``; unknown or None -> ``DEFAULT_BRAND``."""
    cls = DRIVERS.get(brand) if brand is not None else None
    if cls is None:
        _LOGGER.warning("Unknown battery brand %r, using %s", brand, DEFAULT_BRAND)
        cls = DRIVERS[DEFAULT_BRAND]
    return cls


def setup_for(brand: str | None):
    """The ``DriverSetup`` of ``brand``'s driver, or None (no setup, or not registered)."""
    cls = DRIVERS.get(brand) if brand is not None else None
    return cls.SETUP if cls is not None else None


def traits_for_brand(brand: str | None, battery_config: Mapping) -> DriverTraits:
    """Traits of the driver class that serves ``brand`` for this stored config.

    An unregistered brand is served by the ``DEFAULT_BRAND`` class.
    """
    cls = DRIVERS.get(brand) if brand is not None else None
    return (cls or DRIVERS[DEFAULT_BRAND]).traits_for(battery_config)


def redact_keys() -> frozenset[str]:
    """Config keys to redact from diagnostics: the base's plus every driver's."""
    keys = set(BatteryDriver.REDACT_KEYS)
    for cls in DRIVERS.values():
        keys |= cls.REDACT_KEYS
    return frozenset(keys)


def is_registered(brand: str | None) -> bool:
    """True when ``brand`` has its own driver (not the ``DEFAULT_BRAND`` fallback).

    The config flow uses it to pick the default brand's setup for an unknown brand.
    """
    return brand in DRIVERS


def is_ip_addressed(battery_config: Mapping) -> bool:
    """True when this stored battery's driver says it is addressed by IP.

    An unknown brand is served by ``DEFAULT_BRAND``; an entry without ``brand`` is the default.
    """
    cls = driver_class(battery_config.get("brand", DEFAULT_BRAND))
    return cls.traits_for(battery_config).ip_addressed
