#!/usr/bin/env python3
"""
Compare an external hub list (xlsx with lon/lat columns) against the multi-modal
hubs of the TOD Israel map (stations with hub_id set and n_modes >= 2).

Matching is spatial: each map hub is paired with the nearest external hub within
--tolerance metres (EPSG:2039). Three sets come out:

    both        map hub <-> external hub pairs (with the offset between them)
    only_xlsx   external hubs with no map hub within the tolerance
    only_map    map hubs with no external hub within the tolerance

plus `possible` (nearest neighbour between the tolerance and --near metres) for
manual review. Writes an Excel workbook and an interactive HTML map that draws
every pair one on top of the other with a tie line showing the offset.

    python compare_hubs.py PRIORITIZATION.xlsx --leaflet-css leaflet.css \
        [--tolerance 400] [--near 1000] [--min-modes 2] [--copy-map-to ../compare.html]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

ITM = "EPSG:2039"
# external mode vocabulary -> map vocabulary
MODE_MAP = {"Metro": "Metro", "LRT": "LRT", "BRT": "BRT", "Interurban Rail": "Rail", "Suburban Rail": "Rail",
            "HighSpeed Rail": "Rail fast", "Funicular": "Funicular", "Cable Line": "Cable"}
MODE_ORDER = ["Metro", "LRT", "BRT", "Rail", "Rail fast", "Funicular", "Cable"]


def norm_modes(v) -> list[str]:
    if isinstance(v, str) and v.startswith("["):
        try:
            v = ast.literal_eval(v)
        except Exception:  # noqa: BLE001
            v = re.findall(r"'([^']+)'", v)
    if isinstance(v, str):
        v = [m.strip() for m in re.split(r"[|,]", v)]
    out = sorted({MODE_MAP.get(m, m) for m in (v or []) if m}, key=lambda m: MODE_ORDER.index(m) if m in MODE_ORDER else 99)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx", help="external hub list (needs x/y or lon/lat columns in WGS84)")
    ap.add_argument("--data", default="data")
    ap.add_argument("--tolerance", type=float, default=400, help="match radius in metres")
    ap.add_argument("--near", type=float, default=1000, help="upper radius for 'possible' matches")
    ap.add_argument("--min-modes", type=int, default=2)
    ap.add_argument("--leaflet-css", required=True)
    ap.add_argument("--leaflet-js", default="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js")
    ap.add_argument("--copy-map-to", default=None)
    ap.add_argument("--fragment", action="store_true")
    args = ap.parse_args()
    data = Path(args.data)

    # ---- inputs ---------------------------------------------------------------------------------
    ext = pd.read_excel(args.xlsx)
    xcol = next(c for c in ("x", "lon", "lng", "longitude") if c in ext.columns)
    ycol = next(c for c in ("y", "lat", "latitude") if c in ext.columns)
    ext = ext.reset_index(drop=True)
    ext["xlsx_row"] = ext.index + 2                       # Excel row number for easy lookup
    ext["modes_norm"] = ext["Mode_Planned"].map(norm_modes) if "Mode_Planned" in ext.columns else [[]] * len(ext)
    gx = gpd.GeoDataFrame(ext, geometry=gpd.points_from_xy(ext[xcol], ext[ycol]), crs="EPSG:4326").to_crs(ITM)

    st = pd.read_excel(data / "out" / "tod_israel.xlsx", sheet_name="stations")
    mp = st[st["hub_id"].notna() & (st["n_modes"] >= args.min_modes)].copy().reset_index(drop=True)
    mp["modes_norm"] = mp["modes"].map(lambda s: norm_modes(str(s).split(" | ")))
    gm = gpd.GeoDataFrame(mp, geometry=gpd.points_from_xy(mp["lng"], mp["lat"]), crs="EPSG:4326").to_crs(ITM)

    # ---- nearest neighbours -----------------------------------------------------------------------
    X = np.c_[gx.geometry.x, gx.geometry.y]
    M = np.c_[gm.geometry.x, gm.geometry.y]
    d_m2x, i_m2x = cKDTree(X).query(M)          # for every map hub: nearest external hub
    d_x2m, i_x2m = cKDTree(M).query(X)          # for every external hub: nearest map hub

    gm["nearest_xlsx_row"] = ext.loc[i_m2x, "xlsx_row"].values
    gm["nearest_xlsx_name"] = ext.loc[i_m2x, "HubNameHE"].values if "HubNameHE" in ext.columns else None
    gm["dist_m"] = d_m2x.round(0)
    gx["nearest_map_hub_id"] = mp.loc[i_x2m, "hub_id"].astype(int).values
    gx["nearest_map_name"] = mp.loc[i_x2m, "name"].values
    gx["dist_m"] = d_x2m.round(0)

    matched_map = gm["dist_m"] <= args.tolerance
    matched_ext_rows = set(gm.loc[matched_map, "nearest_xlsx_row"])
    gx["matched"] = gx["xlsx_row"].isin(matched_ext_rows)

    # ---- sets -------------------------------------------------------------------------------------
    ext_cols = ["xlsx_row", "group", "HubNameHE", "Metro", "location", "HubType", "Num_Modes", "Overall_Rank",
                "TotalNumLines", "Mode_Planned", xcol, ycol]
    ext_cols = [c for c in ext_cols if c in gx.columns]
    map_cols = ["hub_id", "id", "name", "n_modes", "modes", "type_sum", "line_sum", "planning_status",
                "yearOperation", "municipality", "metropolin", "metropolin_ring", "lng", "lat"]

    both = gm.loc[matched_map, map_cols + ["dist_m", "nearest_xlsx_row", "modes_norm"]].copy()
    both = both.merge(gx[ext_cols + ["modes_norm"]].rename(columns={c: f"xlsx_{c}" for c in ext_cols + ["modes_norm"]}),
                      left_on="nearest_xlsx_row", right_on="xlsx_xlsx_row", how="left").drop(columns=["xlsx_xlsx_row"])
    both["modes_map"] = both["modes_norm"].map(" | ".join)
    both["modes_xlsx"] = both["xlsx_modes_norm"].map(" | ".join)
    both["modes_equal"] = both["modes_norm"].map(set) == both["xlsx_modes_norm"].map(set)
    both["modes_only_in_map"] = [" | ".join(sorted(set(a) - set(b))) for a, b in zip(both["modes_norm"], both["xlsx_modes_norm"])]
    both["modes_only_in_xlsx"] = [" | ".join(sorted(set(b) - set(a))) for a, b in zip(both["modes_norm"], both["xlsx_modes_norm"])]
    both = both.drop(columns=["modes_norm", "xlsx_modes_norm"]).rename(columns={"nearest_xlsx_row": "xlsx_row"})
    both = both.sort_values(["xlsx_row", "dist_m"])
    n_ext_per = both.groupby("xlsx_row").size()
    both["map_hubs_for_this_xlsx_hub"] = both["xlsx_row"].map(n_ext_per)

    only_map = gm.loc[~matched_map, map_cols + ["dist_m", "nearest_xlsx_row", "nearest_xlsx_name"]].copy()
    only_map = only_map.rename(columns={"dist_m": "dist_to_nearest_xlsx_m"}).sort_values("dist_to_nearest_xlsx_m")
    only_xlsx = gx.loc[~gx["matched"], ext_cols + ["dist_m", "nearest_map_hub_id", "nearest_map_name"]].copy()
    only_xlsx = only_xlsx.rename(columns={"dist_m": "dist_to_nearest_map_hub_m"}).sort_values("dist_to_nearest_map_hub_m")
    possible = pd.concat([
        only_xlsx[only_xlsx["dist_to_nearest_map_hub_m"] <= args.near].assign(side="xlsx hub -> nearest map hub"),
        only_map[only_map["dist_to_nearest_xlsx_m"] <= args.near].assign(side="map hub -> nearest xlsx hub"),
    ], ignore_index=True)

    summary = pd.DataFrame([
        {"set": "in both", "xlsx hubs": both["xlsx_row"].nunique(), "map hubs": len(both),
         "note": f"map hub within {args.tolerance:.0f} m of an xlsx hub; several map stations can share one xlsx hub"},
        {"set": "only in xlsx", "xlsx hubs": len(only_xlsx), "map hubs": None, "note": "no map hub (n_modes>=2) within tolerance"},
        {"set": "only in map", "xlsx hubs": None, "map hubs": len(only_map), "note": "no xlsx hub within tolerance"},
        {"set": "possible (review)", "xlsx hubs": int((possible["side"].str.startswith("xlsx")).sum()),
         "map hubs": int((possible["side"].str.startswith("map")).sum()), "note": f"nearest neighbour {args.tolerance:.0f}-{args.near:.0f} m away"},
        {"set": "totals", "xlsx hubs": len(gx), "map hubs": len(gm), "note": f"xlsx: {Path(args.xlsx).name}; map: hub_id set and n_modes>={args.min_modes}"},
        {"set": "modes agree (in both)", "xlsx hubs": None, "map hubs": int(both["modes_equal"].sum()),
         "note": "same mode set after mapping Interurban/Suburban->Rail, HighSpeed->Rail fast"},
    ])

    out_x = data / "out" / "hub_comparison.xlsx"
    with pd.ExcelWriter(out_x, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="summary", index=False)
        both.to_excel(xw, sheet_name="both", index=False)
        only_xlsx.to_excel(xw, sheet_name="only_xlsx", index=False)
        only_map.to_excel(xw, sheet_name="only_map", index=False)
        possible.to_excel(xw, sheet_name="possible_matches", index=False)
    print(f"wrote {out_x}")
    print(summary.to_string(index=False))

    # ---- map ----------------------------------------------------------------------------------------
    gx4, gm4 = gx.to_crs(4326), gm.to_crs(4326)
    pairs = []
    for _, r in both.iterrows():
        e = gx4[gx4["xlsx_row"] == r["xlsx_row"]].iloc[0]
        pairs.append({"hub": int(r["hub_id"]), "mname": r["name"], "mmodes": r["modes_map"], "mn": int(r["n_modes"]),
                      "mlat": round(float(r["lat"]), 6), "mlng": round(float(r["lng"]), 6),
                      "xrow": int(r["xlsx_row"]), "xname": str(r.get("xlsx_HubNameHE", "")), "xmodes": r["modes_xlsx"],
                      "xn": int(r.get("xlsx_Num_Modes", 0) or 0), "xrank": r.get("xlsx_Overall_Rank"), "xtype": r.get("xlsx_HubType"),
                      "xlat": round(float(e.geometry.y), 6), "xlng": round(float(e.geometry.x), 6),
                      "d": int(r["dist_m"]), "eq": bool(r["modes_equal"])})
    ox = [{"xrow": int(r["xlsx_row"]), "xname": str(r.get("HubNameHE", "")), "xmodes": " | ".join(norm_modes(r.get("Mode_Planned"))),
           "xn": int(r.get("Num_Modes", 0) or 0), "xrank": r.get("Overall_Rank"), "xtype": r.get("HubType"),
           "lat": round(float(r.geometry.y), 6), "lng": round(float(r.geometry.x), 6),
           "d": int(r["dist_m"]), "near": r["nearest_map_name"]}
          for _, r in gx4[gx4["xlsx_row"].isin(only_xlsx["xlsx_row"])].iterrows()]
    om = [{"hub": int(r["hub_id"]), "mname": r["name"], "mmodes": r["modes"], "mn": int(r["n_modes"]), "status": r["planning_status"],
           "lat": round(float(r["lat"]), 6), "lng": round(float(r["lng"]), 6), "d": int(r["dist_to_nearest_xlsx_m"]), "near": r["nearest_xlsx_name"]}
          for _, r in only_map.iterrows()]

    def clean(o):
        if isinstance(o, float) and np.isnan(o):
            return None
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if np.isnan(o) else float(o)
        return o
    dumps = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=clean)

    css = re.sub(r"url\([^)]*\)", "none", Path(args.leaflet_css).read_text(encoding="utf-8"))
    html = TEMPLATE
    for k, v in {"__LEAFLET_CSS__": css, "__LEAFLET_JS__": args.leaflet_js, "__PAIRS__": dumps(pairs),
                 "__ONLY_XLSX__": dumps(ox), "__ONLY_MAP__": dumps(om), "__TOL__": f"{args.tolerance:.0f}",
                 "__XLSX_NAME__": Path(args.xlsx).name, "__N_X__": str(len(gx)), "__N_M__": str(len(gm)),
                 "__MINM__": str(args.min_modes)}.items():
        html = html.replace(k, v)
    if not args.fragment:
        html = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                + html.replace('\n<div id="map"', '\n</head>\n<body>\n<div id="map"', 1) + "\n</body>\n</html>\n")
    out_h = data / "out" / "hub_comparison_map.html"
    out_h.write_text(html, encoding="utf-8")
    if args.copy_map_to:
        Path(args.copy_map_to).write_text(html, encoding="utf-8")
    print(f"wrote {out_h} ({out_h.stat().st_size/1e6:.2f} MB)")
    return 0


TEMPLATE = r"""<title>Hub List Comparison</title>
<meta name="description" content="Multi-modal hubs of the TOD Israel map compared with the hub prioritization list: in both, only in the list, only on the map.">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;700&display=swap">
<style>
__LEAFLET_CSS__
:root {
  color-scheme: light;
  --bg: #f4f3ef; --panel: #fffefb; --panel-border: #dcd9d0; --ink: #1c1b18; --ink-2: #5b5950; --ink-3: #8a877c;
  --map-bg: #e9e9e9; --shadow: 0 4px 18px rgba(30,28,20,.16); --accent: #2a78d6; --focus: #2a78d6; --ring: #ffffff;
  --both: #2a78d6; --onlyx: #eb6834; --onlym: #1baf7a; --tie: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #17171a; --panel: #212125; --panel-border: #3a3a40; --ink: #f2f1ea; --ink-2: #c3c2b7; --ink-3: #8b8a82;
    --map-bg: #e9e9e9; --shadow: 0 4px 18px rgba(0,0,0,.5); --accent: #3987e5; --focus: #3987e5; --ring: #ffffff;
    --both: #2a78d6; --onlyx: #d95926; --onlym: #199e70; --tie: #2a78d6;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #17171a; --panel: #212125; --panel-border: #3a3a40; --ink: #f2f1ea; --ink-2: #c3c2b7; --ink-3: #8b8a82;
  --map-bg: #e9e9e9; --shadow: 0 4px 18px rgba(0,0,0,.5); --accent: #3987e5; --focus: #3987e5; --ring: #ffffff;
  --both: #2a78d6; --onlyx: #d95926; --onlym: #199e70; --tie: #2a78d6;
}
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.4 "Heebo", "Segoe UI", system-ui, sans-serif; }
#map { position: absolute; inset: 0; background: var(--map-bg); }
.leaflet-container { background: var(--map-bg); font: inherit; }
.osm-gray { filter: grayscale(1) brightness(1.06) contrast(0.88); }
.leaflet-bar { box-shadow: var(--shadow); border: 1px solid var(--panel-border); }
.leaflet-bar a { background: var(--panel); color: var(--ink); border-bottom-color: var(--panel-border); }
.leaflet-bar a:hover, .leaflet-bar a:focus-visible { background: var(--bg); }
.leaflet-control-attribution { background: color-mix(in srgb, var(--panel) 85%, transparent); color: var(--ink-3); font-size: 11px; }
.leaflet-control-attribution a { color: var(--ink-2); }
.leaflet-popup-content-wrapper, .leaflet-popup-tip { background: var(--panel); color: var(--ink); box-shadow: var(--shadow); }
.leaflet-popup-content-wrapper { border-radius: 8px; border: 1px solid var(--panel-border); }
.leaflet-popup-content { margin: 12px 14px; font-size: 13px; min-width: 220px; }
.leaflet-container a.leaflet-popup-close-button { color: var(--ink-3); }
.leaflet-container :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.panel { position: absolute; z-index: 1000; top: calc(12px + env(safe-area-inset-top, 0px)); left: 12px; width: 316px; max-width: calc(100% - 24px);
  background: var(--panel); border: 1px solid var(--panel-border); border-radius: 10px; box-shadow: var(--shadow); padding: 14px 16px 12px; box-sizing: border-box; }
.panel h1 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -.01em; text-wrap: balance; }
.panel .sub { margin: 2px 0 0; color: var(--ink-2); font-size: 12.5px; }
.eyebrow { margin: 14px 0 6px; font-size: 11px; font-weight: 500; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }
.sets { display: grid; gap: 2px; margin: 0; padding: 0; list-style: none; }
.sets label { display: grid; grid-template-columns: 18px 26px 1fr auto; align-items: center; gap: 8px; padding: 5px 6px; border-radius: 6px; cursor: pointer; }
.sets label:hover { background: var(--bg); }
.sets input { margin: 0; width: 15px; height: 15px; accent-color: var(--accent); }
.sets input:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.sets .lbl { font-weight: 500; line-height: 1.2; }
.sets .lbl small { display: block; font-weight: 400; color: var(--ink-3); font-size: 11.5px; }
.sets .cnt { color: var(--ink-2); font-variant-numeric: tabular-nums; font-size: 13px; text-align: right; }
.sym { justify-self: center; width: 22px; height: 16px; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.actions button { flex: 1 1 auto; white-space: nowrap; font: inherit; font-size: 12.5px; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--panel-border); background: var(--bg); color: var(--ink); cursor: pointer; }
.actions button:hover { border-color: var(--ink-3); }
.actions button:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.note { margin: 10px 0 0; font-size: 11.5px; color: var(--ink-3); }
.pp h2 { margin: 0 0 6px; font-size: 14px; font-weight: 700; line-height: 1.25; }
.pp .tag { display: inline-block; font-size: 11px; font-weight: 500; letter-spacing: .04em; text-transform: uppercase; padding: 1px 7px; border-radius: 99px; color: #fff; margin-bottom: 6px; }
.pp .two { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.pp .col { border: 1px solid var(--panel-border); border-radius: 6px; padding: 6px 8px; }
.pp .col h3 { margin: 0 0 3px; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-3); font-weight: 500; }
.pp .col .nm { font-weight: 600; }
.pp .kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; font-size: 12.5px; margin-top: 6px; }
.pp .kv dt { color: var(--ink-3); } .pp .kv dd { margin: 0; font-variant-numeric: tabular-nums; }
.pp .diff { color: var(--onlyx); }
.pp .same { color: var(--onlym); }
@media (max-width: 560px) {
  .panel { top: auto; bottom: calc(12px + env(safe-area-inset-bottom, 0px)); left: 12px; right: 12px; width: auto; max-height: 48%; overflow: auto; }
  .leaflet-top.leaflet-right { top: env(safe-area-inset-top, 0px); }
}
@media (prefers-reduced-motion: reduce) { .leaflet-zoom-anim .leaflet-zoom-animated, .leaflet-fade-anim .leaflet-popup { transition: none; } }
</style>

<div id="map" role="application" aria-label="Map comparing two hub lists"></div>

<section class="panel" id="panel" aria-label="Sets and legend">
  <h1>Hub list comparison</h1>
  <p class="sub">__N_M__ map hubs (n_modes ≥ __MINM__) vs __N_X__ hubs in __XLSX_NAME__ · matched within __TOL__ m</p>
  <p class="eyebrow">Sets</p>
  <ul class="sets">
    <li><label><input type="checkbox" id="set-both" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><line x1="5" y1="8" x2="17" y2="8" stroke="var(--tie)" stroke-width="1.5"/><circle cx="5" cy="8" r="4.5" fill="var(--both)" stroke="var(--ring)" stroke-width="1.5"/><circle cx="17" cy="8" r="4.5" fill="none" stroke="var(--both)" stroke-width="2.2"/></svg>
      <span class="lbl">In both<small>filled = map hub · ring = xlsx hub · line = offset</small></span><span class="cnt" id="cnt-both"></span></label></li>
    <li><label><input type="checkbox" id="set-onlyx" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><circle cx="11" cy="8" r="5" fill="none" stroke="var(--onlyx)" stroke-width="2.4"/></svg>
      <span class="lbl">Only in xlsx<small>no map hub within __TOL__ m</small></span><span class="cnt" id="cnt-onlyx"></span></label></li>
    <li><label><input type="checkbox" id="set-onlym" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><circle cx="11" cy="8" r="5" fill="var(--onlym)" stroke="var(--ring)" stroke-width="1.5"/></svg>
      <span class="lbl">Only in map<small>no xlsx hub within __TOL__ m</small></span><span class="cnt" id="cnt-onlym"></span></label></li>
  </ul>
  <div class="actions">
    <button type="button" id="btn-fit">Zoom to all</button>
    <button type="button" id="btn-tlv">Tel Aviv</button>
    <button type="button" id="btn-hfa">Haifa</button>
    <button type="button" id="btn-jlm">Jerusalem</button>
  </div>
  <p class="note">Basemap: OpenStreetMap (light gray). Matching is by location only; the popup shows whether the mode sets agree.</p>
</section>

<script src="__LEAFLET_JS__"></script>
<script>
(function () {
  const PAIRS = __PAIRS__, ONLYX = __ONLY_XLSX__, ONLYM = __ONLY_MAP__;
  const css = getComputedStyle(document.documentElement), tok = n => css.getPropertyValue(n).trim();
  const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const map = L.map('map', {zoomControl: false, preferCanvas: true});
  L.control.zoom({position: 'topright'}).addTo(map);
  map.attributionControl.setPrefix('');
  map.attributionControl.addAttribution('© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · TOD Israel explorer (beta)');
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, className: 'osm-gray'}).addTo(map);

  const both = L.layerGroup().addTo(map), onlyx = L.layerGroup().addTo(map), onlym = L.layerGroup().addTo(map);
  const R = 6;
  const modesLine = (a, b) => a === b ? `<span class="same">modes agree</span>` : `<span class="diff">modes differ</span>`;

  PAIRS.forEach(p => {
    const tie = L.polyline([[p.mlat, p.mlng], [p.xlat, p.xlng]], {color: tok('--tie'), weight: 1.5, opacity: .9, interactive: false});
    const m = L.circleMarker([p.mlat, p.mlng], {radius: R, color: tok('--ring'), weight: 1.5, fillColor: tok('--both'), fillOpacity: .95});
    const x = L.circleMarker([p.xlat, p.xlng], {radius: R + 1, color: tok('--both'), weight: 2.2, fillColor: tok('--both'), fillOpacity: 0});
    const html = `<div class="pp" dir="auto"><span class="tag" style="background:var(--both)">In both · ${p.d} m apart</span>
      <div class="two">
        <div class="col"><h3>Map hub ${p.hub}</h3><div class="nm">${esc(p.mname)}</div><div>${esc(p.mmodes)}</div><div>n_modes ${p.mn}</div></div>
        <div class="col"><h3>xlsx row ${p.xrow}</h3><div class="nm">${esc(p.xname)}</div><div>${esc(p.xmodes)}</div><div>Num_Modes ${p.xn}${p.xrank != null ? ` · rank ${p.xrank}` : ''}${p.xtype ? ` · ${esc(p.xtype)}` : ''}</div></div>
      </div><div style="margin-top:6px">${modesLine(p.mmodes, p.xmodes)}</div></div>`;
    m.bindPopup(html, {maxWidth: 360}); x.bindPopup(html, {maxWidth: 360});
    m.bindTooltip(`${p.mname} ↔ ${p.xname} (${p.d} m)`, {direction: 'top', offset: [0, -R]});
    both.addLayer(tie); both.addLayer(x); both.addLayer(m);
  });
  ONLYX.forEach(p => {
    const x = L.circleMarker([p.lat, p.lng], {radius: R + 1, color: tok('--onlyx'), weight: 2.4, fillColor: tok('--onlyx'), fillOpacity: .15});
    x.bindPopup(`<div class="pp" dir="auto"><span class="tag" style="background:var(--onlyx)">Only in xlsx</span>
      <h2>${esc(p.xname)}</h2><dl class="kv"><dt>xlsx row</dt><dd>${p.xrow}</dd><dt>modes</dt><dd>${esc(p.xmodes)}</dd><dt>Num_Modes</dt><dd>${p.xn}</dd>
      ${p.xrank != null ? `<dt>rank</dt><dd>${p.xrank}</dd>` : ''}${p.xtype ? `<dt>HubType</dt><dd>${esc(p.xtype)}</dd>` : ''}
      <dt>nearest map hub</dt><dd>${esc(p.near)} · ${p.d.toLocaleString()} m</dd></dl></div>`, {maxWidth: 320});
    x.bindTooltip(`${p.xname} (xlsx only)`, {direction: 'top', offset: [0, -R]});
    onlyx.addLayer(x);
  });
  ONLYM.forEach(p => {
    const m = L.circleMarker([p.lat, p.lng], {radius: R, color: tok('--ring'), weight: 1.5, fillColor: tok('--onlym'), fillOpacity: .95});
    m.bindPopup(`<div class="pp" dir="auto"><span class="tag" style="background:var(--onlym)">Only in map</span>
      <h2>${esc(p.mname)}</h2><dl class="kv"><dt>hub_id</dt><dd>${p.hub}</dd><dt>modes</dt><dd>${esc(p.mmodes)}</dd><dt>n_modes</dt><dd>${p.mn}</dd>
      <dt>status</dt><dd>${esc(p.status)}</dd><dt>nearest xlsx hub</dt><dd>${esc(p.near)} · ${p.d.toLocaleString()} m</dd></dl></div>`, {maxWidth: 320});
    m.bindTooltip(`${p.mname} (map only)`, {direction: 'top', offset: [0, -R]});
    onlym.addLayer(m);
  });
  document.getElementById('cnt-both').textContent = `${PAIRS.length} pairs`;
  document.getElementById('cnt-onlyx').textContent = ONLYX.length;
  document.getElementById('cnt-onlym').textContent = ONLYM.length;
  const wire = (id, layer) => document.getElementById(id).addEventListener('change', e => e.target.checked ? layer.addTo(map) : map.removeLayer(layer));
  wire('set-both', both); wire('set-onlyx', onlyx); wire('set-onlym', onlym);

  const all = [...PAIRS.map(p => [p.mlat, p.mlng]), ...PAIRS.map(p => [p.xlat, p.xlng]), ...ONLYX.map(p => [p.lat, p.lng]), ...ONLYM.map(p => [p.lat, p.lng])];
  const bounds = L.latLngBounds(all);
  document.getElementById('btn-fit').addEventListener('click', () => map.fitBounds(bounds, {padding: [30, 30]}));
  document.getElementById('btn-tlv').addEventListener('click', () => map.setView([32.08, 34.82], 12));
  document.getElementById('btn-hfa').addEventListener('click', () => map.setView([32.80, 35.02], 12));
  document.getElementById('btn-jlm').addEventListener('click', () => map.setView([31.78, 35.21], 12));
  const panel = document.getElementById('panel');
  L.DomEvent.disableClickPropagation(panel); L.DomEvent.disableScrollPropagation(panel);
  map.fitBounds(bounds, {padding: [30, 30]});
})();
</script>
"""

if __name__ == "__main__":
    raise SystemExit(main())
