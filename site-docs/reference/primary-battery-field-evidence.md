# Field evidence: the charge order on a mixed AC/DC installation

Measurements from the reference installation behind the primary-battery work,
recorded on 26 and 27 August 2026 either side of the fix. It exists so the
change can be judged against what the hardware actually did rather than against
a description of it.

Sign convention throughout: **positive = charging**, negative = discharging.
All per-hour figures are hourly means from Home Assistant's long-term
statistics, not spot readings.

## Installation under test

| | Marstek Venus D | Huawei LUNA2000 |
|---|---|---|
| Coupling | AC | **DC (hybrid inverter)** |
| Usable capacity | 15.36 kWh | 13.8 kWh |
| Power limit | 2500 W | 7000 W |
| Discharge floor | 12 % | 5 % |
| Commandable in daylight | yes | **no** |

Both sit behind one Huawei EMMA energy manager. Two properties of that topology
drive everything below:

1. The EMMA routes DC PV into the LUNA battery on its own authority. That
   energy never crosses the grid meter, so from the outside a hybrid filling
   itself and a hybrid doing nothing look identical.
2. The hybrid refuses forcible charge/discharge commands while its strings
   carry voltage. Every command Omnibattery sends it in daylight is reduced to
   0 by the driver's PV guard, and the inverter has been observed rejecting
   even that write (register 47083, `values=[0]`, 26.08 10:31:59).

The Marstek has the opposite property, and it is what makes the installation
controllable at all: it is ordinary household load as far as the EMMA is
concerned, so a charge command to it is never refused. Asking it to charge
makes the EMMA see more house load, which is how the surplus is moved.

## Instruments

| Quantity | Sensor | Note |
|---|---|---|
| Roof DC | `huawei_luna2000_1_solar_power` | inverter DC total |
| Balcony array | Shelly plug, `..._switch_0_power` | direct measurement, not derived |
| Irradiance | `garten_lichtsensor_illuminance` | **not recorded** — live only |

The garden illuminance sensor is excluded from the recorder, so no historical
lux values exist for either day and none are quoted here. Irradiance is
represented by measured PV power in watts throughout, which is the calibrated
quantity anyway. Live lux readings are being captured from 27.08 09:37 onward
(21 827 lx at 7 452 W of PV) for future comparisons.

## The defect

`_scarce_solar_day` decides whether the day's sun is worth concentrating in one
battery. On a scarce day the charge order puts the DC-coupled battery first,
which is sound: kilowatt-hours stored without an AC conversion lose about 10 %
less.

The threshold was the room left in the **whole fleet** — that is, "is there
enough sun to fill everything". On a mixed installation there almost never is,
so the verdict was scarce essentially every day and the AC-coupled battery was
passed over on all of them.

The rule then fed itself. The AC battery's own empty capacity was the largest
term in "room left in the fleet", so the emptier it got, the more certain the
day was to be called scarce, and the more certain it was to be skipped again.

## 26 August — under the fleet-room threshold

| Local time | Roof | Balcony | PV total | Marstek SoC | Marstek | Huawei SoC | Huawei |
|---|---|---|---|---|---|---|---|
| 06:00 | 12 W | 1 W | **13 W** | 20.0 % | -612 W | 62.8 % | -492 W |
| 07:00 | 85 W | 23 W | **108 W** | 15.8 % | -586 W | 60.2 % | -248 W |
| 08:00 | 301 W | 79 W | **380 W** | 12.4 % | -171 W | 57.6 % | -596 W |
| 09:00 | 821 W | 188 W | **1009 W** | 12.0 % | -12 W | 56.0 % | +512 W |
| 10:00 | 1753 W | 348 W | **2101 W** | 12.0 % | -12 W | 62.9 % | +1628 W |
| 11:00 | 1460 W | 323 W | **1783 W** | 12.0 % | -12 W | 72.8 % | +1222 W |
| 12:00 | 694 W | 166 W | **860 W** | 12.0 % | -12 W | 77.7 % | +331 W |
| 13:00 | 619 W | 148 W | **767 W** | 12.0 % | +4 W | 78.1 % | +184 W |
| 14:00 | 1454 W | 302 W | **1757 W** | 12.0 % | -12 W | 82.2 % | +1230 W |
| 15:00 | 1437 W | 336 W | **1772 W** | 12.0 % | -12 W | 87.5 % | +430 W |
| 16:00 | 864 W | 202 W | **1066 W** | 12.0 % | -12 W | 89.5 % | -95 W |
| 17:00 | 460 W | 107 W | **568 W** | 12.0 % | -12 W | 85.6 % | -695 W |
| 18:00 | 228 W | 54 W | **282 W** | 12.0 % | -12 W | 80.0 % | -731 W |
| 19:00 | 189 W | 44 W | **233 W** | 12.0 % | -12 W | 72.7 % | -1782 W |
| 20:00 | 24 W | 2 W | **26 W** | 12.0 % | -12 W | 59.4 % | -1297 W |

The Marstek reaches its 12 % discharge floor at 08:00 and stays at exactly
12.0 % for the next seventeen hours, drawing its −12 W standby current and
nothing else. The Huawei climbs from 56.0 % to 89.5 % across the same window,
then empties itself overnight to 5 %.

At 10:00 the controller's own diagnostics recorded `uncovered_load_w: -836`
and `absorb_w: 836` — an 836 W surplus, correctly identified. The charge order
that hour read `["Huawei LUNA2000", "Marstek Venus"]` with `thin_solar_day:
true`, so the whole allocation went to the battery that cannot accept a command
in daylight. The Marstek received its first and only charge command of the day
at 13:36, held it for two minutes at roughly 250 W, and stopped.

Day totals, 26 August:

| | charged | discharged |
|---|---|---|
| Marstek | **0.02 kWh** | 4.61 kWh |
| Huawei | 5.78 kWh | 3.14 kWh |

Solar 10.82 kWh, house 11.79 kWh, grid import 0.34 kWh, export 0.42 kWh. The
day was genuinely thin — the forecast said 22.09 kWh and half of it arrived.
The defect is not that a sunny day was misread as thin; it is that on a thin
day the AC battery receives nothing at all, and that the threshold made nearly
every day thin.

## 27 August — under the DC-room threshold

Scarcity is now measured against the room in the battery the order concentrates
into, the DC-coupled one. Past that point the excess has to be shared out
regardless, so the day stops counting as scarce exactly where the preference
stops paying.

| Local time | Roof | Balcony | PV total | Marstek SoC | Marstek | Huawei SoC | Huawei |
|---|---|---|---|---|---|---|---|
| 00:00 | 0 W | 0 W | **0 W** | 12.0 % | -12 W | 21.1 % | -733 W |
| 01:00 | 0 W | 0 W | **0 W** | 12.0 % | -12 W | 15.6 % | -642 W |
| 02:00 | 0 W | 0 W | **0 W** | 12.0 % | -12 W | 10.9 % | -645 W |
| 03:00 | 0 W | 0 W | **0 W** | 12.0 % | -12 W | 6.2 % | -388 W |
| 04:00 | 0 W | 0 W | **0 W** | 12.0 % | -12 W | 5.0 % | -8 W |
| 05:00 | 0 W | 0 W | **0 W** | 12.0 % | -12 W | 5.0 % | +20 W |
| 06:00 | 111 W | 2 W | **113 W** | 12.0 % | -12 W | 5.0 % | +42 W |
| 07:00 | 2319 W | 34 W | **2354 W** | 14.7 % | +964 W | 6.5 % | +766 W |
| 08:00 | 4498 W | 80 W | **4577 W** | 24.5 % | +1973 W | 15.9 % | +1945 W |

Both batteries rise together from first light. The order reads
`["Marstek Venus", "Huawei LUNA2000"]` with `thin_solar_day: false`.

## The controlled comparison

The two days differ in weather, so the honest comparison is between hours of
matched irradiance rather than between daily totals:

| | PV total | Marstek | Huawei |
|---|---|---|---|
| 26.08 10:00 | 2 101 W | **−12 W** at 12.0 % | +1 628 W at 62.9 % |
| 27.08 07:00 | 2 354 W | **+964 W** at 14.7 % | +766 W at 6.5 % |

Comparable sun, opposite outcome. On the first day the AC battery sat idle at
its floor while the nearly two-thirds full hybrid took the entire surplus; on
the second it took the larger share of it.

Note also that the hybrid is *lower* on 27.08 (6.5 % against 62.9 %) and still
does not monopolise the surplus. The change does not simply reverse the
preference — it stops the preference from applying once the surplus exceeds
what the hybrid can hold.

## Handover without a command

A second property falls out of measuring against the hybrid's room: as the
hybrid fills, that room shrinks toward zero, the day turns ample, and the order
hands over on its own. Nothing has to command the hybrid to stand down — which
matters, because on this installation nothing can.

## Secondary observations

**Acknowledgement mismatches on mode changes.** The Marstek fails to
acknowledge standby commands during a direction change, four times between
02:47 and 03:07 and twice more at 06:59 and 07:00 on 27.08:

```
requested(force=0 charge=0W discharge=0W)
readback (force=2 charge=0W discharge=807W battery=-12W)
-> Power command failed after 2 attempts (reason=ack_mismatch)
```

In some cases only the stale power register differs while `force` already
matches, which makes the comparison a false alarm — the register is
don't-care at `force=0`. In others `force` itself still reads the old mode, so
the battery genuinely had not applied the command yet. Both resolve within a
cycle and charging proceeds normally afterwards. This looks like the readback
window being too short for a mode change rather than a lost command, and is not
addressed by this work.

**HACS and prereleases.** Unrelated to the integration, but it cost a day of
testing: releases published with the prerelease flag are invisible to a HACS
install that does not show betas. HACS then reports no releases at all, falls
back to the default branch, and offers its HEAD commit as an upgrade — which on
a feature branch is a silent downgrade. One such automatic attempt is in the log
at 27.08 01:00:02, and it failed only because HACS treated the commit hash as a
branch name and got a 404.

## The full day, 27 August

The charge order held `["Marstek Venus", "Huawei LUNA2000"]` with
`thin_solar_day: false` from first light until late afternoon. Day totals:

| | charged | discharged |
|---|---|---|
| Marstek | **13.99 kWh** | **0.00 kWh** |
| Huawei | 14.03 kWh | 2.80 kWh |

Solar 45.74 kWh, house 9.61 kWh, grid export 12.10 kWh, import 1.74 kWh. The
AC-coupled battery went from 12 % to 95 % without discharging once. On the
previous day it took 0.02 kWh and gave up 4.61 kWh.

At the end of the day the verdict flipped as designed: once the remaining sun
fell below what the hybrid could still hold, `thin_solar_day` turned `true` and
the order returned to `["Huawei", "Marstek"]`. That is the DC preference doing
its job at exactly the point it starts paying again.

## Telling weather from control

A garden illuminance sensor was added mid-day specifically to separate the two.
It settled the one question the power figures alone could not answer.

At 13:40 the Marstek's charge power fell from 2305 W to 1253 W and then to
479 W. Read on its own that looks like a passing cloud. Measured against
irradiance it is the opposite:

| | 13:30 | 13:40 | 13:45 |
|---|---|---|---|
| Marstek | +2305 W | +1253 W | **+479 W** |
| Illuminance | 31 096 lx | 54 513 lx | **83 004 lx** |
| PV | 3617 W | 5889 W | **8488 W** |

Power falling by a factor of five while irradiance rises by a factor of nearly
three. Without the second instrument this would have been recorded as weather.
Four other drops that day were weather, and were identified as such by the same
test.

## The absorption tail

Above roughly 90 % the Venus D tops its six packs up **one at a time**, and
reports a much lower charge ceiling while doing so. It asked for 200 W;
Omnibattery commanded exactly that and followed it correctly — `hours_to_full`
of 6.9 h is 1.38 kWh ÷ 0.2 kW from the same figure.

Pack 1 completed at 13:56, pack 2 at 15:17, with packs 3–6 sitting untouched at
90.0 % throughout. Power did **not** step back up between packs: it stayed flat
at 140–145 W across the handover and across several irradiance swings. The
sequential packs decide *which* cells fill, not how fast.

The tail is slow and expensive, and it did not finish: the battery was still at
95 % and charging at 146 W when observation ended. Conversion efficiency is the
reason it is expensive — see below.

## Measured AC/DC conversion

The setpoint is AC-side; `battery_power` is what reaches the BMS.

| Load | AC | BMS | loss |
|---|---|---|---|
| Full charge | 2468 W | 2308 W | 6.5 % |
| Absorption | 197 W | 144 W | **27 %** |
| Discharge (hourly means) | 513 / 567 / 542 W out | 555 / 612 / 586 W drawn | ~7.5 % |

Round trip is therefore about 86 % at full load, and far worse in the tail.
This matters for the scarce-day preference: the argument for filling the
DC-coupled battery first is not the 7 % at full load but the 27 % in the
absorption phase, where an AC battery spends hours.

Omnibattery does not confuse the two figures — the Marstek driver publishes
`ac_power` separately and the surplus calculation uses it, which is the correct
one because it is what crosses the meter.

## A false BMS cutoff on multi-pack hardware

Reported upstream as ffunes/Omnibattery#350 and fixed there.

At 15:17, the moment pack 2 completed, the charge hysteresis latched at the
aggregate SoC of 93 % and charging stopped. Cause: a finished pack holds the
shared `max_cell_voltage` at 3.481 V, one millivolt above
`NORMAL_BALANCE_TAPER_CELL_VOLTAGE`, which arms the BMS-cutoff detector for the
whole battery while four packs are still at 90 %.

It is self-correcting. The staleness check cleared the latch after 12.5 minutes
and charging resumed with the SoC *rising*, not falling. At absorption power the
pause cost about 0.04 kWh.

Re-measured on 1 September against the fix in #350: two pack handovers passed
with the detector armed, at the same 93 % aggregate where 27 August stalled, and
neither latched.

## Limitations

- Two days, one installation, one brand pairing (Marstek + Huawei).
- No lux history, as noted above; irradiance is represented by PV power.
- 27 August is the sunnier day. The matched-irradiance comparison controls for
  this; the daily totals do not, and should not be read as if they did.
- **The Marstek did not reach 100 %.** It was at 95 % and still charging when
  observation ended; the absorption tail would have run well past sunset.
- **The handover between two receptive batteries was not observed.** The hybrid
  reached 100 % at 12:50, so from then on the surplus beyond what the Marstek
  could take went to the grid — 12.10 kWh over the day. That is the fleet's
  power limit, not a control fault, but it means the case the change was
  designed for (a full hybrid stepping aside for an empty AC battery) still
  needs a day where both have room.
- One irradiance instrument, added mid-day, with no recorded history before
  09:37.
