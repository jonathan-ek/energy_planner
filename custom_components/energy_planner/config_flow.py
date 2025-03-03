from typing import Any

from homeassistant import config_entries
import voluptuous as vol
from .const import DOMAIN


class EnergyPlannerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Example config flow."""
    # The schema version of the entries that it creates
    # Home Assistant will call your migrate method if the version changes
    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            res = self.hass.states.get(user_input["nordpool_entity_id"])
            if res is None:
                return self.async_abort(reason="invalid_nordpool_entity_id")
            return self.async_create_entry(title="Energy Planner", data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({
                vol.Required("nordpool_entity_id", description="Entity id of the nordpool sensor"): str
            })
        )