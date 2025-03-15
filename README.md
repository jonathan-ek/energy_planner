# energy_planner
Energy planner for HA

# card
using HACS plugins:
- card-mod
- Vertical Stack In Card
- Multiple Entity Row

Copy the content of Schedule_card.yml to a new card in your lovelace dashboard.

# Manual add slot Form
- Add the following to your configuration.yaml
```yaml
homeassistant:
  packages:
    energy_planner_extras: !include energy_planner_extras.yaml
```
- Add the energy_planner_extras.yaml file to your configuration folder