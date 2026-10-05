# Testing Examples

This file contains examples of how to run different types of tests.

## Quick Start

```bash
# Make the test script executable
chmod +x run_tests.sh

# Run all tests
./run_tests.sh

# Or use pytest directly
pytest
```

## Running Specific Tests

### Run tests in a specific file
```bash
pytest tests/planner/test_basic_planner.py
```

### Run a specific test class
```bash
pytest tests/planner/test_basic_planner.py::TestPlanDay
```

### Run a specific test method
```bash
pytest tests/planner/test_basic_planner.py::TestPlanDay::test_plan_day_basic
```

### Run tests matching a pattern
```bash
pytest -k "charge"  # Run all tests with "charge" in the name
pytest -k "not slow"  # Skip slow tests
```

## Coverage

### Run with coverage report
```bash
pytest --cov=custom_components.energy_planner
```

### Generate HTML coverage report
```bash
pytest --cov=custom_components.energy_planner --cov-report=html
# Open htmlcov/index.html in browser
```

### Show missing lines
```bash
pytest --cov=custom_components.energy_planner --cov-report=term-missing
```

## Debugging Tests

### Run with verbose output
```bash
pytest -v
```

### Show print statements
```bash
pytest -s
```

### Stop on first failure
```bash
pytest -x
```

### Drop into debugger on failure
```bash
pytest --pdb
```

### Run last failed tests
```bash
pytest --lf
```

## Performance

### Show slowest tests
```bash
pytest --durations=10
```

### Run tests in parallel (requires pytest-xdist)
```bash
uv add --dev pytest-xdist
pytest -n auto
```

## Markers

### Run only unit tests
```bash
pytest -m unit
```

### Run only integration tests
```bash
pytest -m integration
```

### Skip slow tests
```bash
pytest -m "not slow"
```

## Example: Testing a new feature

Let's say you're working on a new feature and want to test it:

```bash
# 1. Write your test in tests/planner/test_basic_planner.py
# 2. Run just that test to verify it works
pytest tests/planner/test_basic_planner.py::TestPlanDay::test_my_new_feature -v

# 3. Run all related tests
pytest tests/planner/test_basic_planner.py::TestPlanDay -v

# 4. Check coverage
pytest tests/planner/test_basic_planner.py --cov=custom_components.energy_planner.planner.basic_planner --cov-report=term-missing

# 5. Run full test suite before committing
./run_tests.sh
```

## Continuous Testing

### Watch mode (requires pytest-watch)
```bash
uv add --dev pytest-watch
ptw
```

This will automatically run tests when files change.

## Common Issues

### Import Errors
If you see import errors, make sure you're in the project root:
```bash
cd /home/jonathan/projects/energy_planner
pytest
```

### Async Test Warnings
If async tests don't run, check that pytest-asyncio is installed:
```bash
uv add --dev pytest-asyncio
```

### Missing Dependencies
Install all test dependencies:
```bash
uv sync
```

