# Is my battery healthy?

The cell balance monitor compares the highest and lowest cell near the top of a full charge. **Balance - Status** gives the plain answer; **Balance - Cell Delta (at 100%)** and its history help you decide whether an unusual result is persistent.

## Do I need it?

**Use it if** your battery exposes minimum and maximum cell voltage and you want to check whether its cells remain balanced over time. The monitor is automatic on compatible batteries, so there is no separate switch to enable.

**You do not need it if** your battery does not expose both cell-voltage extremes. In that case the balance entities do not appear, and their absence does not itself indicate a battery fault.

## Before you start

- Look for **Balance - Status**, **Balance - Cell Delta (at 100%)**, and **Balance - Last Reading** on the battery device.
- Cell balance is available for Marstek Venus E v2/v3 and Venus A/D, Zendure batteries that publish cell extremes, and ESPHome/LilyGo devices when the upstream entities exist.
- Anker, Hoymiles, Huawei, and Sessy drivers do not currently provide both readings required by this monitor.
- Enable **100% Charge Voltage Taper** on the battery, or use [Weekly full charge](weekly-full-charge.md), to obtain a comparable top-of-charge reading.

## How to enable it

This feature is automatic; there is no monitor switch or activation form.

1. Open the battery device in Home Assistant and confirm that **Balance - Cell Delta (at 100%)** and **Balance - Status** exist.
2. In the Omnibattery dashboard, leave **100% Charge Voltage Taper** on for that battery.
3. Let the battery complete a full charge, then check that **Balance - Last Reading** has updated.

## What you will see

### Is my battery healthy?

Read **Balance - Status** first. One orange or red reading does not prove that a cell is degraded; compare readings from completed full charges before drawing a conclusion.

| Status | Balance - Cell Delta (at 100%) | What it means | What to do |
|---|---:|---|---|
| `green` | Below 200 mV | Within the monitor's normal band | No action |
| `yellow` | 200–229 mV | Above the normal band | Check the next full-charge reading and the trend |
| `orange` | 230–249 mV | Moderate imbalance | Repeat a full charge and confirm that the result persists |
| `red` | 250 mV or more | High imbalance | Compare consecutive readings; use the recovery blueprint only if the result persists |
| `unknown` | No comparable reading | The monitor has not recorded a valid top-of-charge result | Check **Balance - Last Reading**, cell-voltage availability, and the charge taper |

Also check these entities:

- **Balance - Cell Delta (at 100%)**: the measured spread in millivolts (mV).
- **Balance - Last Reading**: when the last comparable measurement completed.
- **Balance - Trend**: `rising`, `stable`, or `falling` across recent readings.
- **Balance - Delta Average (4 readings)**: the average of the latest four comparable readings.

On batteries with per-pack data, **Balance - Cell Delta (at 100%)** represents the worst internal pack spread. Its `packs_mV` and `worst_pack` attributes identify the pack behind the result; Omnibattery does not subtract the lowest cell in one pack from the highest cell in another.

If orange or red persists across full charges, use the [Marstek active-balance blueprint](../automations/blueprints.md#active-cell-balancing-for-one-marstek-battery) for a supported Marstek battery. Run it for one battery at a time and follow its cleanup notifications before returning that battery to automatic control.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| Balance entities are missing | The driver does not publish both cell-voltage extremes | Whether **Maximum Cell Voltage** and **Minimum Cell Voltage** exist for that battery |
| **Balance - Status** stays `unknown` | No comparable full-charge measurement has completed | **100% Charge Voltage Taper**, the charge target, and **Balance - Last Reading** |
| The latest value looks much higher or lower than usual | The charge ended differently, or the measurement did not come from the same top-of-charge condition | Compare the timestamp and several completed full-charge readings |
| A Venus A/D value seems inconsistent with the battery-level voltage | Battery-level registers can represent pack 1 while the diagnostic uses available per-pack data | `packs_mV` and `worst_pack` on **Balance - Cell Delta (at 100%)** |
| Orange or red returns after another full charge | The imbalance may be persistent | **Balance - Trend**, recent history, and the active-balance blueprint |

??? "Advanced details: interpreting mV and imbalance"
    **Why measurements are taken near full charge**

    Lithium iron phosphate (LFP) cell voltage stays relatively flat through much of the usable charge range. In that region, voltage differences are poor evidence of a state-of-charge difference. Near the upper knee, cell voltages separate more clearly and the battery management system (BMS) can identify and bleed the leading cells. Omnibattery therefore compares settled top-of-charge readings instead of treating a live mid-charge delta as a health result.

    **How Omnibattery creates a comparable reading**

    With **100% Charge Voltage Taper** enabled, the control path enters the taper zone at 3.48 V and limits that battery to 200 W. A Venus E battery normally stops at 3.60 V, or earlier when a commanded charge is rejected by the BMS. Charging then remains off for 60 seconds before Omnibattery records:

    ```text
    cell_delta_mV = (maximum_cell_voltage - minimum_cell_voltage) × 1000
    ```

    The taper latch releases after the control cell drops below 3.44 V. Starting and releasing at different voltages prevents repeated transitions while the cell relaxes.

    Venus A/D batteries can contain coupled packs, and their battery-level maximum/minimum registers describe pack 1 rather than every pack. Reaching 3.60 V therefore does not stop charging or start the measurement by itself. Omnibattery continues the 200 W command until a BMS cutoff is confirmed, waits 60 seconds, and then records the worst internal spread among the packs that provide valid data. Cross-pack voltage differences are excluded because each pack has its own BMS.

    A BMS cutoff requires a real charge request, delivered power at or below 10 W, and Standby for five consecutive control cycles. This avoids classifying an idle battery as full. The same cutoff can trigger a settled reading below 3.60 V when the battery remains in the taper zone.

    **Status and alerts**

    The status bands use the raw recorded delta: green below 200 mV, yellow from 200 mV to below 230 mV, orange from 230 mV to below 250 mV, and red from 250 mV. Orange and red readings create a persistent notification. A red result on two consecutive full charges adds the degraded-cell warning.

    **Trend and notification logic**

    Omnibattery retains up to 52 comparable readings and calculates the displayed average and trend from the latest four. A change greater than 2 mV per reading is `rising`; less than −2 mV per reading is `falling`; values between those boundaries are `stable`.

    The trend alert accounts for a 180 mV factory baseline. It fires when the trend is rising and the four-reading raw average is above 220 mV. Cell-balance notifications have a seven-day per-battery cooldown, so a continuing condition does not create a new persistent notification every cycle.

    **SOC recalibration on Venus E**

    A Venus E battery can reach 3.60 V while its reported state of charge (SOC) remains below 99%. When that happens outside the weekly cycle, Omnibattery can continue at 200 W until the BMS cuts off. If SOC is still below 100%, it waits for the cell to relax to 3.57 V and allows one more 200 W attempt. This only creates the conditions for recalibration; BMS firmware decides whether the displayed SOC changes.

    **Sensor and diagnostic reference**

    Five diagnostic entities are created only when the driver declares both cell-voltage readings:

    | Entity pattern | Purpose |
    |---|---|
    | `sensor.*_cell_delta` | Last comparable spread in mV, recent history, and optional pack breakdown |
    | `sensor.*_balance_status` | `green`, `yellow`, `orange`, `red`, or `unknown` |
    | `sensor.*_delta_trend` | Direction across recent comparable readings |
    | `sensor.*_last_balance_read` | Timestamp of the last reading |
    | `sensor.*_delta_avg_4w` | Average of the latest four readings |

    Values are restored after a Home Assistant restart. **Integration Status** exposes `normal_balance_protection` for deeper diagnosis:

    | Attribute | Meaning |
    |---|---|
    | `enabled` | Whether the battery's voltage taper is enabled |
    | `in_zone` | Whether its control cell voltage is in the top-charge zone |
    | `max_cell_voltage` / `min_cell_voltage` | Live battery-level voltage extremes |
    | `delta_V` | Live spread in volts |
    | `voltage_taper_latched` | Whether the top-charge taper is latched |
    | `bms_cutoff_charge_active` | Whether a coupled-pack battery remains charge-eligible until BMS cutoff |
    | `bms_cutoff_measurement` | Whether a post-cutoff measurement is `pending` or `done` |
    | `soc_recal_active` | Whether a low reported SOC is being offered a BMS-owned cutoff |
    | `soc_recal_bms_cutoff` | Whether that cutoff has been reached |
    | `soc_recal_retry_pending` / `soc_recal_retry_active` | State of the one-time retry |
    | `soc_recal_first_cutoff_voltage` | Highest voltage seen during the first cutoff |
    | `charge_limit_w` | Effective per-battery charge limit before allocation |

    These attributes explain the current control path; **Balance - Status** remains the user-facing health result.

    The active-balance blueprint runs outside Omnibattery's normal control loop through **Manual Battery Control**. Its own page is the canonical reference for its charge, rest, retry, and cleanup sequence. Settled measurements published by the blueprint enter the same **Balance - Cell Delta (at 100%)** history with `source: blueprint`.
