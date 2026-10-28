"""Build the mobile "Elpris" dashboard (Tibber inspired) and its savings package for a home.

python build_elpris_dashboard.py [--site owner|oscar|parents|kidde] [--colors average|plan|action]
[--push] writes elpris_dashboard[_<site>].yaml and elpris_savings_package[_<site>].yaml (an HA
package: copy it to the home's packages; the owner's here, the other homes' in local/, which git
ignores); --push creates/updates the dashboard `dashboard-elpris` over the websocket API. SITES
holds each home's entity ids, Nord Pool unit and grid fees. What should not be public is in
elpris_sites.local.json (git ignores it), merged over the site's profile:

    {"owner": {"url": "http://<ip>:8123", "token": "~/.ha_token"},
     "oscar": {"url": "...", "token": "...", "pulse": "sensor.tibber_pulse_<address>"}}

HA_URL / HA_TOKEN_FILE override the url and token file.

--colors sets what colours the price line (PRICE_COLORS): `average` teal at or below the shown
span's average, orange above; `plan` teal where the battery plan takes from the grid (charge,
hold) and orange where it uses the battery (self-use, sell); `action` one colour per plan mode.
With `plan` or `action` the quarters before the plan (earlier today) are grey and a legend
follows the charts. The site's `colors` is the default (else `average`); another one is a trial,
pushed as `dashboard-elpris-<colors>` and written with the suffix `_<colors>`.
"""

import asyncio
import json
import os
import sys

import yaml

# Owner's power flow card (the Energi dashboard's), sized for a phone; Oscar's below
SOLIS = "sensor.solis_s6_solis_"
FLOW_ENTITIES = {
    "day_battery_charge_70": SOLIS + "today_battery_charge_energy",
    "day_battery_discharge_71": SOLIS + "today_battery_discharge_energy",
    "day_load_energy_84": SOLIS + "today_energy_consumption",
    "day_pv_energy_108": SOLIS + "pv_today_energy_generation",
    "inverter_voltage_154": SOLIS + "a_phase_voltage",
    "load_frequency_192": SOLIS + "grid_frequency",
    "inverter_current_164": SOLIS + "a_phase_current",
    "inverter_power_175": SOLIS + "backup_load_power",
    "grid_power_169": SOLIS + "ac_grid_port_power",
    "grid_ct_power_172": SOLIS + "meter_total_active_power",
    "battery_voltage_183": SOLIS + "battery_voltage",
    "battery_soc_184": SOLIS + "battery_soc",
    "battery_power_190": SOLIS + "battery_power",
    "battery_current_191": SOLIS + "battery_current",
    "battery_current_direction": SOLIS + "battery_current_direction",
    "essential_power": SOLIS + "household_load_power",
    "pv_total": SOLIS + "total_pv_power",
    "dc_transformer_temp_90": SOLIS + "temperature",
    "inverter_status_59": SOLIS + "status",
    "grid_voltage": SOLIS + "a_phase_voltage",
    **{f"pv{i}_power_{185 + i}": SOLIS + f"pv_power_{i}" for i in range(1, 5)},
    **{f"pv{i}_voltage_{107 + 2 * i}": SOLIS + f"pv_voltage_{i}" for i in range(1, 4)},
    **{f"pv{i}_current_{108 + 2 * i}": SOLIS + f"pv_current_{i}" for i in range(1, 4)},
}

SITES = {
    "owner": {
        "colors": "action",
        "nordpool": "sensor.nordpool_kwh_se3_sek_3_10_025", "price_scale": 1,  # öre/kWh
        "bat_net": "sensor.solis_s6_eh1p_battery_power_net",
        # Tekniska verken alternativ, öre/kWh incl. VAT: energy tax + transfer fee (day 06-23);
        # selling: spot + network compensation (higher on winter weekdays 06-22)
        "fee_js": "45 + (h >= 6 && h < 23 ? 18 : 9)",
        "fee_jinja": "45 + (18 if 6 <= h < 23 else 9)",
        "sell_jinja": "(7.2 if now().month in [1, 2, 3, 11, 12] and now().isoweekday() <= 5"
                      " and 6 <= h < 22 else 5.1)",
        "import_kw": "states('sensor.aktiv_effekt_uttag_momentan_trefaseffekt') | float",
        "export_kw": "states('sensor.aktiv_effekt_inmatning_momentan_trefaseffekt') | float",
        "bought": "sensor.matarstallning_aktiv_energi_uttag",
        "sold": "sensor.matarstallning_aktiv_energi_inmatning",
        "since": "Sedan installationen",
        "note": "Spotpris, energiskatt och överföring; effektavgiften ingår inte.",
        "battery_kw": 10,
        "flow": {
            "battery": {"max_power": 6000, "energy": 25600, "shutdown_soc": 15},
            "solar": {"mppts": 3, "max_power": 16000, "pv1_name": "Fram", "pv2_name": "Bak",
                      "pv3_name": "Garaget", "pv1_max_power": 6000, "pv2_max_power": 7000,
                      "pv3_max_power": 5000},
            "entities": {"day_grid_import_76": "sensor.daily_energy_bought",
                         "day_grid_export_77": "sensor.daily_energy_sold"},
        },
    },
    "oscar": {
        "colors": "action",
        # The Tibber Pulse entity ids carry the address: "pulse" in the local file
        "pulse": "sensor.tibber_pulse_home",
        "nordpool": "sensor.nordpool_kwh_se4_sek_3_10_025", "price_scale": 100,  # SEK/kWh
        "bat_net": "sensor.solis_s6_eh3p_battery_power_net",
        # E.ON Energidistribution 20 A: energy tax + transfer fee; selling: spot + 10 öre
        "fee_js": "45 + 32.3",
        "fee_jinja": "45 + 32.3",
        "sell_jinja": "10",
        "import_kw": "states('{pulse}_power') | float / 1000",
        "export_kw": "states('{pulse}_power_production') | float / 1000",
        "bought": "{pulse}_last_meter_consumption",
        "sold": "{pulse}_last_meter_production",
        "since": "Sedan januari 2025",
        "note": "Spotpris, energiskatt och överföring (E.ON).",
        "battery_kw": 15,
        "flow": {
            "battery": {"max_power": 10000, "energy": 51200, "shutdown_soc": 10},
            "solar": {"mppts": 4, "max_power": 9600, "pv1_name": "Frigge", "pv2_name": "Frigge",
                      "pv3_name": "Branta", "pv4_name": "Platta"},
            "entities": {
                "day_grid_import_76": "{pulse}_accumulated_consumption",
                "day_grid_export_77": "{pulse}_accumulated_production",
            },
        },
    },
    "parents": {
        "colors": "action",
        "nordpool": "sensor.nordpool_kwh_se4_sek_3_10_025", "price_scale": 100,  # SEK/kWh
        "bat_net": "sensor.solis_s6_eh3p_battery_power_net",
        # The planner's tariff is E.ON's 20 A (eon_20a), as Oscar's
        "fee_js": "45 + 32.3",
        "fee_jinja": "45 + 32.3",
        "sell_jinja": "10",
        "import_kw": "states('sensor.aktiv_effekt_uttag_momentan_trefaseffekt') | float",
        "export_kw": "states('sensor.aktiv_effekt_inmatning_momentan_trefaseffekt') | float",
        "bought": "sensor.matarstallning_aktiv_energi_uttag",
        "sold": "sensor.matarstallning_aktiv_energi_inmatning",
        "since": "Sedan januari 2025",
        "note": "Spotpris, energiskatt och överföring (E.ON).",
        "battery_kw": 15,
        "flow": {
            "battery": {"max_power": 10000, "energy": 50000, "shutdown_soc": 20},
            "solar": {"mppts": 4, "max_power": 15000, "pv1_name": "PV1", "pv2_name": "PV2",
                      "pv3_name": "PV3", "pv4_name": "PV4"},
            "entities": {"day_grid_import_76": "sensor.daily_grid_import",
                         "day_grid_export_77": "sensor.daily_grid_export"},
        },
    },
    "kidde": {
        "colors": "action",
        # No working grid meter of its own (Tibber unavailable): the Solis meter, W, positive
        # while exporting. A second, unmonitored inverter behind it: sensor.pv_total_power
        # adds its export, but the house load misses what the house takes from it directly
        "nordpool": "sensor.nordpool_kwh_se4_sek_3_10_025", "price_scale": 100,  # SEK/kWh
        "bat_net": "sensor.solis_s6_eh3p_battery_power_net",
        "fee_js": "45 + 32.3",
        "fee_jinja": "45 + 32.3",
        "sell_jinja": "10",
        "import_kw": "[-(states('sensor.solis_s6_solis_meter_total_active_power') | float), 0] | max / 1000",
        "export_kw": "[states('sensor.solis_s6_solis_meter_total_active_power') | float, 0] | max / 1000",
        "bought": "sensor.solis_s6_solis_total_energy_imported_from_grid",
        "sold": "sensor.solis_s6_solis_total_energy_fed_into_grid",
        "pv": "sensor.pv_total_power",
        "since": "Sedan februari 2025",
        "note": "Spotpris, energiskatt och överföring (E.ON). Hushållet räknas från Solis mätare,"
                " så det huset tar direkt från den andra växelriktaren saknas: besparingen är"
                " i underkant.",
        "battery_kw": 10,
        "flow": {
            "battery": {"max_power": 10000, "energy": 51200, "shutdown_soc": 15},
            "solar": {"mppts": 2, "max_power": 11900, "pv1_name": "PV1", "pv2_name": "PV2"},
            "entities": {"day_grid_import_76": SOLIS + "today_energy_imported_from_grid",
                         "day_grid_export_77": SOLIS + "today_energy_fed_into_grid"},
        },
    },
}


def merge(base, local):
    """`base` with the values of `local` over it, nested dicts merged."""
    out = dict(base)
    for key, value in local.items():
        out[key] = merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


def fill(value, pulse):
    """Put the site's Tibber Pulse prefix into the "{pulse}" of every string."""
    if isinstance(value, dict):
        return {k: fill(v, pulse) for k, v in value.items()}
    return value.replace("{pulse}", pulse) if isinstance(value, str) and pulse else value


SITE_NAME = sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv else "owner"
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_FILE = os.path.join(HERE, "elpris_sites.local.json")
LOCAL = json.load(open(LOCAL_FILE)) if os.path.exists(LOCAL_FILE) else {}
SITE = merge(SITES[SITE_NAME], LOCAL.get(SITE_NAME, {}))
SITE = fill(SITE, SITE.get("pulse"))
COLORS = sys.argv[sys.argv.index("--colors") + 1] if "--colors" in sys.argv else SITE.get("colors", "average")
# Another colouring than the site's own is a trial: its own dashboard and file
TRIAL = "" if COLORS == SITE.get("colors", "average") else "-" + COLORS
DASHBOARD = "dashboard-elpris" + TRIAL

NORDPOOL = SITE["nordpool"]
SCALE = SITE["price_scale"]
PLAN = "sensor.energy_planner_battery_plan"
PV_FC = "sensor.energy_planner_pv_forecast"
SOC = SOLIS + "battery_soc"
BAT_NET = SITE["bat_net"]  # W, negative while charging
PV = SITE.get("pv", SOLIS + "total_pv_power")
LOAD = SOLIS + "household_load_power"
CAPACITY = "number.energy_planner_battery_capacity"
# Daily energy counters (total_increasing, their statistics give kWh per day)
BOUGHT, SOLD = SITE["bought"], SITE["sold"]
PV_DAY = SOLIS + "pv_today_energy_generation"
LOAD_DAY = SOLIS + "today_energy_consumption"
# The savings package: kr since the sensors exist (history imported), and meters
COST, SAVING = "sensor.elkostnad", "sensor.elbesparing"

LOW, HIGH, EST, PLANNED = "#2dd4bf", "#ff8a3d", "#8a94a6", "#9aa4b2"
SOLAR, BATTERY = "#ffa04d", "#22c7e8"

# Price line colours by the plan (--colors plan|action): key -> (colour, label, plan modes).
# Manual slots win over the plan's mode, as in the battery action.
PRICE_COLORS = {
    "plan": {
        "grid": (LOW, "Nätet (laddar, vilar)", ["charge", "hold"]),
        "battery": (HIGH, "Batteriet (självförbrukning, sälj)",
                    ["self_use", "sell", "sell_excess", "discard_excess"]),
    },
    "action": {
        "charge": (LOW, "Ladda", ["charge"]),
        "hold": ("#7c9cff", "Vila", ["hold"]),
        "self_use": (HIGH, "Självförbrukning", ["self_use"]),
        "sell": ("#ff5c7a", "Sälj", ["sell"]),
        "sell_excess": ("#f5c542", "Sälj överskott", ["sell_excess"]),
        "discard_excess": ("#b78cff", "Kasta överskott", ["discard_excess"]),
    },
}
STATE_MODES = {"charge": "charge", "pause": "hold", "discharge": "self_use", "sell": "sell",
               "sell-excess": "sell_excess", "discard-excess": "discard_excess"}
NO_PLAN = (EST, "Ingen plan")

# Grid fees, öre/kWh incl. VAT, at the local hour h
FEE_JS = f"const fee = (ms) => {{ const h = new Date(ms).getHours(); return {SITE['fee_js']}; }};"

# Published prices (Nord Pool, today and tomorrow) then the plan's estimated ones,
# total öre/kWh incl. VAT, energy tax and transfer fee: [[ms, öre, estimated]]
PRICES_JS = (
    FEE_JS
    + f"""
const np = hass.states['{NORDPOOL}'].attributes;
const pub = [...(np.raw_today || []), ...(np.raw_tomorrow || [])]
  .map((p) => [new Date(p.start).getTime(), p.value * {SCALE}, false]);
const last = pub.length ? pub[pub.length - 1][0] : 0;
const plan = hass.states['{PLAN}'].attributes;
const est = (plan.starts || []).map((s, i) => [new Date(s).getTime(), plan.prices[i], (plan.estimated || [])[i]])
  .filter((p) => p[0] > last && p[2]);
const all = [...pub, ...est].map((p) => [p[0], Math.round((p[1] + fee(p[0])) * 10) / 10, p[2]]);
"""
)


def price_series(kind):
    """data_generator for one colour of the price line: low/high (published) or est."""
    body = PRICES_JS + """
const inSpan = all.filter((p) => p[0] >= start.getTime() && p[0] < end.getTime() && !p[2]);
const avg = inSpan.length ? inSpan.reduce((s, p) => s + p[1], 0) / inSpan.length : 0;
"""
    if kind == "est":
        body += """
const firstEst = all.findIndex((p) => p[2]);
if (firstEst < 0) return [];
return all.slice(Math.max(firstEst - 1, 0)).map((p) => [p[0], p[1]]);
"""
    else:
        test = "v > avg" if kind == "high" else "v <= avg"
        body += f"""
const known = all.filter((p) => !p[2]);
return known.map((p, i) => {{
  const v = p[1];
  const prev = i > 0 ? known[i - 1][1] : v;
  const keep = ((v) => {test})(v) || ((v) => {test})(prev);
  return [p[0], keep ? v : null];
}});
"""
    return body


def plan_price_series(key):
    """data_generator for the price line where the plan's mode belongs to `key` of
    PRICE_COLORS[COLORS] ("none": quarters without a plan, before it starts)."""
    groups = {m: k for k, (_, _, modes) in PRICE_COLORS[COLORS].items() for m in modes}
    return PRICES_JS + f"""
const groups = {json.dumps(groups)};
const states = {json.dumps(STATE_MODES)};
const pa = hass.states['{PLAN}'].attributes;
const mode = new Map((pa.starts || []).map((s, i) => [new Date(s).getTime(),
  (pa.manual || [])[i] ? states[pa.manual[i]] : pa.modes[i]]));
const group = (ms) => groups[mode.get(ms)] || 'none';
const known = all.filter((p) => !p[2]);
return known.map((p, i) => {{
  const g = group(p[0]);
  const prev = i > 0 ? group(known[i - 1][0]) : g;
  return [p[0], g === '{key}' || prev === '{key}' ? p[1] : null];
}});
"""


def price_legend():
    """Markdown key to the plan's price colours."""
    items = [*[(c, label) for c, label, _ in PRICE_COLORS[COLORS].values()], NO_PLAN]
    return {"type": "markdown", "grid_options": {"columns": "full"},
            "content": " &nbsp; ".join(f'<font color="{c}">●</font>&nbsp;{label.replace(" ", "&nbsp;")}'
                                                 for c, label in items),
            "card_mod": {"style": {".": "ha-card { border-radius: 20px; border: none; }",
                                   "ha-markdown$": "p { margin: 0; font-size: 13px; opacity: 0.85; }"}}}


def card_mod(extra=""):
    return {
        "style": "ha-card { border-radius: 20px; border: none; overflow: hidden; }" + extra
    }


def apex_base(height, span, offset=None, yaxis=None, xformat="HH"):
    span_cfg = {"start": "day"}
    if offset:
        span_cfg["offset"] = offset
    return {
        "type": "custom:apexcharts-card",
        "graph_span": span,
        "span": span_cfg,
        "update_interval": "5min",
        "now": {"show": True, "color": "#ffffff", "label": ""},
        "header": {"show": False},
        "experimental": {"color_threshold": False},
        "yaxis": yaxis or [{"min": 0, "align_to": 100, "decimals": 0, "apex_config": {"tickAmount": 4}}],
        "apex_config": {
            "chart": {"height": height, "toolbar": {"show": False}, "zoom": {"enabled": False},
                      "background": "transparent"},
            "legend": {"show": False},
            "grid": {"borderColor": "rgba(127,127,127,0.18)", "strokeDashArray": 0,
                     "padding": {"left": 4, "right": 8, "top": 0, "bottom": 0}},
            "xaxis": {"labels": {"format": xformat, "style": {"fontSize": "11px"}},
                      "axisBorder": {"show": False}, "axisTicks": {"show": False},
                      "tooltip": {"enabled": False}},
            "tooltip": {"x": {"format": "ddd HH:mm"}},
            "stroke": {"width": 2},
            "dataLabels": {"enabled": False},
        },
        "card_mod": card_mod(),
        "grid_options": {"columns": "full"},
    }


def area(color, opacity_from=0.45):
    return {"type": "gradient", "gradient": {"type": "vertical", "shadeIntensity": 0,
            "opacityFrom": opacity_from, "opacityTo": 0.02, "stops": [0, 100]}}


def price_chart(height, span, offset=None, est=True, title=None):
    """Tibber-like price chart: one gradient fill (orange high, teal low) under a
    step line coloured teal below the span's average and orange above it."""
    if COLORS != "average":
        return plan_price_chart(height, span, offset, est, title)
    card = apex_base(height, span, offset)
    fill_js = PRICES_JS + "\nreturn all.map((p) => [p[0], p[1]]);\n"
    series = [{
        "entity": NORDPOOL, "name": "fyllning", "type": "area", "curve": "stepline",
        "color": HIGH, "stroke_width": 0, "data_generator": fill_js, "extend_to": False,
        "show": {"in_header": False, "legend_value": False, "in_chart": True},
    }]
    for kind, color in (("low", LOW), ("high", HIGH)) + ((("est", EST),) if est else ()):
        s = {
            "entity": NORDPOOL,
            "name": {"low": "Pris", "high": "Pris", "est": "Prognos"}[kind],
            "type": "line",
            "curve": "stepline",
            "color": color,
            "stroke_width": 2,
            "unit": "öre",
            "float_precision": 0,
            "data_generator": price_series(kind),
            "extend_to": False,
            "show": {"in_header": False, "legend_value": False},
        }
        if kind == "est":
            s["stroke_dash"] = 4
        series.append(s)
    card["series"] = series
    card["apex_config"]["fill"] = {
        "type": ["gradient"] + ["solid"] * (len(series) - 1),
        "opacity": [1] + [0] * (len(series) - 1),
        "colors": [HIGH] + [LOW] * (len(series) - 1),
        "gradient": {"type": "vertical", "shadeIntensity": 0, "gradientToColors": [LOW], "inverseColors": False,
                     "opacityFrom": 0.55, "opacityTo": 0.05, "stops": [0, 100]},
    }
    card["apex_config"]["tooltip"] = {"x": {"format": "ddd HH:mm"},
                                      "enabledOnSeries": list(range(1, len(series)))}
    if title:
        card["header"] = {"show": True, "title": title, "show_states": False}
    return card


def plan_price_chart(height, span, offset=None, est=True, title=None):
    """The price chart coloured by the plan: one step area per colour of PRICE_COLORS[COLORS]
    (each quarter in its mode's colour, grey before the plan) and the estimate dashed."""
    card = apex_base(height, span, offset)
    colors = {**{k: v[:2] for k, v in PRICE_COLORS[COLORS].items()}, "none": NO_PLAN}
    series = [{"entity": NORDPOOL, "name": label, "type": "area", "curve": "stepline", "color": color,
               "stroke_width": 2, "unit": "öre", "float_precision": 0, "data_generator": plan_price_series(key),
               "extend_to": False, "show": {"in_header": False, "legend_value": False}}
              for key, (color, label) in colors.items()]
    if est:
        series.append({"entity": NORDPOOL, "name": "Prognos", "type": "line", "curve": "stepline", "color": EST,
                       "stroke_width": 2, "stroke_dash": 4, "unit": "öre", "float_precision": 0,
                       "data_generator": price_series("est"), "extend_to": False,
                       "show": {"in_header": False, "legend_value": False}})
    card["series"] = series
    n = len(colors)
    card["apex_config"]["fill"] = {
        "type": ["gradient"] * n + ["solid"] * (len(series) - n),
        "opacity": [1] * n + [0] * (len(series) - n),
        "gradient": {"type": "vertical", "shadeIntensity": 0, "opacityFrom": 0.45, "opacityTo": 0.04,
                     "stops": [0, 100]},
    }
    card["apex_config"]["tooltip"] = {"x": {"format": "ddd HH:mm"}}
    if title:
        card["header"] = {"show": True, "title": title, "show_states": False}
    return card


def battery_chart(offset=None):
    """Battery power, kW: charging up, discharging down; the plan hatched."""
    kw = SITE["battery_kw"]
    card = apex_base(170, "24h", offset, yaxis=[{"min": -kw, "max": kw, "decimals": 0, "apex_config": {"tickAmount": 4}}])
    card["header"] = {"show": True, "title": "Batteri kW"}
    card["series"] = [
        {
            "entity": BAT_NET, "name": "Batteri", "type": "area", "color": BATTERY,
            "transform": "return -x / 1000;", "group_by": {"func": "avg", "duration": "5min"},
            "extend_to": "now", "stroke_width": 2,
            "unit": "kW", "float_precision": 1, "show": {"in_header": False},
        },
        {
            "entity": PLAN, "name": "Planerat", "type": "area", "curve": "stepline",
            "color": PLANNED, "unit": "kW", "float_precision": 1, "show": {"in_header": False},
            "extend_to": False,
            "stroke_width": 1,
            "data_generator": f"""
const a = entity.attributes;
const cap = parseFloat(hass.states['{CAPACITY}'].state) / 1000;
let prev = a.soc_start ?? (a.soc || [])[0];
return (a.starts || []).map((s, i) => {{
  const kw = (a.soc[i] - prev) / 100 * cap / 0.25;
  prev = a.soc[i];
  return [new Date(s).getTime(), Math.round(kw * 10) / 10];
}});
""",
        },
    ]
    card["apex_config"]["fill"] = {"type": ["gradient", "pattern"],
                                   "pattern": {"style": "slantedLines", "width": 6, "height": 6,
                                               "strokeWidth": 1},
                                   "gradient": area(None)["gradient"]}
    return card


def solar_chart(offset=None):
    card = apex_base(130, "24h", offset, yaxis=[{"min": 0, "decimals": 1, "apex_config": {"tickAmount": 2}}])
    card["header"] = {"show": True, "title": "Solel kW"}
    card["series"] = [
        {"entity": PV, "name": "Sol", "type": "area", "color": SOLAR, "transform": "return x / 1000;",
         "group_by": {"func": "avg", "duration": "10min"}, "unit": "kW", "float_precision": 1,
         "extend_to": "now",
         "show": {"in_header": False}},
        {"entity": PV_FC, "name": "Prognos", "type": "line", "color": SOLAR, "stroke_dash": 4,
         "unit": "kW", "float_precision": 1, "show": {"in_header": False}, "extend_to": False,
         "data_generator": """
const a = entity.attributes;
return (a.starts || []).map((s, i) => [new Date(s).getTime() + 450000, Math.round(a.pv[i] * 4 * 100) / 100]);
"""},
    ]
    card["apex_config"]["fill"] = {"type": ["gradient", "solid"], "opacity": [1, 0],
                                   "gradient": area(None)["gradient"]}
    return card


def soc_chart(offset=None):
    card = apex_base(130, "24h", offset, yaxis=[{"min": 0, "max": 100, "decimals": 0,
                                                 "apex_config": {"tickAmount": 2}}])
    card["header"] = {"show": True, "title": "Batterinivå %"}
    card["series"] = [
        {"entity": SOC, "name": "Batteri", "type": "area", "color": BATTERY, "unit": "%",
         "group_by": {"func": "last", "duration": "10min"}, "float_precision": 0,
         "extend_to": "now",
         "show": {"in_header": False}},
        {"entity": PLAN, "name": "Plan", "type": "line", "color": PLANNED, "stroke_dash": 4,
         "unit": "%", "float_precision": 0, "show": {"in_header": False}, "extend_to": False,
         "data_generator": """
const a = entity.attributes;
const pts = (a.starts || []).map((s, i) => [new Date(s).getTime() + 900000, a.soc[i]]);
if (a.starts && a.starts.length && a.soc_start != null) pts.unshift([new Date(a.starts[0]).getTime(), a.soc_start]);
return pts;
"""},
    ]
    card["apex_config"]["fill"] = {"type": ["gradient", "solid"], "opacity": [1, 0],
                                   "gradient": area(None)["gradient"]}
    return card


NOW_MD = """{%- set h = now().hour -%}
{%- set p = states('""" + NORDPOOL + """') | float(0) * """ + str(SCALE) + """ + """ + SITE["fee_jinja"] + """ -%}
{%- set ns = namespace(sum=0, n=0) -%}
{%- for r in state_attr('""" + NORDPOOL + """', 'raw_today') or [] -%}
  {%- set h = as_datetime(r.start).hour -%}
  {%- set ns.sum = ns.sum + r.value * """ + str(SCALE) + """ + """ + SITE["fee_jinja"] + """ -%}
  {%- set ns.n = ns.n + 1 -%}
{%- endfor -%}
{%- set avg = ns.sum / ns.n if ns.n else 0 -%}
{%- set q = now().replace(minute=(now().minute // 15) * 15, second=0, microsecond=0) -%}
<ha-icon icon="mdi:lightning-bolt"></ha-icon> **Elpris nu** · {{ q.strftime('%H:%M') }}–{{ (q + timedelta(minutes=15)).strftime('%H:%M') }}

# {{ p | round(0) | int }} <small>öre</small>

{{ '▼' if p <= avg else '▲' }} snitt idag {{ avg | round(0) | int }} öre · inkl. skatt och nätavgift"""

ACTIONS = {"charge": "Laddar", "discharge": "Självförbrukning", "pause": "Vilar",
           "sell": "Säljer", "sell-excess": "Säljer överskott", "discard-excess": "Kastar överskott"}
MODES = {"charge": "Ladda", "self_use": "Självförbrukning", "hold": "Vila", "sell": "Sälj",
         "sell_excess": "Sälj överskott", "discard_excess": "Kasta överskott"}

PLAN_MD = """{%- set a = states.""" + PLAN + """.attributes if states.""" + PLAN + """ else {} -%}
{%- set names = """ + json.dumps(MODES, ensure_ascii=False) + """ -%}
{%- set act = states('sensor.energy_planner_battery_action') -%}
{%- set acts = """ + json.dumps(ACTIONS, ensure_ascii=False) + """ -%}
**Batteriet nu:** {{ acts.get(act, act) }}{% if state_attr('sensor.energy_planner_battery_action','until') %} till {{ as_datetime(state_attr('sensor.energy_planner_battery_action','until')).strftime('%H:%M') }}{% endif %} · {{ states('""" + SOC + """') }} %

{% if a.starts is defined and a.starts -%}
{%- set ns = namespace(rows=[], mode=None, start=None, n=0) -%}
{%- for s in a.starts -%}
  {%- set m = a.modes[loop.index0] -%}
  {%- if m != ns.mode -%}
    {%- if ns.mode is not none and ns.n < 8 -%}
      {%- set ns.rows = ns.rows + [[ns.start, s, ns.mode, a.soc[loop.index0 - 1]]] -%}
      {%- set ns.n = ns.n + 1 -%}
    {%- endif -%}
    {%- set ns.mode = m -%}{%- set ns.start = s -%}
  {%- endif -%}
{%- endfor -%}
| Tid | Plan | SOC |
|:--|:--|--:|
{% for r in ns.rows -%}
| {{ ['mån','tis','ons','tor','fre','lör','sön'][as_datetime(r[0]).weekday()] }} {{ as_datetime(r[0]).strftime('%H:%M') }}–{{ as_datetime(r[1]).strftime('%H:%M') }} | {{ names.get(r[2], r[2]) }} | {{ r[3] | round(0) | int }} % |
{% endfor %}
{%- else -%}
Ingen plan just nu.
{%- endif %}"""

SAVE_MD = """{%- macro kr(entity) -%}{{ '{:,.0f}'.format(states(entity) | float(0)).replace(',', ' ') }}{%- endmacro -%}
<ha-icon icon="mdi:piggy-bank-outline"></ha-icon> **Sparat idag** med sol och batteri

# {{ kr('sensor.elbesparing_idag') }} <small>kr</small>

Betalat {{ kr('sensor.elkostnad_idag') }} kr, utan sol och batteri {{ kr('sensor.elkostnad_utan_sol_och_batteri_idag') }} kr

| Denna månad | """ + SITE["since"] + """ |
|:--|:--|
| **{{ kr('sensor.elbesparing_manad') }} kr** | **{{ kr('sensor.elbesparing_totalt') }} kr** |

<small>""" + SITE["note"] + """</small>"""

MD_STYLE = {
    ".": "ha-card { border-radius: 20px; border: none; padding: 4px 4px 8px; }",
    "ha-markdown$": """
h1 { font-size: 56px; font-weight: 700; line-height: 1.05; margin: 8px 0 6px; letter-spacing: -1px; }
h1 small { font-size: 22px; font-weight: 500; opacity: 0.75; }
p { margin: 2px 0; }
table { width: 100%; border-collapse: collapse; border: none; }
th { text-align: left; opacity: 0.6; font-weight: 500; font-size: 13px; }
td, th { padding: 7px 2px; border: none !important; border-bottom: 1px solid rgba(127,127,127,0.15) !important; }
""",
}


def md_card(content):
    return {"type": "markdown", "content": content, "grid_options": {"columns": "full"},
            "card_mod": {"style": MD_STYLE}}


def tabs(cards_by_label, default=0):
    return {
        "type": "custom:tabbed-card",
        "options": {"defaultTabIndex": default},
        "styles": {"--mdc-theme-primary": "var(--primary-text-color)",
                   "--mdc-tab-text-label-color-default": "var(--secondary-text-color)",
                   "--mdc-typography-button-font-size": "14px"},
        "tabs": [{"card": c if c["type"] == "vertical-stack" else {"type": "vertical-stack", "cards": [c]},
                  "attributes": {"label": label}} for label, c in cards_by_label],
        "grid_options": {"columns": "full"},
    }


def mini_price(offset=None):
    card = price_chart(130, "24h", offset, title="Elpris öre/kWh")
    card["yaxis"] = [{"min": 0, "align_to": 100, "decimals": 0, "apex_config": {"tickAmount": 2}}]
    return card


def day_stack(offset=None):
    return {"type": "vertical-stack", "cards": [
        battery_chart(offset),
        mini_price(offset),
        solar_chart(offset),
        soc_chart(offset),
    ]}


def stat_series(entity, name, color, period, transform=None):
    """Columns of what a counter (or a savings sensor) added per day or month."""
    sr = {"entity": entity, "name": name, "type": "column", "color": color,
          "statistics": {"type": "change", "period": period, "align": "start"},
          "float_precision": 1, "show": {"in_header": False, "legend_value": False}}
    if transform:
        sr["transform"] = transform
    return sr


def history_chart(title, span, period, series, unit, xformat):
    step = 10 if period == "day" else 500
    card = apex_base(170, span, yaxis=[{"decimals": 0, "align_to": step, "apex_config": {"tickAmount": 4}}],
                     xformat=xformat)
    # Bars sit on the start of their day or month: end the span half a period early so
    # the first bar is labelled and no empty period trails
    card["span"] = {"end": "day", "offset": "-12h"} if period == "day" else {"end": "month", "offset": "-15d"}
    card["now"] = {"show": False}
    card["update_interval"] = "30min"
    card["header"] = {"show": True, "title": title}
    for sr in series:
        sr["unit"] = unit
    card["series"] = series
    card["apex_config"]["plotOptions"] = {"bar": {"borderRadius": 3, "columnWidth": "70%"}}
    card["apex_config"]["tooltip"] = {"x": {"format": "ddd d MMM" if period == "day" else "MMMM yyyy"}}
    card["apex_config"]["legend"] = {"show": True, "position": "top", "horizontalAlign": "left",
                                     "fontSize": "12px", "markers": {"size": 5}}
    return card


def history_stack(span, period, xformat):
    cards = [
        history_chart("Kronor", span, period, [
            stat_series(SAVING, "Sparat", LOW, period),
            stat_series(COST, "Betalat", HIGH, period),
        ], "kr", xformat),
        history_chart("Energi kWh", span, period, [
            stat_series(LOAD_DAY, "Förbrukning", BATTERY, period),
            stat_series(PV_DAY, "Sol", SOLAR, period),
        ], "kWh", xformat),
        history_chart("Elnätet kWh", span, period, [
            stat_series(BOUGHT, "Köpt", "#8fa3bf", period),
            stat_series(SOLD, "Sålt", "#f5c542", period, "return -x;"),
        ], "kWh", xformat),
    ]
    if period == "month":
        total = apex_base(150, "540d", yaxis=[{"min": 0, "align_to": 5000, "decimals": 0,
                                                "apex_config": {"tickAmount": 4}}],
                          xformat="MMM")
        total["span"] = {"end": "month"}
        total["now"] = {"show": False}
        total["update_interval"] = "30min"
        total["header"] = {"show": True, "title": "Sparat " + SITE["since"][0].lower() + SITE["since"][1:], "show_states": True,
                           "colorize_states": True}
        total["series"] = [{
            "entity": SAVING, "name": "Totalt", "type": "area", "color": LOW, "unit": "kr",
            "statistics": {"type": "sum", "period": "day"}, "float_precision": 0,
            "show": {"in_header": "before_now", "legend_value": False},
        }]
        total["apex_config"]["fill"] = {"type": "gradient", "gradient": area(None)["gradient"]}
        cards.insert(0, total)
    return {"type": "vertical-stack", "cards": cards}


FLOW = {
    "type": "custom:sunsynk-power-flow-card",
    "cardstyle": "compact",
    "show_solar": True,
    "large_font": False,
    "inverter": {"model": "solis", "modern": False, "colour": "#959595", "autarky": "no",
                 "three_phase": False, "auto_scale": False},
    "battery": {"show_daily": True, "colour": BATTERY, "animation_speed": 6, "auto_scale": False,
                "show_absolute": False, "show_remaining_energy": True, **SITE["flow"]["battery"]},
    "solar": {"show_daily": True, "colour": SOLAR, "animation_speed": 9, "auto_scale": False,
              **SITE["flow"]["solar"]},
    "load": {"show_aux": False, "show_daily": True, "animation_speed": 8, "max_power": 6000,
             "additional_loads": 0},
    "grid": {"show_daily_buy": True, "show_daily_sell": True, "no_grid_colour": "red",
             "animation_speed": 8, "max_power": 6000, "invert_grid": True},
    "entities": {**FLOW_ENTITIES, **SITE["flow"]["entities"]},
    "card_mod": card_mod(),
    "grid_options": {"columns": "full"},
}


def config():
    strip = price_chart(70, "24h", est=False)
    strip["now"] = {"show": True, "color": "#ffffff", "label": ""}
    strip["apex_config"]["yaxis"] = {"show": False}
    strip["yaxis"] = [{"show": False, "min": 0}]
    strip["apex_config"]["grid"] = {"show": False, "padding": {"left": 8, "right": 8, "top": -10, "bottom": -6}}
    strip["apex_config"]["xaxis"]["labels"]["show"] = True
    strip["card_mod"] = card_mod(" ha-card { margin-top: -12px; } ")

    price_section = {"type": "grid", "cards": [
        {"type": "heading", "heading": "Elpris", "heading_style": "title"},
        {"type": "vertical-stack", "grid_options": {"columns": "full"},
         "cards": [md_card(NOW_MD), strip]},
        tabs([("I dag", price_chart(260, "24h")),
              ("Imorgon", price_chart(260, "24h", "+1d")),
              ("2 dagar", price_chart(260, "48h"))]),
        *([price_legend()] if COLORS != "average" else []),
    ]}
    home_section = {"type": "grid", "cards": [
        {"type": "heading", "heading": "Hemmet", "heading_style": "title"},
        FLOW,
    ]}
    savings_section = {"type": "grid", "cards": [
        {"type": "heading", "heading": "Besparing", "heading_style": "title"},
        md_card(SAVE_MD),
        tabs([("Vecka", history_stack("7d", "day", "ddd")),
              ("Månad", history_stack("30d", "day", "d")),
              ("År", history_stack("365d", "month", "MMM"))]),
    ]}
    battery_section = {"type": "grid", "cards": [
        {"type": "heading", "heading": "Batteriaktivitet", "heading_style": "title"},
        tabs([("I dag", day_stack()), ("Imorgon", day_stack("+1d"))]),
        *([price_legend()] if COLORS != "average" else []),
        md_card(PLAN_MD),
    ]}
    return {"title": "Elpris", "views": [{
        "title": "Elpris", "path": "elpris", "icon": "mdi:flash", "type": "sections",
        "max_columns": 3, "dense_section_placement": True,
        "sections": [price_section, savings_section, home_section, battery_section],
    }]}


def package():
    """The savings sensors: cost with the system, without it and the saving, kr/h, integrated
    to kr, with daily / monthly / total meters (calibrated once to the imported history)."""
    spot = f"states('{NORDPOOL}') | float * {SCALE}"
    price = (f"{{% set spot = {spot} %}}\n{{% set h = now().hour %}}\n"
             f"{{% set buy = spot + {SITE['fee_jinja']} %}}\n")
    rate = {"unit_of_measurement": "SEK/h", "state_class": "measurement"}
    imp, exp = SITE["import_kw"], SITE["export_kw"]
    entities_of = lambda expr: expr.split("'")[1]  # noqa: E731
    templates = [
        {"name": "Elkostnad per timme", "unique_id": "elpris_cost_rate", **rate,
         "availability": f"{{{{ has_value('{NORDPOOL}') and has_value('{entities_of(imp)}')"
                         f" and has_value('{entities_of(exp)}') }}}}",
         "state": price + f"{{% set sell = spot / 1.25 + {SITE['sell_jinja']} %}}\n"
                  f"{{{{ ((({imp}) * buy - ({exp}) * sell) / 100) | round(4) }}}}\n"},
        {"name": "Elkostnad utan sol och batteri per timme", "unique_id": "elpris_cost_without_system_rate",
         **rate,
         "availability": f"{{{{ has_value('{NORDPOOL}') and has_value('{LOAD}') }}}}",
         "state": price + f"{{{{ (states('{LOAD}') | float / 1000 * buy / 100) | round(4) }}}}\n"},
        {"name": "Elbesparing per timme", "unique_id": "elpris_saving_rate", **rate,
         "availability": "{{ has_value('sensor.elkostnad_per_timme')"
                         " and has_value('sensor.elkostnad_utan_sol_och_batteri_per_timme') }}",
         "state": "{{ (states('sensor.elkostnad_utan_sol_och_batteri_per_timme') | float"
                  " - states('sensor.elkostnad_per_timme') | float) | round(4) }}"},
    ]
    sums = [("Elkostnad", "elpris_cost", "sensor.elkostnad_per_timme"),
            ("Elkostnad utan sol och batteri", "elpris_cost_without_system",
             "sensor.elkostnad_utan_sol_och_batteri_per_timme"),
            ("Elbesparing", "elpris_saving", "sensor.elbesparing_per_timme")]
    meter = {"net_consumption": True, "periodically_resetting": False}
    return {
        "template": [{"sensor": templates}],
        "sensor": [{"platform": "integration", "name": name, "unique_id": uid, "source": source,
                    "unit_time": "h", "method": "left", "round": 3, "max_sub_interval": {"minutes": 1}}
                   for name, uid, source in sums],
        "utility_meter": {
            "elbesparing_idag": {"source": SAVING, "cycle": "daily", **meter},
            "elbesparing_manad": {"source": SAVING, "cycle": "monthly", **meter},
            "elkostnad_idag": {"source": COST, "cycle": "daily", **meter},
            "elkostnad_utan_sol_och_batteri_idag": {"source": "sensor.elkostnad_utan_sol_och_batteri",
                                                    "cycle": "daily", **meter},
            "elbesparing_totalt": {"source": SAVING, **meter},
        },
    }


PACKAGE_HEADER = """# Generated by build_elpris_dashboard.py --site {site}: what electricity costs with the solar
# panels and the battery, and what the same house load would have cost bought from the grid
# without them. Spot price (Nord Pool, incl. VAT) plus energy tax and transfer fee; selling
# earns spot excl. VAT plus the network compensation. Power charges are not included.
# Copy to the home's packages (packages/elpris_besparing.yaml). The history before the
# sensors existed was imported into their statistics and the meters calibrated.
"""


def dump(data):
    class Dumper(yaml.SafeDumper):
        pass

    def text(dumper, value):
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    Dumper.add_representer(str, text)
    return yaml.dump(data, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=100)


async def push(cfg):
    import aiohttp
    url = os.environ.get("HA_URL") or SITE.get("url")
    token_file = os.environ.get("HA_TOKEN_FILE") or SITE.get("token")
    if not url or not token_file:
        sys.exit(f"--push needs the site's url and token file: {LOCAL_FILE} or HA_URL / HA_TOKEN_FILE")
    token = open(os.path.expanduser(token_file)).read().strip()
    async with aiohttp.ClientSession() as s, s.ws_connect(url + "/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": token})
        print((await ws.receive_json())["type"])
        await ws.send_json({"id": 1, "type": "lovelace/dashboards/list"})
        existing = [d["url_path"] for d in (await ws.receive_json())["result"]]
        if DASHBOARD not in existing:
            await ws.send_json({"id": 2, "type": "lovelace/dashboards/create", "url_path": DASHBOARD,
                                "title": f"Elpris ({COLORS})" if TRIAL else "Elpris", "icon": "mdi:flash", "show_in_sidebar": True,
                                "require_admin": False, "mode": "storage"})
            print("create", await ws.receive_json())
        await ws.send_json({"id": 3, "type": "lovelace/config/save", "url_path": DASHBOARD,
                            "config": cfg})
        print("save", await ws.receive_json())


if __name__ == "__main__":
    cfg = config()
    # The owner's dashboard is the example in the repo; the other homes' (their entity ids)
    # and trials go to local/, which git ignores
    suffix = "" if SITE_NAME == "owner" else "_" + SITE_NAME
    here = HERE if not suffix and not TRIAL else os.path.join(HERE, "local")
    os.makedirs(here, exist_ok=True)
    out = os.path.join(here, f"elpris_dashboard{suffix}{TRIAL.replace('-', '_')}.yaml")
    with open(out, "w") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, width=100)
    pkg = os.path.join(here, f"elpris_savings_package{suffix}.yaml")
    with open(pkg, "w") as f:
        f.write(PACKAGE_HEADER.format(site=SITE_NAME) + dump(package()))
    print("wrote", out, pkg)
    if "--push" in sys.argv:
        asyncio.run(push(cfg))
