import logging
from datetime import datetime, timedelta

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import as_utc
from homeassistant.util.unit_conversion import EnergyConverter
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

            _LOGGER.debug(
                "[EnelGrid Historical] %s: %d days parsed",
                current_date.strftime("%Y-%m"),
                len(data_points),
            )

            if not data_points or len(data_points) == 0:
                today = datetime.now()
                if (current_date.year, current_date.month) == (today.year, today.month):
                    # Enel publishes with a delay of a few days: in the first
                    # days of a month the current month is still empty. That
                    # is not the end of the history, so go on to the
                    # previous month instead of stopping.
                    _LOGGER.warning(
                        f"[EnelGrid Historical] No data yet for the current month "
                        f"{current_date.strftime('%Y-%m')}, continuing with the previous one"
                    )
                    current_date = current_date.replace(day=1) - timedelta(days=1)
                    continue
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

    if not all_months_data:
        # Nothing came back (Enel down, login changed...): leave the stored
        # statistics and the flags alone, so the resync runs again at the next
        # setup instead of being marked done with an empty history.
        _LOGGER.warning(
            "[EnelGrid Historical] No data fetched, statistics left untouched; "
            "the resync will be retried at the next setup"
        )
        return

    all_months_data.reverse()  # Oldest first

    pod_normalized = pod.lower().replace('-', '_').replace('.', '_')
    statistic_id_kw = f"sensor:enelgrid_{pod_normalized}_consumption"
    statistic_id_cost = f"sensor:enelgrid_{pod_normalized}_kw_cost"
    oldest = first_timestamp(all_months_data[0]["data_points"])

    if entry.data.get("clear_statistics_needed", False):
        # Clear only the hours we are about to re-import, and only now that
        # the data is in hand: anything older than what Enel still serves is
        # kept, and a failed fetch never wipes the history.
        try:
            await clear_statistics_from(
                hass, [statistic_id_kw, statistic_id_cost], oldest
            )
            _LOGGER.warning(
                "[EnelGrid Historical] Cleared statistics from %s", oldest
            )
            new_data = dict(entry.data)
            new_data["clear_statistics_needed"] = False
            hass.config_entries.async_update_entry(entry, data=new_data)
        except Exception as e:
            # The import below is idempotent, so it still fixes every hour it
            # rewrites; only stray rows inside the range would remain.
            _LOGGER.error(f"[EnelGrid Historical] Failed to clear statistics: {e}", exc_info=True)

    # Read the base ONCE and chain the months in memory: the statistics we
    # queue are not in the database yet when the next month is saved. The
    # base is the sum stored just before the oldest hour we are about to
    # write, NOT the latest sum: a resync rewrites hours that are already
    # stored, and using the latest sum as base would count them twice (#20).
    cumulative_offset = await sensor.get_sum_before(statistic_id_kw, oldest)
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

    # The daily update sets the monthly total too; after a resync nothing
    # else would until the next day.
    await sensor.update_monthly_sensor(all_months_data[-1]["data_points"], entry.entry_id)

    # Mark as completed (only reached when at least one month was fetched)
    new_data = dict(entry.data)
    new_data["historical_fetch_needed"] = False
    new_data["historical_fetch_completed"] = True
    hass.config_entries.async_update_entry(entry, data=new_data)

    _LOGGER.warning(
        f"[EnelGrid Historical] ✅ Historical fetch completed! "
        f"Total: {months_fetched} months, {total_days} days"
    )
    sensor.set_import_state("Imported")


async def clear_statistics_from(hass, statistic_ids, start):
    """Clear the given statistics from ``start`` on, in the recorder thread.

    clear_statistics must not be called from an executor job (the recorder
    rejects it as an unsafe call). Queue it and wait for the recorder, so the
    following reads see the cleared table.
    """
    recorder_instance = get_instance(hass)
    recorder_instance.async_clear_statistics(
        statistic_ids,
        start_time=as_utc(start),
        end_time=dt_util.utcnow() + timedelta(days=1),
    )
    await recorder_instance.async_block_till_done()


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up enelgrid sensors from a config entry."""
    pod = entry.data[CONF_POD]
    entry_id = entry.entry_id

    consumption_sensor = EnelGridConsumptionSensor(hass, entry)
    monthly_sensor = EnelGridMonthlySensor(pod)

    hass.data.setdefault("enelgrid_monthly_sensor", {})[entry_id] = monthly_sensor

    # Monthly sensor first: the consumption sensor starts fetching as soon as
    # it is added, and the fetch writes the monthly total.
    async_add_entities([monthly_sensor, consumption_sensor])

    _LOGGER.warning(
        f"enelgrid sensors added: {consumption_sensor.entity_id}, {monthly_sensor.entity_id}"
    )

    # The consumption sensor starts the first fetch and the daily timer from
    # async_added_to_hass, so they belong to the entity and stop with it.


class EnelGridConsumptionSensor(SensorEntity):
    """Main sensor to fetch and import data from enelgrid."""

    def __init__(self, hass, entry):
        self.hass = hass
        self._entry = entry
        self.entry_id = entry.entry_id
        self._username = entry.data[CONF_USERNAME]
        self._password = entry.data[CONF_PASSWORD]
        self._pod = entry.data[CONF_POD]
        self._numero_utente = entry.data[CONF_USER_NUMBER]
        self._price_per_kwh = entry.data[CONF_PRICE_PER_KWH]
        self._attr_name = "enelgrid Daily Import"
        # Updates are driven by the daily timer set up in async_added_to_hass;
        # entity polling would log in and import a second time.
        self._attr_should_poll = False
        self._state = None
        self.session = None

    @property
    def state(self):
        return self._state

    async def async_added_to_hass(self):
        """Start the first fetch and the daily update once the entity exists."""
        if self._entry.data.get("historical_fetch_needed", False):
            _LOGGER.warning("[EnelGrid] Historical fetch needed, starting background task...")
            first = historical_fetch_task(self.hass, self._entry, self)
        else:
            first = self.async_update()
        # Entry background tasks are cancelled when the entry is unloaded.
        self._entry.async_create_background_task(self.hass, first, "enelgrid first fetch")

        async def daily_update(_now):
            await self.async_update()

        self.async_on_remove(
            async_track_time_interval(self.hass, daily_update, SCAN_INTERVAL)
        )

    @callback
    def set_import_state(self, state):
        """Set the import status and write it, if the entity is added."""
        self._state = state
        if self.hass is not None and self.entity_id and self.platform is not None:
            self.async_write_ha_state()

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
            # should_poll is False, so nothing else writes the state.
            self.set_import_state(self._state)

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
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": f"Enel {pod} Consumption",
            "source": "sensor",
            "statistic_id": statistic_id_kw,
            "unit_class": EnergyConverter.UNIT_CLASS,
            "unit_of_measurement": "kWh",
        }

        metadata_cost = {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": f"Enel {pod} Cost",
            "source": "sensor",
            "statistic_id": statistic_id_cost,
            "unit_class": None,
            "unit_of_measurement": "EUR",
        }

        # Get cumulative offset: either provided (historical fetch) or read from DB.
        # The API restarts its cumulative value from 0 at the start of the
        # requested window, and the window overlaps hours that are already
        # stored (it always starts on the 1st of the previous month). The base
        # must therefore be the sum stored just before the first hour of the
        # window; the latest stored sum would count the overlap twice.
        if cumulative_offset is None:
            first = first_timestamp(all_data_by_date)
            if first is None:
                return 0.0
            cumulative_offset = await self.get_sum_before(statistic_id_kw, first)
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

    async def get_sum_before(self, statistic_id: str, before: datetime) -> float:
        """Return the sum of the last statistic that starts before ``before``.

        Returns 0.0 when nothing is stored before that hour (first import).
        """
        end = as_utc(before)
        recorder = get_instance(self.hass)
        # Look back a little first, then further: there can be a gap of any
        # length between two imports (integration disabled, Enel outage).
        for lookback in (timedelta(days=2), timedelta(days=62), timedelta(days=3660)):
            stats = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                end - lookback,
                end,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            rows = stats.get(statistic_id)
            if rows:
                return rows[-1]["sum"] or 0.0
        return 0.0

    async def update_monthly_sensor(self, all_data_by_date, entry_id):
        monthly_sensor = self.hass.data.get("enelgrid_monthly_sensor", {}).get(entry_id)

        if not monthly_sensor:
            _LOGGER.error(f"Monthly sensor is not available for entry {entry_id}!")
            return

        total_kwh, month, last_day = monthly_total(all_data_by_date)
        if month is None:
            return

        monthly_sensor.set_total(total_kwh, month, last_day)
        _LOGGER.info(f"Updated monthly sensor to {total_kwh} kWh ({month})")


def monthly_total(all_data_by_date):
    """Return (kWh, "YYYY-MM", last day) for the month of the latest data.

    ``cumulative_kwh`` grows across the whole fetched window (it does not
    restart every day), so the old sum of the daily end values counted the
    earlier days again and again (#11). The month total is the sum of the
    hourly values of that month. Enel publishes data with a delay of a few
    days, so the month is the one of the latest day returned, not today.
    """
    days = [d for d, points in all_data_by_date.items() if points]
    if not days:
        return 0.0, None, None
    last_day = max(days)
    total = sum(
        point["kwh"]
        for day, points in all_data_by_date.items()
        if (day.year, day.month) == (last_day.year, last_day.month)
        for point in points
    )
    return round(total, 3), last_day.strftime("%Y-%m"), last_day


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

    def set_total(self, new_total, month=None, last_day=None):
        self._state = new_total
        self._attr_extra_state_attributes = {
            "source": "enelgrid",
            "month": month,
            "last_day": last_day.isoformat() if last_day else None,
        }
        self.async_write_ha_state()


def first_timestamp(all_data_by_date):
    """Return the earliest hour in a parsed day -> points mapping, or None."""
    return min(
        (p["timestamp"] for points in all_data_by_date.values() for p in points),
        default=None,
    )


def parse_enel_hourly_data(data):
    """Extract all hourly data into per-day structure, preserving cross-day cumulative values."""
    if data.get("data") is None and data.get("status"):
        # Enel answers {"data": null, "status": {"result": "AUT02", ...}} when it
        # rejects the request (e.g. a user number that does not match the POD).
        status = data["status"]
        raise HomeAssistantError(
            "Enel rejected the consumption request: "
            f"{status.get('description', '?')} ({status.get('result', '?')}). "
            "Check the POD and the user number."
        )
    aggregations = (
        data.get("data", {}).get("aggregationResult", {}).get("aggregations", [])
    )

    hourly_aggregation = next(
        (agg for agg in aggregations if agg.get("referenceID") == "hourlyConsumption"),
        None,
    )

    if not hourly_aggregation:
        # A response without the hourly aggregation carries no data for the
        # period: import nothing instead of failing the update. Log the keys
        # so that a change of the response format is not mistaken for this.
        _LOGGER.warning(
            "No hourly consumption data in the Enel response (keys: %s, aggregations: %s)",
            sorted(data) if isinstance(data, dict) else type(data).__name__,
            [agg.get("referenceID") for agg in aggregations],
        )
        return {}

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
