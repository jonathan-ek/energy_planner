# Tests for Energy Planner

This directory contains tests for the Energy Planner Home Assistant custom component.

## Setup

Install test dependencies:

```bash
uv sync
```

## Running Tests

Run all tests:
```bash
pytest
```

Run tests with coverage:
```bash
pytest --cov=custom_components.energy_planner --cov-report=html
```

Run specific test file:
```bash
pytest tests/planner/test_basic_planner.py
```

Run specific test class:
```bash
pytest tests/planner/test_basic_planner.py::TestPlanDay
```

Run specific test:
```bash
pytest tests/planner/test_basic_planner.py::TestPlanDay::test_plan_day_basic
```

Run tests with verbose output:
```bash
pytest -v
```

## Test Structure

```
tests/
├── __init__.py
└── planner/
    ├── __init__.py
    └── test_basic_planner.py
```

## Test Coverage

### test_basic_planner.py

Tests for the basic planner module (`custom_components/energy_planner/planner/basic_planner.py`).

#### TestPlanDay
Tests for the `plan_day` function:

- **test_plan_day_basic**: Verifies basic planning functionality works and creates charge, discharge, and pause slots
- **test_plan_day_charge_slots_in_correct_period**: Ensures charge slots are only created before the discharge period
- **test_plan_day_discharge_slots_in_correct_period**: Ensures discharge slots are only created during the discharge period
- **test_plan_day_soc_values**: Verifies SOC (State of Charge) values are set correctly for different slot types
- **test_plan_day_removes_past_hours**: Confirms that past hours are filtered out from the schedule
- **test_plan_day_empty_data**: Tests graceful handling of empty Nordpool data
- **test_plan_day_zero_charge_hours**: Verifies behavior when no charge hours are requested
- **test_plan_day_slots_are_chronological**: Ensures all slots are in chronological order

#### TestPlanner
Tests for the main `planner` function:

- **test_planner_missing_nordpool_entity_id**: Verifies error handling when Nordpool entity ID is not configured
- **test_planner_nordpool_entity_not_found**: Verifies error handling when Nordpool entity doesn't exist
- **test_planner_success**: Tests successful execution of the complete planning workflow
- **test_planner_missing_nordpool_data**: Tests error handling when Nordpool data is unavailable
- **test_planner_with_tomorrow_data**: Verifies that tomorrow's data is processed when available
- **test_planner_with_string_times**: Tests handling of time configuration as strings vs time objects
- **test_planner_different_areas**: Tests planner with different Nordic electricity areas (NO1, DK1, FI, SE1)

#### TestPlannerIntegration
Integration tests:

- **test_full_planning_cycle**: Tests complete end-to-end planning cycle with validation of slot ordering and state transitions

## Writing New Tests

### Fixtures

The test file provides useful fixtures:

- `mock_hass`: A mock Home Assistant instance with initialized energy_planner data structure
- `mock_nordpool_data`: Mock Nordpool price data for 24 hours with varying prices

### Example Test

```python
@pytest.mark.asyncio
async def test_my_feature(mock_hass, mock_nordpool_data):
    """Test my feature."""
    # Setup
    config = {
        "earliest_charge": dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone),
        "earliest_discharge": dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone),
        "nr_of_charge_hours": 4,
        "nr_of_discharge_hours": 3,
    }
    
    # Execute
    await plan_day(mock_hass, mock_nordpool_data, config)
    
    # Assert
    assert mock_hass.data[DOMAIN]["values"]["slot_1_state"] == "charge"
```

## Mocking

The tests use extensive mocking to isolate the code under test:

- `unittest.mock.MagicMock`: For synchronous mocks
- `unittest.mock.AsyncMock`: For async function mocks
- `unittest.mock.patch`: For patching imported functions and classes

Example:
```python
@patch("custom_components.energy_planner.planner.basic_planner.fetch_nordpool_data")
async def test_with_mock(mock_fetch_nordpool, mock_hass):
    mock_fetch_nordpool.return_value = (yesterday, today, tomorrow)
    await planner(mock_hass)
    mock_fetch_nordpool.assert_called_once()
```

## Continuous Integration

To integrate with CI/CD, add this to your GitHub Actions workflow:

```yaml
- name: Run tests
  run: |
    uv sync
    pytest --cov=custom_components.energy_planner --cov-report=xml

- name: Upload coverage
  uses: codecov/codecov-action@v3
  with:
    file: ./coverage.xml
```

## Test Best Practices

1. **Use descriptive test names**: Test names should clearly describe what is being tested
2. **Follow AAA pattern**: Arrange, Act, Assert
3. **One assertion per test**: Focus each test on a single behavior
4. **Use fixtures**: Reuse common setup code with pytest fixtures
5. **Mock external dependencies**: Don't make real API calls or database connections
6. **Test edge cases**: Include tests for error conditions, empty data, boundary values
7. **Keep tests independent**: Each test should be able to run in isolation

## Troubleshooting

### Import Errors

If you get import errors, ensure you're running pytest from the project root directory and that the `custom_components` directory is in the Python path.

### Async Warnings

If you see warnings about async tests, ensure you have `pytest-asyncio` installed and configured correctly in `pytest.ini`.

### Mock Issues

If mocks aren't working as expected, verify you're patching the correct path (where the function is used, not where it's defined).

