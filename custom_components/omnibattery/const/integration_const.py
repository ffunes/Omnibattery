"""Integration-level configuration constants for Omnibattery."""

DOMAIN = "omnibattery"

# Prefix for every persistent_notification this integration creates/dismisses.
# Lets automations (e.g. the Telegram-forwarding blueprint) reliably select only
# our notifications by ID. All notification_id values MUST start with this.
NOTIFICATION_ID_PREFIX = "marstek_venus_"

# Internal debug switches for maintainer-level troubleshooting.
# Keep these disabled for normal Home Assistant debug logging; enabling them can
# generate very large logs on systems with fast polling or multiple batteries.
DEBUG_RAW_MODBUS_READS = False
DEBUG_POLL_SENSOR_SKIPS = False
DEBUG_POLL_SENSOR_VALUES = False
DEBUG_CONTROL_LOOP_DETAIL = False

SCAN_INTERVAL = {
    "high": 2,       # fast-changing sensors, e.g., power, alarms
    "medium": 5,     # moderately changing sensors, e.g., voltage, current
    "low": 30,        # slow-changing sensors, e.g., cumulative energy counters
    "very_low": 600   # rarely changing info, e.g., device info, firmware versions
}

# Battery version support
CONF_BATTERY_VERSION = "battery_version"
CONF_DC_PV_CONNECTED = "dc_pv_connected"
SUPPORTED_VERSIONS = ["v2", "v3", "vA", "vD"]

# Modbus slave/unit id. Default 1 (Marstek factory default for a direct
# connection). A Modbus TCP proxy that fans out to several batteries on one
# host:port distinguishes them by slave id, so this must be configurable.
CONF_SLAVE_ID = "slave_id"
DEFAULT_SLAVE_ID = 1

# Serial / Modbus-RTU connection. When set, the battery is reached over a serial
# port (USB-RS485 adapter) instead of Modbus TCP (discussion #350). Path string
# such as "/dev/ttyUSB0" or "COM3"; empty/absent means TCP. Marstek's RTU link is
# fixed at 115200 8N1 by the hardware, so only the port path is configurable.
CONF_SERIAL_PORT = "serial_port"
SERIAL_BAUDRATE = 115200

# Maximum power (W) per battery version — used by config_flow to set slider limits
MAX_POWER_BY_VERSION = {
    "v2": 2500,
    "v3": 2500,
    "vA": 1500,
    "vD": 2500,
}
DEFAULT_VERSION = "v2"

VENUS_D_2500W_MIN_EMS_VERSION = 149
VENUS_D_LEGACY_MAX_POWER_W = 2200


def max_power_for_battery_version(
    battery_version: str,
    ems_version: object = None,
) -> int:
    """Return the safe per-battery power ceiling for a Marstek model/FW.

    Venus D accepts 2500 W only from EMS firmware 149 onward.  Unknown Venus D
    firmware is treated conservatively until register 30200 has been read.
    """
    default = int(MAX_POWER_BY_VERSION.get(battery_version, 2500))
    if battery_version != "vD":
        return default
    try:
        firmware = int(float(ems_version))
    except (TypeError, ValueError):
        return VENUS_D_LEGACY_MAX_POWER_W
    return (
        default
        if firmware >= VENUS_D_2500W_MIN_EMS_VERSION
        else VENUS_D_LEGACY_MAX_POWER_W
    )

# Maximum number of independently controllable battery devices in one entry.
# Keep the aggregate system-power slider envelope in sync with the largest
# supported per-battery power ceiling.
MAX_BATTERIES = 10
MAX_SYSTEM_POWER_W = MAX_BATTERIES * max(MAX_POWER_BY_VERSION.values())

# Multi-battery activation thresholds derived from efficiency tables (η external)
# Crossover = power at which splitting load across 2 batteries becomes more efficient
# than running a single battery.  Based on Venus efficiency measurements at 2500 W max.
MULTI_BATTERY_DISCHARGE_CROSSOVER_W = 1500   # 60% of 2500 W physical max
MULTI_BATTERY_CHARGE_CROSSOVER_W    = 1750   # 70% of 2500 W physical max
MULTI_BATTERY_HYSTERESIS_GAP        = 0.10   # fraction gap: activation → deactivation
MULTI_BATTERY_MIN_ACTIVATION        = 0.50   # floor: never activate below this fraction
# Cap at 0.95: stage 5% before single-battery saturation to absorb demand transients,
# even when efficiency analysis alone would keep a single battery active.
MULTI_BATTERY_MAX_ACTIVATION        = 0.95

# Charge hysteresis (per-battery). Hysteresis is mandatory — after a battery
# reaches its ceiling it must not recharge until SOC falls this far below the
# latched peak. The 2 % floor keeps the deadband wider than typical SOC-reading
# drift/quantization, which would otherwise release the latch and cause charge
# chatter at the top. Existing installs are migrated (async_migrate_entry
# v7 -> v8): previously-configured values are preserved, others get the floor.
MIN_CHARGE_HYSTERESIS_PERCENT = 2
DEFAULT_CHARGE_HYSTERESIS_PERCENT = 2
MAX_CHARGE_HYSTERESIS_PERCENT = 50
# Keep additional batteries active long enough to avoid pulsing when bursty loads
# repeatedly cross the split-load threshold. Refreshed while the split condition holds.
MULTI_BATTERY_SELECTION_HOLD_SECONDS = 120

# Predictive Grid Charging Configuration
CONF_ENABLE_PREDICTIVE_CHARGING = "enable_predictive_charging"
# A manual, persistent learning pause. The exact affected periods and the
# measured overnight baseline live in ConsumptionTracker's Store; this flag is
# kept in the entry so the switch restores before the first control cycle.
CONF_VACATION_MODE_ENABLED = "vacation_mode_enabled"
CONF_CHARGING_TIME_SLOT = "charging_time_slot"
CONF_SOLAR_FORECAST_SENSOR = "solar_forecast_sensor"
# Explicit post-now forecast.  The old key remains a whole-day ``today`` value
# for backwards compatibility and must not be silently reinterpreted.
CONF_SOLAR_FORECAST_REMAINING_SENSOR = "solar_forecast_remaining_sensor"
CONF_SOLAR_PRODUCTION_SENSOR = "solar_production_sensor"
# Temporal solar profile.  The normal behaviour is automatic: the learned
# curve takes over once it is mature, and the historical sinusoid remains the
# safe fallback while it learns.  ``shadow`` is retained only as a legacy
# value so existing config entries can be normalized without data loss.
CONF_SOLAR_PROFILE_MODE = "solar_profile_mode"
SOLAR_PROFILE_MODE_OFF = "off"
SOLAR_PROFILE_MODE_SHADOW = "shadow"
SOLAR_PROFILE_MODE_ACTIVE = "active"
SOLAR_PROFILE_MODES = (
    SOLAR_PROFILE_MODE_OFF,
    SOLAR_PROFILE_MODE_SHADOW,
    SOLAR_PROFILE_MODE_ACTIVE,
)
DEFAULT_SOLAR_PROFILE_MODE = SOLAR_PROFILE_MODE_ACTIVE


def normalize_solar_profile_mode(mode: str | None) -> str:
    """Normalize legacy rollout values to the current automatic behaviour."""
    if mode == SOLAR_PROFILE_MODE_OFF:
        return SOLAR_PROFILE_MODE_OFF
    return SOLAR_PROFILE_MODE_ACTIVE


CONF_HOUSEHOLD_CONSUMPTION_SENSOR = "household_consumption_sensor"  # legacy; migrated out in v6
CONF_MAX_CONTRACTED_POWER = "max_contracted_power"

# Optional alternate meter for installations whose backed-up/off-grid circuit is
# measured separately. The software switch only selects this entity as the active
# meter; enabling the battery's physical off-grid/EPS output remains the user's
# responsibility.
CONF_OFFGRID_POWER_SENSOR = "offgrid_power_sensor"
CONF_OFFGRID_METER_INVERTED = "offgrid_meter_inverted"
CONF_OFFGRID_MODE_ENABLED = "offgrid_mode_enabled"

# Optional three-phase current protection.  The main consumption sensor remains
# the only control input; these current sensors are safety limits for the phase
# where a battery is physically installed.
CONF_THREE_PHASE_ENABLED = "three_phase_enabled"
CONF_PHASE_1_CURRENT_SENSOR = "phase_1_current_sensor"
CONF_PHASE_2_CURRENT_SENSOR = "phase_2_current_sensor"
CONF_PHASE_3_CURRENT_SENSOR = "phase_3_current_sensor"
CONF_PHASE_1_FUSE_SIZE = "phase_1_fuse_size"
CONF_PHASE_2_FUSE_SIZE = "phase_2_fuse_size"
CONF_PHASE_3_FUSE_SIZE = "phase_3_fuse_size"
CONF_BATTERY_PHASE = "battery_phase"

PHASE_L1 = "l1"
PHASE_L2 = "l2"
PHASE_L3 = "l3"
PHASE_VALUES = (PHASE_L1, PHASE_L2, PHASE_L3)
# Explicit selector value for a battery that is outside the protected phase
# layout. Runtime leaves it outside the phase-protection envelope.
PHASE_UNASSIGNED = "unassigned"
PHASE_ASSIGNMENT_VALUES = (PHASE_UNASSIGNED, *PHASE_VALUES)
PHASE_CONFIG = {
    PHASE_L1: (CONF_PHASE_1_CURRENT_SENSOR, CONF_PHASE_1_FUSE_SIZE),
    PHASE_L2: (CONF_PHASE_2_CURRENT_SENSOR, CONF_PHASE_2_FUSE_SIZE),
    PHASE_L3: (CONF_PHASE_3_CURRENT_SENSOR, CONF_PHASE_3_FUSE_SIZE),
}
DEFAULT_THREE_PHASE_ENABLED = False
DEFAULT_PHASE_FUSE_SIZE_A = 25


def normalize_battery_phase(value: object) -> str:
    """Normalize legacy/missing battery phase data to the selector value."""
    return value if value in PHASE_VALUES else PHASE_UNASSIGNED


# Battery set-points are expressed in active watts, while phase protection is
# measured in RMS amperes.  Use a conservative fixed conversion for the
# single-phase AC connection; the user-facing fuse limit remains in amperes.
PHASE_NOMINAL_VOLTAGE_V = 230.0
PHASE_BATTERY_POWER_FACTOR = 0.90

# Time slots (operation slots) — v3 schema keys
CONF_TIME_SLOTS = "no_discharge_time_slots"  # legacy key, kept for compat
CONF_SLOT_START_TIME = "start_time"
CONF_SLOT_END_TIME = "end_time"
CONF_SLOT_DAYS = "days"
CONF_SLOT_ENABLED = "enabled"
CONF_SLOT_BATTERY_SCOPE = "battery_scope"
CONF_SLOT_ALLOW_CHARGE = "allow_charge"
CONF_SLOT_ALLOW_DISCHARGE = "allow_discharge"
CONF_SLOT_SOC_OVERRIDE_ENABLED = "soc_override_enabled"
CONF_SLOT_SOC_MAX = "soc_max"
CONF_SLOT_SOC_MIN = "soc_min"
CONF_SLOT_POWER_OVERRIDE_ENABLED = "power_override_enabled"
CONF_SLOT_MAX_CHARGE_POWER_W = "max_charge_power_w"
CONF_SLOT_MAX_DISCHARGE_POWER_W = "max_discharge_power_w"
CONF_SLOT_MODE = "mode"

SLOT_BATTERY_SCOPE_ALL = "all"
SLOT_MODE_PD = "pd"
SLOT_MODE_MANUAL = "manual"

DEFAULT_SLOT_BATTERY_SCOPE = SLOT_BATTERY_SCOPE_ALL
DEFAULT_SLOT_ALLOW_CHARGE = False
DEFAULT_SLOT_ALLOW_DISCHARGE = True
DEFAULT_SLOT_SOC_OVERRIDE_ENABLED = False
DEFAULT_SLOT_POWER_OVERRIDE_ENABLED = False
DEFAULT_SLOT_MODE = SLOT_MODE_PD
DEFAULT_SLOT_SOC_MIN_FLOOR = 12
DEFAULT_SLOT_SOC_MAX_CEILING = 100
MAX_TIME_SLOTS = 8

# Default base consumption fallback (kWh/day)
DEFAULT_BASE_CONSUMPTION_KWH = 5.0  # Fallback when no consumption history available

# Predictive charging / anti-curtailment safety margin
CONF_PREDICTIVE_SAFETY_MARGIN_KWH = "predictive_safety_margin_kwh"
DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH = 0.0  # kWh buffer; 0 = no margin

# Predictive charging grid-charge margin
# Extra % charged from grid on top of the solar-deficit, to hedge against
# optimistic solar forecasts / worse-than-expected weather. 0 = no margin.
# Capped so the charge never exceeds the gap to max SOC.
CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT = "predictive_grid_charge_margin_pct"
DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT = 0.0

# Guaranteed minimum SOC floor (#417)
# The whole-day energy balance can read zero deficit on a solar-positive day,
# yet the battery still hits the hardware floor in the morning before solar
# ramps up. This forces a charge sized to reach the floor SOC regardless of the
# daily balance. 0 = disabled.
CONF_PREDICTIVE_MIN_SOC_FLOOR = "predictive_min_soc_floor"
DEFAULT_PREDICTIVE_MIN_SOC_FLOOR = 20.0
CONF_ENABLE_MIN_SOC_FLOOR = "enable_min_soc_floor"

# Re-evaluation thresholds
SOC_REEVALUATION_THRESHOLD = 30  # Re-evaluate every 30% SOC drop

# Guaranteed-minimum-SOC floor: only re-trigger when SOC drops this many % *below*
# the floor, so tiny dips at the boundary don't re-fire every cycle (relay churn).
# Band: soc < (floor - margin) triggers; charges up to floor.
FLOOR_HYSTERESIS_PCT = 5

# Weekly Full Charge Configuration
CONF_ENABLE_WEEKLY_FULL_CHARGE = "enable_weekly_full_charge"
CONF_MANUAL_MODE_ENABLED = "manual_mode_enabled"
CONF_BATTERY_MANUAL_MODE_ENABLED = "battery_manual_mode_enabled"
CONF_PREDICTIVE_CHARGING_OVERRIDDEN = "predictive_charging_overridden"
CONF_WEEKLY_FULL_CHARGE_DAY = "weekly_full_charge_day"
CONF_ENABLE_WEEKLY_FULL_CHARGE_DELAY = "enable_weekly_full_charge_delay"
CONF_WEEKLY_FULL_CHARGE_SKIP_DELAY = "weekly_full_charge_skip_delay"
# Default True preserves the historic behaviour: the weekly full charge bypasses
# the solar charge delay and charges immediately on its target day. The runtime
# switch flips this so the weekly charge can instead wait for the delay to unlock.
DEFAULT_WEEKLY_FULL_CHARGE_SKIP_DELAY = True
CONF_ENABLE_BALANCE_MONITOR = "enable_balance_monitor"

# Cell Balance Monitor
BALANCE_STORAGE_KEY = "balance_history"
BALANCE_STORAGE_VERSION = 1
# Public event emitted by the extracted active-balance blueprint after a
# settled WAIT_MEASURE interval. The integration reads its own coordinator
# telemetry when handling this event; the blueprint does not provide voltages.
EVENT_BLUEPRINT_BALANCE_MEASUREMENT_READY = "omnibattery_balance_measurement_ready"
# Marstek cells ship from the factory with a sizeable top-of-charge imbalance
# (commonly ~170-180 mV). At 3.55 V the LiFePO4 curve is very steep, so this
# factory spread is normal — not a fault. The status thresholds below are
# absolute raw-delta values chosen to sit above that factory baseline, so a
# fresh battery reads green. The baseline offset is subtracted only in the
# rising-trend magnitude gate, so steady factory-level readings do not trip a
# trend alert (slope is unaffected — subtracting a constant does not change it).
BALANCE_BASELINE_OFFSET_MV = 180  # mV — factory top-of-charge imbalance, used by the trend gate
BALANCE_THRESHOLD_YELLOW = 200    # mV — raw delta above this: yellow
BALANCE_THRESHOLD_ORANGE = 230    # mV — raw delta above this: orange
BALANCE_THRESHOLD_RED = 250       # mV — raw delta above this: red
BALANCE_HISTORY_MAX = 52         # ~1 year of weekly readings
BALANCE_RED_CONSECUTIVE_ALERT = 2
BALANCE_TREND_ALERT_AVG_MV = 40.0   # baseline-corrected avg must exceed this (raw avg > 220 mV) to fire a rising-trend alert
BALANCE_NOTIFY_COOLDOWN_DAYS = 7    # min days between cell-imbalance notifications per battery

# Optional normal full-charge protection.
# When enabled per battery, slow charging only while the target is 100% and
# cells enter the top voltage range. This is voltage-only; SOC is intentionally
# ignored because some batteries report it unreliably near the top.
NORMAL_BALANCE_TAPER_CELL_VOLTAGE = 3.48
# Hysteresis: taper latch releases only after cell drops this far below entry.
# Prevents oscillation: at low taper power the cell can relax below 3.48 V but not
# to 3.44 V, so the latch stays active until the battery is meaningfully discharged.
NORMAL_BALANCE_TAPER_EXIT_CELL_VOLTAGE = 3.44
NORMAL_BALANCE_PAUSE_CELL_VOLTAGE = 3.60
NORMAL_BALANCE_CHARGE_POWER_W = 200
NORMAL_BALANCE_MEASURE_WAIT_SECONDS = 60
# Venus A/D can expose several coupled packs through one inverter. Their top
# cell telemetry may reach the normal pause point while the other packs still
# need charge, so keep the tapered command until the BMS confirms its cutoff.
NORMAL_BALANCE_BMS_CUTOFF_VERSIONS = ("vA", "vD")
# For Venus E models, once the top voltage is reached the taper stops charging
# and latches. It does NOT re-trickle when the cell relaxes (that would pin the
# cell at the top voltage and keep some v3 BMSs from leaving standby to
# discharge). Venus A/D coupled packs use the BMS-owned path above instead. The
# The normal SOC hysteresis releases after its configured percentage, while the
# voltage taper latch itself releases below the 3.44 V exit threshold.
# SOC recalibration on a stuck top voltage.
# A pack that hits the top cell voltage (pause point) while the BMS reports a SOC
# below full may have a drifted coulomb counter. Some BMS firmware only corrects
# that counter after performing the charge cutoff itself (users see e.g. 70% or 96% with a
# cell already at 3.60 V). In that case, instead of holding at the pause voltage,
# keep charging at the tapered power until the BMS itself cuts off, attempting to
# recalibrate SOC. The firmware may keep reporting the old SOC after cutoff, so
# recalibration is explicitly best-effort rather than a completion guarantee.
NORMAL_BALANCE_RECAL_SOC_THRESHOLD = 99        # %: below this, make one best-effort recalibration attempt
NORMAL_BALANCE_RECAL_CUTOFF_POWER_W = 10       # W: charge collapsed (BMS terminated)
NORMAL_BALANCE_RECAL_CUTOFF_CYCLES = 5         # consecutive cycles to confirm the BMS cutoff
NORMAL_BALANCE_RECAL_INVERTER_STANDBY = 1      # inverter_state raw value for Standby
# After a cutoff above the pause voltage, allow one extra 200 W charge when the
# cell has relaxed to this voltage.  The retry is deliberately one-shot.
NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE = 3.57

# BMS low-SOC discharge cutoff (low-SOC counterpart to NORMAL_BALANCE_RECAL_*).
# Below this SOC the BMS may refuse to discharge on its own (protective cutoff,
# e.g. a weak cell sagging under load) even though the reported SOC is still
# above the configured min_soc. The battery then ACKs the discharge command but
# delivers ~0W. Treat that as an expected BMS cutoff instead of a non-responsive
# fault, so the battery stays in the PD pool.
BMS_DISCHARGE_CUTOFF_SOC = 20                  # %: below this, refused discharge = BMS cutoff, not a fault

# Bus-load reduction: the PD loop normally reads 4 registers back after every
# power write (ACK verify + non-delivery detection). Those reads are the bulk of
# the write-path traffic. To cut bus load, only read back every Nth *real* write
# (option-B skips don't count); the others are write-only (no readback, no
# post-write settle delay). Trade-off: ACK mismatches and a battery that stops
# delivering are caught up to N writes later instead of immediately.
PD_READBACK_EVERY_N_WRITES = 5

# Deferred exact-ACK verification: a readback may confirm within the driver's
# echo tolerance while the battery is still ramping toward the set-point (see
# _ACK_TOLERANCE_* in drivers/marstek.py). The exact settled check happens
# later — an exact readback echo or the poll-time skip-write comparison. If
# this many consecutive readback cycles confirm only by tolerance without an
# exact match in between, warn once: the write chain (RS485-ETH bridge serial
# config, firmware) lags more than the settle window ever covers.
ACK_INEXACT_STREAK_WARN = 5

# Transient burst poll: right after a REAL power command change, the delivered-
# power reading (ac_power / battery_power) is polled at this cadence instead of
# its normal "high" scan interval, so _measured_battery_power() isn't stale
# during the actuator ramp. Bounded window — sustained bus load is unaffected.
BURST_POLL_WINDOW_S = 6.0
BURST_POLL_INTERVAL_S = 1.0

# Feedforward step detection (PD mode): a confirmed load step (kettle/oven-sized)
# is covered with ONE deadbeat cycle (measured - error, same law as no-PD mode)
# instead of the ~13 s exponential approach of the incremental P term; the PD
# resumes fine adjustment from the new operating point on the next cycle.
# Detection is 2-sample: an error jump beyond max(5*deadband, floor) arms a
# candidate, and it only fires if the next sample still shows the deviation
# (same sign, >= confirm ratio of the jump) — a 1-sample excursion is a meter
# spike and is rejected. Hardcoded on purpose (no config entities), same policy
# as the adaptive filter / burst poll above.
FEEDFORWARD_STEP_FLOOR_W = 400        # W: minimum error jump to arm a candidate
FEEDFORWARD_CONFIRM_RATIO = 0.8       # fraction of the jump that must persist next sample
FEEDFORWARD_CANDIDATE_MAX_AGE_S = 5.0 # s: candidate expires if not checked in time (deadband gap)
FEEDFORWARD_COOLDOWN_S = 10.0         # s: min spacing between fires (covers actuator ramp 3-6s)
FEEDFORWARD_PULSE_GUARD_S = 30.0      # s: opposite-sign step this soon after a fire = pulsing load, skip

# Zero-cross hold (direction-flip dwell): minimum seconds an opposite-direction
# (charge<->discharge) request must persist before the PD is allowed to flip.
# During a downward load step the grid shows a transient export while the
# discharging battery is still ramping down (actuator settle 3-6 s measured), so
# the incremental PD crosses zero and would command a real charge on another
# battery, zeroing the discharger (5-40 s dips, ping-pong every 1-3 min). The
# effective window is max(this, 2 * slowest actuator_latency_s) — see
# _apply_zero_cross_hold. Legitimate sustained surplus flips after the window.
PD_ZERO_CROSS_MIN_HOLD_S = 5.0

# A grid-power sensor at or above this interval is slow enough to warrant setup
# guidance and a Repairs warning. Slow sensors remain supported: their latest
# reading is authoritative until MAX_SENSOR_STALE_S has elapsed.
SLOW_SENSOR_WARNING_INTERVAL_S = 10.0

# Maximum age of the latest grid-power reading before the watchdog may perform a
# stale safety recalculation. This is intentionally time-based rather than a
# number of 2 s watchdog cycles, so scheduler phase and delayed cycles cannot
# shorten the promised tolerance.
MAX_SENSOR_STALE_S = 65.0

# Consecutive slow main-sensor intervals before the slow-sensor repair is raised.
# Debouncing prevents a single outage/restart gap from flagging an otherwise fast
# sensor. Clearing uses SLOW_SENSOR_RECOVERY_INTERVALS instead.
SLOW_SENSOR_WARN_INTERVALS = 3

# Consecutive fast intervals before an existing slow-sensor repair is cleared.
# Deliberately much longer than SLOW_SENSOR_WARN_INTERVALS: clearing is the
# hysteresis side, and a sensor hovering around the threshold would otherwise
# create and delete the repair over and over. At a 3 s meter this is about a
# minute of sustained fast cadence.
SLOW_SENSOR_RECOVERY_INTERVALS = 20

# How often the dynamic-pricing handler re-parses the price sensor purely to
# refresh _price_data_status for the health check. The parse itself is cheap but
# pointless every 2.5 s control cycle, and price attributes change hourly at most.
PRICE_HEALTH_CHECK_INTERVAL_S = 900.0

# How long price parsing must keep failing before a Repairs issue is raised.
# Long enough to ride out an integration reload, a provider outage or the
# day-ahead publication gap; short enough that a broken sensor is noticed the
# same day instead of silently disabling price-aware charging for weeks.
PRICE_DATA_ISSUE_DELAY_S = 7200.0

# How long a configured solar-forecast sensor must stay unreadable before a
# Repairs issue is raised. Same reasoning as the price feed: long enough to ride
# out a provider outage, short enough to catch a dead sensor the same day.
FORECAST_DATA_ISSUE_DELAY_S = 7200.0

# A readback at or below this settle latency (seconds,
# DriverCapabilities.readback_latency_s with actuator_latency_s as fallback)
# reflects the new command within one poll. Slower telemetry paths skip the
# hot-path ACK readback so they cannot block the shared control loop.
HOT_PATH_READBACK_MAX_LATENCY_S = 1.5

# Discharge engage grace: a slow inverter (e.g. Zendure HTTP) takes seconds to
# reverse from charge/idle into discharge — measured up to ~20-30 s on a cold
# charge→discharge transition. During that window an ACK'd command legitimately
# reads back 0 W out, which is engage latency, not a fault. Suppress non-delivery
# recording for this long after the commanded direction flips to discharge so the
# inverter is not excluded before it has had time to engage. A battery that never
# engages is still caught, just this many seconds later.
DISCHARGE_ENGAGE_GRACE_S = 30

# Idle-runaway floor: a battery commanded to idle (0 W) that is actually moving
# more than this many watts has slipped out of RS485 forced mode and is running
# its own internal logic (a v3 can export to grid this way — see issue #434). Above
# this floor we re-assert RS485 control and write a real standby instead of letting
# the bus-load skip-write trust the matching set-points. Set above standby self-draw
# / metering noise so a resting battery doesn't trigger needless writes.
IDLE_RUNAWAY_POWER_W = 100

# A battery freshly commanded from a move into idle is still ramping down while
# battery_power telemetry lags (actuator settle ~3-4 s + poll grain ~3 s), so
# the set-points read standby a cycle or two before the delivered power reaches
# 0. Judging idle-runaway inside that window fires the RS485 re-assert on every
# ordinary discharge→idle transition (false positives, extra bus writes on a
# fragile v3). Suppress the judgment for this long after the command flips to
# idle; a genuine runaway is still caught, just this many seconds later.
IDLE_RUNAWAY_GRACE_S = 15

# Min-SOC re-entry hysteresis for discharge availability: after a battery empties
# to min_soc its resting SOC rebounds 1-2% (cell relaxation), which would re-admit
# it for a sliver of discharge and drop it again — relay on/off ping-pong plus
# micro-cycles in the worst SOC region. Once excluded at min_soc, the battery only
# becomes dischargeable again after recovering this many percent above min_soc.
DISCHARGE_MIN_SOC_REENTRY_MARGIN = 2

CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED = "full_charge_voltage_taper_enabled"
DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED = True

CONF_ENABLE_CHARGE_DELAY = "enable_charge_delay"
CONF_DELAY_SAFETY_MARGIN_MIN = "delay_safety_margin_min"
DEFAULT_DELAY_SAFETY_MARGIN_MIN = 60
# Deadband (kWh) on the binary "grid needed today?" gate, so a near-balanced day
# (raw forecast ~= consumption) does not flip into a false pre-dawn deficit
# unlock. Runtime-tunable slider (see ChargeDelayManager._should_delay_charge, #4).
CONF_CHARGE_DELAY_BALANCE_DEADBAND_KWH = "charge_delay_balance_deadband_kwh"
DEFAULT_CHARGE_DELAY_BALANCE_DEADBAND_KWH = 0.5
CONF_DELAY_SOC_SETPOINT_ENABLED = "delay_soc_setpoint_enabled"
DEFAULT_DELAY_SOC_SETPOINT_ENABLED = False
CONF_DELAY_SOC_SETPOINT = "delay_soc_setpoint"
DEFAULT_DELAY_SOC_SETPOINT = 50  # % — default when the setpoint is enabled
DELAY_SOC_SETPOINT_HYSTERESIS = 3  # % — SOC must drop this far below setpoint before recharging

# Temperature-based charge power derate. When a battery runs hot, charge power is
# proportionally reduced: full power at/below the limit, ramping linearly down to
# a floor (% of the normal charge ceiling) as temperature rises across the band,
# and back up as it cools. Charge-only; per-battery on internal_temperature.
CONF_ENABLE_TEMP_CHARGE_LIMIT = "enable_temp_charge_limit"
DEFAULT_ENABLE_TEMP_CHARGE_LIMIT = False
CONF_TEMP_CHARGE_LIMIT_C = "temp_charge_limit_c"
DEFAULT_TEMP_CHARGE_LIMIT_C = 40  # °C, derate starts at/above this temperature
CONF_TEMP_CHARGE_LIMIT_BAND_C = "temp_charge_limit_band_c"
DEFAULT_TEMP_CHARGE_LIMIT_BAND_C = 10  # °C, width over which power ramps to the floor
CONF_TEMP_CHARGE_LIMIT_FLOOR_PCT = "temp_charge_limit_floor_pct"
DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT = 40  # % of normal charge power at limit+band (0 = full stop)
# Optionally apply the SAME derate curve to discharge. Discharge tolerates heat
# better than charge, so this shares one (charge-tuned) threshold as a deliberate
# compromise; its main benefit is staying under the BMS hard over-temp cutoff.
CONF_TEMP_LIMIT_APPLY_DISCHARGE = "temp_limit_apply_discharge"
DEFAULT_TEMP_LIMIT_APPLY_DISCHARGE = False

# Hourly Net Balance
CONF_ENABLE_HOURLY_BALANCE = "enable_hourly_balance"
CONF_HOURLY_BALANCE_TARGET_NET_WH = "hourly_balance_target_net_wh"
CONF_HOURLY_BALANCE_MAX_OFFSET_W = "hourly_balance_max_offset_w"
CONF_HOURLY_BALANCE_DEADBAND_WH = "hourly_balance_deadband_wh"
CONF_HOURLY_BALANCE_HYSTERESIS_W = "hourly_balance_hysteresis_w"

DEFAULT_HOURLY_BALANCE_TARGET_NET_WH = 0.0
DEFAULT_HOURLY_BALANCE_MAX_OFFSET_W = 1000
DEFAULT_HOURLY_BALANCE_DEADBAND_WH = 0.0
DEFAULT_HOURLY_BALANCE_HYSTERESIS_W = 15

# Hardcoded — not user-configurable
HOURLY_BALANCE_RAMP_IN_MIN = 5

HOURLY_BALANCE_STORAGE_KEY = "hourly_balance"
HOURLY_BALANCE_STORAGE_VERSION = 1
HOURLY_BALANCE_FORCE_RECALC_REMAINING_MIN = 10  # bypass hysteresis near end of hour
HOURLY_BALANCE_MIN_REMAINING_MIN = 1   # below this, offset = 0

# External net balance sensor candidates (checked in order; first match wins).
# Positive sensor value = net export to grid. Flip sign in _read_external_net_wh if reversed.
EXTERNAL_NET_BALANCE_CANDIDATES: list[str] = ["sensor.balance_neto"]

# Weekly Full Charge Delay Constants
CHARGE_EFFICIENCY = 0.85  # Conservative factor for charge power estimation

# Round-trip efficiency used by the arbitrage-margin gate (see
# CONF_MIN_ARBITRAGE_MARGIN). Distinct from CHARGE_EFFICIENCY, which derates
# charge *power* to size a charging window; this one is the AC-to-AC energy
# ratio (kWh out / kWh in) used to value a stored kWh. Default matches
# CHARGE_EFFICIENCY so behavior is unchanged for anyone who does not tune it.
DEFAULT_ROUND_TRIP_EFFICIENCY = 0.85
MIN_ROUND_TRIP_EFFICIENCY = 0.50
MAX_ROUND_TRIP_EFFICIENCY = 1.0
DELAY_SAFETY_FACTOR = 1.3  # 30% margin on energy balance
LOW_FORECAST_THRESHOLD_FACTOR = 1.5  # forecast < 1.5 × capacity → bad solar day
T_START_THRESHOLD_KWH = 0.1  # Threshold to detect solar production start
T_START_FALLBACK_HOUR = 11  # If no T_start by 11:00, unlock immediately

EVENING_REEVAL_HOURS_BEFORE_TEND = 1.5  # Trigger evening re-evaluation 1.5h before estimated T_end
EVENING_REEVAL_FALLBACK_HOUR = 16.0     # Fallback trigger hour when T_start was never detected
EVENING_DEFICIT_THRESHOLD_KWH = 0.3    # Minimum deficit to bother scheduling evening charging

# Weekday mapping (mon=0, sun=6, matches datetime.weekday())
WEEKDAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6
}

# Capacity Protection Mode Configuration
CONF_CAPACITY_PROTECTION_ENABLED = "capacity_protection_enabled"
CONF_CAPACITY_PROTECTION_EXCLUDED_DEVICES = "capacity_protection_excluded_devices"
CONF_CAPACITY_PROTECTION_SOC_THRESHOLD = "capacity_protection_soc_threshold"
CONF_CAPACITY_PROTECTION_LIMIT = "capacity_protection_limit"

DEFAULT_CAPACITY_PROTECTION_SOC = 30
DEFAULT_CAPACITY_PROTECTION_LIMIT = 2500

# PD Controller Advanced Configuration Keys
CONF_PD_KP = "pd_controller_kp"
CONF_PD_KD = "pd_controller_kd"
CONF_PD_DEADBAND = "pd_controller_deadband"
CONF_PD_MAX_POWER_CHANGE = "pd_controller_max_power_change"
CONF_PD_DIRECTION_HYSTERESIS = "pd_controller_direction_hysteresis"
CONF_PD_MIN_CHARGE_POWER = "pd_min_charge_power"
CONF_PD_MIN_DISCHARGE_POWER = "pd_min_discharge_power"
CONF_PD_RELAY_COOLDOWN = "pd_relay_cooldown"
CONF_PD_MIN_CYCLE_INTERVAL = "pd_min_cycle_interval"
CONF_TARGET_GRID_POWER = "pd_target_grid_power"
# No-PD direct-tracking mode (opt-in): track the consumption sensor 1:1 with no
# integral/derivative/smoothing curve. Reuses the deadband, min charge/discharge
# power, relay min-ON and target-grid-power knobs above; adds only a command delay.
CONF_NO_PD_MODE_ENABLED = "no_pd_mode_enabled"

# --- Mixed-fleet control (a DC-coupled hybrid beside an AC battery) ----------
# Which battery serves the house first, and whether it is handed the real
# quantity directly instead of waiting for the meter to deviate. Both opt-in.
CONF_PRIMARY_BATTERY = "primary_battery"
DEFAULT_PRIMARY_BATTERY = ""
CONF_PRIMARY_FEEDFORWARD_ENABLED = "primary_feedforward_enabled"
DEFAULT_PRIMARY_FEEDFORWARD_ENABLED = False
# Which battery is filled first. Empty = follow the day's outlook.
CONF_CHARGE_PRIORITY = "charge_priority"
DEFAULT_CHARGE_PRIORITY = ""
# How far the guards have to want to move the standing command before the
# deadband/stale shortcuts are skipped to let them. Below this a correction is
# not worth a write.
GUARD_PENDING_TOLERANCE_W = 100
# How clear a surplus has to be before the guard blocks discharge. Wider than
# meter noise, so a cloud edge does not toggle the battery every cycle; release
# has no band at all, because by then the house genuinely needs the battery.
SURPLUS_GUARD_HYSTERESIS_W = 100
# How far the outlook has to move before the scarce/ample verdict flips. A
# forecast wanders all day; without this the charge order would follow it.
SCARCITY_HYSTERESIS_KWH = 2.0
CONF_NO_PD_COMMAND_DELAY = "no_pd_command_delay"
CONF_ENABLE_SYSTEM_POWER_LIMITS = "enable_system_power_limits"
CONF_SYSTEM_MAX_CHARGE_POWER = "system_max_charge_power"
CONF_SYSTEM_MAX_DISCHARGE_POWER = "system_max_discharge_power"

# Default PD Controller Parameters
# Lowered from Kp 0.65 / Kd 0.5 to curb overshoot under the cadence-independent
# control loop; existing installs on the old defaults are migrated (see
# async_migrate_entry, config entry v3 -> v4).
DEFAULT_PD_KP = 0.35
DEFAULT_PD_KD = 0.3
DEFAULT_PD_DEADBAND = 40
DEFAULT_PD_MAX_POWER_CHANGE = 800
DEFAULT_PD_DIRECTION_HYSTERESIS = 60
DEFAULT_PD_MIN_CHARGE_POWER = 0       # Minimum charge power (0 = disabled)
DEFAULT_PD_MIN_DISCHARGE_POWER = 0    # Minimum discharge power (0 = disabled)
# Relay anti-chatter: minimum time (s) the battery stays engaged after leaving
# idle before it may return to 0. Stops the relay toggling on/off when the grid
# signal hovers at the deadband edge during solar ramp-up/down. 0 = disabled
# (default: preserves the pre-feature behaviour; opt-in via the slider).
DEFAULT_PD_RELAY_COOLDOWN = 0
# Power held in the already-engaged direction while the cooldown is running, when
# the user's min charge/discharge power is 0 (otherwise that min is used).
RELAY_COOLDOWN_HOLD_POWER = 100
# Minimum spacing (s) between event-driven control cycles. The grid sensor can
# publish several times per second; without a floor, each out-of-deadband cycle
# issues a Modbus write burst, which slow TCP-serial bridges (e.g. Elfin EW11)
# can choke on. Drops surplus sensor-triggered cycles; the 2 s safety timer is
# never gated. 0 = disabled (pre-feature behaviour); default 1 s caps bursts.
DEFAULT_PD_MIN_CYCLE_INTERVAL = 1.0
# Grid-sample EMA smoothing time constant (s). Single source of truth so no-PD
# mode can drop it to 0 (raw passthrough) and restore it when the mode is off.
DEFAULT_GRID_FILTER_TAU = 2.0
# No-PD direct-tracking mode defaults. Command delay debounces fast meters: events
# inside a delay window collapse into one command issued on the latest value
# (0 = act on every event, paced only by CONF_PD_MIN_CYCLE_INTERVAL).
DEFAULT_NO_PD_MODE_ENABLED = False
DEFAULT_NO_PD_COMMAND_DELAY = 0.0
DEFAULT_TARGET_GRID_POWER = 0
DEFAULT_ENABLE_SYSTEM_POWER_LIMITS = False
DEFAULT_SYSTEM_MAX_CHARGE_POWER = 0       # 0 = disabled
DEFAULT_SYSTEM_MAX_DISCHARGE_POWER = 0    # 0 = disabled

# Legacy alias so existing __init__.py imports don't break during transition
DEFAULT_SLOT_TARGET_GRID_POWER = DEFAULT_TARGET_GRID_POWER


# --- Configured system power envelope ---------------------------------------
# Single source of truth for "how much power did the user configure this system
# to move", derived from config_entry.data alone (no hass, no coordinators).
#
# Deliberately NOT MarstekController._effective_system_capacity: that one sums
# the *live* per-coordinator limits after temperature derate, SOC taper and slot
# ceilings for the current control cycle. This is the *static configured*
# envelope, which is what a UI bound must advertise. Do not merge them — slider
# bounds must not jump around with battery temperature.

def _non_negative_int(value) -> int:
    """Coerce a config value to a non-negative int (missing/None/junk -> 0)."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def effective_battery_power_limits(battery) -> tuple[int, int]:
    """Return the static effective (charge_w, discharge_w) limits for one battery.

    New entries may persist the normalized ``device_max_*`` and
    ``configured_max_*`` fields. Legacy entries use ``max_*_power`` and soft-max
    drivers (for example Zendure and Anker) keep the user's additional ceiling
    in ``user_max_*_power``. Missing normalized/soft-limit keys preserve legacy
    entries while applying the same ``min(device, configured)`` rule.
    """
    device_charge = battery.get("device_max_charge_power")
    device_discharge = battery.get("device_max_discharge_power")
    configured_charge = battery.get("configured_max_charge_power")
    configured_discharge = battery.get("configured_max_discharge_power")

    if device_charge is None:
        device_charge = battery.get("max_charge_power")
    if device_discharge is None:
        device_discharge = battery.get("max_discharge_power")
    if configured_charge is None:
        configured_charge = battery.get(
            "user_max_charge_power", battery.get("max_charge_power")
        )
    if configured_discharge is None:
        configured_discharge = battery.get(
            "user_max_discharge_power", battery.get("max_discharge_power")
        )

    return (
        min(_non_negative_int(device_charge), _non_negative_int(configured_charge)),
        min(
            _non_negative_int(device_discharge),
            _non_negative_int(configured_discharge),
        ),
    )


def total_battery_power(data) -> tuple[int, int]:
    """Return summed static effective (charge_w, discharge_w) limits."""
    batteries = data.get("batteries") or []
    limits = [effective_battery_power_limits(battery) for battery in batteries]
    return (
        sum(charge_w for charge_w, _ in limits),
        sum(discharge_w for _, discharge_w in limits),
    )


def system_power_limits_enabled(data) -> bool:
    """Return whether the optional system-wide power cap is active.

    Entries predating CONF_ENABLE_SYSTEM_POWER_LIMITS had the feature on iff a
    non-zero cap was set, so that stays the default for the missing key.
    """
    charge = _non_negative_int(
        data.get(CONF_SYSTEM_MAX_CHARGE_POWER, DEFAULT_SYSTEM_MAX_CHARGE_POWER)
    )
    discharge = _non_negative_int(
        data.get(CONF_SYSTEM_MAX_DISCHARGE_POWER, DEFAULT_SYSTEM_MAX_DISCHARGE_POWER)
    )
    return bool(
        data.get(CONF_ENABLE_SYSTEM_POWER_LIMITS, charge > 0 or discharge > 0)
    )


def effective_system_power(data) -> tuple[int, int]:
    """Return (charge_w, discharge_w) after the optional system-wide cap.

    A cap of 0 means "disabled" for that direction, mirroring
    MarstekController._configured_system_limit. The cap can only narrow the
    per-battery sum, never widen it.
    """
    charge_w, discharge_w = total_battery_power(data)
    if not system_power_limits_enabled(data):
        return charge_w, discharge_w

    charge_cap = _non_negative_int(
        data.get(CONF_SYSTEM_MAX_CHARGE_POWER, DEFAULT_SYSTEM_MAX_CHARGE_POWER)
    )
    discharge_cap = _non_negative_int(
        data.get(CONF_SYSTEM_MAX_DISCHARGE_POWER, DEFAULT_SYSTEM_MAX_DISCHARGE_POWER)
    )
    return (
        min(charge_w, charge_cap) if charge_cap else charge_w,
        min(discharge_w, discharge_cap) if discharge_cap else discharge_w,
    )

# PD Tuning Profiles
# One-click presets for the PD response-shape parameters (Kp, Kd, max power
# change). Selecting a profile writes those at once; the "custom" profile leaves
# the sliders to the user. Profiles are ordered smoothest → fastest. "balanced"
# equals the shipping defaults, so an untouched install maps onto it.
#
# Deadband is deliberately NOT part of the profiles: it is both the user's
# precision/meter-noise preference and the reference the control-quality sensor
# measures against (oscillation is counted only outside the deadband). Bundling it
# into a profile would clobber that preference and bias the sensor's own yardstick.
CONF_PD_TUNING_PROFILE = "pd_tuning_profile"
PD_PROFILE_CUSTOM = "custom"
DEFAULT_PD_TUNING_PROFILE = PD_PROFILE_CUSTOM

PD_TUNING_PROFILES = {
    "very_smooth": {
        CONF_PD_KP: 0.22,
        CONF_PD_KD: 0.15,
        CONF_PD_MAX_POWER_CHANGE: 400,
    },
    "smooth": {
        CONF_PD_KP: 0.30,
        CONF_PD_KD: 0.25,
        CONF_PD_MAX_POWER_CHANGE: 600,
    },
    "balanced": {
        CONF_PD_KP: DEFAULT_PD_KP,
        CONF_PD_KD: DEFAULT_PD_KD,
        CONF_PD_MAX_POWER_CHANGE: DEFAULT_PD_MAX_POWER_CHANGE,
    },
    "aggressive": {
        CONF_PD_KP: 0.55,
        CONF_PD_KD: 0.45,
        CONF_PD_MAX_POWER_CHANGE: 1200,
    },
    "very_aggressive": {
        CONF_PD_KP: 0.75,
        CONF_PD_KD: 0.45,
        CONF_PD_MAX_POWER_CHANGE: 2000,
    },
}

# Option order shown in the select (custom last); 6 total incl. manual.
PD_TUNING_PROFILE_OPTIONS = list(PD_TUNING_PROFILES.keys()) + [PD_PROFILE_CUSTOM]

# Effective value of each profiled PD param when absent from config_entry.data.
_PD_PROFILE_PARAM_DEFAULTS = {
    CONF_PD_KP: DEFAULT_PD_KP,
    CONF_PD_KD: DEFAULT_PD_KD,
    CONF_PD_MAX_POWER_CHANGE: DEFAULT_PD_MAX_POWER_CHANGE,
}


def pd_profile_from_params(data) -> str:
    """Return the preset name whose values match the PD gain params in `data`.

    Falls back to PD_PROFILE_CUSTOM when no preset matches (i.e. the user has
    hand-tuned the sliders). Deadband is not considered — it is user-owned and not
    part of the profiles. Compared with a small epsilon to tolerate float Kp/Kd.
    """
    for name, params in PD_TUNING_PROFILES.items():
        if all(
            abs(float(data.get(key, _PD_PROFILE_PARAM_DEFAULTS[key])) - float(value)) < 1e-6
            for key, value in params.items()
        ):
            return name
    return PD_PROFILE_CUSTOM


# Dynamic Pricing Mode Configuration
CONF_PREDICTIVE_CHARGING_MODE = "predictive_charging_mode"
CONF_PRICE_SENSOR = "price_sensor"
CONF_PRICE_INTEGRATION_TYPE = "price_integration_type"
CONF_MAX_PRICE_THRESHOLD = "max_price_threshold"
# Discharge floor for the price hysteresis band (#408). Discharge is blocked
# while price <= this value; unset → falls back to max_price_threshold so
# existing single-threshold installs keep identical behavior.
CONF_DISCHARGE_PRICE_THRESHOLD = "discharge_price_threshold"
# Minimum arbitrage margin (currency/kWh) required before a slot is eligible for
# grid charging. Unset (None) → disabled, and slot selection behaves exactly as
# before. When set, a candidate slot must satisfy
#   expected_discharge_price * round_trip_efficiency - slot_price >= margin
# so that charging is skipped on days where the intraday spread cannot repay the
# conversion losses. Applied on top of (not instead of) CONF_MAX_PRICE_THRESHOLD.
CONF_MIN_ARBITRAGE_MARGIN = "min_arbitrage_margin"
CONF_ROUND_TRIP_EFFICIENCY = "round_trip_efficiency"

# Smart pre-discharge / anti-curtailment.  This is deliberately separate from
# the normal predictive-charging settings: it is only read by dynamic pricing
# and remains disabled for existing entries unless explicitly enabled.
CONF_SMART_PREDISCHARGE_ENABLED = "smart_predischarge_enabled"
CONF_NEGATIVE_INJECTION_THRESHOLD = "negative_injection_threshold"
CONF_PREDISCHARGE_RESERVE_SOC = "predischarge_reserve_soc"
CONF_PREDISCHARGE_MAX_EXPORT_POWER_W = "predischarge_max_export_power_w"
DEFAULT_SMART_PREDISCHARGE_ENABLED = False
DEFAULT_NEGATIVE_INJECTION_THRESHOLD = 0.0
DEFAULT_PREDISCHARGE_RESERVE_SOC = 0.0
DEFAULT_PREDISCHARGE_MAX_EXPORT_POWER_W = 0.0

# Opportunistic import charging.  This is deliberately separate from
# CONF_NEGATIVE_INJECTION_THRESHOLD: the latter prices exported solar for
# anti-curtailment, while this feature reacts to negative grid-import prices.
CONF_NEGATIVE_PRICE_CHARGING_ENABLED = "negative_price_charging_enabled"
DEFAULT_NEGATIVE_PRICE_CHARGING_ENABLED = False

PREDICTIVE_MODE_TIME_SLOT = "time_slot"
PREDICTIVE_MODE_DYNAMIC_PRICING = "dynamic_pricing"
PREDICTIVE_MODE_REALTIME_PRICE = "realtime_price"

CONF_AVERAGE_PRICE_SENSOR = "average_price_sensor"

CONF_METER_INVERTED = "meter_inverted"
CONF_DP_PRICE_DISCHARGE_CONTROL = "dp_price_discharge_control"
CONF_RT_PRICE_DISCHARGE_CONTROL = "rt_price_discharge_control"

PRICE_INTEGRATION_NORDPOOL = "nordpool"
PRICE_INTEGRATION_PVPC = "pvpc"
PRICE_INTEGRATION_CKW = "ckw"
PRICE_INTEGRATION_EPEX = "epex"
PRICE_INTEGRATION_ENTSOE = "entsoe"
PRICE_INTEGRATION_TIBBER = "tibber"

# Tibber and the official Nord Pool integration are service-based rather than
# forecast-attribute based. How stale either cache may get before a refresh.
TIBBER_REFRESH_MINUTES = 60
NORDPOOL_REFRESH_MINUTES = 60

# Marker for CONFIG_NUMBER_DEFINITIONS entries whose slider bounds are derived
# at runtime; the authored min/max become the fallback.
DYNAMIC_BOUNDS_SYSTEM_POWER = "system_power"
DYNAMIC_BOUNDS_SYSTEM_POWER_CAP = "system_power_cap"

# Configuration Number Definitions (for config entities exposed in the UI)
CONFIG_NUMBER_DEFINITIONS = [
    {
        "key": CONF_PD_KP,
        "name": "PD Kp",
        "min": 0.1,
        "max": 2.0,
        "step": 0.05,
        "default": DEFAULT_PD_KP,
        "icon": "mdi:tune",
    },
    {
        "key": CONF_PD_KD,
        "name": "PD Kd",
        "min": 0.0,
        "max": 2.0,
        "step": 0.05,
        "default": DEFAULT_PD_KD,
        "icon": "mdi:tune",
    },
    {
        "key": CONF_PD_DEADBAND,
        "name": "PD Deadband",
        "min": 0,
        "max": 200,
        "step": 5,
        "unit": "W",
        "default": DEFAULT_PD_DEADBAND,
        "icon": "mdi:arrow-collapse-horizontal",
    },
    {
        "key": CONF_PD_MAX_POWER_CHANGE,
        "name": "PD Max Power Change",
        "min": 100,
        "max": 2000,
        "step": 50,
        "unit": "W",
        "default": DEFAULT_PD_MAX_POWER_CHANGE,
        "icon": "mdi:delta",
    },
    {
        "key": CONF_PD_DIRECTION_HYSTERESIS,
        "name": "PD Direction Hysteresis",
        "min": 0,
        "max": 200,
        "step": 5,
        "unit": "W",
        "default": DEFAULT_PD_DIRECTION_HYSTERESIS,
        "icon": "mdi:swap-horizontal",
    },
    {
        "key": CONF_PD_MIN_CHARGE_POWER,
        "name": "PD Min Charge Power",
        "min": 0,
        "max": 2000,
        "step": 10,
        "unit": "W",
        "default": DEFAULT_PD_MIN_CHARGE_POWER,
        "icon": "mdi:battery-charging-low",
    },
    {
        "key": CONF_PD_MIN_DISCHARGE_POWER,
        "name": "PD Min Discharge Power",
        "min": 0,
        "max": 2000,
        "step": 10,
        "unit": "W",
        "default": DEFAULT_PD_MIN_DISCHARGE_POWER,
        "icon": "mdi:battery-low",
    },
    {
        "key": CONF_PD_RELAY_COOLDOWN,
        "name": "PD Relay Cooldown",
        "min": 0,
        "max": 60,
        "step": 1,
        "unit": "s",
        "default": DEFAULT_PD_RELAY_COOLDOWN,
        "icon": "mdi:timer-cog-outline",
    },
    {
        "key": CONF_PD_MIN_CYCLE_INTERVAL,
        "name": "PD Min Cycle Interval",
        "min": 0,
        "max": 2,
        "step": 0.1,
        "unit": "s",
        "default": DEFAULT_PD_MIN_CYCLE_INTERVAL,
        "icon": "mdi:timer-pause-outline",
    },
    {
        "key": CONF_NO_PD_COMMAND_DELAY,
        "name": "No-PD Command Delay",
        "min": 0,
        "max": 3,
        "step": 0.1,
        "unit": "s",
        "default": DEFAULT_NO_PD_COMMAND_DELAY,
        "icon": "mdi:timer-sand",
    },
    {
        "key": CONF_TARGET_GRID_POWER,
        "name": "PD Target Grid Power",
        # Fallback only. The live bounds follow the configured system power
        # envelope (see DYNAMIC_BOUNDS_SYSTEM_POWER); these apply when no
        # battery limits are configured yet, or a direction is configured to 0 W.
        "min": -2500,
        "max": 2500,
        "step": 10,
        "unit": "W",
        "default": DEFAULT_TARGET_GRID_POWER,
        "icon": "mdi:transmission-tower-export",
        "dynamic_bounds": DYNAMIC_BOUNDS_SYSTEM_POWER,
    },
    {
        "key": CONF_SYSTEM_MAX_CHARGE_POWER,
        "name": "System Max Charge Power",
        "min": 0,
        # Fallback only; the live maximum is the configured sum of charge
        # limits, so batteries with 4 kW (or higher) ceilings are supported.
        "max": MAX_SYSTEM_POWER_W,
        "step": 50,
        "unit": "W",
        "default": DEFAULT_SYSTEM_MAX_CHARGE_POWER,
        "icon": "mdi:battery-arrow-up-outline",
        "condition": CONF_ENABLE_SYSTEM_POWER_LIMITS,
        "dynamic_bounds": DYNAMIC_BOUNDS_SYSTEM_POWER_CAP,
        "power_direction": "charge",
    },
    {
        "key": CONF_SYSTEM_MAX_DISCHARGE_POWER,
        "name": "System Max Discharge Power",
        "min": 0,
        # Fallback only; the live maximum is the configured sum of discharge
        # limits, so batteries with 4 kW (or higher) ceilings are supported.
        "max": MAX_SYSTEM_POWER_W,
        "step": 50,
        "unit": "W",
        "default": DEFAULT_SYSTEM_MAX_DISCHARGE_POWER,
        "icon": "mdi:battery-arrow-down-outline",
        "condition": CONF_ENABLE_SYSTEM_POWER_LIMITS,
        "dynamic_bounds": DYNAMIC_BOUNDS_SYSTEM_POWER_CAP,
        "power_direction": "discharge",
    },
    {
        "key": CONF_MAX_CONTRACTED_POWER,
        "name": "Max Contracted Power",
        "min": 1000,
        "max": 20000,
        "step": 100,
        "unit": "W",
        "default": 7000,
        "icon": "mdi:transmission-tower",
        "condition": CONF_ENABLE_PREDICTIVE_CHARGING,
    },
    {
        "key": CONF_DELAY_SAFETY_MARGIN_MIN,
        "name": "Charge Delay Safety Margin",
        "min": 1,
        "max": 6,
        "step": 0.5,
        "unit": "h",
        "scale": 60,
        "default": DEFAULT_DELAY_SAFETY_MARGIN_MIN,
        "icon": "mdi:timer-sand",
        "condition": CONF_ENABLE_CHARGE_DELAY,
    },
    {
        "key": CONF_CHARGE_DELAY_BALANCE_DEADBAND_KWH,
        "name": "Charge Delay Balance Deadband",
        "min": 0.0,
        "max": 5.0,
        "step": 0.1,
        "unit": "kWh",
        "default": DEFAULT_CHARGE_DELAY_BALANCE_DEADBAND_KWH,
        "icon": "mdi:arrow-collapse-horizontal",
        "condition": CONF_ENABLE_CHARGE_DELAY,
    },
    {
        "key": CONF_DELAY_SOC_SETPOINT,
        "name": "Charge Delay SOC Setpoint",
        "min": 12,
        "max": 90,
        "step": 5,
        "unit": "%",
        "default": DEFAULT_DELAY_SOC_SETPOINT,
        "icon": "mdi:battery-charging-50",
        "condition": CONF_DELAY_SOC_SETPOINT_ENABLED,
    },
    {
        "key": CONF_CAPACITY_PROTECTION_SOC_THRESHOLD,
        "name": "Capacity Protection SOC Threshold",
        "min": 20,
        "max": 100,
        "step": 1,
        "unit": "%",
        "default": DEFAULT_CAPACITY_PROTECTION_SOC,
        "icon": "mdi:battery-alert-variant-outline",
        "condition": CONF_CAPACITY_PROTECTION_ENABLED,
    },
    {
        "key": CONF_CAPACITY_PROTECTION_LIMIT,
        "name": "Capacity Protection Peak Limit",
        "min": 500,
        "max": 20000,
        "step": 100,
        "unit": "W",
        "default": DEFAULT_CAPACITY_PROTECTION_LIMIT,
        "icon": "mdi:flash-alert",
        "condition": CONF_CAPACITY_PROTECTION_ENABLED,
    },
    {
        "key": CONF_PREDICTIVE_SAFETY_MARGIN_KWH,
        "name": "Solar Forecast Safety Margin",
        "min": 0.0,
        "max": 20.0,
        "step": 0.1,
        "unit": "kWh",
        "default": DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH,
        "icon": "mdi:solar-power-variant",
        "condition": CONF_ENABLE_PREDICTIVE_CHARGING,
    },
    {
        "key": CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT,
        "name": "Predictive Grid Charge Margin",
        "min": 0.0,
        "max": 100.0,
        "step": 5.0,
        "unit": "%",
        "default": DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT,
        "icon": "mdi:transmission-tower-import",
        "condition": CONF_ENABLE_PREDICTIVE_CHARGING,
    },
    {
        "key": CONF_PREDICTIVE_MIN_SOC_FLOOR,
        "name": "Guaranteed Minimum SOC",
        "min": 20.0,
        "max": 90.0,
        "step": 5.0,
        "unit": "%",
        "default": DEFAULT_PREDICTIVE_MIN_SOC_FLOOR,
        "icon": "mdi:battery-arrow-up",
        "condition": CONF_ENABLE_PREDICTIVE_CHARGING,
    },
    {
        "key": CONF_HOURLY_BALANCE_TARGET_NET_WH,
        "name": "Hourly Balance Target",
        "min": -2.0,
        "max": 2.0,
        "step": 0.1,
        "unit": "kWh",
        "default": DEFAULT_HOURLY_BALANCE_TARGET_NET_WH,
        "icon": "mdi:scale-balance",
        "condition": CONF_ENABLE_HOURLY_BALANCE,
    },
    {
        "key": CONF_HOURLY_BALANCE_MAX_OFFSET_W,
        "name": "Hourly Balance Max Offset",
        "min": 100,
        "max": 5000,
        "step": 50,
        "unit": "W",
        "default": DEFAULT_HOURLY_BALANCE_MAX_OFFSET_W,
        "icon": "mdi:arrow-expand-vertical",
        "condition": CONF_ENABLE_HOURLY_BALANCE,
    },
    {
        "key": CONF_HOURLY_BALANCE_DEADBAND_WH,
        "name": "Hourly Balance Deadband",
        "min": 0.0,
        "max": 0.5,
        "step": 0.1,
        "unit": "kWh",
        "default": DEFAULT_HOURLY_BALANCE_DEADBAND_WH,
        "icon": "mdi:arrow-collapse-horizontal",
        "condition": CONF_ENABLE_HOURLY_BALANCE,
    },
    {
        "key": CONF_HOURLY_BALANCE_HYSTERESIS_W,
        "name": "Hourly Balance Hysteresis",
        "min": 0,
        "max": 200,
        "step": 5,
        "unit": "W",
        "default": DEFAULT_HOURLY_BALANCE_HYSTERESIS_W,
        "icon": "mdi:swap-horizontal",
        "condition": CONF_ENABLE_HOURLY_BALANCE,
    },
]
