# Installation

## Requirements

### Hardware

| Component | Description |
|---|---|
| Battery | Marstek Venus E v2/v3, Venus A, Venus D **or** Zendure SolarFlow 2400 AC+ / AC Pro **or** Anker SOLIX Solarbank Max AC |
| Modbus converter | RS485 → Modbus TCP device (e.g. Elfin-EW11) — **Marstek Venus E v2 only**. Venus E v3, Venus A and Venus D connect via Ethernet and support Modbus TCP natively. Anker Solarbank Max AC uses native Modbus TCP (enable under Third-Party Control in the Anker app; only one Modbus client at a time). Not required for Zendure (local HTTP). |
| Serial adapter *(optional)* | USB–RS485 adapter for direct serial (Modbus RTU) connection to Marstek batteries. |
| Grid sensor | HA sensor measuring total grid consumption (e.g. Shelly EM3, Neurio, smart meter integration) |

### Software

- Home Assistant **2024.1.0** or later
- (Optional) Solar forecast sensor for predictive charging (Solcast, Forecast.Solar, etc.)

### Network

The battery must be reachable from Home Assistant by IP on the same network segment or via routing.

---

## Installation via HACS (recommended)

1. Click the button to add the repository to HACS:

    [![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ffunes&repository=Omnibattery&category=integration)

2. Search for **"Omnibattery"** and install.
3. Restart Home Assistant.

![HACS search](assets/screenshots/installation/hacs-search.png){ width="700"  style="display: block; margin: 0 auto;"}

---

## Manual installation

1. Download the zip from the latest release at [GitHub Releases](https://github.com/ffunes/Omnibattery/releases).
2. Extract the `omnibattery` folder.
3. Copy it to the `custom_components/` directory of your Home Assistant instance.
4. Restart Home Assistant.

---

## Blueprint installation

Blueprints are optional and are installed in the Home Assistant configuration folder, not inside `custom_components/`.

The blueprint folder for your Home Assistant instance is:

```text
/config/blueprints/automation/omnibattery/
```

If you access Home Assistant through Samba, Studio Code Server or File Editor, the same path is usually shown as:

```text
config/blueprints/automation/omnibattery/
```

### Install from the Home Assistant UI

1. Go to **Settings** → **Automations & Scenes** → **Blueprints**.
2. Click **Import Blueprint**.
3. Paste the URL of the blueprint you want to import, for example:

    ```text
    https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/different_grid_target_blueprint.yaml
    ```

4. Click **Preview Blueprint** and then **Import Blueprint**.
5. Create a new automation from the imported blueprint and select your entities.

### Manual installation

1. Create the `/config/blueprints/automation/omnibattery/` folder if it does not already exist.
2. Copy the `.yaml` files from this repository's `blueprints/` folder into it.
3. In Home Assistant, go to **Settings** → **Automations & Scenes** → **Blueprints** and click **Reload Blueprints**. If the option is not available, restart Home Assistant.
4. Create a new automation from the installed blueprint.

---

## Adding the integration

After installing and restarting:

1. Go to **Settings** → **Devices & Services**.
2. Click **+ ADD INTEGRATION**.
3. Search for **Omnibattery**.
4. Follow the [configuration wizard](configuration/index.md).

![Add integration in HA](assets/screenshots/installation/add-integration.png){ width="600"  style="display: block; margin: 0 auto;"}
