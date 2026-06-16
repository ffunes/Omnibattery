"""Hardware driver abstraction for the energy manager.

A *driver* owns all brand-specific hardware I/O — transport, connection
lifecycle, telemetry decoding, and control commands — behind a single
brand-agnostic interface (:class:`base.BatteryDriver`). The coordinator and the
control loop talk only to that interface, so a second battery brand is added by
writing a new driver, not by editing the control logic.

Drivers:
  - ``marstek``: Modbus-TCP, register based, polled (the original hardware).
  - ``zendure`` (planned): local MQTT, property based, push telemetry.

See ``docs/plans/driver_abstraction.md`` for the phased extraction plan.
"""

from .base import (
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)
from .marstek import MarstekModbusDriver

__all__ = [
    "BatteryDriver",
    "DriverCapabilities",
    "ReadGroup",
    "SetpointResult",
    "TelemetrySnapshot",
    "MarstekModbusDriver",
]
