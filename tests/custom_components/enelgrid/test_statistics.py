"""Tests for how imported consumption ends up in the long-term statistics.

Enel returns, at every update, the whole window from the first day of the
previous month to today, with a cumulative value that restarts from 0 at the
start of the window. The stored ``sum`` must nevertheless be a single counter
that never jumps: importing the same window twice, or a window that overlaps
what is already stored, must not change the values that are already right.
"""

from datetime import date, datetime, timedelta

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )
except ImportError:  # the package was renamed to pytest-homeassistant
    from pytest_homeassistant.common import MockConfigEntry
    from pytest_homeassistant.components.recorder.common import (
        async_wait_recording_done,
    )

from custom_components.enelgrid.const import (
    CONF_PASSWORD,
    CONF_POD,
    CONF_PRICE_PER_KWH,
    CONF_USER_NUMBER,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.enelgrid.sensor import (
    EnelGridConsumptionSensor,
    parse_enel_hourly_data,
)

POD = "IT001E00000000"
STAT_KWH = "sensor:enelgrid_it001e00000000_consumption"
STAT_COST = "sensor:enelgrid_it001e00000000_kw_cost"
PRICE = 0.2


@pytest.fixture(autouse=True)
async def rome_time_zone(hass: HomeAssistant):
    """Enel data are in Italian local time (the windows include 25 October)."""
    await hass.config.async_set_time_zone("Europe/Rome")


def hourly(day: date) -> list[float]:
    """Deterministic, day-dependent hourly values (kWh)."""
    return [round(0.1 + ((day.day * 7 + h) % 10) / 100, 3) for h in range(24)]


def payload(first: date, last: date) -> dict:
    """Build an aggregateConsumption response for the days first..last."""
    results = []
    day = first
    while day <= last:
        results.append(
            {
                "date": day.strftime("%d%m%Y"),
                "binValues": [
                    {"name": f"H{h + 1}", "value": v}
                    for h, v in enumerate(hourly(day))
                ],
            }
        )
        day += timedelta(days=1)
    return {
        "data": {
            "aggregationResult": {
                "aggregations": [
                    {"referenceID": "hourlyConsumption", "results": results}
                ]
            }
        }
    }


def expected_sums(first: date, last: date) -> list[float]:
    """Running total of every hour from first..last, starting from 0."""
    total, sums = 0.0, []
    day = first
    while day <= last:
        for v in hourly(day):
            total += v
            sums.append(total)
        day += timedelta(days=1)
    return sums


async def stored_sums(hass: HomeAssistant, first: date, last: date, stat=STAT_KWH):
    await async_wait_recording_done(hass)
    start = dt_util.as_utc(datetime.combine(first, datetime.min.time()))
    end = dt_util.as_utc(datetime.combine(last + timedelta(days=1), datetime.min.time()))
    stats = await hass.async_add_executor_job(
        statistics_during_period, hass, start, end, {stat}, "hour", None, {"sum"}
    )
    return [row["sum"] for row in stats.get(stat, [])]


async def import_window(hass, sensor, first: date, last: date):
    await sensor.save_to_home_assistant(
        parse_enel_hourly_data(payload(first, last)), POD, "entry", PRICE
    )
    await async_wait_recording_done(hass)


@pytest.fixture
def sensor(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "x",
            CONF_POD: POD,
            CONF_USER_NUMBER: 1,
            CONF_PRICE_PER_KWH: PRICE,
        },
    )
    entry.add_to_hass(hass)
    return EnelGridConsumptionSensor(hass, entry)


async def test_first_import_starts_from_zero(hass, sensor):
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 10, 3))
    assert await stored_sums(hass, date(2026, 10, 1), date(2026, 10, 3)) == pytest.approx(
        expected_sums(date(2026, 10, 1), date(2026, 10, 3))
    )


async def test_same_window_twice_is_idempotent(hass, sensor):
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 10, 3))
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 10, 3))
    assert await stored_sums(hass, date(2026, 10, 1), date(2026, 10, 3)) == pytest.approx(
        expected_sums(date(2026, 10, 1), date(2026, 10, 3))
    )


async def test_overlapping_daily_windows_keep_one_counter(hass, sensor):
    """The daily update re-imports from the 1st of the previous month."""
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 11, 5))
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 11, 6))
    # On December 1st the window moves forward and starts on November 1st.
    await import_window(hass, sensor, date(2026, 11, 1), date(2026, 11, 8))
    assert await stored_sums(hass, date(2026, 10, 1), date(2026, 11, 8)) == pytest.approx(
        expected_sums(date(2026, 10, 1), date(2026, 11, 8))
    )


async def test_cost_follows_consumption(hass, sensor):
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 10, 2))
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 10, 3))
    assert await stored_sums(
        hass, date(2026, 10, 1), date(2026, 10, 3), STAT_COST
    ) == pytest.approx([s * PRICE for s in expected_sums(date(2026, 10, 1), date(2026, 10, 3))])


async def test_gap_continues_from_last_stored_value(hass, sensor):
    """A window that starts after a gap continues from the last stored sum."""
    await import_window(hass, sensor, date(2026, 9, 1), date(2026, 9, 3))
    await import_window(hass, sensor, date(2026, 11, 1), date(2026, 11, 2))
    before = expected_sums(date(2026, 9, 1), date(2026, 9, 3))[-1]
    assert await stored_sums(hass, date(2026, 11, 1), date(2026, 11, 2)) == pytest.approx(
        [before + s for s in expected_sums(date(2026, 11, 1), date(2026, 11, 2))]
    )


async def test_backfill_older_month_does_not_double_count(hass, sensor):
    """A resync of an older window must not use a later sum as its base (#20)."""
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 11, 5))
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 10, 31))
    assert await stored_sums(hass, date(2026, 10, 1), date(2026, 11, 5)) == pytest.approx(
        expected_sums(date(2026, 10, 1), date(2026, 11, 5))
    )


def empty_payload() -> dict:
    return {
        "data": {
            "aggregationResult": {
                "aggregations": [{"referenceID": "hourlyConsumption", "results": []}]
            }
        }
    }


def mark_for_resync(hass):
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "clear_statistics_needed": True, "historical_fetch_needed": True},
    )
    return entry


async def run_resync(hass, entry, sensor, *responses):
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.enelgrid.sensor import historical_fetch_task

    session = MagicMock()
    session.fetch_consumption_data = AsyncMock(side_effect=list(responses))
    with patch("custom_components.enelgrid.sensor.EnelGridSession", return_value=session):
        await historical_fetch_task(hass, entry, sensor)
    await async_wait_recording_done(hass)


async def test_resync_clears_the_refetched_range_and_keeps_older_history(hass, sensor):
    """The resync clears only the range it re-imports, after fetching (#20).

    September is not served by Enel any more and must survive. October was
    stored with a jump (the #20 corruption) and has stray rows on days the
    resync no longer returns: those must go, which only the clear does, since
    re-importing the same hours is idempotent on its own.
    """
    await import_window(hass, sensor, date(2026, 9, 1), date(2026, 9, 3))
    september_end = expected_sums(date(2026, 9, 1), date(2026, 9, 3))[-1]
    await sensor.save_to_home_assistant(
        parse_enel_hourly_data(payload(date(2026, 10, 1), date(2026, 10, 5))),
        POD, "entry", PRICE, cumulative_offset=1000.0,
    )
    await async_wait_recording_done(hass)

    entry = mark_for_resync(hass)
    await run_resync(
        hass, entry, sensor, payload(date(2026, 10, 1), date(2026, 10, 3)), empty_payload()
    )

    assert await stored_sums(hass, date(2026, 9, 1), date(2026, 9, 3)) == pytest.approx(
        expected_sums(date(2026, 9, 1), date(2026, 9, 3))
    )
    assert await stored_sums(hass, date(2026, 10, 1), date(2026, 10, 5)) == pytest.approx(
        [september_end + v for v in expected_sums(date(2026, 10, 1), date(2026, 10, 3))]
    )
    assert entry.data["clear_statistics_needed"] is False
    assert entry.data["historical_fetch_needed"] is False
    assert entry.data["historical_fetch_completed"] is True


async def test_failed_resync_keeps_history_and_retries(hass, sensor):
    """If nothing can be fetched, nothing is cleared and the resync stays due."""
    await import_window(hass, sensor, date(2026, 10, 1), date(2026, 10, 3))
    before = await stored_sums(hass, date(2026, 10, 1), date(2026, 10, 3))

    entry = mark_for_resync(hass)
    await run_resync(hass, entry, sensor, ConnectionError("Enel down"))

    assert await stored_sums(hass, date(2026, 10, 1), date(2026, 10, 3)) == pytest.approx(before)
    assert len(before) == 72
    assert entry.data["clear_statistics_needed"] is True
    assert entry.data["historical_fetch_needed"] is True
    assert not entry.data.get("historical_fetch_completed")


def test_monthly_total_is_the_sum_of_the_latest_month():
    """The monthly sensor reports the month of the latest data only (#11)."""
    from custom_components.enelgrid.sensor import monthly_total

    data = parse_enel_hourly_data(payload(date(2026, 10, 1), date(2026, 11, 3)))
    total, month, last_day = monthly_total(data)

    november = sum(sum(hourly(date(2026, 11, d))) for d in (1, 2, 3))
    assert total == pytest.approx(november, abs=1e-3)
    assert month == "2026-11"
    assert last_day == date(2026, 11, 3)


def test_monthly_total_without_data():
    from custom_components.enelgrid.sensor import monthly_total

    assert monthly_total({}) == (0.0, None, None)


async def test_resync_skips_an_empty_current_month(hass, sensor, freezer):
    """Early in a month Enel has not published it yet: that is not the end.

    The resync must go on to the previous month instead of stopping, and stop
    at the first empty month after that.
    """
    freezer.move_to("2026-12-02 10:00:00+01:00")
    entry = mark_for_resync(hass)
    await run_resync(
        hass,
        entry,
        sensor,
        empty_payload(),  # December: nothing published yet
        payload(date(2026, 11, 1), date(2026, 11, 3)),
        empty_payload(),  # October: end of the history
    )

    assert await stored_sums(hass, date(2026, 11, 1), date(2026, 11, 3)) == pytest.approx(
        expected_sums(date(2026, 11, 1), date(2026, 11, 3))
    )
    assert entry.data["historical_fetch_completed"] is True
    assert entry.data["historical_fetch_needed"] is False
