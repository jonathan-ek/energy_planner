# Energy Planner

Plans when a home battery, an electric car and a heat pump should charge, discharge or
run, from spot prices, forecasts and grid tariffs, and tells the devices what to do now.

## Language

### Planning

**Plan**:
The battery optimizer's result for every quarter from now to the end of the known
prices: a mode, SOC, import target and the car's and heat pump's power per quarter.
Recalculated often and never written to the slots.
_Avoid_: battery plan dict, schedule

**Schedule**:
The fixed list of slots the planners write and the user can edit.
_Avoid_: plan

**Slot**:
One entry in the schedule: a start, a slot state and a target SOC, lasting until the
next slot starts.

**Mode**:
What the battery does in one quarter of the plan (`self_use`, `hold`, `sell_excess`,
`discard_excess`, `charge`, `sell`).
_Avoid_: slot state (the schedule's name for the same thing)

**Run**:
A stretch of consecutive quarters in the plan with the same value for one device, such
as the same mode or the same car charging power. It ends at its until time.
_Avoid_: period, window

**Stale plan**:
A plan that became available too long ago to follow. The devices then fall back to what
they would do without the planner.

### Acting

**Action**:
What one device should do now (battery, car or heat pump), with its source (slot, plan,
fallback) and until when.
_Avoid_: command, decision

**Fallback**:
The action a device gets when there is no fresh plan covering now.

### Configuration

**Setting**:
A value the user chooses for the planner, with one default, such as the battery reserve
SOC or the main fuse. Some settings make the plan be recalculated when they change.
_Avoid_: option (the integration options hold only the grid tariff), config value

**Reading**:
The current value of a Home Assistant sensor that a setting points to, such as the
battery SOC. It is missing when the sensor is not configured, unavailable or not a
number.
_Avoid_: state, measurement
