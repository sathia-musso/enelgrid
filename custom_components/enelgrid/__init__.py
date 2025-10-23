import logging
import json
import os
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.util.dt import as_utc

from .const import DOMAIN, CONF_POD, CONF_PRICE_PER_KWH

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entry to new format (version 1 → 2)."""
    _LOGGER.info(f"Migrating enelgrid config entry from version {entry.version}")

    if entry.version == 1:
        # This migration fixes the cumulative offset bug in historical statistics
        # by recalculating all monthly offsets to be continuous
        await _migrate_statistics_v1_to_v2(hass, entry)

        # Update config entry to version 2
        hass.config_entries.async_update_entry(entry, version=2)
        _LOGGER.info("Migration to version 2 complete")

    return True


def _get_config_value(entry_data: dict, key: str, legacy_keys: list = None):
    """Get config value handling both new and legacy key formats.

    Args:
        entry_data: The entry.data dictionary
        key: The new/standard key name
        legacy_keys: List of old key names to try if new key not found

    Returns:
        The value if found, None otherwise
    """
    # Try new key first
    if key in entry_data:
        return entry_data[key]

    # Try legacy keys
    if legacy_keys:
        for legacy_key in legacy_keys:
            # Exact match
            if legacy_key in entry_data:
                return entry_data[legacy_key]
            # Partial match for keys like "pod: IT1234567890"
            for entry_key in entry_data.keys():
                if entry_key.startswith(legacy_key):
                    return entry_data[entry_key]

    return None


async def _migrate_statistics_v1_to_v2(hass: HomeAssistant, entry: ConfigEntry):
    """Fix historical statistics by removing anomalous month-boundary jumps."""
    # Handle both new and legacy config key formats
    pod = _get_config_value(entry.data, CONF_POD, ["pod:", "pod: "])
    if not pod:
        _LOGGER.error("Cannot find POD in config entry data. Available keys: %s", list(entry.data.keys()))
        return False

    price_per_kwh = entry.data.get(CONF_PRICE_PER_KWH, 0.33)

    object_id_kw = f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_consumption"
    statistic_id_kw = f"sensor:{object_id_kw}"

    object_id_cost = f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_kw_cost"
    statistic_id_cost = f"sensor:{object_id_cost}"

    _LOGGER.info(f"Starting statistics migration for {statistic_id_kw}")

    # Get all historical statistics
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1),  # From beginning of time
        None,  # Until now
        {statistic_id_kw},
        "hour",
        None,
        {"sum"}
    )

    if statistic_id_kw not in stats or not stats[statistic_id_kw]:
        _LOGGER.warning(f"No statistics found for {statistic_id_kw}, skipping migration")
        return

    all_stats = stats[statistic_id_kw]
    _LOGGER.info(f"Found {len(all_stats)} historical statistics records")

    # Group by month and detect anomalous jumps
    stats_by_month = {}
    for stat in all_stats:
        start_timestamp = stat["start"]
        start = datetime.fromtimestamp(start_timestamp)
        month_key = (start.year, start.month)
        if month_key not in stats_by_month:
            stats_by_month[month_key] = []
        stats_by_month[month_key].append(stat)

    # Sort months chronologically
    sorted_months = sorted(stats_by_month.keys())

    # Identify and fix anomalous jumps
    corrected_stats_kw = []
    corrected_stats_cost = []

    prev_month_last_corrected = None

    for month_key in sorted_months:
        month_stats = sorted(stats_by_month[month_key], key=lambda x: x["start"])
        first_stat = month_stats[0]
        last_stat = month_stats[-1]

        # Calculate offset for THIS month only
        offset_this_month = 0

        if prev_month_last_corrected is not None:
            # Where this month SHOULD start (continuous from prev month)
            expected_start = prev_month_last_corrected
            actual_start = first_stat["sum"]
            jump = actual_start - expected_start

            # If jump > 1000 kWh, it's anomalous (bug from old code)
            if abs(jump) > 1000:
                _LOGGER.info(
                    f"Found anomalous jump at {month_key}: "
                    f"{prev_month_last_corrected:.2f} → {actual_start:.2f} "
                    f"(jump: {jump:.2f} kWh)"
                )
                # Offset to subtract from this month's records
                offset_this_month = jump

        # Apply correction to this month's statistics
        # Preserve original deltas between consecutive records
        for i, stat in enumerate(month_stats):
            if i == 0:
                # First record of the month: continue from previous month's last value
                # BUT preserve the real consumption delta from original data
                if prev_month_last_corrected is not None:
                    # Get the delta between first and second record of this month (from original data)
                    # This represents the real consumption for the first hour
                    if len(month_stats) > 1:
                        first_hour_delta = month_stats[1]["sum"] - month_stats[0]["sum"]
                    else:
                        first_hour_delta = 0
                    corrected_sum = prev_month_last_corrected + first_hour_delta
                else:
                    corrected_sum = stat["sum"]
            else:
                # Subsequent records: preserve the delta from the original data
                original_delta = stat["sum"] - month_stats[i-1]["sum"]
                corrected_sum = corrected_stats_kw[-1]["sum"] + original_delta

            start_dt = as_utc(datetime.fromtimestamp(stat["start"]))
            corrected_stats_kw.append({
                "start": start_dt,
                "sum": corrected_sum
            })
            corrected_stats_cost.append({
                "start": start_dt,
                "sum": corrected_sum * price_per_kwh
            })

        # Update for next month - use CORRECTED last value
        prev_month_last_corrected = corrected_stats_kw[-1]["sum"]

    if corrected_stats_kw:
        _LOGGER.info(
            f"Processed {len(corrected_stats_kw)} statistics records across {len(sorted_months)} months"
        )

        # Create backup of original data before applying changes
        backup_path = os.path.join(hass.config.path(), ".storage", f"enelgrid_backup_{pod.lower().replace('-', '_')}_v1.json")
        try:
            backup_data = {
                "version": 1,
                "backup_timestamp": datetime.now().isoformat(),
                "statistic_id_consumption": statistic_id_kw,
                "statistic_id_cost": statistic_id_cost,
                "pod": pod,
                "original_statistics": [
                    {
                        "start": stat["start"],
                        "sum": stat["sum"]
                    }
                    for stat in all_stats
                ]
            }

            # Write backup file asynchronously to avoid blocking event loop
            def _write_backup():
                with open(backup_path, "w") as f:
                    json.dump(backup_data, f, indent=2)

            await hass.async_add_executor_job(_write_backup)

            _LOGGER.info(f"Created backup of original statistics at: {backup_path}")
        except Exception as e:
            _LOGGER.error(f"Failed to create backup: {e}")
            # Continue anyway - backup failure shouldn't block migration
            # But log it prominently so user is aware

        # Get metadata
        metadata = get_metadata(hass, statistic_ids={statistic_id_kw, statistic_id_cost})

        if statistic_id_kw in metadata:
            # Write corrected statistics back to database
            _LOGGER.info("Writing corrected consumption statistics...")
            async_add_external_statistics(
                hass,
                metadata[statistic_id_kw][1],  # metadata dict
                corrected_stats_kw
            )

            if statistic_id_cost in metadata:
                _LOGGER.info("Writing corrected cost statistics...")
                async_add_external_statistics(
                    hass,
                    metadata[statistic_id_cost][1],
                    corrected_stats_cost
                )

            _LOGGER.info("Statistics migration completed successfully")
            _LOGGER.info(f"Backup saved at: {backup_path} (can be restored if needed)")
    else:
        _LOGGER.info("No historical data found to migrate")


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
