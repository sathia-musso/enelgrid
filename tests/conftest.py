import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The recorder test fixture autospecs a few recorder helpers. On Python 3.14
# that evaluates their lazy annotations, and names imported only under
# TYPE_CHECKING raise NameError. Expose them where they are missing. Test-only;
# drop it once pytest-homeassistant handles this itself.
def _expose_type_checking_names():
    try:
        import homeassistant.components.recorder.migration as migration
        import homeassistant.helpers.recorder as helpers_recorder
        from homeassistant.components.recorder.core import Recorder
        from sqlalchemy.orm.session import Session
    except ImportError:
        return
    for module, name, value in (
        (migration, "Recorder", Recorder),
        (helpers_recorder, "Session", Session),
    ):
        if not hasattr(module, name):
            setattr(module, name, value)


_expose_type_checking_names()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Enable custom components for every test.

    enelgrid depends on the recorder, so a real (in-memory) recorder is set
    up first; it must be requested before ``hass``.
    """
    yield


@pytest.fixture(autouse=True)
def no_real_enel_session():
    """Never reach the Enel portal from a test.

    With a real recorder the entry sets up for real, and the sensor starts a
    fetch in the background. This default mock logs in and returns no data;
    tests that need data or a login failure patch EnelGridSession themselves
    (an inner patch wins over this one).
    """
    session = MagicMock()
    session.login = AsyncMock()
    session.close = AsyncMock()
    session.fetch_consumption_data = AsyncMock(
        return_value={"data": {"aggregationResult": {"aggregations": []}}}
    )
    with (
        patch("custom_components.enelgrid.sensor.EnelGridSession", return_value=session),
        patch("custom_components.enelgrid.config_flow.EnelGridSession", return_value=session),
    ):
        yield session
