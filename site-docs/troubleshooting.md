# Troubleshooting by symptom

Start with what you can see in Home Assistant. **Integration Status** and its blocker attributes usually explain why Omnibattery is waiting, limiting power, or excluding a battery.

!!! note "Marstek app compatibility"
    You do not need to change anything in the Marstek app for Omnibattery to work, including its energy meter setting. Once Omnibattery is running, do not change the operating mode or a setting from the Marstek app: doing so breaks compatibility until you disable and re-enable the integration.

## Battery does nothing

The battery may be idle by design when the grid is already near target. If household load changes and the battery still neither charges nor discharges, use this table.

| Likely cause | What to check | Action |
|---|---|---|
| Automatic control is paused | **Manual Mode** and per-battery **Manual Battery Control** | Turn off manual ownership when you want automatic control |
| The battery is not eligible | **Allow Charge**, **Allow Discharge**, and `battery_charge_blockers` / `battery_discharge_blockers` on **Integration Status** | Enable the required direction or remove the reported blocker |
| The battery is unavailable or excluded | Battery entities, **Non-Responsive Batteries**, and Home Assistant **Repairs** | Follow [Entities are unavailable](#entities-are-unavailable) |
| A backup output is active | **Backup Function**, backup power, and `backup_cooldown_batteries` | Let backup activity finish before expecting grid control |
| The grid meter is invalid | Main grid sensor state and update time | Follow [Grid meter is unavailable or frozen](#grid-meter-is-unavailable-or-frozen) |

**Expected result:** **Integration Status** changes from a blocked/manual state to charging, discharging, or standby as grid flow changes. See [multiple batteries](features/multi-battery.md) for per-battery ownership.

## Battery does not charge

| Likely cause | What to check | Action |
|---|---|---|
| Maximum SOC or charge hysteresis reached | `battery_charge_blockers`, charging cutoff/target SOC, and **Charge Hysteresis** | Lower the target only if intentional; otherwise wait for SOC to fall |
| Charging is disabled for this battery | **Allow Charge** | Turn it on |
| Charge Delay is waiting for solar | **Charge Delay** and **Charge Delay Status** | Review [solar charge delay](features/solar-charge-delay.md) |
| A time slot blocks charging | **Discharge Window**, time-slot switches, and `charge_blockers` | Review [time slots](configuration/time-slots.md) |
| Predictive charging found no deficit | **Predictive Charging Active**, its reason, forecast, and consumption estimate | This is expected; review [predictive charging](configuration/predictive-charging/index.md) |
| Temperature or battery protection limits charging | **Integration Status**, temperature status, battery alarm, and fault entities | Review [temperature charge limit](features/temperature-charge-limit.md) and the battery manual |

**Expected result:** the blocker disappears and charge power rises when surplus solar, a manual request, or an eligible predictive period requires charging.

## Battery does not discharge

| Likely cause | What to check | Action |
|---|---|---|
| Minimum SOC reached | `battery_discharge_blockers` and the battery minimum SOC | Wait for charging or adjust the limit deliberately |
| Discharging is disabled | **Allow Discharge** | Turn it on |
| Current time slot blocks discharge | **Discharge Window** and time-slot switches | Review [time slots](configuration/time-slots.md) |
| Price or reserve control is holding energy | **Integration Status**, **Price-Based Discharge**, **Discharge Reserve**, and related statuses | Review [Dynamic Pricing](configuration/predictive-charging/dynamic-pricing.md) |
| Capacity protection or excluded-load logic owns the response | **Capacity Protection**, active excluded devices, and blockers | Review [capacity protection](features/peak-shaving.md) and [load exclusion](features/load-exclusion.md) |
| Battery is unavailable or excluded | **Non-Responsive Batteries** and Home Assistant **Repairs** | Follow [Entities are unavailable](#entities-are-unavailable) |

**Expected result:** discharge resumes when household demand exists and no safety, schedule, price, or participation rule blocks it.

## Imports or exports more than expected

| Likely cause | What to check | Action |
|---|---|---|
| Grid-meter sign is reversed | Compare the configured grid sensor with the utility meter while importing | Correct **Invert grid meter** in [main sensor configuration](configuration/main-sensor.md) |
| Grid target is intentionally non-zero | **PD Target Grid Power** | Set the target that matches your intended import or export |
| Meter updates arrive late | Grid sensor `last_updated` and **PD Control Quality** | Use a faster local meter or tune the controller after fixing latency |
| A large load is excluded | Excluded-device state and its exclusion controls | Review [load exclusion](features/load-exclusion.md) |
| Phase protection limits one or more batteries | **Three-Phase Protection Status** and phase assignment | Review [three-phase protection](configuration/three-phase.md) |
| AC and cell power describe different points | **AC Power**, **Battery Cell Power**, and solar inputs | Use **Home Consumption** and the correct power entity for the task |

**Expected result:** grid flow settles around the configured target after the meter and battery have reacted. Small short-lived errors can be normal.

## Entities are unavailable

| Likely cause | What to check | Action |
|---|---|---|
| Host, port, slave ID, or battery model is wrong | Integration entry and battery-specific setup | Correct the connection in the relevant [battery guide](configuration/batteries/index.md) |
| Marstek RS-485 control is off | **RS485 Control Mode** | Enable it before sending control commands |
| Gateway or bridge is offline | Gateway, ESPHome, MQTT, or API device status | Restore the local connection and reload the integration if needed |
| Driver rejected repeated reads or writes | Home Assistant **Repairs**, logs, and **Non-Responsive Batteries** | Follow the Repair instructions; attach diagnostics if it repeats |
| An old entity belongs to a previous driver | Entity registry device and integration | Remove the stale unavailable entity if the current driver has created its replacement |

**Expected result:** the coordinator updates and supported entities return to numeric or named states. For Marstek protocol details, see the [Modbus overview](reference/modbus-registers.md).

## Grid meter is unavailable or frozen

!!! warning "An unavailable meter can leave the last battery command active"
    When the grid sensor becomes `unavailable` or `unknown`, the control loop sends no new command. A battery can therefore continue at its last requested power — for example 2000 W discharge — until meter data returns. There is no automatic timeout that ramps the battery to idle; only SOC and other safety limits still apply.

| Likely cause | What to check | Action |
|---|---|---|
| Wi-Fi or broker connection failed | Meter integration, MQTT broker, and sensor availability | Restore connectivity before relying on automatic control |
| Sensor value stopped changing | `last_updated` while household power changes | Restart or repair the source integration |
| Wrong entity was selected | Configured main grid sensor | Select the net grid-power entity described in [main sensor configuration](configuration/main-sensor.md) |
| Shelly publishes too slowly | MQTT sensor update cadence | Use the matching [Shelly Pro 3EM MQTT script](hardware/shelly-pro-3em-mqtt-script.md) |

For up to 65 seconds after the last reading, the frozen value is still treated as authoritative. Past that point the controller may perform a safety recalculation with the derivative term suppressed, still using the stale value; it is safer to restore the source promptly rather than rely on this.

## Keeps oscillating between charge and discharge

| Likely cause | What to check | Action |
|---|---|---|
| Control is too aggressive | **PD Control Quality**, tuning profile, deadband, and derivative setting | Select a smoother profile or increase deadband in [follow home consumption](features/pd-controller.md) |
| Grid sensor is noisy or delayed | Sensor graph and update intervals | Fix the meter source before further tuning |
| A pulsing load repeatedly crosses the target | Load history and excluded-device status | Configure it through [load exclusion](features/load-exclusion.md) |
| Relay minimum power causes repeated starts | Minimum charge/discharge power and relay timing controls | Review the actuator settings in [follow home consumption](features/pd-controller.md) |

**Expected result:** **PD Control Quality** becomes stable after the controller has observed enough normal operation.

## Did not charge overnight

| Likely cause | What to check | Action |
|---|---|---|
| No energy deficit was forecast | **Predictive Charging Active** reason and target energy | No action is needed if stored energy and forecast solar cover demand |
| Price or forecast data is missing | `price_data_status`, solar forecast, and source entity availability | Restore the source described by your [predictive mode](configuration/predictive-charging/index.md) |
| No eligible period exists before demand | `chronological_plan_reason`, deadlines, selected slots, and time-slot switches | Adjust the schedule or price ceiling |
| Charge power or free capacity is insufficient | `deadline_shortfall_kwh`, battery SOC, charge limit, and target SOC | Increase an intentional limit or accept the reported shortfall |
| Another feature owned charging | `charge_blockers`, manual mode, charge delay, or time-slot state | Remove the conflicting rule |

**Expected result:** the diagnostic sensor either shows a feasible grid-charge plan or clearly reports why charging is unnecessary or physically impossible. See [Dynamic Pricing](configuration/predictive-charging/dynamic-pricing.md) or [Time Slot mode](configuration/predictive-charging/time-slot.md).

??? "Consumption source shows `legacy_daily`, or the solar profile falls back"
    A consumption forecast source of `legacy_daily` is expected while the 28-day profile is still learning, or when the requested interval does not meet its coverage contract. Check **Expected Home Consumption Profile** and the integration diagnostics. Changing a source or an excluded-load adjustment keeps every learned day; a timezone change re-bins them by the offset between the two zones, and Recorder backfill rebuilds whatever is still missing in the background. A gap longer than five minutes is not interpolated.

    A solar forecast that stays immature or falls back is also expected during the first days. Learning needs direct PV power from the configured external sensor or readable MPPT channels, at least seven closed quality days, recent coverage, and enough evidence in the requested future range; invalid, negative, and long-gap samples are excluded, and curtailment signals can exclude intervals. A source or capacity change starts a new generation. Check the diagnostics `solar_profile` section and `solar_timeline_fallback_reason`; the profile cannot repair a bad weather forecast or model curtailment it cannot observe.

## Weekly full charge or cell balancing did not finish

| Likely cause | What to check | Action |
|---|---|---|
| Wrong day or feature disabled | **Weekly Full Charge** and **Weekly Full Charge Day** | Enable and schedule [weekly full charge](features/weekly-full-charge.md) |
| Charging is delayed or blocked | Weekly charge status, `charge_blockers`, and manual ownership | Remove the blocker or disable the configured delay for that run |
| Battery management system stopped at the top | SOC, cell voltage, charge power, alarm, and fault entities | Let the integration apply its supported taper; inspect persistent faults |
| Required cell telemetry is missing | Max/min cell voltage and balance entities | Check compatibility in [cell balance monitor](features/cell-balance-monitor.md) |
| Blueprint owns the battery | **Manual Battery Control** and automation trace | Review the [active balancing blueprint](automations/blueprints.md) |

## One battery in a multi-battery system does not participate

| Likely cause | What to check | Action |
|---|---|---|
| Its SOC or priority makes another battery preferable | **Active Batteries**, **Primary Battery**, and **Charge Priority** | This can be expected; review [multiple batteries](features/multi-battery.md) |
| Direction is disabled | Per-battery **Allow Charge** / **Allow Discharge** | Enable the required direction |
| Battery is manually owned | **Manual Battery Control** | Release manual ownership after returning the battery to idle |
| Power or SOC limit was reached | Per-battery blockers and limit entities | Adjust only the limit you intend to change |
| Delivery or communication failed | **Non-Responsive Batteries** and **Repairs** | Follow the Repair and inspect diagnostics |

## Battery alarm or fault appears

| Likely cause | What to check | Action |
|---|---|---|
| Battery reports a warning or protection | **System Alarm Status**, per-battery **Alarm Status**, and **Fault Status** | Follow the battery manufacturer's guidance for the named condition |
| Condition has already cleared | Current status and persistent notification | Confirm that the notification clears; reload only if state remains stale |
| Model does not expose alarm registers | Entity availability for that driver | Use the manufacturer's app or local interface |

Alarm and fault registers (polled every 5 seconds) are only available on v2 hardware; v3, vA, and vD do not expose them over Modbus. When a new bit is set, Omnibattery raises a persistent notification titled with 🚨 for a fault or ⚠️ for an alarm, naming the exact condition (for example *BAT Overvoltage* or *Fan Abnormal Warning*); it is dismissed automatically once every bit clears.

## Before asking for help

1. Open **Settings → Devices & services → Omnibattery**.
2. Open the affected config entry and choose **Download diagnostics**.
3. Review the JSON and remove anything you do not want to share.
4. Enable debug logging from the integration page, reproduce the problem, then disable logging to download the log file.
5. Include the observed symptom, approximate time, relevant entity states, diagnostics, and log with your report.

Diagnostics redact known connection fields and identifiers, but you should still review the file before sharing it.
