"""Registry of battery drivers: the one place a new brand is listed.

The dict order is the order of the config flow's brand selector. A brand the
registry does not know falls back to ``DEFAULT_BRAND``, which is what the
coordinator has always done with an unrecognised ``brand`` value.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from .anker import AnkerModbusDriver
from .base import BatteryDriver, DriverTraits
from .esphome import EsphomeEntityDriver
from .hoymiles import HoymilesMqttDriver
from .huawei import HuaweiSolarDriver
from .indevolt import IndevoltDriver
from .marstek import MarstekModbusDriver
from .sessy import SessyLocalDriver
from .zendure import ZendureLocalDriver
from .apsystems_ezhi import ApsystemsEzhiDriver

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
lifecycle, telemetry decoding, and control commands — behind a single
brand-agnostic interface (:class:`base.BatteryDriver`). The coordinator and the
control loop talk only to that interface, so a second battery brand is added by
writing a new driver, not by editing the control logic.

Architectural rule: shared calculations, state representations, orchestration,
and user-facing behavior remain brand/model agnostic. Hardware differences are
expressed through semantic driver capabilities or hooks; brand/model branches
are limited to driver selection, configuration, and migrations.

Drivers:
  - ``marstek``: Modbus-TCP, register based, polled (the original hardware).
  - ``zendure``: local HTTP REST, property based, polled (SolarFlow series).
  - ``esphome``: HA-entity based, push (Marstek behind a LilyGo RS485 bridge).
  - ``anker``: Modbus-TCP, register based, polled (SOLIX Solarbank Max AC / 4 E5000 Pro).
  - ``hoymiles``: HA MQTT based, push telemetry (MS-A2).
  - ``huawei``: native Modbus-TCP telemetry, set-points via huawei_solar
    services (SUN2000 + LUNA2000).
  - ``indevolt``: local HTTP, paced scheduler, realtime RAM tuple only
    (SolidFlex / PowerFlex).

See ``docs/plans/driver_abstraction.md`` for the phased extraction plan.
"""

# Registration lives in ``registry.py``: a new driver is added there, not here.
# The re-exports below are kept for callers that import the classes directly.
from .base import (
    DELIVERED_AC_POWER_KEY,
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)
"""Registry of battery drivers: the one place a new brand is listed.

The dict order is the order of the config flow's brand selector. A brand the
registry does not know falls back to ``DEFAULT_BRAND``, which is what the
coordinator has always done with an unrecognised ``brand`` value.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from .anker import AnkerModbusDriver
from .base import BatteryDriver, DriverTraits
from .esphome import EsphomeEntityDriver
from .hoymiles import HoymilesMqttDriver
from .huawei import HuaweiSolarDriver
from .indevolt import IndevoltDriver
from .marstek import MarstekModbusDriver
from .sessy import SessyLocalDriver
from .zendure import ZendureLocalDriver
from .apsystems_ezhi import ApsystemsEzhiDriver

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

