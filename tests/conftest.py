import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

# The recorder test fixture autospecs a few recorder helpers. On Python 3.14
# that evaluates their lazy annotations, and names imported only under
# TYPE_CHECKING raise NameError. Make them resolvable.
import homeassistant.components.recorder.migration as _migration  # noqa: E402
import homeassistant.helpers.recorder as _helpers_recorder  # noqa: E402
from homeassistant.components.recorder.core import Recorder as _Recorder  # noqa: E402
from sqlalchemy.orm.session import Session as _Session  # noqa: E402

_migration.Recorder = _Recorder
_helpers_recorder.Session = _Session


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Enable custom components for every test.

    enelgrid depends on the recorder, so a real (in-memory) recorder is set
    up first; it must be requested before ``hass``.
    """
    yield
