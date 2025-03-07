import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN, CONF_POD, CONF_USER_NUMBER, CONF_USERNAME, CONF_PASSWORD

_LOGGER = logging.getLogger(__name__)


class EnelGridConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EnelGrid."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step where the user configures the integration."""
        errors = {}

        if user_input is not None:
            # Save all user-provided data to the config entry
            return self.async_create_entry(
                title=f"EnelGrid ({user_input[CONF_POD]})",
                data=user_input
            )

        # Show the form if user_input is None
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_POD): str,
                vol.Required(CONF_USER_NUMBER): int,
            }),
            errors=errors,
        )


@callback
def async_get_options_flow(config_entry):
    """Return the options flow handler if you want to add optional future settings."""
    return EnelGridOptionsFlowHandler(config_entry)


class EnelGridOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle any future options flow (optional)."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))
