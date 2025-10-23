import logging
from datetime import datetime, timedelta

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.dt import as_utc
from homeassistant.components.sensor import SensorDeviceClass

from .const import (
    CONF_PASSWORD,
    CONF_POD,
    CONF_PRICE_PER_KWH,
    CONF_USER_NUMBER,
    CONF_USERNAME,
    DOMAIN,
)
from .login import EnelGridSession

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(days=1)  # Fetch once a day


def _get_config_value(entry_data: dict, key: str, legacy_keys: list = None):
    """Get config value handling both new and legacy key formats.

    Args:
        entry_data: The entry.data dictionary
        key: The new/standard key name
        legacy_keys: List of old key names to try if new key not found

    Returns:
        The value if found, raises KeyError if not found
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

    # Not found - raise KeyError with helpful message
    raise KeyError(
        f"Config key '{key}' not found. Tried legacy keys: {legacy_keys}. "
        f"Available keys: {list(entry_data.keys())}"
    )


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up enelgrid sensors from a config entry."""
    pod = _get_config_value(entry.data, CONF_POD, ["pod:", "pod: "])
    entry_id = entry.entry_id

    consumption_sensor = EnelGridConsumptionSensor(hass, entry)
    monthly_sensor = EnelGridMonthlySensor(pod)

    hass.data.setdefault("enelgrid_monthly_sensor", {})[entry_id] = monthly_sensor

    async_add_entities([consumption_sensor, monthly_sensor])  # cost_sensor

    _LOGGER.warning(
        f"enelgrid sensors added: {consumption_sensor.entity_id}, {monthly_sensor.entity_id}"
    )

    # Immediately fetch data upon install
    hass.async_create_task(consumption_sensor.async_update())

    # Set up daily update
    async def daily_update_callback(_):
        await consumption_sensor.async_update()

    async_track_time_interval(hass, daily_update_callback, SCAN_INTERVAL)


class EnelGridConsumptionSensor(SensorEntity):
    """Main sensor to fetch and import data from enelgrid."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry_id = entry.entry_id
        self._username = entry.data.get(CONF_USERNAME, entry.data.get("username"))
        self._password = entry.data.get(CONF_PASSWORD, entry.data.get("password"))
        self._pod = _get_config_value(entry.data, CONF_POD, ["pod:", "pod: "])
        self._numero_utente = _get_config_value(entry.data, CONF_USER_NUMBER, ["numero utente", "numero_utente"])
        self._price_per_kwh = entry.data.get(CONF_PRICE_PER_KWH, 0.33)
        self._attr_name = "enelgrid Daily Import"
        self._state = None
        self.session = None

    @property
    def state(self):
        return self._state

    async def async_update(self):
        try:
            self.session = EnelGridSession(
                self._username, self._password, self._pod, self._numero_utente
            )

            data = await self.session.fetch_consumption_data()
            data_points = parse_enel_hourly_data(data)

            if data_points:
                await self.save_to_home_assistant(
                    data_points, self._pod, self.entry_id, self._price_per_kwh
                )
                await self.update_monthly_sensor(data_points, self.entry_id)
                self._state = "Imported"
            else:
                _LOGGER.warning("No hourly data found.")
                self._state = "No data"
        except ConfigEntryAuthFailed as err:
            self._state = "Login error"
            async_create(
                self.hass,
                message=f"Login failed. Please check your credentials. {err}",
                title="EnelGrid Login Error",
            )

            await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "reauth", "entry_id": self.entry_id}, data={}
            )

        except Exception as err:
            _LOGGER.exception(f"Failed to update enelgrid data: {err}")
            self._state = "Error"
        finally:
            if self.session:
                await self.session.close()

    async def save_to_home_assistant(
        self, all_data_by_date, pod, entry_id, price_per_kwh
    ):

        object_id_kw = (
            f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_consumption"
        )
        statistic_id_kw = f"sensor:{object_id_kw}"

        object_id_cost = (
            f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_kw_cost"
        )
        statistic_id_cost = f"sensor:{object_id_cost}"

        metadata_kw = {
            "has_mean": False,
            "has_sum": True,
            "name": f"Enel {pod} Consumption",
            "source": "sensor",
            "statistic_id": statistic_id_kw,
            "unit_of_measurement": "kWh",
        }

        metadata_cost = {
            "has_mean": False,
            "has_sum": True,
            "name": f"Enel {pod} Cost",
            "source": "sensor",
            "statistic_id": statistic_id_cost,
            "unit_of_measurement": "EUR",
        }

        # Get last saved timestamp and cumulative value to avoid re-saving old data
        last_timestamp, cumulative_offset = await self.get_last_statistic(statistic_id_kw)

        # Filter out days that are already in the database
        # Only save new data (days after the last saved timestamp)
        new_data_by_date = {}
        for day_date, data_points in all_data_by_date.items():
            # Check if this day has any data points newer than what's in DB
            day_has_new_data = False
            if last_timestamp is None:
                # DB is empty, save everything
                day_has_new_data = True
            else:
                # Check if any point in this day is newer than last saved
                for point in data_points:
                    if point["timestamp"] > last_timestamp:
                        day_has_new_data = True
                        break

            if day_has_new_data:
                new_data_by_date[day_date] = data_points

        if not new_data_by_date:
            _LOGGER.info(f"No new data to save for {statistic_id_kw} (last saved: {last_timestamp})")
            return

        _LOGGER.info(
            f"Saving {len(new_data_by_date)} new days for {statistic_id_kw} "
            f"(last saved: {last_timestamp}, offset: {cumulative_offset} kWh)"
        )

        for day_date, data_points in new_data_by_date.items():
            stats_kw = []
            stats_cost = []

            for point in data_points:
                # Skip points that are already in the database
                if last_timestamp and point["timestamp"] <= last_timestamp:
                    continue

                # Calculate absolute cumulative value (relative to meter start)
                absolute_cumulative = point["cumulative_kwh"] + cumulative_offset

                stats_kw.append(
                    {
                        "start": as_utc(point["timestamp"]),
                        "sum": absolute_cumulative,
                    }
                )
                stats_cost.append(
                    {
                        "start": as_utc(point["timestamp"]),
                        "sum": absolute_cumulative * price_per_kwh,
                    }
                )

            if not stats_kw:
                continue

            try:
                async_add_external_statistics(self.hass, metadata_kw, stats_kw)
                async_add_external_statistics(
                    self.hass, metadata_cost, stats_cost
                )
                _LOGGER.info(
                    f"Saved {len(stats_kw)} new points for {day_date} "
                    f"({statistic_id_kw}: {len(stats_kw)}, {statistic_id_cost}: {len(stats_cost)})"
                )
            except HomeAssistantError as e:
                _LOGGER.exception(
                    f"Failed to save statistics for {statistic_id_kw}: {e}"
                )
                raise

    async def get_last_statistic(self, statistic_id: str):
        """Get the last recorded timestamp and cumulative kWh for a given statistic_id.

        Returns:
            tuple: (last_timestamp, last_cumulative_kwh)
                   - last_timestamp: datetime of last saved record, or None if DB is empty
                   - last_cumulative_kwh: last cumulative value, or 0.0 if DB is empty
        """
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )

        if last_stats and statistic_id in last_stats:
            last_record = last_stats[statistic_id][0]
            last_timestamp = datetime.fromtimestamp(last_record["start"])
            last_sum = last_record["sum"]
            _LOGGER.info(
                f"Last recorded for {statistic_id}: {last_timestamp} → {last_sum} kWh"
            )
            return (last_timestamp, last_sum)

        _LOGGER.info(f"No previous data found for {statistic_id}, starting fresh")
        return (None, 0.0)

    async def get_last_cumulative_kwh(self, statistic_id: str):
        """Get the last recorded cumulative kWh for a given statistic_id.

        This method is kept for backwards compatibility but now uses get_last_statistic.
        """
        _, cumulative = await self.get_last_statistic(statistic_id)
        return cumulative

    async def update_monthly_sensor(self, all_data_by_date, entry_id):
        monthly_sensor = self.hass.data.get("enelgrid_monthly_sensor", {}).get(entry_id)

        if not monthly_sensor:
            _LOGGER.error(f"Monthly sensor is not available for entry {entry_id}!")
            return

        total_kwh = sum(
            points[-1]["cumulative_kwh"] for points in all_data_by_date.values()
        )

        monthly_sensor.set_total(total_kwh)
        _LOGGER.info(f"Updated monthly sensor to {total_kwh} kWh")


class EnelGridMonthlySensor(SensorEntity):
    """Monthly cumulative total sensor."""

    def __init__(self, pod):
        object_id = f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_monthly_consumption"
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = f"Enel {pod} Monthly Consumption"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = "total_increasing"
        self._attr_native_unit_of_measurement = "kWh"
        self._state = 0
        self._attr_extra_state_attributes = {"source": "enelgrid"}

    @property
    def state(self):
        return self._state

    def set_total(self, new_total):
        self._state = new_total
        self.async_write_ha_state()


def parse_enel_hourly_data(data):
    """Extract all hourly data into per-day structure, preserving cross-day cumulative values."""
    aggregations = (
        data.get("data", {}).get("aggregationResult", {}).get("aggregations", [])
    )

    hourly_aggregation = next(
        (agg for agg in aggregations if agg.get("referenceID") == "hourlyConsumption"),
        None,
    )

    if not hourly_aggregation:
        raise ValueError("No hourly consumption data found in JSON")

    all_data_by_date = {}
    cumulative_offset = 0

    sorted_results = sorted(
        hourly_aggregation.get("results", []),
        key=lambda r: datetime.strptime(r["date"], "%d%m%Y"),
    )

    for day_result in sorted_results:
        date_str = day_result.get("date")
        day_date = datetime.strptime(date_str, "%d%m%Y").date()

        hourly_points = []
        running_total = cumulative_offset

        for hour_entry in day_result.get("binValues", []):
            hour_number = int(hour_entry["name"][1:])
            hour_time = datetime.combine(day_date, datetime.min.time()) + timedelta(
                hours=hour_number - 1
            )

            running_total += hour_entry["value"]
            hourly_points.append(
                {
                    "timestamp": hour_time,
                    "kwh": hour_entry["value"],
                    "cumulative_kwh": running_total,
                }
            )

        all_data_by_date[day_date] = hourly_points

        if hourly_points:
            cumulative_offset = hourly_points[-1]["cumulative_kwh"]

    return all_data_by_date
