# Comfort (work in progress)

Indoor temperature schedules with optimum start, in `comfort/` (pure, tested, not
wired into HA yet). `schedule.py`: profiles (points of time and °C, optionally ramping
to the next) per room group (`living`, `bedrooms`, `offices`) and day kind
(`workday`, `offday`, `work_from_home`, `away`); `day_kind` picks the kind from an
override, calendar words (`borta`, `hemifrån`, ...), offdays and presence.
`thermal.py`: a first-order model per room (`loss`, `gain`, `heat` in °C/h at full
heating), `fit` from hourly samples, `plan` sets the setpoints so a higher target is
reached on time without overshoot.

The house (owner, Oct 2026): district heating through a Siemens RVA36.531 controller
(outdoor curve, mixing valve) feeds radiators with Tado valves (`climate.sovrum`,
`gastrum` = Elise's room, `arbetsrum_uppe` with a faulty valve, `arbetsrum_kallaren`,
`room_5` = workshop, unavailable) and water floor heating zones over MQTT
(`climate.gillestugan`, `tvattstugan`, `vardagsrum`, `badrum`; the `heatfloor` bridge
writes the setpoint to the floor controller at once, the battery room thermostats show it
some minutes later). The hall has the heat
pump and 4 m² of electric floor (Ebeco, `climate.hallen_2`, left alone: electricity at
COP 1); its temperature is `sensor.gw1100a_indoor_temperature` (the Ebeco and MELCloud
sensors read the outer wall or the unit's air). Doors are mostly open, the plan is
open. Radiator supply/return: `sensor.utgaende_radiatortemperatur` /
`retur_radiatortemperatur` (since 2023): supply about 31 °C (its floor) above +5 °C
outdoor, 40 at 0, 43 at -10, 50 at -20.

Last winter's Tado data: with the valve closed rooms cool 0.03-0.10 °C/h at 20 °C
(time constant 230-270 h, but the rooms around them were warm; with the whole house
cooling it is 48-100 h, see below); with it fully open they rise at most about 0.3 °C/h, some
not at all in the cold (Elise's room, the basement office), so the radiators, not the
control, limit setbacks. The heat pump is the only fast source. A weekend step test
(2026-10-09 - 11, `packages/heating_test.yaml` in HA, a copy in
`examples/comfort/heating_test_package.yaml`, phase in
`input_select.heating_test_phase`): everything off, then district heating only, then
the heat pump only, to fit the rooms and how heat spreads. Results in
`docs/heating_step_test_2026_10.md`: with the whole house cooling, the rooms above
ground lose 0.010-0.021 /h (48-100 h), the basement 0.004; the supply stays at the
curve's floor (about 32 °C) whatever the valves do, so 17.6 -> 20.1 °C took 20 h and no
room reached 22; the Tado sensors read 1.5-3 °C high while their valve heats; the heat
pump drew about 0.4 kW and held the hall at about 20.5 °C with a 22 °C setpoint.
Tekniska verken's daily district heating values: about 3.4 kW in B (all the
radiators give at the curve's floor), and the house as one zone loses about
0.19 kW/K with about 18 kWh/K stored (last winter's bills agree).

The RVA36 (manual SE1G2455): curve slope row 17, parallel shift row 64, min/max supply
rows 68/69, its own time program rows 5-11 with reduced setpoint row 14, summer/winter
limit row 16. Room influence and optimum start/stop (rows 65, 73, 74) need a QAA50
room unit on the PPS bus. Input H1 (a potential-free contact, row 96) switches the
operating mode, or with row 96 = 2 forces a minimum supply setpoint (row 97): a relay
on H1 gives HA a boost or a setback. The outdoor sensor is a QAC32 NTC on B9; a
resistance emulator there (e.g. Ohmigo) would let HA set the supply temperature
freely.
