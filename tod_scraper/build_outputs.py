#!/usr/bin/env python3
"""
Turn the raw JSON / GeoJSON captured by scrape_tod.py into GIS + Excel outputs.

Reads every *.json / *.geojson / *.csv in  <data>/raw/  and, for each one that
can be interpreted as spatial data, writes:

    <data>/out/shp/<layer>_wgs84.shp   EPSG:4326
    <data>/out/shp/<layer>_itm.shp     EPSG:2039 (Israel TM Grid)
    <data>/out/tod_israel.gpkg         all layers, EPSG:4326 (no 10-char field limit)
    <data>/out/tod_israel.xlsx         one sheet per layer + summary + field map

Features are split by geometry type, so a GeoJSON that mixes stations and lines
becomes  <name>_points, <name>_lines, <name>_polygons.  Point layers with an
identical schema are also merged into  stations_all.

Non-spatial JSON (plain records) still lands in the workbook.

Usage:
    python build_outputs.py                # uses ./data
    python build_outputs.py --data data    # explicit folder
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape

WGS84 = "EPSG:4326"
ITM = "EPSG:2039"

LAT_KEYS = ("lat", "latitude", "y", "lat_wgs84", "Lat", "LAT", "Y")
LON_KEYS = ("lon", "lng", "long", "longitude", "x", "lon_wgs84", "Lon", "LON", "Lng", "X")


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# parsing raw files -> list of (layer_name, GeoDataFrame | DataFrame)
# --------------------------------------------------------------------------- #
def _features_from_json(obj) -> list[dict] | None:
    """Return a list of GeoJSON features if `obj` is GeoJSON-ish, else None."""
    if isinstance(obj, dict):
        t = obj.get("type")
        if t == "FeatureCollection":
            return obj.get("features", [])
        if t == "Feature":
            return [obj]
        if t in ("Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"):
            return [{"type": "Feature", "geometry": obj, "properties": {}}]
        # {"stations": FeatureCollection, "lines": FeatureCollection} -> handled by caller
    if isinstance(obj, list) and obj and all(isinstance(x, dict) and x.get("type") == "Feature" for x in obj):
        return obj
    return None


def _records_from_json(obj) -> list[dict] | None:
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        return obj
    if isinstance(obj, dict):
        # column-oriented {"col": [..], ...}
        vals = list(obj.values())
        if vals and all(isinstance(v, list) for v in vals) and len({len(v) for v in vals}) == 1:
            return pd.DataFrame(obj).to_dict("records")
    return None


def _find_coord_cols(df: pd.DataFrame) -> tuple[str, str] | None:
    cols = {c.lower(): c for c in df.columns}
    lat = next((cols[k.lower()] for k in LAT_KEYS if k.lower() in cols), None)
    lon = next((cols[k.lower()] for k in LON_KEYS if k.lower() in cols), None)
    if lat and lon:
        return lon, lat
    return None


def _flatten(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def gdf_from_features(feats: list[dict]) -> gpd.GeoDataFrame:
    rows, geoms = [], []
    for f in feats:
        g = f.get("geometry")
        if not g or not g.get("coordinates") and g.get("type") != "GeometryCollection":
            continue
        try:
            geom = shape(g)
        except Exception:  # noqa: BLE001
            continue
        props = {k: _flatten(v) for k, v in (f.get("properties") or {}).items()}
        if "id" in f and "id" not in props:
            props["id"] = f["id"]
        rows.append(props)
        geoms.append(geom)
    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs=WGS84)
    return gdf


def gdf_from_records(recs: list[dict]) -> gpd.GeoDataFrame | pd.DataFrame:
    df = pd.DataFrame([{k: _flatten(v) for k, v in r.items()} for r in recs])
    # nested geometry column?
    for c in ("geometry", "geom", "location", "coordinates"):
        if c in df.columns:
            try:
                geoms = [shape(json.loads(v)) if isinstance(v, str) else shape(v) for v in df[c]]
                return gpd.GeoDataFrame(df.drop(columns=[c]), geometry=geoms, crs=WGS84)
            except Exception:  # noqa: BLE001
                pass
    cc = _find_coord_cols(df)
    if cc:
        lon, lat = cc
        x = pd.to_numeric(df[lon], errors="coerce")
        y = pd.to_numeric(df[lat], errors="coerce")
        ok = x.notna() & y.notna()
        crs = WGS84
        # ITM coordinates? (metres, x ~ 100k-300k, y ~ 380k-800k)
        if ok.any() and x[ok].abs().max() > 1000:
            crs = ITM
        gdf = gpd.GeoDataFrame(df[ok], geometry=gpd.points_from_xy(x[ok], y[ok]), crs=crs)
        return gdf.to_crs(WGS84) if crs != WGS84 else gdf
    return df


def load_raw_file(path: Path) -> list[tuple[str, object]]:
    """Return [(layer_name, GeoDataFrame|DataFrame)] parsed from one raw file."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", path.stem)[:40].strip("_") or "layer"
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
        return [(name, gdf_from_records(df.to_dict("records")))]
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"skip {path.name}: not JSON ({exc})")
        return []

    out: list[tuple[str, object]] = []
    feats = _features_from_json(obj)
    if feats is not None:
        out.append((name, gdf_from_features(feats)))
        return out
    recs = _records_from_json(obj)
    if recs is not None:
        out.append((name, gdf_from_records(recs)))
        return out
    if isinstance(obj, dict):
        # nested: {"stations": FC, "lines": FC, "meta": {...}}
        for k, v in obj.items():
            sub = _features_from_json(v)
            if sub is not None:
                out.append((f"{name}_{re.sub(r'[^A-Za-z0-9_]', '_', k)}"[:40], gdf_from_features(sub)))
                continue
            sub = _records_from_json(v)
            if sub is not None:
                out.append((f"{name}_{re.sub(r'[^A-Za-z0-9_]', '_', k)}"[:40], gdf_from_records(sub)))
    if not out:
        log(f"skip {path.name}: no recognisable features/records")
    return out


# --------------------------------------------------------------------------- #
# split by geometry type and merge same-schema point layers
# --------------------------------------------------------------------------- #
def split_by_geom(name: str, gdf: gpd.GeoDataFrame) -> list[tuple[str, gpd.GeoDataFrame]]:
    if gdf.empty:
        return []
    kinds = gdf.geometry.geom_type.str.replace("Multi", "", regex=False)
    groups = {"Point": "points", "LineString": "lines", "Polygon": "polygons"}
    parts = []
    present = [k for k in groups if (kinds == k).any()]
    for k in present:
        sub = gdf[kinds == k].copy()
        suffix = groups[k]
        lname = name if len(present) == 1 else f"{name}_{suffix}"
        parts.append((lname, sub))
    return parts


def dedupe_names(items: list[tuple[str, object]]) -> list[tuple[str, object]]:
    seen: Counter = Counter()
    out = []
    for n, d in items:
        seen[n] += 1
        out.append((n if seen[n] == 1 else f"{n}_{seen[n]}", d))
    return out


# --------------------------------------------------------------------------- #
# writers
# --------------------------------------------------------------------------- #
def shp_safe(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, dict[str, str]]:
    """Rename columns to <=10 unique chars for the .dbf; return the mapping."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for c in gdf.columns:
        if c == gdf.geometry.name:
            continue
        base = re.sub(r"[^A-Za-z0-9_]", "_", str(c))[:10] or "f"
        cand, i = base, 1
        while cand.lower() in used:
            i += 1
            cand = f"{base[: 10 - len(str(i)) - 1]}_{i}"
        used.add(cand.lower())
        mapping[c] = cand
    g = gdf.rename(columns=mapping).copy()
    for c in g.columns:
        if c == g.geometry.name:
            continue
        if g[c].dtype == object:
            g[c] = g[c].astype(str).where(g[c].notna(), None)
            # dbf text field limit
            g[c] = g[c].map(lambda s: s[:254] if isinstance(s, str) else s)
        elif str(g[c].dtype).startswith("bool"):
            g[c] = g[c].astype(int)
    return g, mapping


def write_layers(layers: list[tuple[str, object]], out: Path) -> pd.DataFrame:
    shp_dir = out / "shp"
    shp_dir.mkdir(parents=True, exist_ok=True)
    gpkg = out / "tod_israel.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    xlsx = out / "tod_israel.xlsx"

    summary_rows, fieldmap_rows = [], []
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        for name, data in layers:
            if isinstance(data, gpd.GeoDataFrame) and not data.empty:
                gdf = data
                gtype = gdf.geometry.geom_type.mode().iat[0]
                # GeoPackage keeps full field names
                gdf.to_file(gpkg, layer=name, driver="GPKG")
                # shapefiles
                g10, mapping = shp_safe(gdf)
                g10.to_file(shp_dir / f"{name}_wgs84.shp", driver="ESRI Shapefile", encoding="utf-8")
                g10.to_crs(ITM).to_file(shp_dir / f"{name}_itm.shp", driver="ESRI Shapefile", encoding="utf-8")
                for k, v in mapping.items():
                    if k != v:
                        fieldmap_rows.append({"layer": name, "field": k, "shapefile_field": v})
                # excel: attributes + coordinates
                df = pd.DataFrame(gdf.drop(columns=gdf.geometry.name))
                pts = gdf.geometry if gtype.endswith("Point") else gdf.geometry.representative_point()
                itm = gpd.GeoSeries(pts, crs=WGS84).to_crs(ITM)
                df["lon"] = pts.x.values
                df["lat"] = pts.y.values
                df["x_itm"] = itm.x.round(1).values
                df["y_itm"] = itm.y.round(1).values
                if not gtype.endswith("Point"):
                    df["geom_type"] = gdf.geometry.geom_type.values
                    if "Line" in gtype:
                        df["length_m"] = gdf.to_crs(ITM).length.round(1).values
                    else:
                        df["area_m2"] = gdf.to_crs(ITM).area.round(1).values
                    df["wkt"] = gdf.geometry.to_wkt().values
                df.to_excel(xw, sheet_name=name[:31], index=False)
                summary_rows.append({"layer": name, "geometry": gtype, "features": len(gdf),
                                     "fields": ", ".join(map(str, gdf.columns.drop(gdf.geometry.name)))})
                log(f"{name:40s} {gtype:12s} {len(gdf):6d} features")
            elif isinstance(data, pd.DataFrame) and not data.empty:
                data.to_excel(xw, sheet_name=name[:31], index=False)
                summary_rows.append({"layer": name, "geometry": "(table)", "features": len(data),
                                     "fields": ", ".join(map(str, data.columns))})
                log(f"{name:40s} {'table':12s} {len(data):6d} rows")

        summary = pd.DataFrame(summary_rows)
        # per-mode breakdown for point layers, if a mode-like column exists
        mode_rows = []
        for name, data in layers:
            if isinstance(data, gpd.GeoDataFrame):
                col = next((c for c in data.columns if str(c).lower() in ("mode", "modes", "type", "system", "network")), None)
                if col:
                    for k, v in data[col].value_counts(dropna=False).items():
                        mode_rows.append({"layer": name, "field": col, "value": k, "count": int(v)})
        summary.to_excel(xw, sheet_name="summary", index=False)
        if mode_rows:
            pd.DataFrame(mode_rows).to_excel(xw, sheet_name="counts_by_mode", index=False)
        if fieldmap_rows:
            pd.DataFrame(fieldmap_rows).to_excel(xw, sheet_name="shapefile_field_map", index=False)
    log(f"wrote {xlsx}")
    log(f"wrote {gpkg}")
    log(f"wrote shapefiles to {shp_dir}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data", help="folder holding raw/ (from scrape_tod.py)")
    ap.add_argument("--out", default=None, help="output folder (default <data>/out)")
    args = ap.parse_args()

    raw = Path(args.data) / "raw"
    out = Path(args.out) if args.out else Path(args.data) / "out"
    files = sorted(p for p in raw.glob("*") if p.suffix.lower() in (".json", ".geojson", ".csv"))
    if not files:
        log(f"no raw files in {raw} - run scrape_tod.py first")
        return 2

    layers: list[tuple[str, object]] = []
    seen_hashes: set[str] = set()
    for f in files:
        h = hashlib.md5(f.read_bytes()).hexdigest()
        if h in seen_hashes:            # same payload captured twice (network + map source)
            log(f"skip {f.name}: duplicate of an earlier file")
            continue
        seen_hashes.add(h)
        for name, data in load_raw_file(f):
            if isinstance(data, gpd.GeoDataFrame):
                layers += split_by_geom(name, data)
            else:
                layers.append((name, data))
    layers = dedupe_names(layers)

    # merge point layers sharing a schema -> stations_all
    pts = [(n, d) for n, d in layers if isinstance(d, gpd.GeoDataFrame) and d.geometry.geom_type.str.endswith("Point").all()]
    if len(pts) > 1:
        schemas = Counter(tuple(sorted(map(str, d.columns))) for _, d in pts)
        top, cnt = schemas.most_common(1)[0]
        if cnt > 1:
            merged = pd.concat([d.assign(source_layer=n) for n, d in pts if tuple(sorted(map(str, d.columns))) == top],
                               ignore_index=True)
            layers.append(("stations_all", gpd.GeoDataFrame(merged, crs=WGS84)))

    if not layers:
        log("nothing spatial or tabular could be parsed from the raw files")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    summary = write_layers(layers, out)
    print()
    print(summary.to_string(index=False) if not summary.empty else "(no layers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
