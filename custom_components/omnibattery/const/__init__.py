"""Constants for the Omnibattery integration.

Backward-compatible facade. Definitions live in:
  - integration_const.py : integration/feature configuration constants
  - registers_common.py  : shared register infra (maps, timing, bit descriptions, calc sensors)
  - registers_v2.py / _v3.py / _va.py / _vd.py : per-model Modbus register & entity definitions

Everything is re-exported here so existing `from .const import X` imports keep working.
"""
from .integration_const import *  # noqa: F401,F403
from .registers_common import *  # noqa: F401,F403
from .registers_v2 import *  # noqa: F401,F403
from .registers_v3 import *  # noqa: F401,F403
from .registers_va import *  # noqa: F401,F403
from .registers_vd import *  # noqa: F401,F403

# Deliberate anti-curtailment export is not configurable: the planner always
# runs in "automatic", where it may use the fleet's own discharge power but
# never selects more than the headroom it actually needs.  The conf key only
# survives for the v14 -> v15 migration that drops the old stored pair.
CONF_PREDISCHARGE_EXPORT_MODE = "predischarge_export_mode"
PREDISCHARGE_EXPORT_MODE_AUTOMATIC = "automatic"
