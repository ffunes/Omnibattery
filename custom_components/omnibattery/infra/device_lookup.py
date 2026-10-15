"""Find a battery's device in the device registry, on old and new Home Assistant.

Home Assistant 2026.10 deprecates ``DeviceRegistry.async_get_device`` (device
identifiers are only unique within a config entry now) and removes it in
2027.8. The replacement, ``async_get_device_by_identifier``, does not exist on
the older releases Omnibattery still supports, so this helper uses whichever
the running version has.
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr


def get_entry_device(
    dev_reg: dr.DeviceRegistry, identifier: tuple[str, str], config_entry_id: str
) -> dr.DeviceEntry | None:
    """Return the device with ``identifier`` that belongs to ``config_entry_id``."""
    lookup = getattr(dev_reg, "async_get_device_by_identifier", None)
    if lookup is not None:
        return lookup(identifier, config_entry_id)
    return dev_reg.async_get_device(identifiers={identifier})
