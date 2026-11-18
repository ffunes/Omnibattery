"""Local wall-clock time in Home Assistant's time zone."""
from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util


def local_now() -> datetime:
    """Return naive local time in Home Assistant's configured time zone.

    ``datetime.now()`` reads the process (OS) zone, which Home Assistant never
    sets: on a host running UTC it disagrees with the price slots, which are
    normalised to naive Home Assistant local time.
    """
    return dt_util.now().replace(tzinfo=None)
