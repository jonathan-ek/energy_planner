"""Tests for the spot prices: published, recorded and estimated."""

import datetime as dt
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.energy_planner.const import DOMAIN
from custom_components.energy_planner.planner import price_estimate

prices = importlib.import_module("custom_components.energy_planner.planner.prices")
# The network calls, before the autouse fixture replaces them
WIND_FORECAST = prices._wind_forecast
ENERGY_CHARTS = prices._energy_charts_history

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 1, 13, 22, 50, tzinfo=TZ)  # Tuesday evening, winter
START = dt.datetime(2026, 1, 13, tzinfo=TZ)
STARTS = [START + dt.timedelta(minutes=15 * i) for i in range(192)]


@pytest.fixture(autouse=True)
def no_network():
    """Keep the price history sources off the network and the disk."""
    with (
        patch.object(prices, "_wind_forecast", AsyncMock(return_value=None)),
        patch.object(prices, "_energy_charts_history", AsyncMock(return_value=None)),
        patch.object(prices, "async_save_to_store", AsyncMock()) as save,
    ):
        yield save


@pytest.fixture
def mock_hass():
    """Create a mock hass with a Nord Pool sensor and an empty price history."""
    default = prices.dt_utils.DEFAULT_TIME_ZONE
    prices.dt_utils.set_default_time_zone(TZ)
    hass = MagicMock()
    hass.states.get = lambda _: SimpleNamespace(attributes={})
    hass.async_add_executor_job = AsyncMock(side_effect=lambda job: job())
    hass.data = {
        DOMAIN: {
            "config": {"nordpool_entity_id": "sensor.nordpool_kwh_se3_sek"},
            prices.PRICE_STORE: {},
        }
    }
    yield hass
    prices.dt_utils.set_default_time_zone(default)


def quarter_prices():
    """Cheap night 00-06, expensive evening, for today and tomorrow."""
    return {
        s: 200.0 if s.hour < 6 else 1500.0 if s.hour >= 17 else 800.0 for s in STARTS
    }


def test_nordpool_zone():
    """Test that the area and currency come from their position in the entity id."""
    assert prices.nordpool_zone("sensor.nordpool_kwh_se3_sek_3_10_025") == (
        "SE3",
        "SEK",
    )


async def test_published_prices_are_stored_before_the_estimate(mock_hass):
    """Test that the heat pump has the price now even when the estimate fails."""
    published = quarter_prices()
    with (
        patch.object(prices.dt_utils, "now", return_value=NOW),
        patch.object(prices, "_published", AsyncMock(return_value=published)),
        patch.object(prices, "_estimated_prices", AsyncMock(side_effect=RuntimeError)),
        pytest.raises(RuntimeError),
    ):
        await prices.async_update_prices(mock_hass, NOW)
    assert mock_hass.data[DOMAIN]["prices"].published == published
    assert prices.spot_at(mock_hass, NOW) == 1500.0


def test_no_spot_price_before_the_first_fetch(mock_hass):
    """Test that the heat pump sensors get no price before the first plan run."""
    assert prices.spot_at(mock_hass, NOW) is None


async def test_wind_forecast(mock_hass):
    """Test that Open-Meteo's answer for all sites becomes speed per date and hour."""
    times = ["2026-01-14T00:00", "2026-01-14T01:00"]
    answer = [
        {"hourly": {"time": times, "wind_speed_100m_previous_day1": [36.0, None]}}
        for _ in price_estimate.WIND_SITES
    ]
    response = MagicMock()
    response.json = AsyncMock(return_value=answer)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get.return_value = context
    with patch.object(prices, "async_get_clientsession", return_value=session):
        wind = await WIND_FORECAST(mock_hass, dt.date(2026, 1, 15))
    assert wind is not None
    assert set(wind) == set(price_estimate.WIND_SITES)
    assert wind["se3"] == {dt.date(2026, 1, 14): {0: 36.0}}
    params = session.get.call_args.kwargs["params"]
    assert params["end_date"] == "2026-01-15"
    assert params["hourly"] == "wind_speed_100m_previous_day1"
    assert len(params["latitude"].split(",")) == len(price_estimate.WIND_SITES)
    # Two days ahead: the forecast made two days before
    for location in answer:
        hourly = location["hourly"]
        hourly["wind_speed_100m_previous_day2"] = hourly.pop(
            "wind_speed_100m_previous_day1"
        )
    with patch.object(prices, "async_get_clientsession", return_value=session):
        wind = await WIND_FORECAST(mock_hass, dt.date(2026, 1, 15), 2)
    assert wind is not None
    assert wind["se3"] == {dt.date(2026, 1, 14): {0: 36.0}}
    params = session.get.call_args.kwargs["params"]
    assert params["hourly"] == "wind_speed_100m_previous_day2"


async def test_no_wind_forecast_on_error(mock_hass):
    """Test that an Open-Meteo failure gives no wind, so the estimate goes without."""
    session = MagicMock()
    session.get.side_effect = aiohttp.ClientError("down")
    with patch.object(prices, "async_get_clientsession", return_value=session):
        assert await WIND_FORECAST(mock_hass, dt.date(2026, 1, 15)) is None


async def test_record_prices(mock_hass, no_network):
    """Test that complete days are kept, older ones dropped, and saved by date."""
    old = (NOW - dt.timedelta(days=prices.PRICE_HISTORY_DAYS + 1)).date()
    mock_hass.data[DOMAIN][prices.PRICE_STORE] = {old: {0: 1.0}}
    quarters = {s: p for s, p in quarter_prices().items() if s.date() == NOW.date()}
    quarters[START + dt.timedelta(days=1)] = 50.0  # one quarter of tomorrow
    await prices._record_prices(mock_hass, quarters, NOW)
    stored = mock_hass.data[DOMAIN][prices.PRICE_STORE]
    assert set(stored) == {NOW.date()}
    assert stored[NOW.date()][3] == 200.0
    saved = no_network.call_args.args[2]
    assert list(saved) == [NOW.date().isoformat()]
    assert saved[NOW.date().isoformat()]["3"] == 200.0
    # Nothing new: not saved again
    no_network.reset_mock()
    await prices._record_prices(mock_hass, quarters, NOW)
    no_network.assert_not_called()


async def test_stored_prices_are_loaded_once(mock_hass):
    """Test that the stored prices are read from disk with dates and hours."""
    del mock_hass.data[DOMAIN][prices.PRICE_STORE]
    load = AsyncMock(return_value={"2026-01-12": {"0": 210.0}})
    with patch.object(prices, "async_load_from_store", load):
        assert await prices._stored_prices(mock_hass) == {
            dt.date(2026, 1, 12): {0: 210.0}
        }
        await prices._stored_prices(mock_hass)
    load.assert_awaited_once()


async def test_gaps_are_filled_from_energy_charts(mock_hass):
    """Test that a sensor history without afternoons is filled from Energy-Charts."""
    mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = (
        "sensor.nordpool_kwh_se4_sek"
    )
    today = {s: p for s, p in quarter_prices().items() if s.date() == NOW.date()}
    days = [(START - dt.timedelta(days=i)).date() for i in range(30)]
    profile = {h: 200.0 if h < 6 else 1500.0 if h >= 17 else 800.0 for h in range(24)}
    # SEK/kWh incl. VAT, only until 13:00 (the attributes passed 16 KB)
    sensor = {
        day: {h: p * 1.25 / 1000 for h, p in profile.items() if h < 14} for day in days
    }
    # EUR/MWh, complete
    euro = {day: {h: p / 11 for h, p in profile.items()} for day in days}
    with (
        patch.object(prices.dt_utils, "now", return_value=NOW),
        patch.object(prices, "_price_history", AsyncMock(return_value=sensor)),
        patch.object(
            prices, "_energy_charts_history", AsyncMock(return_value=euro)
        ) as charts,
    ):
        estimated = await prices._estimated_prices(mock_hass, today, NOW)
    assert charts.call_args.args[1] == "SE4"
    estimate = mock_hass.data[DOMAIN]["price_estimate"]
    assert estimate["sources"]["energy_charts"] == len(days)
    # The same as with a complete sensor history
    complete = {day: {h: p * 1.25 / 1000 for h, p in profile.items()} for day in days}
    del mock_hass.data[DOMAIN]["price_estimate"]
    with (
        patch.object(prices.dt_utils, "now", return_value=NOW),
        patch.object(prices, "_price_history", AsyncMock(return_value=complete)),
    ):
        expected = await prices._estimated_prices(mock_hass, today, NOW)
    assert estimated == pytest.approx(expected)
    evening = dt.datetime(2026, 1, 14, 19, tzinfo=TZ)
    assert estimated[evening] > 1500.0  # the evening profile, not the sensor's gap


async def test_energy_charts_history(mock_hass):
    """Test that Energy-Charts' quarters become the hour's mean per local date."""
    first = int(dt.datetime(2026, 1, 13, 0, tzinfo=TZ).timestamp())
    answer = {
        "unix_seconds": [first + 900 * i for i in range(5)],
        "price": [10.0, 20.0, 30.0, None, 50.0],
    }
    response = MagicMock()
    response.json = AsyncMock(return_value=answer)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get.return_value = context
    with patch.object(prices, "async_get_clientsession", return_value=session):
        history = await ENERGY_CHARTS(
            mock_hass, "SE4", dt.date(2026, 1, 13), dt.date(2026, 1, 14)
        )
    assert history == {dt.date(2026, 1, 13): {0: 20.0, 1: 50.0}}
    assert session.get.call_args.kwargs["params"]["bzn"] == "SE4"
    session.get.side_effect = aiohttp.ClientError("down")
    with patch.object(prices, "async_get_clientsession", return_value=session):
        assert (
            await ENERGY_CHARTS(
                mock_hass, "SE4", dt.date(2026, 1, 13), dt.date(2026, 1, 14)
            )
            is None
        )


async def test_prices_in_the_repeated_hour(mock_hass):
    """Test that the 02:xx quarters before and after DST ends keep their own prices."""
    start = dt.datetime(2026, 10, 25, 0, 0, tzinfo=dt.UTC)  # 02:00 CEST
    quarters = [
        {"start": (start + dt.timedelta(minutes=15 * i)).isoformat(), "value": i}
        for i in range(8)
    ]
    mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = (
        "sensor.nordpool_kwh_se3_sek"
    )
    mock_hass.states.get = lambda _: SimpleNamespace(attributes={})
    with patch.object(
        prices, "fetch_nordpool_data", AsyncMock(return_value=([], quarters, []))
    ):
        published = await prices._published(mock_hass)
    assert sorted(published.values()) == [float(i) for i in range(8)]
    # Both 02:xx hours are local hour 2: the hourly mean takes all eight
    assert prices._hourly(published)[dt.date(2026, 10, 25)] == {2: 3.5}


async def test_prices_without_an_offset_are_local(mock_hass):
    """Test that a time without an offset is in HA's time zone, not the host's."""
    mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = (
        "sensor.nordpool_kwh_se3_sek"
    )
    mock_hass.states.get = lambda _: SimpleNamespace(attributes={})
    quarters = [{"start": "2026-01-13T21:00:00", "value": 1.0}]
    with patch.object(
        prices, "fetch_nordpool_data", AsyncMock(return_value=([], quarters, []))
    ):
        published = await prices._published(mock_hass)
    assert list(published) == [dt.datetime(2026, 1, 13, 20, 0, tzinfo=dt.UTC)]
