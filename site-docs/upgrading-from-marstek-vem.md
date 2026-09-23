# Upgrade from Marstek Venus Energy Manager

Move from the old `marstek_venus_energy_manager` integration to Omnibattery without rebuilding your settings, dashboards or automations. The migration keeps the existing entity IDs so Home Assistant history remains connected.

## Is this guide for me?

**Use it if** Home Assistant currently has, or previously had, a **Marstek Venus Energy Manager** integration entry.

**You do not need it if** this is your first installation. Follow the [installation guide](installation.md) instead.

## Before you start

- Create a full Home Assistant backup from **Settings → System → Backups**.
- Update Marstek Venus Energy Manager to **v2.0.6** while it is still installed.
- Restart Home Assistant and wait for the old integration to load. This writes the recovery copy used if the config entry is later deleted.

!!! important "Keep the legacy config entry"
    Do not delete **Marstek Venus Energy Manager** from **Settings → Devices & services** before running the migration. Omnibattery can migrate a live legacy entry directly. The recovery copy is a fallback when that entry has already been deleted.

## Run the upgrade

1. Add the Omnibattery repository to Home Assistant Community Store (HACS), download **Omnibattery**, and restart Home Assistant.
2. Open **Settings → Devices & services**, select **Add integration**, and search for **Omnibattery**.
3. On **Migrate from Marstek Venus**, confirm the migration.
4. Wait for the success message, then reload the browser page. Use a hard refresh if the Omnibattery sidebar panel does not appear.

The flow creates Omnibattery entries itself. A normal new-installation form should not appear when Home Assistant still has a legacy entry to migrate.

## What is preserved

| Item | Result after migration |
|---|---|
| Battery connections and integration settings | Copied to the Omnibattery config entry |
| Control tuning, time slots, limits and other options | Copied unchanged |
| Entity IDs and unique IDs | Kept so dashboards, automations and templates continue to refer to the same entities |
| Recorder history and long-term statistics | Remain attached to the unchanged entity IDs |
| Daily energy, accumulators and integration history | Copied to the new integration storage keys |
| Entity names and area assignments | Kept in the Home Assistant entity registry |

Existing entity IDs can still begin with `marstek_venus_`. This is expected and protects recorder history and existing references.

## Check the result

1. Open **Settings → Devices & services → Omnibattery** and confirm that each previous configuration appears.
2. Open the Omnibattery sidebar panel and check the live grid, battery power and state of charge.
3. Check an existing dashboard or automation that uses a `marstek_venus_*` entity.
4. Review **Settings → System → Repairs** and the Home Assistant log before discarding your pre-upgrade backup.

## If the old integration was already deleted

Start **Add integration → Omnibattery**. If the recovery copy exists and there are no live old or new entries, Omnibattery shows **Restore previous configuration**. Leave **Restore previous configuration** enabled and submit the form.

This recovery path recreates the config entries from the saved connection data and options. It also reconnects entity history and copies persisted integration storage into the new entry's namespace.

If neither the migration screen nor the restore screen appears, the old config entry and its recovery copy are unavailable. Restore the full Home Assistant backup you made before the upgrade, update the old integration to v2.0.6, restart it, and begin again.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The regular Omnibattery setup opens | No live legacy entry or usable recovery copy was found | Restore your Home Assistant backup and confirm the old integration loads before switching |
| **Restore previous configuration** appears instead of **Migrate from Marstek Venus** | The legacy config entry was deleted, but its recovery copy survived | Accept the restore; this is the intended fallback path |
| Migration succeeds but a battery is unavailable | Its saved network address, bridge or credentials are no longer reachable | Open the battery device, check its connection and use the relevant [battery guide](configuration/batteries/index.md) |
| The sidebar still shows the old panel or no panel | Browser frontend files are cached | Hard-refresh the browser or clear the Home Assistant frontend cache |
| An entity ID still starts with `marstek_venus_` | The migration deliberately retained it | Leave it unchanged unless you are ready to update every external reference |

??? "Advanced details"
    Omnibattery first looks for config entries owned by the legacy `marstek_venus_energy_manager` domain. It unloads each old entry, creates its Omnibattery replacement with the same data and options, repoints registry entities to the new platform, copies integration storage files and then loads the new entry.

    If the legacy entry was deleted, the fallback reads a domain-independent recovery store written by the old integration. It recreates the entries under the `omnibattery` domain and copies storage files from the saved entry namespace.

    The recorder database is not rewritten. Home Assistant continues to find its history and long-term statistics because the migration retains each entity ID and unique ID.

    You can later use Home Assistant's **Recreate entity IDs** action if you want fresh system entities to use the `omnibattery_*` prefix. That rename requires you to update automations, templates, Energy dashboard entries and any external consumers that use the old IDs.
