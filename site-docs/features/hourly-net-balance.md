# Hourly net balance

Hourly net balance adjusts battery power during each clock hour so grid import minus export approaches the energy target you choose. It is useful when your tariff or compensation is settled hour by hour rather than from each instant of power flow.

## Do I need it?

**Use it if** your electricity contract values net grid energy within each clock hour and you want Omnibattery to correct an early import or export before that hour ends.

**You do not need it if** your billing uses a different settlement period, instant zero-grid control already meets your goal, or you do not want the battery to spend energy correcting the current hour.

## Before you start

- Configure the grid consumption sensor used by Omnibattery. It is the fallback source for this feature.
- Enable automatic battery control and make sure at least one battery can act in the required direction.
- If you use time slots, the feature operates only while a configured slot allows discharge. With no enabled slots, it operates all day.
- Check whether [capacity protection (peak shaving)](peak-shaving.md) is active. When it intervenes, its safety target takes precedence over the hourly correction.

## How to enable it

1. Open the Omnibattery sidebar panel and select **Control**.
2. Find **Hourly net balance** and turn it on.
3. Leave **Hourly Balance Target** at `0.0 kWh` for net zero, or choose a positive target for net import and a negative target for net export.
4. Start with the default **Hourly Balance Max Offset** of `1,000 W`, then confirm that **Balance Neto** begins tracking the current hour.

## What you will see

**Balance Neto** shows the current hour's net grid energy. A positive state means net export; a negative state means net import. Its status indicates whether the feature is idle, outside a time slot, compensating toward import or export, capped by its maximum offset, or blocked from charging.

The correction changes during the hour. For example, after net import has accumulated, Omnibattery shifts the grid target toward discharge or export for the remaining time. It does not erase the measured history; it adjusts the power needed to approach the target by the end of the hour.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| **Balance Neto** stays **Idle** | The switch is off, no valid sample has arrived, or the balance is already within tolerance | Confirm **Hourly net balance**, the grid sensor, and **Hourly Balance Deadband** |
| Status is **Out of slot** | Enabled time slots exist and the current time is outside all of them | Check the days and times in [Time slots](../configuration/time-slots.md) |
| Status is **Blocked** or `compensation_stopped` | Charging is prevented by solar charge delay, a time slot, electric-vehicle pause, charge hysteresis, or maximum state of charge | Inspect `charge_block_reason` and the corresponding control |
| Status remains **Capped** and the hour misses its target | The required correction exceeds **Hourly Balance Max Offset**, or the battery lacks available power or energy | Check battery limits first; then raise **Hourly Balance Max Offset** gradually if the installation can support it |
| The offset changes too often | Offset hysteresis is too small for the meter noise | Increase **Hourly Balance Hysteresis** |
| No correction occurs near the target | The deviation is inside the energy tolerance | Reduce **Hourly Balance Deadband** if tighter correction is worth the extra cycling |
| Correction disappears while peak shaving is active | Capacity protection owns the grid target | This is expected; its absolute safety target replaces hourly and other additive targets while active |
| The displayed source is unavailable | The optional external balance sensor stopped updating | Restore that sensor; Omnibattery uses the grid-power integration when no external sensor is detected |

??? "Advanced details"
    ### Settings

    | Control | Default | Range | Effect |
    |---|---:|---:|---|
    | **Hourly Balance Target** | `0.0 kWh` | `−2.0–2.0 kWh` | Net grid energy wanted for each civil hour; positive means import and negative means export |
    | **Hourly Balance Max Offset** | `1,000 W` | `100–5,000 W` | Limits how far the feature can move the grid-power target |
    | **Hourly Balance Deadband** | `0.0 kWh` | `0.0–0.5 kWh` | Applies no correction while the energy deviation remains within this tolerance |
    | **Hourly Balance Hysteresis** | `15 W` | `0–200 W` | Requires this much offset change before publishing a new correction |

    A larger maximum offset can close a larger energy gap in the time remaining, but it also makes the power response more aggressive. The battery and system limits still apply.

    ### Calculation

    On every control cycle, Omnibattery accumulates import and export for the current local civil hour and calculates an additive target offset:

    ```text
    net_Wh = imported_Wh − exported_Wh
    deficit_Wh = target_net_Wh − net_Wh
    offset_W = deficit_Wh / remaining_hours
    ```

    A positive offset moves the target toward grid import; a negative offset moves it toward discharge or export. The offset ramps in during the first `5 min` of the hour, is clamped to **Hourly Balance Max Offset**, and stops during the final `1 min`. Offset hysteresis is bypassed during the final `10 min` so the correction can follow the remaining time more closely.

    The feature clears its offset in manual mode and outside active time slots. With no enabled time slots, it remains eligible throughout the day.

    ### Data source

    Omnibattery first looks for `sensor.balance_neto`. It assumes a positive value means export and chooses a reading method from the unit and state class:

    | Source type | Unit | State class | Reading method |
    |---|---|---|---|
    | Cumulative energy | `kWh` or `Wh` | `total` or `total_increasing` | Difference from a snapshot taken at the hour boundary |
    | Instantaneous net energy | `kWh` or `Wh` | `measurement` or another non-total class | Read directly |
    | Grid power | `W` or `kW` | Any | Integrate power over time with a trapezoidal calculation |

    When no supported external sensor is detected, the configured grid consumption sensor supplies the power samples. **Balance Neto** exposes the active entity ID in `source`, or `trapezoidal` for the fallback.

    ### Blocking and target priority

    Charge-direction correction can report these `charge_block_reason` values:

    | Reason | Meaning |
    |---|---|
    | `solar_charge_delay` | Solar charge delay prevents charging |
    | `time_slot` | The active time-slot rules prevent charging |
    | `ev_pause` | Electric-vehicle load handling has paused charging |
    | `hysteresis` | Battery charge hysteresis is active |
    | `max_soc` | Every battery with data has reached its maximum state of charge |

    While charging is blocked, the positive target offset remains registered. This prevents the PD controller from discharging the battery to cover the home load while the grid supplies it, and allows the stored correction to take effect when the blocker clears. Export-direction corrections are not blocked by these charge conditions.

    Hourly balance is an additive target: Omnibattery sums it with the user's grid target and other additive preferences. An active absolute override replaces that sum. Capacity protection uses such an override, so it takes precedence while controlling a peak.

    ### Diagnostic attributes and persistence

    **Balance Neto** can expose these attributes:

    | Attribute | Meaning |
    |---|---|
    | `status` | `idle`, `out_of_slot`, `capped`, `compensating_import`, `compensating_export`, or `compensation_stopped` |
    | `offset_w` | Active target correction in watts |
    | `imp_wh` | Grid import accumulated in the current hour |
    | `exp_wh` | Grid export accumulated in the current hour |
    | `target_net_wh` | Configured hourly target in watt-hours |
    | `remaining_min` | Time remaining in the current hour |
    | `source` | External source entity ID or `trapezoidal` |
    | `hour_iso` | Local timestamp at the start of the tracked hour |
    | `charge_block_reason` | Charge blocker, present only while one applies |

    Accumulators and the last offset are saved about every `5 min` and when the integration unloads. A restart restores them only when the saved data belongs to the current local civil hour.
