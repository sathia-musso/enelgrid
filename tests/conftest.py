import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom components for every test."""
    yield


@pytest.fixture(autouse=True)
def mock_recorder(hass):
    hass.data["recorder"] = MagicMock()
