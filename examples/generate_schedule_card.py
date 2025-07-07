header = """
type: vertical-stack
title: Laddningsschema
cards:
"""
entry = """
  - type: conditional
    conditions:
      - condition: state
        entity: select.energy_planner_slot_{nr}_state
        state_not: "off"
    card:
      type: entities
      entities:
        - entity: switch.energy_planner_slot_{nr}_active
          type: custom:multiple-entity-row
          name: Slot {nr}
          toggle: true
          state_color: true
          icon: mdi:timer
          secondary_info:
            entity: select.energy_planner_slot_{nr}_state
            name: False
          tap_action:
            action: more-info
            entity: select.energy_planner_slot_{nr}_state
          entities:
            - entity: number.energy_planner_slot_{nr}_soc
              name: SOC
              unit: false
            - entity: datetime.energy_planner_slot_{nr}_date_time_start
              name: From
            - entity: datetime.energy_planner_slot_{nr_plus_one}_date_time_start
              name: To
"""
if __name__ == "__main__":
    body = header
    for i in range(1, 49):
        body += entry.format(nr=i, nr_plus_one=i + 1).lstrip("\n")

    with open("schedule_card.yml", "w") as file:
        file.write(body)
