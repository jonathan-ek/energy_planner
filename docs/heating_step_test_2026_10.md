# Heating step test, 2026-10-09 - 11 (the owner's house)

A weekend test of how the rooms cool and heat, for the comfort models
(`comfort/thermal.py`). Run by `packages/heating_test.yaml` in HA
(`examples/comfort/heating_test_package.yaml`), phase in
`input_select.heating_test_phase`. Nobody was home (the owner at the cabin) from Fri
15:46 to Sun 20:35.

| Phase | Time | Settings |
| --- | --- | --- |
| A, cool-down | Fri 10:00 - Sat 08:00 (22 h) | Tado valves off, floor zones 10 °C, heat pump off |
| B, district heating | Sat 08:00 - Sun 04:00 (20 h) | Tado valves and floor zones 22 °C, heat pump off |
| C, heat pump | Sun 04:00 - 20:35 (16.5 h) | district heating zones back to normal (19-20.5 °C), heat pump `heat` 22 °C |

Outdoor (`sensor.gw1100a_outdoor_temperature`): A 5.5 °C mean (1.8 - 8.7), B 6.9,
C 8.3. Little sun (at most about 110 W/m² on Friday, 60-80 on Saturday and Sunday).
Data: the recorder's states, exported to CSV and analysed with numpy (2 h
differences, so the 0.1 °C sensor steps average out).

## Temperatures, every 2 h

Rooms: the Tado sensors (`sensor.<room>_temperature`, in the valve head) for sovrum,
gastrum (Elise's room), arbetsrum uppe (faulty valve) and arbetsrum källaren; the hall
`sensor.gw1100a_indoor_temperature`; the floor zones' `current_temperature`. Supply and
return are the radiator circuit's mean over the 2 h, load the house's mean (kW).

```
time        phase sovrum gastrum arb_upp arb_kal  hall  vardags gillest tvattst badrum  ute   sun  supply return load
Fri 08:00     -    19.7   20.5   20.8   20.7   21.5   21.7   20.1   20.7   22.7    4.0    2   31.6  26.9  1.2
Fri 10:00     A    19.7   21.2   20.5   22.4   21.4   21.5   20.0   20.5   22.4    5.3   16   31.9  29.9  1.3
Fri 14:00     A    19.1   19.6   20.1   21.6   20.2   20.4   20.4   20.3   21.4    6.7   59   32.6  27.1  0.9
Fri 18:00     A    18.6   18.9   19.1   20.7   19.8   19.9   20.2   20.1   20.7    8.2   43   32.3  26.2  0.7
Fri 22:00     A    17.4   18.1   18.3   20.5   19.0   19.2   20.0   20.0   20.1    5.7    -   34.1  25.6  0.7
Sat 02:00     A    16.3   17.3   17.7   20.2   18.3   18.6   19.6   19.7   19.5    1.8    -   33.0  26.9  0.6
Sat 06:00     A    15.6   16.7   17.0   19.9   17.7   18.0   19.4   19.5   18.8    4.9    -   32.7  26.9  0.7
Sat 08:00     B    15.1   16.5   16.8   19.8   17.6   17.7   19.2   19.4   18.6    5.0    1   32.5  26.7  0.7
Sat 10:00     B    19.3   18.7   17.5   21.4   18.0   17.9   19.5   19.9   18.6    5.7   16   32.7  26.2  0.7
Sat 14:00     B    20.0   19.5   18.1   21.5   19.0   18.8   20.3   20.6   19.3    6.5   78   31.7  28.3  0.6
Sat 18:00     B    20.4   19.9   18.2   21.6   19.4   19.4   20.5   20.4   19.9    7.6   26   31.1  28.4  0.6
Sat 22:00     B    21.0   20.1   18.3   21.6   19.7   19.7   20.7   20.2   20.3    7.5    -   30.8  28.2  0.7
Sun 02:00     B    21.1   20.3   18.5   21.7   20.0   19.9   20.8   20.2   20.6    7.1    -   31.2  28.5  0.7
Sun 04:00     C    21.3   20.4   18.6   21.7   20.1   20.0   20.8   20.2   20.8    7.0    -   31.1  28.4  0.7
Sun 06:00     C    19.7   20.9   19.1   21.1   20.4   20.4   20.7   20.2   21.1    7.4    -   30.7  29.2  1.2
Sun 10:00     C    19.7   20.6   19.5   20.3   20.5   20.7   20.4   20.1   21.5    8.4   54   33.8  27.6  1.1
Sun 14:00     C    19.1   20.7   19.7   20.1   20.5   20.7   20.2   20.1   21.6    9.8   64   32.4  26.7  1.0
Sun 18:00     C    19.0   20.5   19.5   20.0   20.6   20.9   20.2   20.1   21.8    8.3   34   32.6  26.4  1.1
```

## Findings

### Cooling (phase A)

| Room | Drop (Fri 12:00 - Sat 08:00) | Rate | Loss (1/h) | Time constant |
| --- | --- | --- | --- | --- |
| sovrum | 19.2 -> 15.1 | -0.20 °C/h | 0.021 | 48 h |
| gastrum | 19.7 -> 16.5 | -0.16 | 0.013 | 78 h |
| arbetsrum uppe | 20.2 -> 16.8 | -0.17 | 0.012 | 83 h |
| hall | 21.2 -> 17.6 | -0.18 | 0.011 | 89 h |
| vardagsrum (floor) | 21.0 -> 17.7 | -0.17 | 0.011 | 94 h |
| badrum (floor) | 21.9 -> 18.6 | -0.17 | 0.010 | 102 h |
| arbetsrum källaren | 21.8 -> 19.8 | -0.10 | 0.0044 | 230 h |
| gillestugan (floor, basement) | 20.5 -> 19.2 | -0.07 | 0.0044 | 225 h |
| tvättstugan (floor, basement) | 20.4 -> 19.4 | -0.05 | 0.0035 | 286 h |

Loss = rate / (outdoor - room), fitted on the night samples without a constant (a fit
with a constant and the sun is not identifiable from one cool-down: outdoor and time
move together).

- With the whole house cooling, the ground floor and upstairs rooms lose heat 3-5 times
  faster than last winter's Tado data said (0.004 /h, 230-270 h, the valve closed while
  the rest of the house was warm). That winter value was mostly heat from the warm
  neighbours, so a room model needs the neighbours (or the house as one zone), not one
  loss per room. The basement keeps about 0.004 /h (the ground and the floor slab).
- A full day off costs about 4 °C upstairs and 1-2 °C in the basement at 5 °C outdoor.

### District heating (phase B)

- The supply did not change: 31.5 °C mean in B, 32.9 in A, the curve's floor at this
  outdoor temperature. The RVA36 has no room unit, so it does not react when every
  valve opens; only the return rose (27 -> 28 °C). So the radiators' heat is fixed by
  the outdoor temperature, and the valves only switch it on and off.
- The Tado sensors in the valve heads read high while the valve heats: sovrum jumped
  15.1 -> 18.8 °C in the first hour (impossible for a room with a 48 h time constant;
  the hall's own sensor rose 0.1 °C) and fell 21.3 -> 19.7 in the 2 h after the valve
  closed at the start of C. The bias is about 1.5-3 °C, largest just after the valve
  opens. Fits and comfort targets need a room sensor away from the radiator, or a
  correction for the valve's state.
- Real warming (hall and living room, independent sensors): about 0.05 °C/h the first
  hour, 0.25 °C/h at 2-4 h, then 0.06-0.09 °C/h; 17.6 -> 20.1 °C in 20 h. None of the
  rooms reached 22 °C, and all were still rising slowly at the end (gastrum 20.4,
  hall 20.1, arbetsrum uppe 18.6 with its faulty valve, which rose mostly from the
  rooms around it).
- So with this supply temperature a 3 °C setback takes about a day to recover:
  setbacks of more than about 1 °C do not pay unless the supply can be raised
  (a relay on the RVA36's H1 input or a sensor emulator on B9, see comfort.md).

- Floor zones: the basement rose 0.8 (tvättstugan) and 1.6 °C (gillestugan), the
  ground floor (vardagsrum, badrum) 2.2-2.3 °C, at the same pace as the radiator rooms.

### Heat pump (phase C)

- The house load rose from about 0.66-0.75 kW (A, B) to 1.08 kW: the heat pump drew
  about 0.4 kW on average at 8 °C outdoor, about 1.5 kW of heat at a COP of 3.6.
- MELCloud's room temperature (`sensor.hallen_room_temperature`, the air at the
  unit) went 14.5 -> 21.0 °C in the first hour and stayed at about 21.5, so the heat
  pump throttled back with the hall itself at 20.4-20.6 °C (gw1100a). With a 22 °C
  setpoint it holds about 20.5 °C in the hall: the setpoint has to be about 1.5 °C
  above the hall temperature wanted.
- The heat spreads to the rooms next to the hall: vardagsrum 20.0 -> 20.9 and badrum
  20.8 -> 21.8 °C (both floor zones at their normal 19-19.5 °C setpoints, so this heat
  came from the heat pump), arbetsrum uppe 18.6 -> 19.5 (its valve is faulty and
  always open). Sovrum, gastrum and the basement fell back towards their normal
  setpoints.
- The Ebeco floor sensor in the hall (`sensor.hallen_floor_temperature`) rose
  18.9 -> 20.1 °C over the day.

### District heating energy (Tekniska verken, per day)

`sensor.fjarrvarme` has been stuck since 2025, so the energy comes from Mitt Tekniska
verken's daily values (read from the chart, ±1 kWh; Oct 10 from the tooltip), with
the daily means from HA:

| Day | District heating (kWh) | Outdoor (°C) | Hall (°C) | Load (kW) | Heat pump (h) | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Oct 1-8 | 6-11 (mean 7.4, Oct 4: 21) | 10.0-15.1 | 21.5-22.7 | 0.8-1.6 | Oct 8: 24 | normal |
| Fri Oct 9 | 8 | 6.2 | 20.8 | 0.92 | 10 | normal until 10:00, then A |
| Sat Oct 10 | **56** | 5.8 | 18.7 | 0.66 | 0 | A until 08:00, then B |
| Sun Oct 11 | 22 | 7.9 | 20.4 | 1.05 | 20 | B until 04:00, then C |
| Mon Oct 12 | 14 | 6.6 | 20.5 | 1.32 | 23 | normal, heat pump on |

- Phase B took about 54 kWh in 16 h on Saturday (less a little hot water
  circulation, nobody home): **about 3.4 kW** with every valve open and the supply at
  the curve's floor (32 °C). That is all the radiators give at this supply with the
  rooms at 17-19 °C, and it is why the house warmed so slowly.
- A normal October day takes only 6-11 kWh (0.3-0.45 kW incl. hot water): at 10-15 °C
  outdoor the house's electricity (0.8-1.3 kW, all ends up as heat), people and the
  sun cover most of the loss.
- Whole house as one zone, P = UA × (indoor − outdoor) + C × dT/dt, with the five
  sensors outside the valve heads (hall and the four floor zones) as the house mean
  and the electric load as heat: A (no district heating, 0.7 kW, −0.11 °C/h at
  14.1 K) and B (3.4 + 0.66 kW, +0.094 °C/h at 12.3 K) give **UA ≈ 0.19 kW/K** and
  **C ≈ 18 kWh/K** (time constant about 93 h, within the rooms' 48-100 h).
- Last winter's bills agree: Jan 2026 2.53 MWh (3.4 kW) at −3.1 °C and Feb 2.42 MWh
  (3.6 kW) at −4.4 °C, with about 0.3 kW of hot water, the house's 1.15-1.17 kW of
  electricity and 0.2 kW from people, give UA 0.197 and 0.195 kW/K at a house mean of
  19.5 °C.
- So the house needs about 0.19 kW per °C between in- and outdoor, less 1.2-1.5 kW
  of free heat: nothing from district heating above about 13 °C outdoor, 2.5 kW at
  0 °C, 4.3 kW at −10 °C. A 1 °C lower indoor temperature saves about 4.6 kWh of
  district heating per day (4.6 kr/day at the winter price).
- The heat pump's 1.5 kW of heat covers about 8 °C of that difference: on Monday
  (23 h of heating, 6.6 °C outdoor) the model gives about 8 kWh of district heating
  with it and about 44 without, the meter said 14. So about 30 kWh less district
  heating per day (23 kr at 76.7 öre in October) for about 10 kWh of electricity.

## Not measured

- The district heating energy per hour (only per day, see above), so the split of
  Sunday between B and C and how fast the radiators' output fell as the rooms warmed
  are estimates.
- People and cooking: nobody was home, so A-C have no internal gains. Normal days have
  them.
- One weekend at 2-10 °C outdoor; the supply curve is steeper below 0 °C (40 °C at 0,
  43 at -10), so the radiators' heating in °C/h is larger in winter.
