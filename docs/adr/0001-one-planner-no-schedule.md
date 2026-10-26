# One planner, and no schedule of slots

The battery optimizer's plan is the only planner. The basic, cheapest hours and price
peak planners and the 49-slot schedule they wrote are removed (Oct 2026). Manual slots
remain: a mode, with an optional target SOC, over whole quarters, forced into the
plan. The optimizer does everything the rule-based planners did, with the forecast,
the tariff and power charges, and better. The owner's, the parents' and Kiddes'
houses all run it. One more user still runs a rule-based planner and moves to the
optimizer separately; until then they stay on the tag `v-last-slot-planners`. The schedule existed so those planners could hand a result to the inverter.
Keeping it meant every planner needed slot rewriting, shifting and disabled-state
bookkeeping: about 1 200 lines, most of the untested code, and a second time
convention next to the plan's.

## Considered options

- **Keep the rule-based planners as rules that pick a mode per quarter, with the
  optimizer only simulating the result.** Rejected: nobody uses them, and every
  planner would still need its own settings, tests and day windows. A simple mode can
  come back this way if a user needs one. Do not bring back slots for it.
- **Keep an `auto` slot that means "follow the plan".** Rejected: with one planner it
  is always true, and it kept the slot resolution in front of every action.
