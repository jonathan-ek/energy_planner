from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol
from .const import DOMAIN, TARIFF_CUSTOM, TARIFF_FLAT
from .planner.tariff import TARIFF_PRESETS, tariff_from_dict


class EnergyPlannerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Example config flow."""

    # The schema version of the entries that it creates
    # Home Assistant will call your migrate method if the version changes
    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle a flow initiated by the user."""
        if user_input is not None:
            res = self.hass.states.get(user_input["nordpool_entity_id"])
            if res is None:
                return self.async_abort(reason="invalid_nordpool_entity_id")
            return self.async_create_entry(title="Energy Planner", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "nordpool_entity_id",
                        description="Entity id of the nordpool sensor",
                    ): str
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return EnergyPlannerOptionsFlow()


class EnergyPlannerOptionsFlow(config_entries.OptionsFlow):
    """Choose the grid tariff: a preset, the flat network settings or custom YAML."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Choose the tariff preset."""
        if user_input is not None:
            preset = user_input["tariff_preset"]
            if preset == TARIFF_CUSTOM:
                return await self.async_step_custom()
            return self.async_create_entry(data={"tariff_preset": preset})

        presets = [TARIFF_FLAT, *TARIFF_PRESETS, TARIFF_CUSTOM]
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "tariff_preset",
                        default=self.config_entry.options.get(
                            "tariff_preset", TARIFF_FLAT
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=presets, translation_key="tariff_preset"
                        )
                    )
                }
            ),
        )

    async def async_step_custom(self, user_input: dict[str, Any] | None = None):
        """Enter a custom tariff as YAML."""
        errors = {}
        if user_input is not None:
            try:
                tariff_from_dict(user_input["tariff"])
            except ValueError:
                errors["tariff"] = "invalid_tariff"
            else:
                return self.async_create_entry(
                    data={
                        "tariff_preset": TARIFF_CUSTOM,
                        "tariff": user_input["tariff"],
                    }
                )

        current = self.config_entry.options.get("tariff") or next(
            iter(TARIFF_PRESETS.values())
        )
        return self.async_show_form(
            step_id="custom",
            data_schema=vol.Schema(
                {vol.Required("tariff", default=current): selector.ObjectSelector()}
            ),
            errors=errors,
        )
