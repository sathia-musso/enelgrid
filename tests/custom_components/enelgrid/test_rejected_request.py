"""Enel rejects the consumption request.

The login succeeds, but when the user number does not match the POD the
consumption API answers with ``"data": null`` and an error status. The error
must say so instead of ending in an ``AttributeError`` traceback.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.enelgrid.sensor import (
    historical_fetch_task,
    parse_enel_hourly_data,
)

from .test_statistics import mark_for_resync, sensor  # noqa: F401  (fixture)

REJECTED = {
    "data": None,
    "status": {"code": "-1", "result": "AUT02", "description": "Utente non autorizzato"},
}


def rejecting_session():
    session = MagicMock()
    session.fetch_consumption_data = AsyncMock(return_value=REJECTED)
    session.close = AsyncMock()
    return session


def test_parse_reports_the_enel_status():
    with pytest.raises(HomeAssistantError, match=r"Utente non autorizzato \(AUT02\)"):
        parse_enel_hourly_data(REJECTED)


def test_parse_response_without_data_is_still_no_data():
    """A response without the hourly aggregation is not an error."""
    assert parse_enel_hourly_data({}) == {}


async def test_daily_update_logs_the_rejection(hass, sensor, caplog):  # noqa: F811
    with patch(
        "custom_components.enelgrid.sensor.EnelGridSession",
        return_value=rejecting_session(),
    ):
        await sensor.async_update()

    assert sensor.state == "Error"
    assert "Utente non autorizzato (AUT02)" in caplog.text
    assert "AttributeError" not in caplog.text


async def test_resync_logs_the_rejection_and_stays_due(hass, sensor, caplog):  # noqa: F811
    entry = mark_for_resync(hass)
    with patch(
        "custom_components.enelgrid.sensor.EnelGridSession",
        return_value=rejecting_session(),
    ):
        await historical_fetch_task(hass, entry, sensor)

    assert "Utente non autorizzato (AUT02)" in caplog.text
    assert "AttributeError" not in caplog.text
    assert entry.data["historical_fetch_needed"] is True
