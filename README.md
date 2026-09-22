# HubsScraping

Tools for pulling the data behind the **TOD Israel** Transit-Oriented Development
explorer (the Mapbox app at `tod-israel-d99f0bf7ea5d.herokuapp.com`, embedded in
the password-protected page on nsa-hub.com) into GIS and Excel files.

Output: stations, transit lines and area layers as **shapefiles** (WGS84 and
Israel TM Grid, EPSG:2039), a **GeoPackage**, and an **Excel workbook** with one
sheet per layer plus a summary and per-mode counts.

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
* `data/` is git-ignored. Commit outputs deliberately if you want them versioned.
