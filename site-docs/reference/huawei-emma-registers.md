# Huawei EMMA and charger over Modbus

What the SmartHEMS (EMMA) energy manager and a Huawei charger expose over
Modbus, and what they do not. Every address below was read from hardware — a
SmartHEMS with a SUN2000-8K-MAP0, a LUNA2000 and an SCharger-22KT-S0 — and
checked against Huawei's *SmartHEMS MODBUS Interface Definitions*
V100R025C00SPC100 (2025-06-18).

Addresses are holding registers, read with FC03. The EMMA answers on its own
unit id, the inverter and the charger on theirs; on the reference installation
that is 0, 4 and 9. Input registers (FC04) are not answered at all.

## The charger cannot be controlled over Modbus — but it can over OCPP

**There is no writable register for a Huawei charger.** Section 3.3 of the
specification lists nine registers and every one of them is read-only:

| Register | Signal | Type | Unit | Gain | Read on the reference unit |
|---:|---|---|---|---:|---|
| 30000 | Offering name | str(15) | — | — | `FusionCharge` |
| 30015 | ESN | str(16) | — | — | `NS2491378783` |
| 30031 | Software version | str(16) | — | — | `FusionCharge V100R023C10SPC220` |
| 30076 | Rated power | u32 | kW | 10 | 22.0 |
| 30078 | Charger model | str(14) | — | — | `SCharger-22KT-S0` |
| 30094 | Bluetooth name | str(16) | — | — | — |
| 30500 / 30502 / 30504 | Phase A/B/C voltage | u32 | V | 10 | 238.0 / 237.6 / 235.0 |
| 30506 | Total energy charged | u32 | kWh | 1000 | 1338.219 |
| 30508 | Charger temperature | i32 | °C | 10 | 38.3 |

Searching beyond the specification found nothing either: 47000–47600, 37000–37200
and 31000–31100 answer with a Modbus exception on the charger's unit, and only
30510–30518 return anything (41, 220, 0, 0, 1 — undocumented).

Tables circulating with addresses like `0x1000` for phase voltage and `0x2000`
for a writable *Max. Charge Power* do not describe this interface. Those
addresses are rejected as invalid here, on FC03 and FC04 alike. They presumably
belong to a charger reached directly rather than through an EMMA.

A charger wired into the EMMA by LAN is reachable only as a Modbus unit behind
it, so the direct interface — whatever it offers — is not available either.

**The control path is OCPP.** evcc drives this exact model through its
`ocpp-huawei` template, and the direction of the connection is what makes it
work: the charger is given a backend URL and dials out to it as an OCPP 1.6J
client, rather than being polled. Pointed at a listener on the local network —
`ws://<host>:8887/<stationid>` in evcc's case — it accepts charging-profile
commands, which is the current limit a battery-aware controller would want to
set.

Two things to weigh before going that way, neither of them tested here. A
charger normally holds **one** backend URL, so pointing it at a local listener
takes it away from wherever it points now. And its surplus charging is decided
by the EMMA locally, which raises the same question this integration keeps
meeting elsewhere: two parties deciding the same thing on one meter.

Home Assistant has an OCPP integration that plays the same server role, so the
charger could be surfaced there as entities without evcc. Nothing in Omnibattery
does this today, and a charger is a load rather than a battery — it would be a
separate piece of work, not a driver.

## The EMMA can be configured

Its settings live at 40000, which is worth knowing: a search of the ranges
Huawei uses elsewhere for settings (42000, 47000) finds nothing at all.

| Register | Signal | Type | Unit | Gain | Values | Read on the reference unit |
|---:|---|---|---|---:|---|---|
| 40000 | ESS control mode | enum16 | — | — | 2 = maximum self-consumption, 5 = time of use | 2 |
| 40001 | Preferred use of surplus PV | enum16 | — | — | 0 = fed to grid, 1 = charge | 1 |
| 40002 | Max. power for charging batteries from grid | u32 | kW | 1000 | [0, 50] | 50.0 |
| 40004 | Charge/discharge time window | 43 registers | — | — | — | — |
| 40100 | Control mode at grid connection point | enum16 | — | — | 0 = unlimited | 0 |
| 40101 | Limitation mode | enum16 | — | — | 0 = total power, 1 = single-phase | 0 |
| 40107 | Maximum grid feed-in power | i32 | kW | 1000 | [−1, Pmax] | 0.0 |
| 40109 | Maximum grid feed-in power | u16 | % | 10 | [0, 100] | 0.0 |
| 40110 | Three-phase imbalance control | enum16 | — | — | 0 = disabled, 1 = enabled | 1 |
| 40470, 40490–40495 | System and local time | — | — | — | — | — |
| 41214, 41215 | SmartGuard power supply configuration | enum16 | — | — | — | — |

`40000 = 2` is the register behind an observation this integration keeps running
into: the energy manager regulates the same meter Omnibattery does, because it
is set to maximum self-consumption. Register 40004 holds the time-of-use plan
that would have to be cleared before that mode could be left. Neither has been
written here — this page records what is possible, not a recommendation.

## The EMMA's own measurements

| Register | Signal | Type | Unit | Gain | Note |
|---:|---|---|---|---:|---|
| 30354 | PV output power | u32 | W | 1 | |
| 30356 | Load power | u32 | W | 1 | **Not the house load** — see below |
| 30358 | Feed-in power | i32 | W | 1 | |
| 30360 | Battery charge/discharge power | i32 | W | 1 | |
| 30364 | Inverter active power | i32 | W | 1 | |
| 30368 | State of capacity | u16 | % | 100 | |
| 31657 | Built-in meter active power | i32 | W | 1 | +import / −export |

**31657 is the useful one for control.** It is the EMMA's own grid meter, live on
every request — 25 ms per read, 20 of 20 on the reference installation — where
the `huawei_solar` integration publishes the same figure on a 30 s coordinator.
The Omnibattery driver reads it directly and offers it as a sensor, so an
installation metered by an EMMA needs no second meter.

**30356 looks like the house load and is not usable as one.** The EMMA derives it
from its own PV, battery and meter, so storage it does not control is invisible
to it: while a second battery covered the whole house, this register read 0 W
against a real 660 W. Feeding it into a controller would oscillate, since the
value collapses precisely when the controller acts on it.
