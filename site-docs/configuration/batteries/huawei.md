# Huawei SUN2000 + LUNA2000

Omnibattery reads a Huawei SUN2000 inverter and its LUNA2000 battery locally, then controls battery power when doing so will not reduce your solar harvest. This gives Home Assistant automatic battery control at night while the inverter keeps control when solar production matters.

## Do I need it?

**Use it if** your SUN2000 reports a LUNA2000 battery over Modbus TCP and you want Omnibattery to coordinate it with your grid sensor, schedules, or other supported batteries.

**Do not use it for daytime forced discharge.** A forced command can limit the inverter's solar production. Omnibattery therefore leaves the inverter in control while its solar strings are producing; discharge is available after dark, and charge may be sent in daylight only when it cannot constrain available solar production.

| Supported equipment | Connection | Automatic control | Important limit |
|---|---|---|---|
| SUN2000 inverter reporting an attached LUNA2000 | Modbus TCP, directly or through a Modbus proxy | Yes, subject to the solar-production safeguard | The effective charge and discharge limit is the lower of your configured limit and the live limit reported by the battery. |

Omnibattery reads the battery capacity, energy counters, temperature, battery voltage, state of charge (SOC), battery power, solar power, and inverter status. It can also show per-string solar power for up to four strings. An attached EMMA energy manager is detected automatically when available and can provide a fast grid-power reading.

## Before you start

- Confirm that Home Assistant can reach the inverter or a Modbus proxy on the local network.
- If **Huawei Solar** is already connected to the inverter, use a Modbus proxy: the inverter accepts one Modbus connection at a time.
- Choose one control route:
    - Default: install and configure **Huawei Solar**, then identify its LUNA2000 battery device in the wizard.
    - Alternative: use **Direct Modbus writes**. This does not require Huawei Solar, but is off by default.
- Use the inverter's Modbus unit ID, not the EMMA or charger unit ID. You can leave it empty for Omnibattery to search.

!!! important "Daytime control is intentionally limited"
    The inverter and solar panels are DC-coupled. During solar production, Omnibattery releases control rather than sending a command that could curtail the array. This is expected behaviour, not a failed command.

## How to add it

This route is configured in the Omnibattery wizard; there is no separate switch to enable it.

1. Open **Settings → Devices & services → Add integration** and select **Omnibattery**. For an existing installation, open Omnibattery and select **Configure**.
2. At **Battery _n_ — Brand**, select **Huawei SUN2000 + LUNA2000**.
3. At **Configure battery _n_ — Connection (Huawei)**, enter **Name**, **IP Address**, and **Modbus Port** for the inverter or Modbus proxy.
4. Leave **Modbus slave id (leave empty to search)** blank to find the inverter automatically, or enter its inverter unit ID. If more than one inverter with a battery answers, choose the correct one at **Configure battery _n_ — Choose inverter**.
5. Leave **Direct Modbus writes** off and select **Huawei Solar battery device**, or turn on **Direct Modbus writes** and leave the device field empty. Finish the shared power and SOC limits.

The wizard verifies that a LUNA2000 is attached. On the default route, it also rejects a Huawei Solar battery device that belongs to a different inverter.

## What you will see

You will see automatic battery control, **Battery Manual Control**, and the shared manual power controls. A zero or idle command releases the battery back to the inverter's own working mode; it does not hold the battery at zero power.

The battery device provides readings including **Battery SOC**, **Battery Power**, **Battery Voltage**, **Solar Power**, **Storage Status**, **Working Mode**, energy counters, and **Battery Temperature**. **Grid Power** appears only when an EMMA is detected. **MPPT1 Power** through **MPPT4 Power** appear only for strings the inverter reports.

Omnibattery uses software enforcement for your general minimum and maximum SOC. The inverter's own cutoff registers are used only as a narrower backstop. Cell-balance monitoring, voltage taper, and alarm-register support are not available for this route because it does not provide the required cell readings or validated alarm data.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard cannot connect | Wrong address, port, or inaccessible Modbus endpoint | Confirm that Home Assistant can reach the inverter or proxy and that Modbus TCP is available. |
| The wizard finds no battery | The selected unit is not the inverter, or no LUNA2000 is attached | Leave **Modbus slave id (leave empty to search)** empty, then check the inverter connection and LUNA2000 installation. |
| The wizard asks which inverter to use | More than one inverter with a battery answered on the bus | Select the inverter that owns this LUNA2000; add another battery entry for another inverter. |
| The default control route cannot be completed | Huawei Solar is missing, or its battery device was not selected | Configure **Huawei Solar** and select **Huawei Solar battery device**, or enable **Direct Modbus writes**. |
| Commands have no effect in daylight | Omnibattery detected producing solar strings | This is the solar-production safeguard. Check battery control after dark instead. |
| Requested power is lower than configured | The battery currently reports a lower charge or discharge capability | Check **Max Charge Power** or **Max Discharge Power** and the battery's pack configuration. |
| Grid Power is unavailable | No EMMA was detected on the Modbus bus | Configure your normal Home Assistant grid-power sensor; the Huawei battery remains usable without the EMMA reading. |

??? "Advanced details"
    Telemetry uses Modbus TCP holding-register reads. The default control route sends forcible charge, discharge, and release commands through Huawei Solar services; **Direct Modbus writes** writes the same forcible-control sequence to the inverter.

    The wizard searches inverter unit IDs from `0` to `247` when the slave ID is blank. It distinguishes inverter, energy-manager, and charger addresses by probing the inverter model and attached storage. If a Modbus proxy serves cascaded inverters, the wizard asks you to select the one with this battery.

    The limits form accepts `100–15,000 W` for each direction. Omnibattery initially uses the live charge and discharge limits reported by the battery, and each command is limited again to the lower of that live value and your configured limit. The inverter's own charge-cutoff register accepts `90–100%`; its discharge-cutoff register accepts `0–20%`. Limits outside those hardware ranges remain software-enforced.

    A forcible command lasts `10 min`, but Omnibattery refreshes or changes commands as needed and releases the inverter for an idle target. The command path has a `250 W` write deadband, waits at least `20 s` between ordinary writes, and refreshes a held command after `240 s`. The declared actuator latency is `25 s`, so an immediate power readback is not treated as an exact delivery check.

    The driver reads up to four string voltage/current pairs and derives each **MPPT _n_ Power** value. Those readings describe the inverter's DC solar side; they are not exposed as an AC battery port. The LUNA2000 reports pack information rather than the per-cell voltages needed for cell-balance monitoring and voltage taper.
