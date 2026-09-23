# Install Omnibattery

Install the integration, connect a supported battery and choose the Home Assistant sensor that measures your home's grid exchange. You can finish a basic setup first and add forecasts, schedules and price-based charging later.

Already using **Marstek Venus Energy Manager**? Stop here and follow [Upgrading from Marstek VEM](upgrading-from-marstek-vem.md) to preserve your configuration and history.

## Before you start

You need:

- Home Assistant **2024.4.1** or later
- a [supported battery](configuration/batteries/index.md) reachable from Home Assistant
- a Home Assistant power sensor that measures total grid import and export in watts or kilowatts

The battery or its bridge must be reachable over the relevant local connection. A grid sensor is required because Omnibattery uses it to decide how much the batteries should charge or discharge.

| If you have | Prepare this before setup | Connection |
|---|---|---|
| **Marstek Venus E v2/v3, Venus A or Venus D** | Enable or add the connection described in the [Marstek guide](configuration/batteries/marstek.md). | Modbus TCP, Modbus RTU over USB–RS-485, or a LilyGo RS-485/ESPHome bridge for Venus E v2 |
| **Zendure SolarFlow 800, 800 Plus, 800 Pro, 1600 AC+, 2400 AC+, 2400 AC Pro, 3000 Mix AC+, 4000 Mix AC+ or 4000 Mix Pro** | Keep the manufacturer's **HEMS** control disabled so it does not replace Omnibattery's power request. | Local HTTP API |
| **Anker SOLIX Solarbank Max AC, Solarbank 4 E5000 Pro or Solarbank XE AC** | Enable **Third-Party Control** in the Anker app and disconnect any other Modbus client. | Modbus TCP |
| **Huawei SUN2000 + LUNA2000** | Prepare the inverter's Modbus connection. The default control method also needs the Huawei Solar integration. | Modbus TCP through the inverter or a shared proxy |
| **Sessy Home Battery** | Have the local credentials printed on the Sessy dongle available. | Local HTTP API |
| **Hoymiles MS-A2 or HiBattery** | Configure Home Assistant's MQTT integration and a local MQTT broker, then enable **MQTT Service** in S-Miles Home. | MQTT through Home Assistant |

!!! warning "Use a responsive grid sensor"
    An update every **1–2 seconds** is recommended. Sensors that publish every **10 seconds or more** are accepted, but Omnibattery raises a Repairs warning because delayed readings reduce control quality. The latest reading remains valid for up to **65 seconds**.

    See [Main sensor](configuration/main-sensor.md) for sign conventions, units and meter-specific guidance.

### Optional information

You can leave these fields empty or disabled during the first setup and add them later through **Settings → Devices & services → Omnibattery → Configure**:

- **Solar forecast remaining today** for predictive charging and solar charge delay
- **Solar production sensor** when an external inverter measures panels that do not feed the battery's own solar inputs
- **Off-grid power sensor** when a separate meter measures the backed-up circuit
- **Three-phase current protection** when phase-current sensors are available; see [Three-phase current protection](configuration/three-phase.md)

Set **Maximum contracted power** to the actual import limit for your home. Omnibattery uses it as a charging safety ceiling.

??? "Connection details by battery type"
    **Marstek:** Venus E v2 needs an RS-485 connection, such as a USB adapter or an RS-485-to-TCP converter. Venus E v3, Venus A and Venus D can use their network connection. The LilyGo path requires the supported ESPHome firmware and its stock Home Assistant entities.

    **Zendure and Sessy:** Home Assistant must be able to reach the device's local HTTP endpoint. Omnibattery communicates locally rather than through a manufacturer cloud account.

    **Anker:** its Modbus server accepts one client at a time. Close another integration or tool before Omnibattery tests the connection.

    **Huawei:** Omnibattery reads through the SUN2000 inverter. By default, control commands use Huawei Solar services; direct Modbus writes are an optional setup choice.

    **Hoymiles:** Omnibattery uses Home Assistant's configured MQTT broker. It does not install or manage a broker.

## Install with HACS

Home Assistant Community Store (HACS) is the recommended installation method.

1. Use the button below to add the repository to HACS.

    [![Add Omnibattery to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ffunes&repository=Omnibattery&category=integration)

2. In HACS, search for **Omnibattery** and select **Download**.
3. Restart Home Assistant when HACS asks you to.

## Install manually

1. Download the zip file from the [latest Omnibattery release](https://github.com/ffunes/Omnibattery/releases).
2. Extract the `omnibattery` folder into your Home Assistant `custom_components` directory.
3. Confirm the resulting path is `custom_components/omnibattery/manifest.json`.
4. Restart Home Assistant.

## Add the integration

1. Open **Settings → Devices & services**.
2. Select **Add integration** and search for **Omnibattery**.
3. Choose your grid sensor and enter your installation's electrical settings.
4. Add each battery, then finish or configure the optional time slots, excluded devices and predictive charging sections.

![Add Omnibattery from Home Assistant's integration dialog](assets/screenshots/installation/add-integration.png){ width="600" style="display: block; margin: 0 auto;" }

The [configuration guide](configuration/index.md) explains each page of the setup wizard.

## Check the result

After the wizard finishes:

- open the Omnibattery sidebar panel and confirm that **Grid**, **Home** and **Battery** show plausible power values
- open **Batteries** and confirm that each battery reports its state of charge (SOC) and power
- open **Control** to enable only the optional features you want

If the battery is unavailable or its values have the wrong sign, use the relevant [battery setup guide](configuration/batteries/index.md) and [Troubleshooting](troubleshooting.md) before enabling automatic control.

## Blueprint installation

Blueprints are optional and are installed separately from the integration. Omnibattery works without them. When the basic integration is operating correctly, see [Blueprints](automations/blueprints.md) for Home Assistant and manual installation steps.
