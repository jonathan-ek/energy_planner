"""Tests for the tariff options flow."""

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_planner.const import DOMAIN


def add_entry(hass):
    """Add a config entry without setting it up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"nordpool_entity_id": "sensor.nordpool_kwh_se3_sek_3_10_025"},
    )
    entry.add_to_hass(hass)
    return entry


async def test_choose_preset(hass):
    """Test that a preset is stored without a tariff."""
    entry = add_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tariff_preset": "tekniska_verken_alternativ"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"tariff_preset": "tekniska_verken_alternativ"}


async def test_custom_tariff(hass):
    """Test that an invalid custom tariff is rejected and a valid one stored."""
    entry = add_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tariff_preset": "custom"}
    )
    assert result["step_id"] == "custom"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tariff": {"energy_fees": [{"hours": [6, 23]}]}}
    )
    assert result["errors"] == {"tariff": "invalid_tariff"}

    tariff = {"power_charges": [{"kr_per_kw": 80, "peaks": 3, "one_per_day": True}]}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tariff": tariff}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"tariff_preset": "custom", "tariff": tariff}
