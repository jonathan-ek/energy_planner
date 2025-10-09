import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry

from custom_components.energy_planner.const import DOMAIN, SELECT_ENTITIES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the select platform."""
    _LOGGER.info("Setting up datetime platform")
    selects = [
        *[
            EnergyPlannerSelectEntity(
                hass,
                {
                    "id": f"slot_{i}_state",
                    # Charge = charge with grid (C, SOC, x A)
                    #   Fallback: pause
                    # Discharge = (self-use) use sun to charge or let
                    # battery discharge to house (-, -)
                    #   Fallback: -
                    # Sell = sell to grid (D, SOC, x A)
                    #   Fallback: discharge
                    # Sell excess = sell to grid when house need is met (D, min, 0 A)
                    #   Fallback: -
                    # Discard excess = Disable export (-, -, -)
                    #   Fallback: -
                    # Pause = save battery for later use (C, max, 0 A)
                    #   Fallback: -
                    "options": [
                        "charge",
                        "discharge",
                        "sell",
                        "sell-excess",
                        "discard-excess",
                        "pause",
                        "off",
                    ],
                    "default": "off",
                    "name": f"Slot {i} state",
                    "enabled": True,
                },
            )
            for i in range(1, 50)
        ],
        EnergyPlannerSelectEntity(
            hass,
            {
                "id": "planner_state",
                "options": ["basic", "cheapest hours", "dynamic", "price peak", "off"],
                "default": "basic",
                "name": "Planner state",
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerSelectEntity(
            hass,
            {
                "id": "price_peak_planner_cheap_state",
                "options": [
                    "charge",
                    "discharge",
                    "sell",
                    "sell-excess",
                    "discard-excess",
                    "pause",
                ],
                "default": "charge",
                "name": "Price peak planner cheap hours state",
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerSelectEntity(
            hass,
            {
                "id": "price_peak_planner_expensive_state",
                "options": [
                    "charge",
                    "discharge",
                    "sell",
                    "sell-excess",
                    "discard-excess",
                    "pause",
                ],
                "default": "discharge",
                "name": "Price peak planner expensive hours state",
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerSelectEntity(
            hass,
            {
                "id": "price_peak_planner_inbetween_state",
                "options": [
                    "charge",
                    "discharge",
                    "sell",
                    "sell-excess",
                    "discard-excess",
                    "pause",
                ],
                "default": "pause",
                "name": "Price peak planner inbetween hours state",
                "enabled": True,
                "data_store": "config",
            },
        ),
    ]

    hass.data[DOMAIN][SELECT_ENTITIES] = selects

    for select in selects:
        if hass.data[DOMAIN][select.data_store].get(select.id) is None:
            hass.data[DOMAIN][select.data_store][select.id] = select.current_option
    async_add_devices(selects)
    for select in selects:
        select.update()
    # Return boolean to indicate that initialization was successful
    return True


class EnergyPlannerSelectEntity(SelectEntity):
    """Representation of a Select entity."""

    def __init__(self, hass, entity_definition):
        """Initialize the Select entity."""
        self._hass = hass
        self.id = entity_definition["id"]

        self.entity_id = f"select.{DOMAIN}_{self.id}"
        self._attr_unique_id = "{}_{}".format(DOMAIN, self.id)
        self._attr_has_entity_name = True
        self._attr_name = entity_definition["name"]
        self.data_store = entity_definition.get("data_store", "values")
        self._attr_assumed_state = entity_definition.get("assumed", False)
        self._attr_options = entity_definition.get("options", [])
        self._attr_current_option = entity_definition.get("default", None)
        self._attr_available = True
        self.is_added_to_hass = False
        self._attr_device_class = "enum"
        self._attr_icon = entity_definition.get("icon", None)
        self._attr_native_step = entity_definition.get("step", 1.0)
        self._attr_should_poll = False
        self._attr_entity_registry_enabled_default = entity_definition.get(
            "enabled", False
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.is_added_to_hass = True

    def update(self):
        """Update data."""
        self._attr_available = True
        value = self._hass.data[DOMAIN][self.data_store].get(self.id, None)
        self._attr_native_value = value
        self._attr_current_option = value
        self.schedule_update_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Update the current value."""
        self._attr_current_option = option
        self._attr_native_value = option
        self._hass.data[DOMAIN][self.data_store][self.id] = option
        await self._hass.data[DOMAIN]["save"]()
        self.schedule_update_ha_state()
