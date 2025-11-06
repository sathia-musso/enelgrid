import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    clear_statistics,
    list_statistic_ids,
)

from .const import DOMAIN, CONF_POD

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entry to new format."""
    _LOGGER.warning(f"[EnelGrid Migration] Starting migration from version {entry.version}")

    if entry.version == 1:
        # Version 1 -> 2: Clear all statistics and trigger full historical fetch
        # This fixes corrupted data from previous buggy migrations
        await _migrate_v1_to_v2(hass, entry)
        hass.config_entries.async_update_entry(entry, version=2)
        _LOGGER.warning("[EnelGrid Migration] Migration to version 2 complete")

    if entry.version == 2:
        # Version 2 -> 3: Clear corrupted statistics and trigger full historical fetch
        # This fixes data corruption from v1.4.0/v1.4.1 buggy migrations
        await _migrate_v2_to_v3(hass, entry)
        hass.config_entries.async_update_entry(entry, version=3)
        _LOGGER.warning("[EnelGrid Migration] Migration to version 3 complete")

    if entry.version == 3:
        # Version 3 -> 4: Fix race condition bug (clear and re-fetch)
        # Version 3 had incomplete migration that didn't actually clear statistics
        await _migrate_v3_to_v4(hass, entry)
        hass.config_entries.async_update_entry(entry, version=4)
        _LOGGER.warning("[EnelGrid Migration] Migration to version 4 complete")

    return True


async def _migrate_v1_to_v2(hass: HomeAssistant, entry: ConfigEntry):
    """Clear all statistics and mark for full historical re-fetch."""
    await _clear_statistics_and_mark_fetch(hass, entry, "v1→v2")


async def _migrate_v2_to_v3(hass: HomeAssistant, entry: ConfigEntry):
    """Clear corrupted statistics from v1.4.0/v1.4.1 and trigger fresh fetch."""
    await _clear_statistics_and_mark_fetch(hass, entry, "v2→v3")


async def _migrate_v3_to_v4(hass: HomeAssistant, entry: ConfigEntry):
    """Fix race condition bug - clear incomplete statistics and re-fetch."""
    await _clear_statistics_and_mark_fetch(hass, entry, "v3→v4")


async def _clear_statistics_and_mark_fetch(hass: HomeAssistant, entry: ConfigEntry, migration_name: str):
    """Mark for clearing statistics and full historical re-fetch.

    Note: We can't clear statistics here because migration runs in the main thread.
    The historical_fetch_task will clear statistics before fetching new data.
    """
    # Try multiple key formats for POD (handle legacy config entries)
    pod = entry.data.get(CONF_POD)
    if not pod:
        # Try legacy formats
        for key in entry.data.keys():
            if key.startswith("pod"):
                pod = entry.data[key]
                break

    if not pod:
        _LOGGER.error(f"[EnelGrid Migration {migration_name}] Cannot find POD in config entry, skipping migration")
        return

    _LOGGER.warning(f"[EnelGrid Migration {migration_name}] Will clear and re-fetch all historical data for POD {pod}")

    try:
        # Mark that we need to clear old statistics and do full historical fetch
        # The historical_fetch_task will handle the actual clearing
        new_data = dict(entry.data)
        new_data["historical_fetch_needed"] = True
        new_data["historical_fetch_completed"] = False
        new_data["clear_statistics_needed"] = True  # Flag for historical_fetch_task
        hass.config_entries.async_update_entry(entry, data=new_data)

        _LOGGER.warning(f"[EnelGrid Migration {migration_name}] Marked for statistics clear and full historical fetch")

    except Exception as e:
        _LOGGER.error(f"[EnelGrid Migration {migration_name}] Failed during migration: {e}", exc_info=True)
        raise


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up enelgrid from a config entry."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry.data

    # Forward the setup to the sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
