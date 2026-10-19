# HubsScraping

Tools for pulling the data behind the **TOD Israel** Transit-Oriented Development
explorer (the Mapbox app at `tod-israel-d99f0bf7ea5d.herokuapp.com`, embedded in
the password-protected page on nsa-hub.com) into GIS and Excel files.

Output: stations, transit lines and area layers as **shapefiles** (WGS84 and
Israel TM Grid, EPSG:2039), a **GeoPackage**, and an **Excel workbook** with one
sheet per layer plus a summary and per-mode counts.

## Data already captured (2026-09-22)

`tod_scraper/data/out/` holds the outputs built from the live explorer:

| layer | geometry | features | main fields |
|---|---|---|---|
| `stations` | Point | 1,459 | id, name, modes, n_modes, is_Metro/LRT/BRT/Rail/Railfast/Funicular, yearOperation, planning_status (Detailed / Strategic / Operating), municipality, settlement, metropolin, metropolin_ring, tama_35, tamam_yeud, built_landuse_current/planned, hub_id, line_sum, type_sum |
| `lines` | LineString | 997 | line_id, mode, length_m |
| `accessibility_radiuses` | MultiPolygon | 3,668 | combinations_radiuses (e.g. "Metro 800 \| Rail 1000"), lines_Metro/LRT/Rail/Rail fast |
| `municipalities` | Polygon | 314 | CBS municipality attributes plus the app's TOD indicators (pop_2024, stations per mode, hubs, corridor stations, transit_system, cluster_archetype, ...) |
| `metropolins` | MultiPolygon | 23 | METRO_NAME, ZONE_NAME (core / rings), SEC_NAME |
| `functional_areas` | Polygon | 28 | Code_Ezor, Name |

Stations per mode: Metro 212, LRT 720, BRT 606, Rail 126, Rail fast 10, Funicular 27
(a multi-modal station counts once per mode; 1,459 unique stations).

Files: `data/out/shp/<layer>_itm.shp` (EPSG:2039), `data/out/shp/<layer>_wgs84.shp`,
`data/out/tod_israel.gpkg`, `data/out/tod_israel.xlsx` (sheets per layer + `summary`,
`stations_by_mode`, `stations_breakdown`, `lines_by_mode`, `shapefile_field_map`).
Raw captures are in `data/raw/`; the app is marked *Beta* by its authors, so treat the
data as a working snapshot.

The scrape ran on GitHub Actions (`.github/workflows/scrape.yml`), which commits fresh
outputs to this branch. Re-run it from the Actions tab ("Run workflow") to refresh.

## Hub stations map (GitHub Pages)

`tod_scraper/make_hub_map.py` builds `data/out/hub_stations_map.html`: the 432 hub
stations (hub_id not null), one toggleable layer per `type_sum`, popups with
`n_modes` and the modes served. A copy sits at the repository root as `index.html`
so GitHub Pages (source: deploy from branch) serves it at the site root:

* map: https://oh-da.github.io/HubsScraping/
* workbook download: https://oh-da.github.io/HubsScraping/tod_scraper/data/out/tod_israel.xlsx

`.github/workflows/pages.yml` deploys the same files through the Actions route, for
repositories where the Pages source is set to "GitHub Actions" instead.

Rebuild the map after a new scrape with
`python make_hub_map.py --leaflet-css <path to leaflet.css> --copy-to ../index.html`
(get the CSS with `npm pack leaflet@1.9.4`).

## Comparing with an external hub list

`tod_scraper/compare_hubs.py` matches a hub list (xlsx with x/y in WGS84, e.g. the
hub prioritization results, rows with HubType = Not Hub dropped) against the map's multi-modal hubs (hub_id set,
n_modes >= 2) by location, 400 m tolerance by default:

```bash
python compare_hubs.py data/input/hub_prioritization_results.xlsx \
    --leaflet-css <leaflet.css> --copy-map-to ../compare.html
```

Outputs `data/out/hub_comparison.xlsx` (sheets: summary, both, only_xlsx, only_map,
possible_matches) and `data/out/hub_comparison_map.html` (also served at
https://oh-da.github.io/HubsScraping/compare.html), which draws each matched pair
one on top of the other with a tie line showing the offset.

## Comparing the transit lines: Hubs project vs Minhal

Two sources are compared:

* **Hubs project** (HubsPrioritizing): `Data/All_nodes+lines_V1.01.csv`, one row per node x
  line (`node`, `LINE_ID`, `X`, `Y` in EPSG:2039), no route geometry and no stop order.
* **Minhal** (מנהל התכנון): the `lines` and `stations` layers scraped from the TOD Israel map
  by this repository (`tod_israel.gpkg` / `tod_israel.xlsx`).

`tod_scraper/compare_lines.py` compares them by geometry only (the line names of the two
sources do not correspond and are never compared):

1. **offset**: the sources do not sit exactly on top of each other (Hubs nodes are about
   60 m west and 37 m south of the Minhal stations in Tel Aviv and Haifa, 21 m east and
   37 m north in Be'er Sheva, on the spot in Jerusalem). A local shift field (median
   node to station offset within 10 km) is estimated and applied to the Hubs nodes first;
   raw and corrected distances are both kept.
2. **mode**: the mode of a Hubs line is the mode whose Minhal lines run under most of its
   nodes (the name prefix is only reported as a hint, `mode_hint_differs` flags e.g. a
   "BRT" line that runs on Minhal's LRT).
3. **nodes**: each Hubs node is matched to the nearest Minhal station (150 m) and Minhal
   line (100 m, Rail 250 m).
4. **routing**: the stops of each Hubs line are ordered (shortest path through them) and
   each consecutive pair is routed along the Minhal network of the line's mode. No path, a
   Minhal route 1.5x longer than the straight line, or a stop off the Minhal network is a
   difference.
5. **reverse**: Minhal stations with no Hubs node nearby, and stretches (>= 300 m) of the
   Minhal network that no Hubs line uses (parallel carriageways of a used line excluded).

```bash
python compare_lines.py ../Data/All_nodes+lines_V1.01.csv \
    --leaflet-css <leaflet.css> --copy-map-to ../compare_lines.html \
    --copy-overlay-to ../compare_lines_overlay.html
```

Outputs `data/out/line_comparison.xlsx` (sheets: summary, shift, hubs_lines, hubs_stop_pairs,
hubs_nodes, only_minhal_stations, only_minhal_lines, minhal_coverage),
`data/out/line_comparison.gpkg` (hubs_routes, hubs_stop_pairs, hubs_nodes, hubs_nodes_raw,
minhal_lines_used, only_minhal_lines, only_minhal_stations; EPSG:2039) and
`data/out/line_comparison_map.html` (also served at
https://oh-da.github.io/HubsScraping/compare_lines.html): the Hubs lines routed over Minhal,
node to station ties, routing differences, Minhal stretches and stations the Hubs project
lacks, with a picker to isolate one line or one status.

For a plain visual check there is also `data/out/line_overlay_map.html`
(https://oh-da.github.io/HubsScraping/compare_lines_overlay.html): the Hubs lines (stops
joined in order) over the Minhal lines layer, each layer switchable on and off, a mode
filter, a line picker, and a toggle that shifts the Hubs lines by the estimated offset.

## Quick start

```bash
cd tod_scraper
pip install -r requirements.txt
playwright install chromium          # once
./run_all.sh                          # scrape + build, everything lands in data/
```

Or step by step:

```bash
python scrape_tod.py                  # -> data/raw/*.geojson, data/manifest.json, data/screenshot.png
python build_outputs.py               # -> data/out/shp/*.shp, data/out/tod_israel.gpkg, data/out/tod_israel.xlsx
```

Useful flags:

| flag | what it does |
|---|---|
| `scrape_tod.py --via-portal` | opens the nsa-hub page first, enters the password (`tod@2026` by default, `--password` to override) and follows the embedded app |
| `scrape_tod.py --headed` | shows the browser so you can watch what gets loaded |
| `scrape_tod.py --chromium /path/to/chrome` | use an existing Chrome/Chromium instead of Playwright's download (or set `TOD_CHROMIUM`) |
| `scrape_tod.py --no-toggle` | don't click the sidebar toggles (faster, but lazily loaded layers are skipped) |
| `build_outputs.py --data DIR` | read `DIR/raw`, write `DIR/out` |

## How it works

`scrape_tod.py` drives a real Chromium through Playwright and collects data
three ways, so whichever way the app ships its data, it is caught:

1. **Network capture**: every JSON / GeoJSON / CSV response the page downloads
   is saved verbatim.
2. **UI poking**: every sidebar toggle that is off (accessibility radiuses,
   metropolins, municipalities, functional areas, planning status) is switched
   on so lazily fetched layers are requested too.
3. **Map memory dump**: the Mapbox GL map instance is located (global
   variable or React fiber), the view is fitted to the whole of Israel, and
   every GeoJSON source is read straight from memory; vector-tile sources are
   read with `querySourceFeatures` per source layer.

`build_outputs.py` reads everything in `data/raw`, drops duplicate payloads,
splits mixed collections into point / line / polygon layers, merges point
layers that share a schema into `stations_all`, and writes:

```
data/out/shp/<layer>_wgs84.shp     EPSG:4326
data/out/shp/<layer>_itm.shp       EPSG:2039 (field names truncated to 10 chars; see sheet shapefile_field_map)
data/out/tod_israel.gpkg           all layers, full field names
data/out/tod_israel.xlsx           one sheet per layer (+ lon/lat, x_itm/y_itm, length_m / area_m2, WKT), summary, counts_by_mode
```

`probe_endpoints.py` is a browser-free fallback: it downloads the app's JS
bundles, extracts endpoint strings and tries them. It cannot see data fetched
only after user interaction, so use it only when Chromium is unavailable.

## Notes

* Nested attribute values (for example a station's list of modes) are kept as
  JSON strings so they survive the shapefile format.
* Hebrew text is written as UTF-8 (`.cpg` sidecar); open with a recent QGIS/ArcGIS.
* `data/` is git-ignored for local runs; the committed snapshot was added with `git add -f`.
* The station data lives in the app's React state (not a network call), so the JS state
  scan is what captures it; lines and polygon layers come from the Mapbox sources.
