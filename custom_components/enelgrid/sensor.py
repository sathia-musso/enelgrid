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


async def historical_fetch_task(hass, entry, sensor):
    """Background task to fetch all historical data month by month.

    Starts from current month and goes backwards until API returns no data.
    """
    _LOGGER.warning("[EnelGrid Historical] Starting full historical fetch...")

    pod = entry.data[CONF_POD]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    numero_utente = entry.data[CONF_USER_NUMBER]
    price_per_kwh = entry.data[CONF_PRICE_PER_KWH]

    # Check if we need to clear old statistics first (from migration)
    if entry.data.get("clear_statistics_needed", False):
        _LOGGER.warning("[EnelGrid Historical] Clearing old statistics as requested by migration...")

        pod_normalized = pod.lower().replace('-', '_').replace('.', '_')
        statistic_id_consumption = f"sensor:enelgrid_{pod_normalized}_consumption"
        statistic_id_cost = f"sensor:enelgrid_{pod_normalized}_kw_cost"

        try:
            from homeassistant.components.recorder.statistics import clear_statistics
            recorder_instance = get_instance(hass)

            # Clear consumption statistics
            await recorder_instance.async_add_executor_job(
                clear_statistics,
                recorder_instance,
                [statistic_id_consumption]
            )
            _LOGGER.warning(f"[EnelGrid Historical] Cleared consumption statistics: {statistic_id_consumption}")

            # Clear cost statistics
            await recorder_instance.async_add_executor_job(
                clear_statistics,
                recorder_instance,
                [statistic_id_cost]
            )
            _LOGGER.warning(f"[EnelGrid Historical] Cleared cost statistics: {statistic_id_cost}")

            # Clear the flag
            new_data = dict(entry.data)
            new_data["clear_statistics_needed"] = False
            hass.config_entries.async_update_entry(entry, data=new_data)

        except Exception as e:
            _LOGGER.error(f"[EnelGrid Historical] Failed to clear statistics: {e}", exc_info=True)
            # Continue anyway - better to have duplicate data than no data

    current_date = datetime.now()
    months_fetched = 0
    total_days = 0

    # Collect all months data first (from newest to oldest)
    all_months_data = []

    while True:
        try:
            # Calculate month range
            first_day = current_date.replace(day=1)

            # Last day of month
            if current_date.month == 12:
                last_day_of_month = current_date.replace(day=31)
            else:
                last_day_of_month = (current_date.replace(month=current_date.month + 1, day=1) - timedelta(days=1))

            # For current month, use today instead of last day of month
            # (Enel API returns future data which is wrong)
            today = datetime.now()
            if current_date.year == today.year and current_date.month == today.month:
                last_day = today
            else:
                last_day = last_day_of_month

            validity_from = first_day.strftime("%d%m%Y")
            validity_to = last_day.strftime("%d%m%Y")

            _LOGGER.warning(
                f"[EnelGrid Historical] Fetching month {current_date.strftime('%Y-%m')} "
                f"({validity_from} → {validity_to})..."
            )

            # Fetch data for this month
            session = EnelGridSession(username, password, pod, numero_utente)
            data = await session.fetch_consumption_data(validity_from, validity_to)
            data_points = parse_enel_hourly_data(data)

            # Save parsed data_points for debugging
            try:
                import os
                import json
                debug_dir = "/config/enelgrid_debug"
                os.makedirs(debug_dir, exist_ok=True)
                filename = f"parsed_{validity_from}_{validity_to}.json"
                # Convert data_points to JSON-serializable format
                serializable = {}
                for date_key, points in data_points.items():
                    serializable[str(date_key)] = [
                        {
                            "timestamp": p["timestamp"].isoformat(),
                            "kwh": p["kwh"],
                            "cumulative_kwh": p["cumulative_kwh"]
                        }
                        for p in points
                    ]
                with open(os.path.join(debug_dir, filename), "w") as f:
                    json.dump(serializable, f, indent=2)
                _LOGGER.warning(f"[EnelGrid Debug] Saved parsed data to {filename}")
            except Exception as e:
                _LOGGER.warning(f"[EnelGrid Debug] Failed to save parsed data: {e}")

            if not data_points or len(data_points) == 0:
                _LOGGER.warning(
                    f"[EnelGrid Historical] No data returned for {current_date.strftime('%Y-%m')}, "
                    "reached the limit of available historical data"
                )
                break

            # Store this month's data
            all_months_data.append({
                "month": current_date.strftime("%Y-%m"),
                "data_points": data_points
            })

            months_fetched += 1
            total_days += len(data_points)

            _LOGGER.warning(
                f"[EnelGrid Historical] Month {current_date.strftime('%Y-%m')}: "
                f"{len(data_points)} days fetched"
            )

            # Move to previous month
            current_date = (current_date.replace(day=1) - timedelta(days=1))

        except Exception as e:
            _LOGGER.error(
                f"[EnelGrid Historical] Error fetching month {current_date.strftime('%Y-%m')}: {e}",
                exc_info=True
            )
            _LOGGER.warning("[EnelGrid Historical] Stopping fetch due to error")
            break

    # Now save all data in chronological order (oldest first)
    _LOGGER.warning(
        f"[EnelGrid Historical] Finished fetching. Got {months_fetched} months, "
        f"{total_days} days total. Now saving in chronological order..."
    )

    all_months_data.reverse()  # Oldest first

    # Read offset ONCE at the beginning to avoid race conditions
    # (async_add_external_statistics doesn't immediately update DB)
    object_id_kw = f"enelgrid_{pod.lower().replace('-', '_').replace('.', '_')}_consumption"
    statistic_id_kw = f"sensor:{object_id_kw}"
    cumulative_offset = await sensor.get_last_cumulative_kwh(statistic_id_kw)
    _LOGGER.warning(f"[EnelGrid Historical] Starting with cumulative offset: {cumulative_offset:.2f} kWh")

    for month_data in all_months_data:
        try:
            _LOGGER.warning(
                f"[EnelGrid Historical] Saving month {month_data['month']} "
                f"({len(month_data['data_points'])} days) with offset {cumulative_offset:.2f} kWh..."
            )
            # Pass offset and get updated offset after saving
            cumulative_offset = await sensor.save_to_home_assistant(
                month_data["data_points"],
                pod,
                entry.entry_id,
                price_per_kwh,
                cumulative_offset=cumulative_offset
            )
            _LOGGER.warning(
                f"[EnelGrid Historical] Month {month_data['month']} saved. "
                f"New offset: {cumulative_offset:.2f} kWh"
            )
        except Exception as e:
            _LOGGER.error(
                f"[EnelGrid Historical] Error saving month {month_data['month']}: {e}",
                exc_info=True
            )

    # Mark as completed
    new_data = dict(entry.data)
    new_data["historical_fetch_needed"] = False
    new_data["historical_fetch_completed"] = True
    hass.config_entries.async_update_entry(entry, data=new_data)

    _LOGGER.warning(
        f"[EnelGrid Historical] ✅ Historical fetch completed! "
        f"Total: {months_fetched} months, {total_days} days"
    )


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up enelgrid sensors from a config entry."""
    pod = entry.data[CONF_POD]
    entry_id = entry.entry_id

    consumption_sensor = EnelGridConsumptionSensor(hass, entry)
    monthly_sensor = EnelGridMonthlySensor(pod)

    hass.data.setdefault("enelgrid_monthly_sensor", {})[entry_id] = monthly_sensor

    async_add_entities([consumption_sensor, monthly_sensor])  # cost_sensor

    _LOGGER.warning(
        f"enelgrid sensors added: {consumption_sensor.entity_id}, {monthly_sensor.entity_id}"
    )

    # Check if we need full historical fetch (after migration)
    if entry.data.get("historical_fetch_needed", False):
        _LOGGER.warning("[EnelGrid] Historical fetch needed, starting background task...")
        hass.async_create_task(
            historical_fetch_task(hass, entry, consumption_sensor)
        )
    else:
        # Normal behavior: immediately fetch current data
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
        self._username = entry.data[CONF_USERNAME]
        self._password = entry.data[CONF_PASSWORD]
        self._pod = entry.data[CONF_POD]
        self._numero_utente = entry.data[CONF_USER_NUMBER]
        self._price_per_kwh = entry.data[CONF_PRICE_PER_KWH]
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
        self, all_data_by_date, pod, entry_id, price_per_kwh, cumulative_offset=None
    ):
        """Save data to Home Assistant statistics.

        Args:
            all_data_by_date: Dict of date -> list of data points
            pod: POD identifier
            entry_id: Config entry ID
            price_per_kwh: Price per kWh for cost calculation
            cumulative_offset: Optional pre-calculated offset. If None, will read from DB.
                              This is used by historical_fetch_task to avoid race conditions.

        Returns:
            The final cumulative value after saving (for updating offset in historical fetch)
        """

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

        # Get cumulative offset: either provided (historical fetch) or read from DB (daily update)
        if cumulative_offset is None:
            cumulative_offset = await self.get_last_cumulative_kwh(statistic_id_kw)
            _LOGGER.info(f"Read cumulative offset from DB: {cumulative_offset:.2f} kWh")

        final_cumulative = cumulative_offset  # Track final value for return

        for day_date, data_points in all_data_by_date.items():
            stats_kw = []
            stats_cost = []

            for point in data_points:
                final_value = point["cumulative_kwh"] + cumulative_offset
                stats_kw.append(
                    {
                        "start": as_utc(point["timestamp"]),
                        "sum": final_value,
                    }
                )
                stats_cost.append(
                    {
                        "start": as_utc(point["timestamp"]),
                        "sum": final_value * price_per_kwh,  # Use final_value, not point["cumulative_kwh"]!
                    }
                )
                final_cumulative = final_value  # Update with each point

            try:
                async_add_external_statistics(self.hass, metadata_kw, stats_kw)
                async_add_external_statistics(
                    self.hass, metadata_cost, stats_cost
                )
                _LOGGER.info(
                    f"Saved {len(stats_kw)} points for {statistic_id_kw} and {len(stats_cost)} for {statistic_id_cost}"
                )
            except HomeAssistantError as e:
                _LOGGER.exception(
                    f"Failed to save statistics for {statistic_id_kw}: {e}"
                )
                raise

        return final_cumulative

    async def get_last_cumulative_kwh(self, statistic_id: str):
        """Get the last recorded cumulative kWh for a given statistic_id."""
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )

        if last_stats and statistic_id in last_stats:
            _LOGGER.info(
                f"Last recorded cumulative sum for {statistic_id}: {last_stats[statistic_id][0]['sum']}"
            )
            return last_stats[statistic_id][0]["sum"]  # Last recorded cumulative sum
        return 0.0

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
