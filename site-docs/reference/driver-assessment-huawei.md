# Battery driver assessment: Huawei SUN2000 + LUNA2000

Completed against [driver-requirements-template.md](driver-requirements-template.md).

Every value below was read from hardware, not from a datasheet. Where a figure
was measured, the measurement is stated. Where something is unknown, it says so.

## Assessment outcome

**SUITABLE WITH LIMITATIONS.** Every blocking requirement is covered. The
limitations are not gaps in the data but two architectural mismatches with the
existing driver model, both described in [§13](#13-what-a-dc-coupled-hybrid-breaks).

## 1. Device and documentation evidence

| Field | Value |
|---|---|
| Manufacturer | Huawei |
| Commercial model | SUN2000-8K-MAP0 with LUNA2000 |
| Device-reported model | Storage `LUNA2000` (register 47000); inverter `SUN2000-8K-MAP0` (register 30000) |
| Verified firmware range | Inverter `V200R024C00SPC110`; power module and packs `V200R025C00SPC103` |
| Region/hardware variant | EU, three-phase, EMMA-A02 energy manager, SmartGuard-63A-T0 |
| Rated capacity and power | 13.8 kWh in two LUNA2000-7-E1 packs on a LUNA2000-10KW-C1 power module; the battery reports 7000 W charge and discharge; inverter 8000 W rated, 8800 W maximum |
| Coupling type | **DC** — battery and PV strings share one inverter |
| Official document | Solar Inverter Modbus Interface Definitions v05; SmartHEMS V100R024C00 Modbus Interface Definitions |
| Interface used | Modbus TCP throughout. Control optionally through the `huawei_solar` integration by @wlcrs, whose register map this work builds on |
| Hardware used for validation | The installation above, one unit |
| Test date | 2026-08-22 / 2026-08-23 |

- [x] The interface is published by the manufacturer (PDFs above).
- [x] Applicable models and firmware are known for the tested unit.
- [ ] **Only one installation tested.** Everything below is single-sample.
- [x] Every field used documents type, unit, scale and sign; each was verified
      against the same value surfaced by `huawei_solar`.
- [x] Read rate and concurrency measured (see transport).
- [x] Restart and disconnect behaviour known (watchdog, see control).

### Firmware compatibility matrix

| Model | Firmware | Transport | Read | Write | Known differences | Status |
|---|---|---|---|---|---|---|
| SUN2000-8K-MAP0 | V200R024C00SPC110 | Modbus TCP via proxy | yes | via `huawei_solar` | Per-string current reads 0 below ~100 W; registers 37026/37036 answer with a Modbus exception; the pack 1 address run answers with padding because that slot is unpopulated | tested |
| Other SUN2000 | — | — | — | — | String count read from 30071; up to four published | untested |

### Transport and access worksheet

| Aspect | Value |
|---|---|
| Scope | local |
| Protocol | Modbus TCP, FC03 only |
| Address | The inverter, reached through a `modbus-proxy` add-on |
| Unit id | **4** = inverter. Also on the bus: 0 = EMMA (`SmartHEMS`), 2 = SmartGuard, 9 = charger. Not derivable, but discoverable: a scan reads 30000 on nine candidate ids and keeps those answering as an inverter with an SOC |
| Discovery | Address manual, unit id scanned. A cascade yields several inverters, and the user picks |
| Authentication | none |
| Maximum simultaneous connections | The inverter tolerates one client; a Modbus proxy fans it out. Verified: this driver read continuously while `huawei_solar` held its own connection |
| Read latency | **median 3.6 ms, 6 ms at p95** over 800 consecutive reads with nothing else on the bus |
| Post-connect pause | **1500 ms required.** The first request after the TCP handshake is dropped without it |
| Request pacing | Single in-flight request; the inverter does not tolerate pipelining. 800 reads each at 10 ms and 0 ms pacing produced zero failures, so the reference library's 50 ms is conservative for Modbus TCP; the driver uses 20 ms |
| Telemetry freshness | Battery and PV values refresh every **2–3 s** at the register |
| Volatile vs persistent | Forcible-charge registers are volatile and carry a duration. SOC cutoffs (47081/47082) are **persistent** |
| Behaviour without network | Forcible commands expire on their own; see the watchdog below |

> The 30 s figure often quoted for Huawei is the `huawei_solar` coordinator
> interval, not the hardware. Reading the registers directly is ~4 ms and the
> data is 2–3 s old at worst.

## 2. Admission gate for automatic control

- [x] Programmable transport with controlled connect, reconnect and close.
- [x] Real, fresh SOC as a percentage — register 37004, verified against the
      `huawei_solar` sensor.
- [x] Real battery power — register 37001, `+charge / −discharge`, already the
      Omnibattery convention.
- [x] Power-limited charging — `huawei_solar.forcible_charge`.
- [x] Power-limited discharging — `huawei_solar.forcible_discharge`.
- [x] A safe idle. **Not the obvious one** — see §13.1.
- [x] Safe per-unit maxima — registers 37046/37048 report 7000 W each.
- [x] BMS protections remain active; the inverter enforces its own cutoffs.
- [x] Write cadence is safe **only with a driver-side throttle** — see §13.2.
- [x] Stale communications are detectable; a failed block omits its keys.

## 3. Declared capabilities

| `DriverCapabilities` | Value | Evidence |
|---|---:|---|
| `hardware_soc_cutoff` | `False` | 47081/47082 exist and are writable, but accept only 90–100 % and 0–20 % — narrower than the window a user may configure. Claiming hardware enforcement would leave an out-of-range limit unenforced anywhere |
| `has_force_mode` | `True` | Forcible charge/discharge/stop |
| `push_telemetry` | `False` | Polled |
| `max_charge_power_w` | 7000 | Register 37046 |
| `max_discharge_power_w` | 7000 | Register 37048 |
| `has_mppt_pv` | `False` | Deliberate. See §13.3 |
| `has_alarm_registers` | `False` | 37014 exists but was not validated |
| `has_rs485_control` | `False` | No external-control gate needed |
| `has_energy_counters` | `True` | 37066/37068 cumulative, 37015/37017 daily |
| `setpoint_confirm_reliable` | `False` | The command registers echo instantly while the battery is still ramping |
| `actuator_latency_s` | 25.0 | **Measured 19.7 s** to 90 % of a +1000 W charge from idle, 11.3 s to reverse to −1000 W, sampled at 1 Hz with exclusive access |
| `readback_latency_s` | 25.0 | Telemetry is milliseconds away; the delay is the physical ramp |
| `engage_grace_s` | 25.0 | Same ramp |

## 4. Telemetry mapping worksheet

All registers FC03 holding, unit id 4, verified against the corresponding
`huawei_solar` sensor on the same installation.

| Omnibattery key | B/R/O | Register | Type | Scale → unit | Cadence | Src | Tested |
|---|---|---|---|---|---|---|---|
| `battery_soc` | B | 37004 | u16 | ÷10 → % | high | N | [x] |
| `battery_power` | B | 37001 | i32 | → W, +charge/−discharge | high | N | [x] |
| `battery_voltage` | O | 37003 | u16 | ÷10 → V | high | N | [x] |
| `inverter_state` | O | 37000 | u16 | enum, refined by direction (§13.4) | high | D | [x] |
| `battery_total_energy` | R | 37758 | u32 | **×0.001 → kWh** | very_low | N | [x] |
| `total_charging_energy` | O | 37066 | u32 | ÷100 → kWh | low | N | [x] |
| `total_discharging_energy` | O | 37068 | u32 | ÷100 → kWh | low | N | [x] |
| `total_daily_charging_energy` | O | 37015 | u32 | ÷100 → kWh | low | N | [x] |
| `total_daily_discharging_energy` | O | 37017 | u32 | ÷100 → kWh | low | N | [x] |
| `internal_temperature` | O | 37022 | i16 | ÷10 → °C | low | N | [x] |
| `max_charge_power` | R | 37046 | u32 | → W | low | N | [x] |
| `max_discharge_power` | R | 37048 | u32 | → W | low | N | [x] |
| `charging_cutoff_capacity` | O | 47081 | u16 | ÷10 → % | low | N | [x] |
| `discharging_cutoff_capacity` | O | 47082 | u16 | ÷10 → % | low | N | [x] |
| `user_work_mode` | O | 47086 | u16 | enum | low | N | [x] |
| `solar_power` | O | 32064 | i32 | → W (DC total) | high | N | [x] |
| `mppt1..4_power` | O | 32016+2n | i16 ×2 | V × I → W | high | D | [x] |
| `inverter_ac_power` | O | 32080 | i32 | → W | high | N | [x] |
| `inverter_max_power` | R | 30075 | u32 | → W | very_low | N | [x] |
| `grid_power` | O | 31657 | i32 | → W, +import/−export | high | N | [x] |
| `off_grid_state` | O | 32003 | u32 | bitfield | medium | N | [x] |
| `ac_offgrid_power` | O | — | — | derived (§13.5) | medium | D | [x] |
| `device_name` | O | 30000 | str(15) | inverter model | very_low | N | [x] |
| `storage_product_model` | O | 47000 | u16 | enum | very_low | N | — |
| `power_module_serial_number` | O | 37052 | str(10) | — | very_low | N | [x] |
| `power_module_firmware_version` | O | 37814 | str(15) | — | very_low | N | [x] |
| `inverter_serial_number` | O | 30015 | str(10) | — | very_low | N | [x] |
| `inverter_software_version` | O | 30050 | str(15) | — | very_low | N | [x] |
| `pack1..3_firmware_version` | O | 38210/38252/38294 | str(15) | — | very_low | N | [x] |
| `pack1..3_serial_number` | O | 38200/38242/38284 | str(10) | — | very_low | N | [x] |
| `max_cell_voltage` / `min_cell_voltage` | O | — | — | **X** | — | X | — |

**Not available.** A LUNA2000 reports per *pack* values, not per cell. There is
nothing honest to put in the cell fields, so balance monitoring and the 100 %
voltage taper are disabled for this brand.

**Registers that did not answer** on the tested unit: 37026 (DCDC version) and
37036 (BMS version) return a Modbus exception; the whole pack 1 run (38200)
returns padding, because that slot holds no pack — the tested unit has packs 2
and 3 only. All three are optional and omitted rather than shipped as
permanently-missing entities.

## 5. Control mapping worksheet

Set-points take either of two paths, selected per battery.

By default they go through the `huawei_solar` services. Optionally the driver
writes the same four-register sequence itself via FC16 — same registers, same
order — which removes the dependency for control. Verified on hardware: a no-op
FC16 write was acknowledged through the Modbus proxy while another client held
its own connection, and a full charge/reverse/release cycle drove the battery as
commanded.

Writing directly means reimplementing two things the services provided: the
power is clamped to the register maximum (`huawei_solar` refuses an over-range
value outright, aborting the control cycle), and the mode register is written
last so a sequence failing earlier leaves the inverter untouched.

| Operation | Call | Range | Persistence | Latency | Safe failure | Tested |
|---|---|---|---|---|---|---|
| Charge at W | `forcible_charge`, or FC16 47247/47083/47246/47100 | 0…37046, duration 1–1440 min | volatile | 19.7 s | expires | [x] |
| Discharge at W | `forcible_discharge`, or FC16 47249/47083/47246/47100 | 0…37048 | volatile | 11.3 s from charge | expires | [x] |
| Idle | `huawei_solar.stop_forcible_charge` | — | volatile | — | already released | [x] |
| Shutdown | same as idle, from `standby()` | — | — | — | — | [x] |
| Max/min SOC | `number.set_value` on the cutoff entities | 90–100 % / 0–20 % | **persistent** | — | skipped when out of range | [x] |

**The service validates power against the register maximum** and raises
`ValueError: Power cannot be more than 7000W` — it refuses an over-range command
rather than trimming it, and the control cycle dies with it. Since the configured
limit may legitimately sit above the present reading (§12), the driver clamps
every set-point to 37046/37048 as read, on both control paths. The configured
figure still binds whenever it is the lower of the two.

**The cutoff entities live on two different devices.** `huawei_solar` puts the
charge cutoff on the inverter and the discharge cutoff on the battery, so
resolving against the configured battery device alone finds one and misses the
other. The driver searches the whole config entry.

**Watchdog — do not rely on it.** Every command carries a duration (10 minutes
as issued), and the register still reads 10 while a command stands. On the
reference installation a forcible discharge written at 04:32 was still in force
at 09:22, five hours later, with the integration disabled for the last of them.
Whatever the duration governs, it did not release this. The release has to be
written, and it has to go out over the same path the command did: releasing
through the `huawei_solar` service while writing registers directly addresses a
device that does not exist on that path, and the failure is silent because
shutdown suppresses the warning.

## 6. Feature degradation matrix

| Feature | Status |
|---|---|
| PD charge/discharge | Supported, with the throttle of §13.2 |
| Multi-battery | Supported; capacity is native |
| Min/max SOC | Software-enforced; registers written as a backstop when representable |
| Predictive / pricing charge | Supported |
| Energy, cycles, efficiency | Native counters |
| 100 % taper, balance monitoring | **Disabled** — no cell voltages |
| Thermal limit | Supported (37022) |
| Backup exclusion | Derived, see §13.5 |
| MPPT / DC production | Reported, with the caveat of §13.6 |
| Alarms | Omitted — 37014 not validated |

## 7. Minimum acceptance tests

Covered by `tests/test_huawei_driver.py` (152 tests). Beyond the template's
list, these encode failures found on hardware — each was a live installation
misbehaving first and a test second:

- Capacity is published in kWh, not the register's Wh.
- A throttled set-point reports the standing command, not the request.
- A reversal skips the deadband but not the ramp.
- The dynamic discharge limit ignores the battery's own contribution.
- No read group may hold a single key.
- The inverter's AC total is not published as the battery's AC port.
- Every form schema survives the serialisation the frontend needs (§13.10).
- Nothing is commanded at all while the strings carry voltage.
- Shutdown clears the registers over the path the commands took.
- An unpopulated pack slot produces no entities at all.
- A battery device belonging to a different inverter is refused.
- The limits form allows more than the battery reports today.
- A set-point above the battery's present reading is trimmed, never sent.

## 12. Setup and identity

**What the user is asked for.** An address and a port; everything else the setup
can find out for itself. The slave id is optional, and left empty it triggers the
scan — which is the recommended path, because a wrong id reads an energy manager
or a charger rather than failing outright. One inverter with a battery is taken
straight away; several mean a cascade and the user picks from a list naming each
model and id. A single checkbox chooses the control path, and the battery-device
field is only needed on the service side.

**A Huawei inverter accepts one Modbus connection.** Anything else already
talking to it — `huawei_solar`, evcc, another system — takes that one. The setup
says so and points at a Modbus proxy, and the reference installation runs one:
this driver read continuously while `huawei_solar` held its own connection.

**One "battery" is three kinds of hardware.** A LUNA2000 installation is an
inverter, a power module, and one to three battery packs, and each carries its
own serial and firmware in its own registers — 30015/30050 for the inverter,
37052/37814 for the power module, 38200+ per pack. Publishing any one of them as
*the* serial mislabels the other two, so the driver names each part. The device
registry entry stands for the storage, so it takes the power module's serial —
and its model from 47000 (`2` = LUNA2000) rather than from 30000, which is the
inverter's. Calling the device a SUN2000 would read as though the packs belonged
to the inverter. That enum is telemetry-only: it resolves to a label and is
dropped, so no entity carries a bare `2`.

The Modbus endpoint can be a fourth device again: on the reference installation
the address answers as `SmartHEMS` (an EMMA-A02, serial NS24A1211290) on slave 0,
with the inverter behind it on slave 4. That is the gateway, not the battery, and
it appears nowhere in the telemetry.

**An EMMA carries the grid meter, and it is worth reading here.** Register
31657 on the EMMA's own unit id is its built-in meter's active power, already in
the sign convention Omnibattery uses. It answers live on every request — 25 ms
per read, 20 of 20 on the reference installation — where `huawei_solar`
publishes the same figure on a 30 s coordinator, far too slow to control
against. An installation with an EMMA is metered by it and may well have no
other meter, so the setup looks for one by model name (`SmartHEMS`) and wires it
up without asking. No EMMA means no entity, rather than one that reads unknown.

Its neighbour `load_power` (30356) is **not** usable as a house-load figure,
despite the name. The EMMA derives it from its own PV, battery and meter, so
storage it does not control is invisible to it: while a second battery covered
the whole house, 30356 read 0 W against a real 660 W. Feeding that back into a
controller would oscillate — the value collapses precisely when the controller
acts on it.

**The battery's reported power caps are a starting value, not a ceiling.**
37046/37048 say what the battery permits right now, and that moves with the pack
count — a third pack raises it. The limits form therefore opens on that figure
but allows up to the inverter's maximum active power (30075), which is what the
installation genuinely cannot exceed: charge and discharge both pass through it.
On the tested unit that is 7000 W reported against an 8800 W inverter.

The driver then has to hold the other end of that bargain: a command above what
the battery permits right now is refused outright, so every set-point is clamped
to the live reading before it is sent (§5).

**The setup names the battery twice.** On the service path a Modbus address
identifies the inverter and a registry device identifies the battery, and
nothing forces those to be the same unit — Huawei inverters cascade, so a
two-inverter bus can readily have telemetry coming from one while the commands
land on the other. Register 30015 carries the inverter serial, which is also
what `huawei_solar` builds its device identifiers from, so the config flow
compares the two and refuses a pairing that contradicts itself. A serial that
is simply absent — older `huawei_solar` releases leave `serial_number` unset —
never blocks the setup; only a contradiction does. Where `huawei_solar` is not
installed at all, the setup says that rather than asking for a device that
cannot exist.

## 13. What a DC-coupled hybrid breaks

This is the part worth reading. Every previous brand is an AC battery with its
own inverter, and several of the driver model's assumptions quietly depend on
that. Each item below was found by breaking a live installation.

### 13.1 Idle cannot mean "hold at zero"

`stop_forcible_charge` does not idle the battery — it returns control to the
inverter's working mode, which resumes self-consumption. The obvious
alternative, a forcible charge at 0 W, does hold it, and was the driver's first
implementation.

It was wrong twice over. A pinned battery cannot absorb its own PV, so the
inverter derates the strings instead — observed dropping from 4757 W to 55 W
within one 30 s sample and staying there for over half an hour of daylight. And
the control layer means something else by idle: manual mode idles once on
turn-on and then deliberately leaves the device alone, so switching a battery to
manual left 13.8 kWh frozen with nothing to clear the command.

**A zero set-point must release this battery.** That does hand it back to a
second regulator on the same meter, which is what the pin was meant to avoid.
Measured against a derated array and a frozen battery, it is the cheaper
side-effect.

### 13.2 The write path needs its own throttle

A set-point costs four serialised Modbus writes inside `huawei_solar`, and the
battery needs ~15 s to reach any target. A 2 s control loop that sees no
response yet keeps revising its request.

Without a throttle, the battery received a new forced command every 10–20 s
swinging between 0 and 4190 W, and the inverter answered by derating PV. A
throttle that lets direction changes bypass it is no throttle at all: the loop
flip-flops between a held zero and a discharge, so nearly every cycle counts as
a reversal. The rule that works is *a reversal skips the deadband but not the
ramp*.

The throttle must also report the **standing** command when it suppresses a
write. Reporting the request tells the control layer the battery was commanded
to a value it never received; it then measures the older power and flags a
battery that accepts commands without delivering.

### 13.3 The static power envelope is not reachable

Battery and PV share one inverter, so the discharge power actually available is
whatever the AC rating has left after PV. At 7 kW of PV on an 8.8 kW inverter
the battery can contribute 1.8 kW whatever its 7 kW BMS allows — and load
sharing, allocating proportionally to nameplate limits, hands it 74 % of the
deficit and starves the batteries that could have delivered.

`BatteryDriver.dynamic_discharge_limit_w(data)` was added for this, defaulting
to None so every AC brand keeps its static envelope. The subtraction must
exclude the battery's own contribution, or the limit chases its own output.

### 13.4 The status register knows no direction

Register 37000 reports Offline / Standby / Running / Fault / Sleep. The panel
prints that verbatim, so this brand sat on the word "Running" all day. Running
is refined by measured battery power into Charge / Discharge / Standby, using
the Marstek register map's wording so the header reads the same across brands.

The direction deadband is not cosmetic: this inverter idles around +50 W, so
comparing against zero labels a standing battery "Charge".

### 13.5 Backup output is not metered

There is no register for off-grid power. Register 32003 bit 0 says on-grid or
off-grid, and while the grid is disconnected the inverter feeds nothing but the
backup circuit — so its AC power *is* the backup output. On-grid the value is
zero, deliberately: printing the house supply in that tile would be wrong.

### 13.6 `ac_power` means something else here

The system aggregates derive household load as
`home = grid + sum(ac_power) + external_solar`, reading `ac_power` as the
battery's own AC port with DC-coupled PV already netted into it.

Register 32080 is the whole inverter's AC output, PV included. Publishing it
under that key counted the roof array twice — once inside `ac_power`, once in
the external solar sensor — and showed 8.42 kW of house load against 8.87 kW of
PV when the real figure was near 0.7 kW.

The inverter total is published as `inverter_ac_power` instead. With no
`ac_power` key the aggregates take their documented fallback, `-battery_power`,
which is what a battery with no AC port of its own actually contributes.

### 13.7 A read group must never hold one optional key

Not brand-specific, but it bites here. The coordinator counts a group that
returns nothing as a failed read, and a cycle in which every attempted group
fails marks the whole battery unavailable and stops the control loop writing to
it.

A block holding a single optional value therefore takes the battery offline on
every poll of that block. The tested inverter answers register 38210 with
padding, so the pack 1 firmware string decodes to nothing, and the battery
flapped in and out of the pool every three seconds all day. Groups are now one
per cadence rather than one per block.

### 13.8 A forcible command caps the inverter's own production

The worst fault this driver has caused, and the one most specific to a hybrid.

**A forcible command is not a request but a ceiling.** The inverter produces
exactly what it was told and curtails the rest of the roof. Caught in the act
with a 315 W charge standing: 288 W harvested from an array that made 5054 W six
seconds after the command ended, while a separate balcony array on the same roof
held steady throughout — so not weather.

A discharge is worse still. It serves the house from the battery and leaves the
MPPT tracker down entirely, so the panels sit at open-circuit voltage drawing
nothing: 374 V at 0.00 A. And with the roof producing nothing, the meter shows a
real deficit — which is what asks for discharge. The command goes on justifying
itself and survives sunrise: commanded at 04:32 against a genuinely dark sky,
still standing at 09:22.

**So while the roof is producing, this driver commands nothing.** It releases
instead and leaves the inverter to its own regulation, which harvests
everything and runs the battery from it. That is not a workaround: on a
DC-coupled hybrid the inverter is the better controller during daylight,
because it is the only party that knows what the array could be making.

#### What "producing" means, and why it takes two signals

The first test was string voltage alone — a string carries voltage in light and
collapses in the dark, so no forecast, sun elevation or clock is involved. It
survives our own commands, which is exactly why it was chosen: under a forcible
discharge the panels sit at open-circuit voltage and still read daylight.

What it cannot do is tell a bright morning from a dim one. An overcast dawn
stands near 300 V while the array makes 50 W, and refusing to command there
costs the house a discharge it needs, for nothing. Measured across three dawns
on the reference installation, from the first non-zero watt to a sustained
150 W:

| Dawn | First harvest | Sustained >150 W | Band |
|---|---|---|---|
| 30.08 | 06:55 | 07:45 | 50 min |
| 31.08 | 06:15 | 07:00 | 45 min |
| 01.09 | 06:48 | 07:38 | 50 min |

So harvested power (32064) is the second signal — with one asymmetry that makes
it usable at all. A *low* reading proves nothing while a command stands,
because the production it suppresses is the command's own doing; gating on it
would read a curtailed roof as darkness, keep commanding, and keep the roof
curtailed. A *high* reading is decisive whatever the driver is doing, since a
command can only ever push that number down. That asymmetry is what hands
control back at sunrise without waiting for anything.

The remaining case — commanding, strings lit, roof quiet — is genuinely
ambiguous, and is resolved by letting go for a moment and reading again. The
release is throttled like any other write, so a probe costs the settle window
plus the ramp interval, about 20 s in every 120 s. Sunrise is worth more.

The threshold is set low on purpose. Too high curtails a roof that is genuinely
producing; too low only costs availability in the dim band.

What this costs is real and belongs in the decision: **a Huawei battery is only
under Omnibattery's control after dark.** During the day it follows its own
energy manager. An installation whose second battery is AC-coupled still gets
full control of that one, which is where the surplus can be steered.

### 13.9 A second battery is household load to the hybrid's manager

Anything drawing power behind the same meter reads as consumption to the
inverter's energy manager, and a second battery is no exception. That cuts both
ways, and both matter.

It is what leaves any control at all during daylight. The hybrid cannot be
commanded while the sun is on the panels (§13.8), so it would be easy to assume
this integration can only stand aside until dark. It cannot command the hybrid —
but on an installation with a second, AC-coupled battery it can command that
one, and the manager has no way to refuse: it sees load and covers it. A command
to the AC battery is a decision that takes effect, not a preference that gets
overruled.

It is also the hazard. Whatever is asked for gets covered — from the sun when it
is there, and **from the hybrid's own battery when it is not**. A charge command
larger than the real surplus therefore pumps one battery into the other through
two conversions, while the meter sits at zero and nothing looks wrong. Observed
with no such bound in place: 1391 W of PV over a 529 W house, one battery taking
in 1110 W while the other gave up 205 W, the meter reading 3 W.

The bound that prevents it is the *uncovered* surplus — what the meter would
read if every battery stopped — rather than the fleet's charging capacity. That
belongs to the control layer rather than to this driver, and is noted here
because the hybrid is what makes it necessary.

### 13.10 A form schema must survive serialisation

Home Assistant hands the frontend a *serialised* copy of every form schema, and
not every voluptuous construct has a serialised form. A `vol.Any` in the
slave-id field — added so an empty value could mean "search" — made the whole
step fail with "Unknown error occurred" before the form was ever drawn. Setup
and reconfiguration alike were unreachable for that brand.

Nothing caught it, because the tests inspected schema markers and never
converted them. Any driver contributing a config-flow step should convert its
schemas in test the way Home Assistant converts them, including the output the
step actually returns rather than only the helper it calls.

## 11. Decision report

```text
Manufacturer/model: Huawei SUN2000-8K-MAP0 + LUNA2000 13.8 kWh
Firmware tested:    inverter V200R024C00SPC110, storage V200R025C00SPC103
Documentation:      Solar Inverter Modbus Interface Definitions v05

Verdict: SUITABLE WITH LIMITATIONS
         Control is available after dark only (§13.8)

Blocking items:
- Real SOC:        N — register 37004, verified
- Real power:      N — register 37001, sign already matches
- Adjustable charge/discharge: yes — 0..7000 W via huawei_solar services
- Safe idle:       yes, as a release rather than a hold (§13.1)
- Safe limits:     registers 37046/37048
- Freshness:       2-3 s at the register; failed blocks omit their keys

Omnibattery adaptations:
- Split transport: native Modbus for telemetry, services or direct registers for control
- Dynamic discharge limit from the inverter's AC headroom
- Per-string power derived from separate voltage and current registers
- Inverter status refined by measured direction
- Unit id discovered by scan; a cascade is resolved by the user
- Inverter, power module and each populated pack named separately
- Limits form bounded by the inverter rating, not by the battery's reading

Disabled features:
- Cell voltages, balance monitoring, 100% voltage taper (per-pack data only)
- Alarms (37014 not validated)

Open risks:
- One installation, one firmware pair
- Direct Modbus writes verified on one installation only; off by default
- Two regulators share the meter whenever the battery is released
- Cascaded inverters reasoned about and guarded against, but never tested:
  the reference bus holds exactly one

Pending hardware tests:
- A second installation, ideally with more than two strings
- A genuine cascade, and a three-pack storage
- Off-grid behaviour (the tested unit has its off-grid switch disabled)
- Whether per-string current is unpopulated below ~100 W on other models
```
