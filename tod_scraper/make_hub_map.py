#!/usr/bin/env python3
"""
Build an interactive HTML map of the TOD Israel *hub* stations.

Reads the `stations` sheet of data/out/tod_israel.xlsx, keeps rows where hub_id
and type_sum are not null, and writes a self-contained Leaflet page:

  * one toggleable layer per type_sum (number of distinct modes at the hub),
    colour + marker size encode the group;
  * popup with the station name, hub id, n_modes and the modes served
    (from is_Metro / is_LRT / is_BRT / is_Rail / is_Railfast / is_Funicular);
  * transit lines and municipal boundaries embedded (simplified) as context so
    the map still reads where basemap tiles cannot load.

    python make_hub_map.py [--data data] [--leaflet-css PATH] [--leaflet-js URL]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd

MODES = [("is_Metro", "Metro"), ("is_LRT", "LRT"), ("is_BRT", "BRT"),
         ("is_Rail", "Rail"), ("is_Railfast", "Rail fast"), ("is_Funicular", "Funicular")]
LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"


def r6(x: float) -> float:
    return round(float(x), 6)


def simplify_layer(gdf: gpd.GeoDataFrame, tol_m: float, keep: list[str]) -> dict:
    g = gdf.to_crs("EPSG:2039")
    g["geometry"] = g.geometry.simplify(tol_m, preserve_topology=True)
    g = g.to_crs("EPSG:4326")
    g = g[keep + ["geometry"]]
    fc = json.loads(g.to_json(drop_id=True))
    # trim coordinate precision (5 dp ~ 1 m)
    def trim(c):
        return [trim(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(c[0], 5), round(c[1], 5)]
    for f in fc["features"]:
        f["geometry"]["coordinates"] = trim(f["geometry"]["coordinates"])
    return fc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default=None, help="output html (default <data>/out/hub_stations_map.html)")
    ap.add_argument("--leaflet-css", required=True, help="path to leaflet.css to inline")
    ap.add_argument("--leaflet-js", default=LEAFLET_JS, help="script src for leaflet.js")
    ap.add_argument("--copy-to", default=None,
                    help="also write the page to this path (e.g. the repo-root index.html served by GitHub Pages)")
    ap.add_argument("--fragment", action="store_true",
                    help="emit only the page body (for the claude.ai artifact wrapper) instead of a full document")
    args = ap.parse_args()
    data = Path(args.data)
    out = Path(args.out) if args.out else data / "out" / "hub_stations_map.html"

    # ---- stations -----------------------------------------------------------------------------
    st = pd.read_excel(data / "out" / "tod_israel.xlsx", sheet_name="stations")
    hubs = st[st["hub_id"].notna() & st["type_sum"].notna()].copy()
    hubs["type_sum"] = hubs["type_sum"].astype(int)
    rows = []
    for _, r in hubs.iterrows():
        rows.append({
            "id": int(r["id"]) if pd.notna(r["id"]) else None,
            "hub": int(r["hub_id"]),
            "name": (str(r["name"]).strip() if pd.notna(r["name"]) else "").strip(" -") or f"Hub {int(r['hub_id'])}",
            "ts": int(r["type_sum"]),
            "n": int(r["n_modes"]),
            "modes": [label for col, label in MODES if col in hubs.columns and int(r[col] or 0) == 1],
            "lines": int(r["line_sum"]) if pd.notna(r.get("line_sum")) else None,
            "status": r.get("planning_status") if pd.notna(r.get("planning_status")) else None,
            "year": int(r["yearOperation"]) if pd.notna(r.get("yearOperation")) else None,
            "muni": r.get("municipality") if pd.notna(r.get("municipality")) else None,
            "metro": r.get("metropolin") if pd.notna(r.get("metropolin")) else None,
            "lat": r6(r["lat"]), "lng": r6(r["lng"]),
        })
    counts = hubs["type_sum"].value_counts().sort_index().to_dict()

    # ---- context layers -------------------------------------------------------------------------
    gpkg = data / "out" / "tod_israel.gpkg"
    lines = simplify_layer(gpd.read_file(gpkg, layer="lines"), 40, ["mode"])
    munis = simplify_layer(gpd.read_file(gpkg, layer="municipalities"), 120, ["Muni_Heb"])
    munis["features"] = [f for f in munis["features"] if f["geometry"]]

    leaflet_css = Path(args.leaflet_css).read_text(encoding="utf-8")
    leaflet_css = re.sub(r"url\([^)]*\)", "none", leaflet_css)      # no external images

    html = TEMPLATE
    for k, v in {
        "__LEAFLET_CSS__": leaflet_css,
        "__LEAFLET_JS__": args.leaflet_js,
        "__STATIONS__": json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        "__COUNTS__": json.dumps({int(k): int(v) for k, v in counts.items()}),
        "__LINES__": json.dumps(lines, ensure_ascii=False, separators=(",", ":")),
        "__MUNIS__": json.dumps(munis, ensure_ascii=False, separators=(",", ":")),
        "__N_HUBS__": f"{len(rows):,}",
        "__N_STATIONS__": f"{len(st):,}",
    }.items():
        html = html.replace(k, v)
    if not args.fragment:
        html = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                + html.replace("\n<div id=\"map\"", "\n</head>\n<body>\n<div id=\"map\"", 1)
                + "\n</body>\n</html>\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    if args.copy_to:
        Path(args.copy_to).write_text(html, encoding="utf-8")
        print(f"copied to {args.copy_to}")
    print(f"wrote {out} ({out.stat().st_size/1e6:.2f} MB): {len(rows)} hub stations, "
          f"{len(lines['features'])} line segments, {len(munis['features'])} municipalities; type_sum counts {counts}")
    return 0


TEMPLATE = r"""<title>TOD Israel Hubs</title>
<meta name="description" content="Hub stations of the TOD Israel transit network, grouped by the number of transit modes they combine.">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;700&display=swap">
<style>
__LEAFLET_CSS__
:root {
  color-scheme: light;
  --bg: #f4f3ef; --panel: #fffefb; --panel-border: #dcd9d0; --ink: #1c1b18; --ink-2: #5b5950; --ink-3: #8a877c;
  --map-bg: #e9e9e9; --muni-line: #a3a3a3; --muni-fill: #f4f4f4; --shadow: 0 4px 18px rgba(30,28,20,.16);
  --accent: #2a78d6; --focus: #2a78d6;
  --g1: #2a78d6; --g2: #eb6834; --g3: #1baf7a; --g4: #eda100; --g5: #e87ba4; --g6: #008300;
  --ln-metro: #d3286f; --ln-lrt: #1f6b5c; --ln-brt: #2fb3a3; --ln-rail: #2d3f6b; --ln-funi: #7b4ac7;
  --ring: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #17171a; --panel: #212125; --panel-border: #3a3a40; --ink: #f2f1ea; --ink-2: #c3c2b7; --ink-3: #8b8a82;
    --map-bg: #e9e9e9; --muni-line: #a3a3a3; --muni-fill: #f4f4f4; --shadow: 0 4px 18px rgba(0,0,0,.5);
    --accent: #3987e5; --focus: #3987e5;
    --g1: #3987e5; --g2: #d95926; --g3: #199e70; --g4: #c98500; --g5: #d55181; --g6: #008300;
    --ln-metro: #e0508a; --ln-lrt: #3f9c88; --ln-brt: #3fc7b6; --ln-rail: #7d93c9; --ln-funi: #a684e0;
    --ring: #17171a;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #17171a; --panel: #212125; --panel-border: #3a3a40; --ink: #f2f1ea; --ink-2: #c3c2b7; --ink-3: #8b8a82;
  --map-bg: #e9e9e9; --muni-line: #a3a3a3; --muni-fill: #f4f4f4; --shadow: 0 4px 18px rgba(0,0,0,.5);
  --accent: #3987e5; --focus: #3987e5;
  --g1: #3987e5; --g2: #d95926; --g3: #199e70; --g4: #c98500; --g5: #d55181; --g6: #008300;
  --ln-metro: #e0508a; --ln-lrt: #3f9c88; --ln-brt: #3fc7b6; --ln-rail: #7d93c9; --ln-funi: #a684e0;
  --ring: #17171a;
}
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.4 "Heebo", "Segoe UI", system-ui, sans-serif; }
#map { position: absolute; inset: 0; background: var(--map-bg); }
.leaflet-container { background: var(--map-bg); font: inherit; }
.osm-gray { filter: grayscale(1) brightness(1.06) contrast(0.88); }
:root[data-theme="dark"] .osm-gray { filter: grayscale(1) brightness(1.06) contrast(0.88); }
.leaflet-bar { box-shadow: var(--shadow); border: 1px solid var(--panel-border); }
.leaflet-bar a { background: var(--panel); color: var(--ink); border-bottom-color: var(--panel-border); }
.leaflet-bar a:hover, .leaflet-bar a:focus-visible { background: var(--bg); }
.leaflet-control-attribution { background: color-mix(in srgb, var(--panel) 85%, transparent); color: var(--ink-3); font-size: 11px; }
.leaflet-control-attribution a { color: var(--ink-2); }
.leaflet-popup-content-wrapper, .leaflet-popup-tip { background: var(--panel); color: var(--ink); box-shadow: var(--shadow); }
.leaflet-popup-content-wrapper { border-radius: 8px; border: 1px solid var(--panel-border); }
.leaflet-popup-content { margin: 12px 14px; font-size: 13px; min-width: 200px; }
.leaflet-container a.leaflet-popup-close-button { color: var(--ink-3); }
.leaflet-container :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

/* ---- panel ------------------------------------------------------------------------------- */
.panel {
  position: absolute; z-index: 1000; top: calc(12px + env(safe-area-inset-top, 0px)); left: 12px; width: 300px; max-width: calc(100% - 24px);
  background: var(--panel); border: 1px solid var(--panel-border); border-radius: 10px; box-shadow: var(--shadow);
  padding: 14px 16px 12px; box-sizing: border-box;
}
.panel h1 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -.01em; text-wrap: balance; }
.panel .sub { margin: 2px 0 0; color: var(--ink-2); font-size: 12.5px; }
.panel .sub b { color: var(--ink); font-weight: 500; font-variant-numeric: tabular-nums; }
.eyebrow { margin: 14px 0 6px; font-size: 11px; font-weight: 500; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }
.groups { display: grid; gap: 2px; margin: 0; padding: 0; list-style: none; }
.groups label { display: grid; grid-template-columns: 18px 22px 1fr auto; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 6px; cursor: pointer; }
.groups label:hover { background: var(--bg); }
.groups input { margin: 0; width: 15px; height: 15px; accent-color: var(--accent); }
.groups input:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.swatch { justify-self: center; border-radius: 50%; border: 1.5px solid var(--ring); box-shadow: 0 0 0 1px var(--panel-border); }
.groups .lbl { font-weight: 500; }
.groups .cnt { color: var(--ink-2); font-variant-numeric: tabular-nums; font-size: 12.5px; }
.groups .cnt small { color: var(--ink-3); }
.ctx { display: grid; gap: 4px; margin-top: 2px; }
.ctx label { display: flex; align-items: center; gap: 8px; padding: 3px 6px; border-radius: 6px; cursor: pointer; color: var(--ink-2); }
.ctx label:hover { background: var(--bg); }
.ctx input { margin: 0; width: 15px; height: 15px; accent-color: var(--accent); }
.ctx .line-key { display: inline-flex; gap: 4px; margin-left: auto; }
.ctx .line-key i { display: inline-block; width: 14px; height: 3px; border-radius: 2px; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.actions button { flex: 1 1 auto; white-space: nowrap; font: inherit; font-size: 12.5px; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--panel-border); background: var(--bg); color: var(--ink); cursor: pointer; }
.actions button:hover { border-color: var(--ink-3); }
.actions button:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.note { margin: 10px 0 0; font-size: 11.5px; color: var(--ink-3); }
.panel-toggle { display: none; }

/* ---- popup ------------------------------------------------------------------------------- */
.pp h2 { margin: 0 0 2px; font-size: 15px; font-weight: 700; line-height: 1.25; }
.pp .meta { color: var(--ink-2); font-size: 12px; margin-bottom: 8px; }
.pp .meta span + span::before { content: " · "; color: var(--ink-3); }
.pp .kv { display: grid; grid-template-columns: auto 1fr; gap: 3px 10px; font-size: 12.5px; }
.pp .kv dt { color: var(--ink-3); }
.pp .kv dd { margin: 0; font-variant-numeric: tabular-nums; }
.pp .modes { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }
.chip { display: inline-flex; align-items: center; gap: 5px; padding: 1px 7px 1px 5px; border-radius: 99px; border: 1px solid var(--panel-border); background: var(--bg); font-size: 12px; font-weight: 500; white-space: nowrap; }
.chip i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.pp .n { font-weight: 700; }

@media (max-width: 560px) {
  .panel { top: auto; bottom: calc(12px + env(safe-area-inset-bottom, 0px)); left: 12px; right: 12px; width: auto; max-height: 46%; overflow: auto; }
  .panel-toggle { display: block; position: absolute; top: 8px; right: 12px; font: inherit; font-size: 12px; border: 1px solid var(--panel-border); background: var(--bg); color: var(--ink-2); border-radius: 6px; padding: 3px 8px; cursor: pointer; }
  .panel.collapsed > :not(h1):not(.sub):not(.panel-toggle) { display: none; }
  .leaflet-top.leaflet-left { top: env(safe-area-inset-top, 0px); }
}
@media (prefers-reduced-motion: reduce) { .leaflet-zoom-anim .leaflet-zoom-animated, .leaflet-fade-anim .leaflet-popup { transition: none; } }
</style>

<div id="map" role="application" aria-label="Map of TOD Israel hub stations"></div>

<section class="panel" id="panel" aria-label="Layers and legend">
  <button class="panel-toggle" id="panel-toggle" type="button" aria-expanded="true">Hide</button>
  <h1>TOD Israel hub stations</h1>
  <p class="sub"><b>__N_HUBS__</b> hubs out of <b>__N_STATIONS__</b> planned stations · click a marker for its modes</p>

  <p class="eyebrow">Modes combined at the hub (type_sum)</p>
  <ul class="groups" id="groups"></ul>

  <p class="eyebrow">Context</p>
  <div class="ctx">
    <label><input type="checkbox" id="ctx-lines" checked> Transit lines
      <span class="line-key" aria-hidden="true"><i style="background:var(--ln-metro)"></i><i style="background:var(--ln-lrt)"></i><i style="background:var(--ln-brt)"></i><i style="background:var(--ln-rail)"></i><i style="background:var(--ln-funi)"></i></span></label>
    <label><input type="checkbox" id="ctx-munis" checked> Municipal boundaries</label>
    <label><input type="checkbox" id="ctx-tiles" checked> OpenStreetMap (light gray)</label>
  </div>

  <div class="actions">
    <button type="button" id="btn-all">All hubs</button>
    <button type="button" id="btn-multi">2+ modes</button>
    <button type="button" id="btn-fit">Zoom to hubs</button>
  </div>
  <p class="note">Marker size grows with type_sum. Source: TOD Israel explorer (beta), scraped 2026-09-22. Lines and boundaries are simplified for display.</p>
</section>

<script src="__LEAFLET_JS__"></script>
<script>
(function () {
  const STATIONS = __STATIONS__;
  const COUNTS = __COUNTS__;
  const LINES = __LINES__;
  const MUNIS = __MUNIS__;

  const css = getComputedStyle(document.documentElement);
  const tok = n => css.getPropertyValue(n).trim();
  const GROUPS = [1, 2, 3, 4, 5, 6];
  const RADIUS = {1: 4.5, 2: 6, 3: 7.5, 4: 9, 5: 10.5, 6: 12};
  const MODE_COLOR = {"Metro": "--ln-metro", "LRT": "--ln-lrt", "BRT": "--ln-brt", "Rail": "--ln-rail", "Rail fast": "--ln-rail", "Funicular": "--ln-funi", "FUNI": "--ln-funi"};

  const map = L.map('map', {zoomControl: false, preferCanvas: true, attributionControl: true});
  L.control.zoom({position: 'topright'}).addTo(map);
  map.attributionControl.setPrefix('');
  map.attributionControl.addAttribution('Data: TOD Israel explorer (beta) · Basemap © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors');
    // OpenStreetMap standard tiles, rendered light gray through a CSS filter (same look in both themes)
  const OSM = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
  const makeTiles = () => L.tileLayer(OSM, {maxZoom: 19, className: 'osm-gray', opacity: 1});
  let tiles = makeTiles().addTo(map);

  // ---- context layers --------------------------------------------------------------------
  const munisLayer = L.geoJSON(MUNIS, {
    style: () => ({color: tok('--muni-line'), weight: 0.7, fillColor: tok('--muni-fill'), fillOpacity: 0, interactive: false})
  }).addTo(map);
  const linesLayer = L.geoJSON(LINES, {
    style: f => ({color: tok(MODE_COLOR[f.properties.mode] || '--ink-3'), weight: 1.6, opacity: 0.55, interactive: false})
  }).addTo(map);

  // ---- hub groups ----------------------------------------------------------------------------
  const groupLayers = {};
  const ul = document.getElementById('groups');
  const modeChip = m => `<span class="chip"><i style="background:var(${MODE_COLOR[m] || '--ink-3'})"></i>${m}</span>`;
  const popupHtml = s => `
    <div class="pp" dir="auto">
      <h2>${escapeHtml(s.name)}</h2>
      <div class="meta"><span>Hub ${s.hub}</span>${s.status ? `<span>${escapeHtml(s.status)}</span>` : ''}${s.year ? `<span>${s.year}</span>` : ''}</div>
      <dl class="kv">
        <dt>n_modes</dt><dd class="n">${s.n}</dd>
        <dt>modes</dt><dd><div class="modes">${s.modes.map(modeChip).join('') || '<span class="chip">none listed</span>'}</div></dd>
        <dt>type_sum</dt><dd>${s.ts}</dd>
        ${s.lines != null ? `<dt>lines</dt><dd>${s.lines}</dd>` : ''}
        ${s.muni ? `<dt>municipality</dt><dd>${escapeHtml(s.muni)}</dd>` : ''}
        ${s.metro ? `<dt>metropolin</dt><dd>${escapeHtml(s.metro)}</dd>` : ''}
      </dl>
    </div>`;
  function escapeHtml(t) { return String(t).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  GROUPS.forEach(g => {
    const color = tok('--g' + g);
    const layer = L.layerGroup();
    STATIONS.filter(s => s.ts === g).forEach(s => {
      const m = L.circleMarker([s.lat, s.lng], {
        radius: RADIUS[g], color: tok('--ring'), weight: 1.5, fillColor: color, fillOpacity: 0.92, pane: 'markerPane'
      });
      m.bindPopup(popupHtml(s), {maxWidth: 320});
      m.bindTooltip(`${s.name} · ${s.modes.join(', ')}`, {direction: 'top', offset: [0, -RADIUS[g]], opacity: 0.95});
      layer.addLayer(m);
    });
    groupLayers[g] = layer;
    const n = COUNTS[g] || 0;
    if (n) layer.addTo(map);
    const li = document.createElement('li');
    const d = RADIUS[g] * 2;
    li.innerHTML = `<label><input type="checkbox" id="grp-${g}" ${n ? 'checked' : 'disabled'}>
      <span class="swatch" style="width:${d}px;height:${d}px;background:${color}"></span>
      <span class="lbl">${g} mode${g > 1 ? 's' : ''}</span>
      <span class="cnt">${n.toLocaleString()} <small>hub${n === 1 ? '' : 's'}</small></span></label>`;
    ul.appendChild(li);
    li.querySelector('input').addEventListener('change', e => e.target.checked ? layer.addTo(map) : map.removeLayer(layer));
  });

  // ---- controls --------------------------------------------------------------------------------
  const setGroups = pred => GROUPS.forEach(g => {
    const cb = document.getElementById('grp-' + g);
    if (cb.disabled) return;
    cb.checked = pred(g);
    cb.checked ? groupLayers[g].addTo(map) : map.removeLayer(groupLayers[g]);
  });
  document.getElementById('btn-all').addEventListener('click', () => setGroups(() => true));
  document.getElementById('btn-multi').addEventListener('click', () => setGroups(g => g >= 2));
  const hubBounds = L.latLngBounds(STATIONS.map(s => [s.lat, s.lng]));
  document.getElementById('btn-fit').addEventListener('click', () => map.fitBounds(hubBounds, {padding: [30, 30]}));
  document.getElementById('ctx-lines').addEventListener('change', e => e.target.checked ? linesLayer.addTo(map) : map.removeLayer(linesLayer));
  document.getElementById('ctx-munis').addEventListener('change', e => e.target.checked ? munisLayer.addTo(map) : map.removeLayer(munisLayer));
  document.getElementById('ctx-tiles').addEventListener('change', e => {
    e.target.checked ? tiles.addTo(map) : map.removeLayer(tiles);
    munisLayer.setStyle({fillOpacity: e.target.checked ? 0 : 0.55});   // boundaries carry the ground when tiles are off
  });
  const panel = document.getElementById('panel'), tg = document.getElementById('panel-toggle');
  tg.addEventListener('click', () => { const c = panel.classList.toggle('collapsed'); tg.textContent = c ? 'Show' : 'Hide'; tg.setAttribute('aria-expanded', String(!c)); });
  // keep the panel from stealing map drags
  L.DomEvent.disableClickPropagation(panel); L.DomEvent.disableScrollPropagation(panel);

  // theme changes: restyle vectors and swap the basemap
  const restyle = () => {
    munisLayer.setStyle({color: tok('--muni-line'), fillColor: tok('--muni-fill')});
    linesLayer.setStyle(f => ({color: tok(MODE_COLOR[f.properties.mode] || '--ink-3')}));
    GROUPS.forEach(g => groupLayers[g].eachLayer(m => m.setStyle({fillColor: tok('--g' + g), color: tok('--ring')})));
  };
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', restyle);
  new MutationObserver(restyle).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});

  map.fitBounds(hubBounds, {padding: [30, 30]});
  // 1-mode hubs are the most numerous: draw them under the multi-modal ones
  [6, 5, 4, 3, 2, 1].forEach(g => groupLayers[g].eachLayer(m => m.bringToFront && m.bringToFront()));
})();
</script>
"""

if __name__ == "__main__":
    raise SystemExit(main())
