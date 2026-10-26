# Tests for Energy Planner

```bash
make install   # uv sync
make test      # pytest tests/ -v
uv run pytest tests/planner/test_battery_action.py -k reserve
```

- `planner/`: the pure planning modules (optimizer, plan, actions, forecast, price
  estimate, tariff, car, heat pump, fuse, `build_forecast`) are tested without
  patches; the HA glue (`battery_plan`, `forecast_update`) with a `MagicMock` hass
  and patched fetches.
  `planner/plans.py` builds `Plan`s for the action tests.
- `comfort/`: the comfort schedules and room thermal model.
- `test_*.py` at the top: config flow, entities, services and plan triggers.

pytest runs with `asyncio_mode = auto`; tests patch `dt_utils.now` where the time
matters.
