# Huawei SUN2000 + LUNA2000

Omnibattery connects to a Huawei LUNA2000 through its SUN2000 hybrid inverter
over Modbus TCP. Telemetry is always read directly from Modbus. Power commands
can use the **Huawei Solar** integration's services (the default) or optional
direct Modbus writes.

!!! warning "Tested hardware"
    Support has been validated on one EU three-phase installation with a
    SUN2000-8K-MAP0, a LUNA2000-10KW-C1 power module and two LUNA2000-7-E1
    packs. Other SUN2000 models, hardware layouts and firmware versions remain
    untested.

## Choose the control method

Huawei inverters accept only one Modbus connection at a time. Choose the
connection layout before adding the battery:

| Control method | Requirements | Modbus endpoint in Omnibattery |
|---|---|---|
| **Huawei Solar services** (default) | Install and configure the [Huawei Solar integration](https://github.com/wlcrs/huawei_solar), and make sure its LUNA2000 battery device appears in Home Assistant. | A [Modbus proxy](https://github.com/Akulatraxas/ha-modbusproxy) shared by Huawei Solar and Omnibattery. |
| **Direct Modbus writes** | Huawei Solar is not required. This path has only been validated on one installation, so start with the default path when possible. | The inverter itself if Omnibattery is the only Modbus client; otherwise a shared Modbus proxy. |

!!! danger "Do not open two direct connections"
    Do not point Huawei Solar, Omnibattery, evcc or another client directly at
    the inverter at the same time. Put a Modbus proxy in front of it and point
    every client at the proxy.

For the default path, configure Huawei Solar first and verify that its battery
device is available. Configure the proxy with the inverter as its upstream
device, then use the proxy's address and port in both integrations. Omnibattery
does not require Modbus authentication.

## Add the battery

In the Omnibattery setup wizard, select **Huawei SUN2000 + LUNA2000** and fill
in the connection form.

| Field | Description | Default |
|---|---|---|
| **Name** | Name used for the battery device | `Huawei LUNA2000 1` |
| **IP address** | Address of the Modbus proxy, or of the inverter when Omnibattery is its only client | — |
| **Modbus port** | TCP port exposed by the proxy or inverter | `502` |
| **Modbus slave ID** | Unit ID of the SUN2000 inverter, not the EMMA energy manager or a charger. Leave it empty to search automatically. | Automatic search |
| **Direct Modbus writes** | Send power commands directly instead of using Huawei Solar services | Disabled |
| **Huawei Solar battery device** | LUNA2000 device created by Huawei Solar. Required when direct writes are disabled; leave it empty when they are enabled. | — |

The automatic slave-ID search takes about 15 seconds. If one inverter with a
battery is found, Omnibattery selects it. If several are found on a cascaded
installation, choose the inverter to which this LUNA2000 belongs. Add another
battery entry with the other slave ID for each additional storage system.

The wizard checks that the selected Huawei Solar battery device belongs to the
same inverter that answers at the Modbus slave ID. This prevents telemetry from
one inverter and commands from being sent to another in a cascade.

## Power and SOC limits

During the connection test, Omnibattery reads the battery's current charge and
discharge limits. Those values seed the next form. You can reduce them for your
installation; the upper range is bounded by the inverter's maximum active power
because the battery's reported limit can change when packs are added. Every
command is still clamped to the live hardware limit. Nominal capacity is read
from the battery automatically and requires no manual input.

The common limits page also includes:

- maximum SOC: 80–100% (default `100%`);
- minimum SOC: 0–30% (default `10%`);
- mandatory charge hysteresis (minimum 2%);
- backup offgrid threshold.

Huawei only accepts a narrower range in its persistent SOC cutoff registers, so
Omnibattery enforces the complete configured range in software and uses the
hardware cutoffs only when the value can be represented.

LUNA2000 exposes pack data but not individual cell voltages. Cell-balance
monitoring and the 100% cell-voltage taper are therefore unavailable. For the
common runtime controls and system limits, see [Battery configuration](index.md).

## Behaviour specific to Huawei

Because the battery and PV strings share the SUN2000 inverter, available
discharge power decreases as PV output approaches the inverter limit.
Omnibattery accounts for this automatically.

A `0 W` command releases the battery back to the inverter's own working mode;
it does not hold the LUNA2000 idle. The inverter may therefore resume its
self-consumption strategy. Enabling **Battery Manual Control** likewise hands
the battery back to the inverter after the automatic controller stops.

## Troubleshooting

| Problem | Check |
|---|---|
| **Cannot connect** | Verify the address and port, confirm that the proxy can reach the inverter, and make sure no client bypasses the proxy. |
| **Inverter reached, but no battery found** | Leave the slave ID empty to search again, or verify that the chosen ID belongs to the inverter with the LUNA2000 attached. |
| **Huawei Solar battery device is missing** | Install and configure Huawei Solar, or enable direct Modbus writes and leave the device field empty. |
| **Battery device does not match the inverter** | In a cascade, select the Huawei Solar device and slave ID belonging to the same inverter. |
| **The form appears to pause after submission** | An automatic slave-ID scan normally takes about 15 seconds. |

For verified firmware, register mappings and implementation limitations, see
the [Huawei driver assessment](../../reference/driver-assessment-huawei.md).
