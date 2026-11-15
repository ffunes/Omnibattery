"""Registry of battery drivers: the one place a new brand is listed.

The dict order is the order of the config flow's brand selector. A brand the
registry does not know falls back to ``DEFAULT_BRAND``, which is what the
coordinator has always done with an unrecognised ``brand`` value.
"""
from __future__ import annotations

import logging

from .anker import AnkerModbusDriver
from .base import BatteryDriver
from .esphome import EsphomeEntityDriver
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
}
DEFAULT_BRAND = "marstek"


def driver_class(brand: str | None) -> type[BatteryDriver]:
    """Return the driver class for ``brand``; unknown or None -> ``DEFAULT_BRAND``."""
    cls = DRIVERS.get(brand) if brand is not None else None
    if cls is None:
        _LOGGER.warning("Unknown battery brand %r, using %s", brand, DEFAULT_BRAND)
        cls = DRIVERS[DEFAULT_BRAND]
    return cls


def redact_keys() -> frozenset[str]:
    """Config keys to redact from diagnostics: the base's plus every driver's."""
    keys = set(BatteryDriver.REDACT_KEYS)
    for cls in DRIVERS.values():
        keys |= cls.REDACT_KEYS
    return frozenset(keys)
