"""Hardware driver abstraction for the energy manager.

A *driver* owns all brand-specific hardware I/O — transport, connection
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

See ``docs/plans/driver_abstraction.md`` for the phased extraction plan.
"""

from .base import (
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)
from .esphome import EsphomeEntityDriver
from .marstek import MarstekModbusDriver
from .zendure import ZendureLocalDriver
from .anker import AnkerModbusDriver
from .sessy import SessyLocalDriver
from .hoymiles import HoymilesMqttDriver
from .huawei import HuaweiSolarDriver

__all__ = [
    "BatteryDriver",
    "DriverCapabilities",
    "ReadGroup",
    "SetpointResult",
    "TelemetrySnapshot",
    "EsphomeEntityDriver",
    "MarstekModbusDriver",
    "ZendureLocalDriver",
    "AnkerModbusDriver",
    "SessyLocalDriver",
    "HoymilesMqttDriver",
    "HuaweiSolarDriver",
]
