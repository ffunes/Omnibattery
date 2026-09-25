# Is my battery healthy?

The cell balance monitor compares the highest and lowest cell near the top of a full charge. **Balance - Status** gives the plain answer; **Balance - Cell Delta at 100% (last full charge)** and its history help you decide whether an unusual result is persistent.

## Do I need it?

**Use it if** your battery exposes minimum and maximum cell voltage and you want to check whether its cells remain balanced over time. The monitor is automatic on compatible batteries, so there is no separate switch to enable.

**You do not need it if** your battery does not expose both cell-voltage extremes. In that case the balance entities do not appear, and their absence does not itself indicate a battery fault.

## Before you start

- Look for **Balance - Status**, **Balance - Cell Delta at 100% (last full charge)**, and **Balance - Last Reading** on the battery device.
- Cell balance is available for Marstek Venus E v2/v3 and Venus A/D, Zendure batteries that publish cell extremes, and ESPHome/LilyGo devices when the upstream entities exist.
- Anker, Hoymiles, Huawei, and Sessy drivers do not currently provide both readings required by this monitor.
- Enable **100% Charge Voltage Taper** on the battery, or use [Weekly full charge](weekly-full-charge.md), to obtain a comparable top-of-charge reading.

## How to enable it

This feature is automatic; there is no monitor switch or activation form.

1. Open the battery device in Home Assistant and confirm that **Balance - Cell Delta at 100% (last full charge)** and **Balance - Status** exist.
2. In the Omnibattery dashboard, leave **100% Charge Voltage Taper** on for that battery.
3. Let the battery complete a full charge, then check that **Balance - Last Reading** has updated.

## What you will see

### Is my battery healthy?

Read **Balance - Status** first. One orange or red reading does not prove that a cell is degraded; compare readings from completed full charges before drawing a conclusion.

| Status | Balance - Cell Delta at 100% (last full charge) | What it means | What to do |
|---|---:|---|---|
| `green` | Below 200 mV | Within the monitor's normal band | No action |
| `yellow` | 200–229 mV | Above the normal band | Check the next full-charge reading and the trend |
| `orange` | 230–249 mV | Moderate imbalance | Repeat a full charge and confirm that the result persists |
| `red` | 250 mV or more | High imbalance | Compare consecutive readings; use the recovery blueprint only if the result persists |
| `unknown` | No comparable reading | The monitor has not recorded a valid top-of-charge result | Check **Balance - Last Reading**, cell-voltage availability, and the charge taper |

Also check these entities:

- **Balance - Cell Delta at 100% (last full charge)**: the measured spread in millivolts (mV).
- **Balance - Last Reading**: when the last comparable measurement completed.
- **Balance - Trend**: `rising`, `stable`, or `falling` across recent readings.
- **Balance - Delta Average (4 readings)**: the average of the latest four comparable readings.

Its `measured_at` and `soc_at_measurement` attributes say when that snapshot was taken and at what SOC; it does not change between full charges.

### Why the delta does not match max minus min cell voltage

**Balance - Cell Delta at 100% (last full charge)** is not live. It is recorded near the top of a full charge, where LFP cells actually separate. **Maximum Cell Voltage** and **Minimum Cell Voltage** are live, and between roughly 20 % and 90 % SOC an LFP cell's voltage is so flat that even an unbalanced pack shows only a few millivolts between them. A stored 243 mV next to a live 3.331 V / 3.328 V at 80 % SOC is therefore normal and both values are correct.

**Balance - Cell Delta (live)** shows that live spread directly (max − min, in mV). Use it to watch a charge approach the top; judge balance by the top-of-charge value. On Venus A/D with per-pack data it is the widest single pack, with the pack number in its `pack` attribute.

On batteries with per-pack data, **Balance - Cell Delta at 100% (last full charge)** represents the worst internal pack spread. Its `packs_mV` and `worst_pack` attributes identify the pack behind the result; Omnibattery does not subtract the lowest cell in one pack from the highest cell in another.

If orange or red persists across full charges, use the [Marstek active-balance blueprint](../automations/blueprints.md#active-cell-balancing-for-one-marstek-battery) for a supported Marstek battery. Run it for one battery at a time and follow its cleanup notifications before returning that battery to automatic control.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| Balance entities are missing | The driver does not publish both cell-voltage extremes | Whether **Maximum Cell Voltage** and **Minimum Cell Voltage** exist for that battery |
| **Balance - Status** stays `unknown` | No comparable full-charge measurement has completed | **100% Charge Voltage Taper**, the charge target, and **Balance - Last Reading** |
| The latest value looks much higher or lower than usual | The charge ended differently, or the measurement did not come from the same top-of-charge condition | Compare the timestamp and several completed full-charge readings |
| A Venus A/D value seems inconsistent with the battery-level voltage | Battery-level registers can represent pack 1 while the diagnostic uses available per-pack data | `packs_mV` and `worst_pack` on **Balance - Cell Delta at 100% (last full charge)** |
| Orange or red returns after another full charge | The imbalance may be persistent | **Balance - Trend**, recent history, and the active-balance blueprint |

??? "Advanced details: interpreting mV and imbalance"
    **Why measurements are taken near full charge**

    Lithium iron phosphate (LFP) cell voltage stays relatively flat through much of the usable charge range. In that region, voltage differences are poor evidence of a state-of-charge difference. Near the upper knee, cell voltages separate more clearly and the battery management system (BMS) can identify and bleed the leading cells. Omnibattery therefore compares settled top-of-charge readings instead of treating a live mid-charge delta as a health result.

    **The LFP charge curve in detail**

    A typical 3.2 V nominal LFP cell follows a curve where voltage stays almost flat through most of the usable range and only separates near the top:

    | SOC range | Cell voltage range | Slope |
    |---|---|---|
    | 0–10% | 2.50 V → 3.20 V | Steep entry knee |
    | 10–90% | 3.20 V → 3.30 V | Almost flat — about 1 mV per % SOC |
    | 90–97% | 3.30 V → 3.45 V | Mild rise begins |
    | 97–99% | 3.45 V → 3.55 V | Knee — voltage starts climbing sharply |
    | 99–100% | 3.55 V → 3.65 V | Steep top knee — full-charge cliff |

    On the plateau, two cells reading nearly the same voltage can differ by several percentage points of SOC, so a mid-charge voltage delta is not useful evidence of imbalance. It also means passive balancing cannot do anything there: the BMS bleeds the highest cell through a resistor, and to identify which cell is highest it needs the spread between cells to rise above measurement noise. Only above the knee do cell voltages separate enough for the BMS to find and bleed the leader — which is why the thresholds below sit in that narrow top-of-charge window rather than the flat middle.

    **How Omnibattery creates a comparable reading**

    With **100% Charge Voltage Taper** enabled, the control path enters the taper zone at 3.48 V and limits that battery to 200 W. A Venus E battery normally stops at 3.60 V, or earlier when a commanded charge is rejected by the BMS. Charging then remains off for 60 seconds before Omnibattery records:

    ```text
    cell_delta_mV = (maximum_cell_voltage - minimum_cell_voltage) × 1000
    ```

    The taper latch releases after the control cell drops below 3.44 V. Starting and releasing at different voltages prevents repeated transitions while the cell relaxes.

    Venus A/D batteries can contain coupled packs, and their battery-level maximum/minimum registers describe pack 1 rather than every pack. Reaching 3.60 V therefore does not stop charging or start the measurement by itself. Omnibattery continues the 200 W command until a BMS cutoff is confirmed, waits 60 seconds, and then records the worst internal spread among the packs that provide valid data. Cross-pack voltage differences are excluded because each pack has its own BMS.

    A BMS cutoff requires a real charge request, delivered power at or below 10 W, and Standby for five consecutive control cycles. This avoids classifying an idle battery as full. The same cutoff can trigger a settled reading below 3.60 V when the battery remains in the taper zone.

    **Why these voltage thresholds**

    | Threshold | Where it is used | Why this value |
    |---|---|---|
    | 3.45 V | Reference for the start of the upper knee | Roughly where the LFP curve leaves the plateau; below this, cell voltages are too close together to distinguish a real imbalance |
    | 3.48 V | Trigger for tapering charge to 200 W (`NORMAL_BALANCE_TAPER_CELL_VOLTAGE`) | A small margin above the knee confirms the pack is genuinely entering the balance window, not just bouncing on a load step, before power is reduced |
    | 3.44 V | Taper release point (`NORMAL_BALANCE_TAPER_EXIT_CELL_VOLTAGE`) | Starting and releasing the taper at different voltages avoids repeated transitions while the cell relaxes |
    | 3.60 V | Top measurement point; charge stops and the integration waits 60 s before reading the delta (`NORMAL_BALANCE_PAUSE_CELL_VOLTAGE`) | High enough for supported BMS firmware to reach its native top-charge behaviour while retaining headroom below the LFP ceiling; the battery's own BMS can still cut off earlier |
    | 3.57 V | SOC-recalibration retry voltage | The cell must relax back into the balance window before the one-shot 200 W retry begins |
    | 0.20 V (200 mV) | Green/yellow status boundary (`BALANCE_THRESHOLD_YELLOW`) | Set above the normal factory top-of-charge spread so an expected small mismatch does not read as a fault |

    The optional active-balance blueprint uses its own configurable defaults at a finer grain — 3.49 V as its regulated-charge switch-over and discharge floor between retries, 3.40 V as its lowest retry voltage, and 0.03 V (30 mV) as its completion target — since it runs outside the integration's automatic control loop.

    **Status and alerts**

    The status bands use the raw recorded delta: green below 200 mV, yellow from 200 mV to below 230 mV, orange from 230 mV to below 250 mV, and red from 250 mV. Orange and red readings create a persistent notification. A red result on two consecutive full charges adds the degraded-cell warning.

    **Trend and notification logic**

    Omnibattery retains up to 52 comparable readings and calculates the displayed average and trend from the latest four. A change greater than 2 mV per reading is `rising`; less than −2 mV per reading is `falling`; values between those boundaries are `stable`.

    The trend alert accounts for a 180 mV factory baseline. It fires when the trend is rising and the four-reading raw average is above 220 mV. Cell-balance notifications have a seven-day per-battery cooldown, so a continuing condition does not create a new persistent notification every cycle.

    **Why this takes so long**

    Active cell balancing is slow, for two reasons. Passive balancing current is small: a typical LFP BMS bleeds the highest cell through a balance resistor at somewhere between roughly 30 mA and 150 mA, and Marstek Venus packs are typically observed at the low end of that range — around 50 mA for a 100 Ah cell, which removes only about 0.05% SOC per hour from the high cell. These are field-observed estimates, not fixed specifications. The balance window is also narrow: the BMS can only bleed while the pack is above roughly 3.45 V and the highest cell is detectably above the rest, so a charge that reaches the top and immediately returns to discharge spends only minutes there.

    Field observations on real packs are consistent with that arithmetic: reducing the top-of-charge cell delta by roughly 5 mV typically takes around 24 hours of cumulative time at the top of the balance window. Larger imbalances (50 mV or more) can take multiple days of repeated top-balance sessions, and a pack left unbalanced for months may take a week or more to recover. If you run the active-balance blueprint to recover a noticeably imbalanced pack, leave it running overnight (or longer) before checking the result — watching the delta in real time will not show movement within minutes.

    **SOC recalibration on Venus E**

    A Venus E battery can reach 3.60 V while its reported state of charge (SOC) remains below 99%. When that happens outside the weekly cycle, Omnibattery can continue at 200 W until the BMS cuts off. If SOC is still below 100%, it waits for the cell to relax to 3.57 V and allows one more 200 W attempt. This only creates the conditions for recalibration; BMS firmware decides whether the displayed SOC changes.

    **Sensor and diagnostic reference**

    Six diagnostic entities are created only when the driver declares both cell-voltage readings:

    | Entity pattern | Purpose |
    |---|---|
    | `sensor.*_cell_delta` | Last comparable top-of-charge spread in mV, `measured_at`, `soc_at_measurement`, recent history, and optional pack breakdown |
    | `sensor.*_cell_delta_live` | Live max − min cell voltage in mV; unavailable while either reading is missing |
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

    The active-balance blueprint runs outside Omnibattery's normal control loop through **Manual Battery Control**. Its own page is the canonical reference for its charge, rest, retry, and cleanup sequence. Settled measurements published by the blueprint enter the same **Balance - Cell Delta at 100% (last full charge)** history with `source: blueprint`.
