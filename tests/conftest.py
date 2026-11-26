import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

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
