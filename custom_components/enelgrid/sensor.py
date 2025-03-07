import logging
from datetime import timedelta, datetime

from homeassistant.components.recorder.statistics import async_add_external_statistics, get_last_statistics
from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.dt import as_utc
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_POD, CONF_USER_NUMBER, CONF_USERNAME, CONF_PASSWORD
from .login import EnelGridSession

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(days=1)  # Fetch once a day


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up EnelGrid sensors from a config entry."""
    pod = entry.data[CONF_POD]
    entry_id = entry.entry_id

    consumption_sensor = EnelGridConsumptionSensor(hass, entry)
    monthly_sensor = EnelGridMonthlySensor(pod)

    hass.data.setdefault("enelgrid_monthly_sensor", {})[entry_id] = monthly_sensor

    async_add_entities([consumption_sensor, monthly_sensor])

    _LOGGER.warning(f"EnelGrid sensors added: {consumption_sensor.entity_id}, {monthly_sensor.entity_id}")

    # Immediately fetch data upon install
    hass.async_create_task(consumption_sensor.async_update())

    # Set up daily update
    async def daily_update_callback(_):
        await consumption_sensor.async_update()

    async_track_time_interval(hass, daily_update_callback, SCAN_INTERVAL)


class EnelGridConsumptionSensor(SensorEntity):
    """Main sensor to fetch and import data from EnelGrid."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry_id = entry.entry_id
        self._username = entry.data[CONF_USERNAME]
        self._password = entry.data[CONF_PASSWORD]
        self._pod = entry.data[CONF_POD]
        self._numero_utente = entry.data[CONF_USER_NUMBER]
        self._attr_name = "EnelGrid Daily Import"
        self._state = None
        self.session = None

    @property
    def state(self):
        return self._state

    async def async_update(self):
        try:
            self.session = EnelGridSession(self._username, self._password, self._pod, self._numero_utente)

            data = await self.session.fetch_consumption_data()
            data_points = parse_enel_hourly_data(data)

            if data_points:
                await self.save_to_home_assistant(data_points, self._pod, self.entry_id)
                await self.update_monthly_sensor(data_points, self.entry_id)
                self._state = "Imported"
            else:
                _LOGGER.warning("No hourly data found.")
                self._state = "No data"
        except Exception as err:
            _LOGGER.exception(f"Failed to update EnelGrid data: {err}")
            self._state = "Error"
        finally:
            if self.session:
                await self.session.close()

    async def save_to_home_assistant(self, all_data_by_date, pod, entry_id):
        object_id = f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_consumption"
        statistic_id = f"sensor:{object_id}"

        metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": f"EnelGrid {pod} Consumption",
            "source": "sensor",
            "statistic_id": statistic_id,
            "unit_of_measurement": "kWh",
        }

        for day_date, data_points in all_data_by_date.items():
            stats = []

            cumulative_offset = await self.hass.async_add_executor_job(
                self.get_last_cumulative_kwh, statistic_id
            )

            for point in data_points:
                stats.append({
                    "start": as_utc(point["timestamp"]),
                    "sum": point["cumulative_kwh"] + cumulative_offset
                })

            try:
                async_add_external_statistics(self.hass, metadata, stats)
                _LOGGER.info(f"Saved {len(stats)} points for {statistic_id} on {day_date}")
            except HomeAssistantError as e:
                _LOGGER.exception(f"Failed to save statistics for {statistic_id}: {e}")
                raise

    def get_last_cumulative_kwh(self, statistic_id):
        """Get the last recorded cumulative kWh for a given statistic_id."""
        last_stats = get_last_statistics(self.hass, 1, statistic_id, True, {"sum"})
        if last_stats and statistic_id in last_stats:
            return last_stats[statistic_id][0]["sum"]  # Last recorded cumulative sum
        return 0.0

    async def update_monthly_sensor(self, all_data_by_date, entry_id):
        monthly_sensor = self.hass.data.get("enelgrid_monthly_sensor", {}).get(entry_id)

        if not monthly_sensor:
            _LOGGER.error(f"Monthly sensor is not available for entry {entry_id}!")
            return

        total_kwh = sum(points[-1]["cumulative_kwh"] for points in all_data_by_date.values())

        monthly_sensor.set_total(total_kwh)
        _LOGGER.info(f"Updated monthly sensor to {total_kwh} kWh")


class EnelGridMonthlySensor(SensorEntity):
    """Monthly cumulative total sensor."""

    def __init__(self, pod):
        object_id = f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_monthly_consumption"
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = f"EnelGrid {pod} Monthly Consumption"
        self._attr_device_class = "energy"
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
    aggregations = data.get("data", {}).get("aggregationResult", {}).get("aggregations", [])

    hourly_aggregation = next(
        (agg for agg in aggregations if agg.get("referenceID") == "hourlyConsumption"),
        None
    )

    if not hourly_aggregation:
        raise ValueError("No hourly consumption data found in JSON")

    all_data_by_date = {}
    cumulative_offset = 0

    sorted_results = sorted(
        hourly_aggregation.get("results", []),
        key=lambda r: datetime.strptime(r["date"], "%d%m%Y")
    )

    for day_result in sorted_results:
        date_str = day_result.get("date")
        day_date = datetime.strptime(date_str, "%d%m%Y").date()

        hourly_points = []
        running_total = cumulative_offset

        for hour_entry in day_result.get("binValues", []):
            hour_number = int(hour_entry["name"][1:])
            hour_time = datetime.combine(day_date, datetime.min.time()) + timedelta(hours=hour_number - 1)

            running_total += hour_entry["value"]
            hourly_points.append({
                "timestamp": hour_time,
                "kwh": hour_entry["value"],
                "cumulative_kwh": running_total
            })

        all_data_by_date[day_date] = hourly_points

        if hourly_points:
            cumulative_offset = hourly_points[-1]["cumulative_kwh"]

    return all_data_by_date
