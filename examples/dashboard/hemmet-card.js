// "Hemmet just nu": a kitchen-screen card with the house picture and the sun, grid, house and
// battery tiles over it, then strings, grid, inverter and battery details. Copy to
// /config/www/, add /local/hemmet-card.js as a module resource and use it alone in a panel view:
//
//   type: custom:hemmet-card
//   image: /local/hemmet/hus.png
//   pv_power: sensor.x_pv_power            # W
//   pv_today: sensor.x_pv_today            # kWh
//   pv_max: 10000                          # W, full sun bar
//   grid_import: sensor.x_import_power     # W (or grid_power: signed, + = buying,
//                                          #  grid_power_sign: -1 when + = selling)
//   grid_export: sensor.x_export_power     # W
//   grid_max: 11000                        # W, full grid bar
//   bought_today: sensor.x / sold_today: sensor.x   # kWh
//   load_power / load_today / load_max     # house W, kWh, W
//   battery_soc / battery_power            # %, W (magnitude)
//   battery_current: sensor.x              # A, negative = discharging
//   battery_voltage, battery_charged_today, battery_discharged_today
//   battery_capacity: number.x             # Wh, with battery_reserve (%, default 10)
//   strings: [{name: Söder, voltage: sensor.x, current: sensor.x}, ...]
//   inverter_voltage, inverter_current, inverter_frequency, inverter_temperature
//   tiles_below: true                      # wide screens: tiles in a row under the picture
//   image_size: [1024, 768]                # the picture's pixels, the cables' coordinates
//   cables:                                # dots move along d toward the inverter
//     - {tone: pv, line: true, d: "M 425 250 C 430 285 433 312 436 336"}   # pv, grid or battery;
//                                          # line: false when the picture already shows the cable

const SUN = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
const MAST = '<path d="M12 22V4M5 6h14M7 6l5 7 5-7M8 3l4 3 4-3"/>';
const HOME = '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>';
const BATT = '<rect x="2" y="7" width="17" height="10" rx="2"/><path d="M22 11v2M6 10v4M10 10v4"/>';
const BOLT = '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>';
const icon = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;

// dir: +1 along the path (into the inverter), -1 against it, 0 no flow
function cablesSvg(cables, [w, h], dirs, colors) {
  const g = cables.map(({ tone, line: drawn, d }) => {
    const dir = dirs[tone];
    const line = !drawn ? "" : `<path class="glow" d="${d}"/><path class="core" d="${d}"/>`;
    const back = dir < 0 ? ' keyPoints="1;0" keyTimes="0;1" calcMode="linear"' : "";
    const dots = dir === 0 ? "" : [0, -0.9, -1.8].map((t) =>
      `<circle r="${3.5 * w / 1024}" class="dot"><animateMotion dur="2.7s" begin="${t}s" repeatCount="indefinite" path="${d}"${back}/></circle>`).join("");
    return `<g class="cable ${tone}" data-dir="${dir}" style="--c:${colors[tone]}">${line}${dots}</g>`;
  }).join("");
  return `<svg class="cables" style="--k:${w / 1024}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid slice">${g}</svg>`;
}

const fmt = (v, d = 1) => (v == null || isNaN(v) ? "–" : v.toLocaleString("sv-SE", { minimumFractionDigits: d, maximumFractionDigits: d }));
const power = (w) => (w == null ? "–" : Math.abs(w) < 999.5 ? `${fmt(Math.abs(w), 0)} W` : `${fmt(Math.abs(w) / 1000, 2)} kW`);

class HemmetCard extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._key = null;
    this._dirKey = null;
  }

  set hass(hass) {
    this._hass = hass;
    const c = this._config;
    const ids = Object.values(c).flatMap((v) => (Array.isArray(v) ? v.flatMap(Object.values) : [v]));
    const key = ids.map((id) => hass.states[id]?.state).join("|");
    if (key === this._key) return; // re-render only when one of our entities changed
    this._key = key;
    this._render();
  }

  _n(key) {
    const s = this._hass.states[this._config[key]];
    const v = s ? parseFloat(s.state) : NaN;
    return isNaN(v) ? null : v;
  }

  _id(id) {
    const v = parseFloat(this._hass.states[id]?.state);
    return isNaN(v) ? null : v;
  }

  _tile(pos, color, ico, title, value, pct, subs) {
    const bar = Math.max(2, Math.min(100, pct ?? 0));
    return `<div class="tile ${pos}" style="--c:${color}">
      <div class="head">${icon(ico)}<span>${title}</span></div>
      <div class="value">${value}</div>
      <div class="bar"><i style="width:${bar}%"></i></div>
      ${subs.map((s) => `<div class="sub">${s}</div>`).join("")}</div>`;
  }

  _panel(color, ico, title, rows) {
    return `<div class="panel" style="--c:${color}">
      <div class="head">${icon(ico)}<span>${title}</span></div>
      ${rows.map(([l, v, sub]) => `<div class="row"><span>${l}${sub ? `<small>${sub}</small>` : ""}</span><b>${v}</b></div>`).join("")}</div>`;
  }

  _render() {
    const c = this._config;
    const n = (k) => this._n(k);
    const kwh = (k) => `${fmt(n(k))} kWh`;
    const b = (v) => `<b>${v}</b>`;

    const pv = n("pv_power");
    const imp = n("grid_import"), exp = n("grid_export"), gp = n("grid_power");
    let grid = c.grid_power ? (gp == null ? null : (c.grid_power_sign ?? 1) * gp)
      : imp == null && exp == null ? null : (imp ?? 0) - (exp ?? 0);
    if (grid != null && Math.abs(grid) < 50) grid = 0; // meter noise
    const load = n("load_power");
    const soc = n("battery_soc");
    const bp = n("battery_power");
    const cur = n("battery_current");
    const known = bp != null && cur != null; // the direction comes from the current's sign
    const discharging = known && cur < 0;
    const idle = known && Math.abs(bp) < 50;
    const battState = !known ? "–" : idle ? "Vilar" : discharging ? "Laddar ur" : "Laddar";
    const cap = n("battery_capacity");
    const left = cap != null && soc != null ? Math.max(0, (cap / 1000) * (soc - (c.battery_reserve ?? 10)) / 100) : null;

    const gridState = grid == null ? "–" : grid === 0 ? "Inget flöde" : grid > 0 ? "Köper från nätet" : "Säljer till nätet";
    const SOL = "#f5b740", NET = "#2fd4e0", HUS = "#f5b740", BAT = "#3ee0a8";

    const tiles = [
      this._tile("tl", SOL, SUN, "Sol", power(pv), (100 * pv) / (c.pv_max ?? 10000), [`Idag ${b(kwh("pv_today"))}`]),
      this._tile("tr", NET, MAST, "Nätet", power(grid), (100 * Math.abs(grid)) / (c.grid_max ?? 11000), [gridState]),
      this._tile("bl", HUS, HOME, "Huset", power(load), (100 * load) / (c.load_max ?? 11000), [`Idag ${b(kwh("load_today"))}`]),
      this._tile("br", BAT, BATT, "Batteri", `${fmt(soc, 0)}%`, soc, [
        battState,
        !known || idle ? "" : `Uppmätt ${discharging ? "urladdning" : "laddning"} ${b(power(bp))}`,
      ]),
    ].join("");

    const strings = (c.strings ?? []).map((s) => {
      const v = this._id(s.voltage), a = this._id(s.current);
      return [s.name, power(v != null && a != null ? v * a : null), `${fmt(a)} A · ${fmt(v, 0)} V`];
    });
    const panels = [
      this._panel(SOL, SUN, "Solsträngar", strings),
      this._panel(NET, MAST, "Nätet", [["Sålt idag", kwh("sold_today")], ["Köpt idag", kwh("bought_today")]]),
      this._panel(SOL, BOLT, "Växelriktare", [
        ["Spänning / ström", `${fmt(n("inverter_voltage"), 0)} V · ${fmt(n("inverter_current"))} A`],
        ["Frekvens / temp.", `${fmt(n("inverter_frequency"), 2)} Hz · ${fmt(n("inverter_temperature"), 0)}°`],
      ]),
      this._panel(BAT, BATT, "Batteri", [
        ["Spänning / ström", `${fmt(n("battery_voltage"), 0)} V · ${fmt(cur == null ? null : Math.abs(cur))} A`],
        ["Laddat idag", kwh("battery_charged_today")],
        ["Urladdat idag", kwh("battery_discharged_today")],
        [`Kvar till ${c.battery_reserve ?? 10}%`, `${fmt(left)} kWh`],
      ]),
    ].join("");

    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" }).innerHTML = `<style>${CSS}</style>
        <div class="wrap"><h1>Hemmet just nu</h1>
          <div class="layout">
            <div class="hero"><div class="scene"><img alt=""><div class="cables-box"></div></div><div class="tiles"></div></div>
            <div class="panels"></div>
          </div></div>`;
    }
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    if ($("img").getAttribute("src") !== c.image) $("img").setAttribute("src", c.image); // setConfig may change it
    $(".hero").classList.toggle("below", !!c.tiles_below);
    $(".tiles").innerHTML = tiles;
    $(".panels").innerHTML = panels;
    const sign = (w) => (w != null && Math.abs(w) > 50 ? Math.sign(w) : 0);
    const dirs = { pv: sign(pv), grid: sign(grid), battery: !known || idle ? 0 : discharging ? 1 : -1 };
    const dirKey = JSON.stringify(dirs);
    if (dirKey !== this._dirKey) { // rebuilding the cables restarts their dots, so only on a change
      this._dirKey = dirKey;
      $(".cables-box").innerHTML = cablesSvg(c.cables ?? [], c.image_size ?? [1024, 768], dirs, { pv: SOL, grid: NET, battery: BAT });
    }
  }

  getCardSize() {
    return 12;
  }
}

const CSS = `
:host { display:block; container-type:inline-size; }
/* the panel view sits under HA's header */
.wrap { min-height:calc(100vh - var(--header-height, 56px)); box-sizing:border-box; padding:24px;
  background:radial-gradient(ellipse at 50% 30%, #13213a 0%, #070c16 70%); color:#e8edf5;
  font-family:Manrope, system-ui, sans-serif; }
h1 { font-family:"Bricolage Grotesque", Manrope, sans-serif; font-weight:700; font-size:2rem; margin:0 0 16px; }
.layout { display:grid; gap:20px; }
.hero { position:relative; border-radius:18px; overflow:hidden; background:#081020; }
.hero { aspect-ratio:4/3; }
.scene { position:absolute; inset:0; }
.scene img, .cables { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
/* tiles_below: a row of tiles under the uncovered picture (wide screens only) */
.below { aspect-ratio:auto; } .below .scene { position:relative; aspect-ratio:4/3; }
.below .tiles { display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; padding:12px; }
.below .tile { position:static; width:auto; padding:12px 14px; }
.below .value { font-size:1.7rem; white-space:nowrap; margin:4px 0 8px; } .below .sub { font-size:.85rem; }
.cable path { fill:none; stroke:var(--c); stroke-linecap:round; }
.cable .glow { stroke-width:calc(7px * var(--k)); opacity:.5; filter:blur(5px); }
.cable .core { stroke-width:calc(1.5px * var(--k)); opacity:.8; }
.cable[data-dir="0"] .glow { opacity:0; } .cable[data-dir="0"] .core { stroke:#7c889b; opacity:.2; }
.cable.grid[data-dir="0"] .glow { opacity:.35; } .cable.grid[data-dir="0"] .core { stroke:var(--c); opacity:.65; }
.dot { fill:var(--c); filter:drop-shadow(0 0 5px var(--c)); }
@media (prefers-reduced-motion: reduce) { .dot { display:none; } }
.tile { position:absolute; width:38%; box-sizing:border-box; padding:14px 18px; border-radius:14px;
  background:rgba(12,20,34,.82); border:1px solid rgba(255,255,255,.08); backdrop-filter:blur(6px); }
.tl { top:3%; left:3%; } .tr { top:3%; right:3%; } .bl { bottom:3%; left:3%; } .br { bottom:3%; right:3%; }
.head { display:flex; align-items:center; gap:10px; color:var(--c); font-weight:600; font-size:1.05rem; }
.head svg { width:24px; height:24px; }
.value { font-family:"Bricolage Grotesque", Manrope, sans-serif; font-weight:700; font-size:2.3rem; margin:6px 0 10px; }
.bar { height:6px; border-radius:3px; background:rgba(255,255,255,.1); margin-bottom:10px; }
.bar i { display:block; height:100%; border-radius:3px; background:var(--c); transition:width .6s; }
.sub { color:#9aa7ba; font-size:.95rem; margin-top:4px; min-height:1.2em; } .sub b { color:#e8edf5; font-weight:600; }
.panels { display:grid; grid-template-columns:1fr 1fr; border-top:1px solid rgba(255,255,255,.08); }
.panel { padding:20px 24px; border-bottom:1px solid rgba(255,255,255,.08); }
.panel:nth-child(odd) { border-right:1px solid rgba(255,255,255,.08); }
.panel .head { margin-bottom:12px; font-size:1.15rem; }
.row { display:flex; justify-content:space-between; gap:12px; padding:7px 0; color:#aab5c6; font-size:1.05rem; }
.row > span { white-space:nowrap; }
.row b { color:#e8edf5; font-weight:500; white-space:nowrap; }
.row small { display:block; color:#7c889b; font-size:.9rem; margin-top:2px; }
@container (min-width: 1100px) {
  .layout { grid-template-columns:3fr 2fr; align-items:start; }
  .panels { border-top:0; }
}
@container (max-width: 600px) {
  .wrap { padding:16px 12px; } h1 { font-size:1.6rem; }
  /* phone: a taller scene, the picture in its middle and the tiles above and below it */
  .hero, .below { aspect-ratio:4/5; }
  .scene, .below .scene { position:absolute; inset:13% 0 auto; height:74%; aspect-ratio:auto; }
  .below .tiles { display:block; padding:0; } .below .tile { position:absolute; width:47%; }
  .tile { width:47%; padding:10px 12px; border-radius:12px; }
  .tl, .tr { top:2%; } .bl, .br { bottom:2%; } .tl, .bl { left:2%; } .tr, .br { right:2%; }
  .head { font-size:.9rem; gap:6px; } .head svg { width:18px; height:18px; }
  .value { font-size:1.6rem; margin:2px 0 6px; } .bar { height:4px; margin-bottom:6px; }
  .sub { font-size:.78rem; margin-top:2px; }
  .panels { grid-template-columns:1fr; } .panel { padding:16px 8px; } .panel:nth-child(odd) { border-right:0; }
  .row { font-size:.95rem; padding:5px 0; }
}`;

if (!document.querySelector('link[href*="Bricolage+Grotesque"]')) {
  const font = document.createElement("link");
  font.rel = "stylesheet";
  font.href = "https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=Manrope:wght@400;500;600&display=swap";
  document.head.appendChild(font);
}
if (!customElements.get("hemmet-card")) customElements.define("hemmet-card", HemmetCard); // the resource may load twice
