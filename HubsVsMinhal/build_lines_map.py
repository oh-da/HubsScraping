#!/usr/bin/env python3
"""
Lines map: the mass-transit lines of the Hubs project next to those of the Minhal project.

Hubs lines have no alignment in the DataPack, only stops (All_nodes+lines), so each line is drawn
as straight links between consecutive stops. The stop order comes from Routes_and_Nodes when that
order is geographically sensible, otherwise it is inferred as the shortest path through the stops
(column `order_source` in the workbook says which).

Each stretch of each project's lines is classed against the other project:

    same    the other project has a line of the same mode family within --tolerance metres
    other   the other project has a line there, but of a different mode family
    none    the other project has no line there

Hubs links are tested at both end stops against Minhal's alignments; Minhal alignments are sampled
every --step metres and tested against the Hubs stop-to-stop links. Heavy rail is the exception on
the Minhal side: Hubs rail services skip stations, so their links cut corners by kilometres and say
nothing about the alignment in between. Minhal rail alignments are therefore left unclassed and
Minhal's rail *stations* are compared with the Hubs rail stops instead (--rail-tolerance).

    python build_lines_map.py [--tolerance 200] [--rail-tolerance 500]

Writes lines_map.html and lines_comparison.xlsx next to this script.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

CLASSES = ["same", "other", "none"]


# ---- Hubs lines: stops -> ordered polyline -------------------------------------------------------
def path_length(xy: np.ndarray) -> float:
    return float(np.hypot(*np.diff(xy, axis=0).T).sum()) if len(xy) > 1 else 0.0


def shortest_path_order(xy: np.ndarray) -> list[int]:
    """Short open path through all points: nearest-neighbour from every start, then 2-opt."""
    n = len(xy)
    if n <= 2:
        return list(range(n))
    d = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
    best, best_len = None, np.inf
    for start in range(n):
        order, left = [start], set(range(n)) - {start}
        while left:
            nxt = min(left, key=lambda j: d[order[-1], j])
            order.append(nxt)
            left.remove(nxt)
        improved = True
        while improved:                                   # 2-opt on an open path
            improved = False
            for i in range(n - 1):
                for j in range(i + 1, n):
                    before = (d[order[i - 1], order[i]] if i > 0 else 0) + (d[order[j], order[j + 1]] if j < n - 1 else 0)
                    after = (d[order[i - 1], order[j]] if i > 0 else 0) + (d[order[i], order[j + 1]] if j < n - 1 else 0)
                    if after < before - 1e-6:
                        order[i:j + 1] = order[i:j + 1][::-1]
                        improved = True
        length = sum(d[a, b] for a, b in zip(order, order[1:]))
        if length < best_len:
            best, best_len = order, length
    return best


def build_hubs_lines(route_slack: float) -> gpd.GeoDataFrame:
    nodes = C.read_csv_he(C.HUBS_NODES)
    modes = C.read_csv_he(C.HUBS_MODES).drop_duplicates("Line_ModelName").set_index("Line_ModelName")
    routes = pd.read_excel(C.HUBS_ROUTES, sheet_name="All")
    route_nodes = routes.groupby("Line")["Node"].apply(list).to_dict()

    rows = []
    for line_id, g in nodes.groupby("LINE_ID", sort=True):
        g = g.drop_duplicates("node")
        xy_by_node = {int(n): (x, y) for n, x, y in zip(g["node"], g["X"], g["Y"])}
        ids = list(xy_by_node)
        inferred = [ids[i] for i in shortest_path_order(np.array([xy_by_node[i] for i in ids]))]
        order, source = inferred, "inferred (no route order in Routes_and_Nodes)"
        rt = [n for i, n in enumerate(route_nodes.get(line_id, [])) if i == 0 or n != route_nodes[line_id][i - 1]]
        if rt:
            if set(rt) != set(ids):
                source = "inferred (route order lists different stops)"
            elif path_length(np.array([xy_by_node[n] for n in rt])) > route_slack * path_length(np.array([xy_by_node[n] for n in inferred])):
                source = "inferred (route order is not geographic)"
            else:
                order, source = rt, "Routes_and_Nodes"
        xy = np.array([xy_by_node[n] for n in order])
        if line_id not in modes.index:
            raise ValueError(f"{line_id} has no row in {C.HUBS_MODES.name}")
        m = modes.loc[line_id]
        rows.append({"line_id": line_id, "mode_src": m["Mode_Planned"], "mode": C.MODE_MAP[m["Mode_Planned"]],
                     "line_name": m["Line_Name"], "description": m["Line_Description"], "area": m["Area"],
                     "n_stops": len(ids), "order_source": source, "stop_ids": order,
                     "geometry": LineString(xy) if len(xy) > 1 else None, "xy": xy})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=C.ITM)
    gdf["family"] = gdf["mode"].map(C.MODE_FAMILY)
    gdf["length_km"] = (gdf.length / 1000).round(2)
    return gdf


# ---- the comparison --------------------------------------------------------------------------------
def nearest_by_family(points: np.ndarray, target: gpd.GeoDataFrame) -> pd.DataFrame:
    """Distance from every point to the nearest target line of each family, and of any family."""
    pts = shapely.points(points)
    out = {}
    for fam, g in target.groupby("family"):
        out[fam] = shapely.distance(pts, shapely.union_all(g.geometry.values))
    df = pd.DataFrame(out)
    df["any"] = df.min(axis=1)
    df["nearest_family"] = df.drop(columns="any").idxmin(axis=1)
    return df


def classify(dist: pd.DataFrame, family, tol) -> np.ndarray:
    """same / other / none per row. `family` and `tol` are scalars or per-row arrays."""
    fam = np.broadcast_to(np.asarray(family, dtype=object), (len(dist),))
    tol = np.broadcast_to(np.asarray(tol, dtype=float), (len(dist),))
    same = np.array([dist[f].values[i] if f in dist.columns else np.inf for i, f in enumerate(fam)])
    return np.where(same <= tol, "same", np.where(dist["any"].values <= tol, "other", "none"))


def runs(labels) -> list[tuple[str, int, int]]:
    """[(label, first_index, last_index_inclusive), ...] for consecutive equal labels."""
    out, start = [], 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[start]:
            out.append((labels[start], start, i - 1))
            start = i
    return out


def smooth(labels: np.ndarray, min_len: int) -> np.ndarray:
    """Absorb runs shorter than min_len points into the longer neighbouring run."""
    labels = labels.copy()
    while True:
        rs = runs(labels)
        short = [r for r in rs if r[2] - r[1] + 1 < min_len]
        if not short or len(rs) == 1:
            return labels
        lab, a, b = min(short, key=lambda r: r[2] - r[1])
        i = rs.index((lab, a, b))
        left = rs[i - 1] if i > 0 else None
        right = rs[i + 1] if i < len(rs) - 1 else None
        nb = max([r for r in (left, right) if r], key=lambda r: r[2] - r[1])
        labels[a:b + 1] = nb[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tolerance", type=float, default=200, help="metres within which the other project's line counts as 'there'")
    ap.add_argument("--rail-tolerance", type=float, default=500, help="metres between a Minhal rail station and a Hubs rail stop to count as the same station")
    ap.add_argument("--step", type=float, default=50, help="sampling interval along Minhal alignments, metres")
    ap.add_argument("--route-slack", type=float, default=1.3, help="accept the Routes_and_Nodes stop order up to this multiple of the shortest path")
    ap.add_argument("--simplify", type=float, default=5, help="Douglas-Peucker tolerance for the Minhal alignments on the map, metres")
    args = ap.parse_args()

    hubs = build_hubs_lines(args.route_slack)
    minhal, minhal_stations = C.load_minhal()
    minhal["family"] = minhal["mode"].map(C.MODE_FAMILY)
    minhal["piece"] = range(len(minhal))
    minhal["length_km"] = (minhal.length / 1000).round(2)
    minhal_active = minhal[minhal["active"]]
    hubs_drawn = hubs[hubs.geometry.notna()]

    # ---- Hubs side: every stop against Minhal's (active) alignments; a link takes the worse end ----
    order = {c: i for i, c in enumerate(CLASSES)}
    hubs_feats, stops, hubs_rows = [], {}, []
    for r in hubs.itertuples():
        d = nearest_by_family(r.xy, minhal_active)
        stop_cls = classify(d, r.family, args.tolerance)
        for nid, (x, y), c in zip(r.stop_ids, r.xy, stop_cls):
            s = stops.setdefault(nid, {"xy": (x, y), "lines": [], "cls": c})
            s["lines"].append(r.line_id)
            s["cls"] = min(s["cls"], c, key=order.get)          # a stop shared by lines keeps its best class
        link_cls = [max(a, b, key=order.get) for a, b in zip(stop_cls, stop_cls[1:])]
        link_len = np.hypot(*np.diff(r.xy, axis=0).T) if len(r.xy) > 1 else np.array([])
        share = {c: (float(link_len[np.array(link_cls) == c].sum() / link_len.sum()) if link_len.sum() else float(stop_cls[0] == c))
                 for c in CLASSES}
        other_fam = d.loc[stop_cls == "other", "nearest_family"].mode()
        hubs_rows.append({"line_id": r.line_id, "line_name": r.line_name, "description": r.description, "area": r.area,
                          "mode_hubs": r.mode_src, "mode": r.mode, "n_stops": r.n_stops, "length_km": r.length_km,
                          "pct_same_mode": round(100 * share["same"], 1), "pct_other_mode": round(100 * share["other"], 1),
                          "pct_not_in_minhal": round(100 * share["none"], 1),
                          "other_mode_in_minhal": other_fam.iat[0] if len(other_fam) else None,
                          "order_source": r.order_source})
        if r.geometry is None:
            continue
        ll = gpd.GeoSeries([r.geometry], crs=C.ITM).to_crs(C.WGS).iat[0]
        coords = C.round_coords(ll.coords)
        hubs_feats.append({"id": r.line_id, "mode": r.mode, "name": r.line_name, "desc": r.description, "area": r.area,
                           "src": r.mode_src, "stops": r.n_stops, "km": r.length_km, "inferred": not r.order_source.startswith("Routes"),
                           "pct": [hubs_rows[-1]["pct_same_mode"], hubs_rows[-1]["pct_other_mode"], hubs_rows[-1]["pct_not_in_minhal"]],
                           "runs": [[c, coords[a:b + 2]] for c, a, b in runs(link_cls)]})

    # ---- Minhal side: sample every alignment against the Hubs links ----------------------------------
    minhal_feats, minhal_rows = [], []
    to_wgs = lambda g: gpd.GeoSeries([g], crs=C.ITM).to_crs(C.WGS).iat[0]
    for r in minhal.itertuples():
        is_rail = r.family == "Rail"
        feat_runs, len_by = [], dict.fromkeys(CLASSES, 0.0)
        for part in r.geometry.geoms:
            if is_rail:                                   # compared by station, below
                feat_runs.append(["rail", C.round_coords(to_wgs(part.simplify(args.simplify)).coords)])
                continue
            dense = shapely.segmentize(part, args.step)
            xy = np.asarray(dense.coords)
            cls = smooth(classify(nearest_by_family(xy, hubs_drawn), r.family, args.tolerance), max(2, int(round(args.tolerance / args.step))))
            for c, a, b in runs(list(cls)):
                seg = LineString(xy[a:min(b + 2, len(xy))]) if b + 1 < len(xy) or a < b else None
                if seg is None:
                    continue
                len_by[c] += seg.length
                feat_runs.append([c, C.round_coords(to_wgs(seg.simplify(args.simplify)).coords)])
        total = sum(len_by.values())
        pct = [round(100 * len_by[c] / total, 1) for c in CLASSES] if total else [None, None, None]
        label = r.line_name if isinstance(r.line_name, str) else r.name
        minhal_rows.append({"piece": r.piece, "line_id": r.line_id, "line_name": r.line_name, "name": r.name, "type": r.type,
                            "mode": r.mode, "year_operation": r.year_operation, "year_cancel": r.year_cancel,
                            "source_plan": r.source_plan, "source_name": r.source_name, "length_km": r.length_km,
                            "active": r.active, "pct_same_mode": pct[0], "pct_other_mode": pct[1], "pct_not_in_hubs": pct[2]})
        minhal_feats.append({"id": int(r.line_id), "mode": r.mode, "name": label, "alt": r.name if isinstance(r.line_name, str) else None,
                             "year": int(r.year_operation), "cancel": None if pd.isna(r.year_cancel) else int(r.year_cancel),
                             "plan": r.source_plan, "plan_name": r.source_name, "km": r.length_km, "active": bool(r.active),
                             "pct": pct, "runs": feat_runs})

    # ---- Minhal heavy rail: stations against Hubs rail stops ------------------------------------------
    rs = minhal_stations[minhal_stations["mode"].map(C.MODE_FAMILY) == "Rail"].copy()
    line_family = hubs.set_index("line_id")["family"]
    stop_xy = np.array([s["xy"] for s in stops.values()])
    stop_is_rail = np.array([any(line_family[l] == "Rail" for l in s["lines"]) for s in stops.values()])
    rxy = np.c_[rs.geometry.x, rs.geometry.y]
    d_rail, i_rail = cKDTree(stop_xy[stop_is_rail]).query(rxy)
    d_any, i_any = cKDTree(stop_xy).query(rxy)
    stop_ids = np.array(list(stops))
    rs["cls"] = np.where(d_rail <= args.rail_tolerance, "same", np.where(d_any <= args.rail_tolerance, "other", "none"))
    rs["hubs_rail_stop"], rs["dist_to_hubs_rail_stop_m"] = stop_ids[stop_is_rail][i_rail], d_rail.round(0)
    rs["hubs_any_stop"], rs["dist_to_hubs_any_stop_m"] = stop_ids[i_any], d_any.round(0)
    rs_ll = rs.to_crs(C.WGS)
    rail_feats = [{"name": r.station_name, "mode": r.mode, "line": int(r.line_id), "year": int(r.year_operation),
                   "plan": r.source_plan, "active": bool(r.active), "cls": r.cls, "d": int(r.dist_to_hubs_rail_stop_m),
                   "ll": [round(r.geometry.y, 5), round(r.geometry.x, 5)]} for r in rs_ll.itertuples()]
    rail_tbl = pd.DataFrame(rs.drop(columns=["geometry", "hub_id", "rail_hub", "hub_stop_id", "family"], errors="ignore"))

    # ---- outputs ---------------------------------------------------------------------------------------
    hubs_tbl, minhal_tbl = pd.DataFrame(hubs_rows), pd.DataFrame(minhal_rows)
    km = lambda df, col: (df["length_km"] * df[col].fillna(0) / 100).sum()
    summary = []
    for mode in C.MODE_ORDER:
        h, m = hubs_tbl[hubs_tbl["mode"] == mode], minhal_tbl[(minhal_tbl["mode"] == mode) & minhal_tbl["active"]]
        mc = minhal_tbl[(minhal_tbl["mode"] == mode) & ~minhal_tbl["active"]]
        st = rs[(rs["mode"] == mode) & rs["active"]]
        is_rail = C.MODE_FAMILY[mode] == "Rail"
        summary.append({"mode": mode, "hubs_lines": len(h), "hubs_km": round(h["length_km"].sum(), 1),
                        "hubs_km_same_mode_in_minhal": round(km(h, "pct_same_mode"), 1), "hubs_km_other_mode_in_minhal": round(km(h, "pct_other_mode"), 1),
                        "hubs_km_not_in_minhal": round(km(h, "pct_not_in_minhal"), 1),
                        "minhal_pieces": len(m), "minhal_line_ids": m["line_id"].nunique(), "minhal_km": round(m["length_km"].sum(), 1),
                        "minhal_km_same_mode_in_hubs": None if is_rail else round(km(m, "pct_same_mode"), 1),
                        "minhal_km_other_mode_in_hubs": None if is_rail else round(km(m, "pct_other_mode"), 1),
                        "minhal_km_not_in_hubs": None if is_rail else round(km(m, "pct_not_in_hubs"), 1),
                        "minhal_rail_stations": len(st) if is_rail else None,
                        "minhal_rail_stations_same_mode_in_hubs": int((st["cls"] == "same").sum()) if is_rail else None,
                        "minhal_rail_stations_other_mode_in_hubs": int((st["cls"] == "other").sum()) if is_rail else None,
                        "minhal_rail_stations_not_in_hubs": int((st["cls"] == "none").sum()) if is_rail else None,
                        "minhal_pieces_cancelled": len(mc), "minhal_km_cancelled": round(mc["length_km"].sum(), 1)})
    summary = pd.DataFrame(summary)
    notes = pd.DataFrame({"note": [
        "Hubs lines are per direction (each LINE_ID is one direction), so Hubs km count both directions; Minhal alignments are drawn once.",
        "Hubs geometry = straight links between consecutive stops; a link is 'same' only when both end stops are within the tolerance of a Minhal line of the same mode family.",
        f"Tolerance {args.tolerance:.0f} m; Minhal alignments sampled every {args.step:.0f} m.",
        f"Minhal heavy rail (Rail, Rail fast) alignments are not classed: Hubs rail services skip stations, so their stop-to-stop links do not follow the track. "
        f"Minhal rail stations are compared with Hubs rail stops instead ({args.rail_tolerance:.0f} m), sheet minhal_rail_stations.",
        "Minhal lines with a year_cancel (column active = False) are not part of the comparison target for Hubs lines and are excluded from the Minhal totals above.",
        "Mode families: Rail = Rail + Rail fast (Hubs: Suburban, Interurban, HighSpeed); Funicular / cable = Minhal FUNI, Hubs Funicular + Cable Line.",
    ]})
    out_x = C.HERE / "lines_comparison.xlsx"
    with pd.ExcelWriter(out_x, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="summary_by_mode", index=False)
        hubs_tbl.sort_values(["mode", "area", "line_id"]).to_excel(xw, sheet_name="hubs_lines", index=False)
        minhal_tbl.sort_values(["mode", "line_id", "piece"]).to_excel(xw, sheet_name="minhal_lines", index=False)
        rail_tbl.sort_values(["cls", "line_id"]).to_excel(xw, sheet_name="minhal_rail_stations", index=False)
        notes.to_excel(xw, sheet_name="notes", index=False)
    print(f"wrote {out_x.relative_to(C.HERE.parent)}")
    print(summary.to_string(index=False))
    print("Hubs stop order:", hubs["order_source"].value_counts().to_dict())

    sx, sy = np.array([s["xy"] for s in stops.values()]).T
    slon, slat = gpd.GeoSeries(gpd.points_from_xy(sx, sy), crs=C.ITM).to_crs(C.WGS).pipe(lambda g: (g.x.values, g.y.values))
    line_mode = hubs.set_index("line_id")["mode"]
    stop_feats = [{"id": int(nid), "ll": [round(float(la), 5), round(float(lo), 5)], "cls": s["cls"],
                   "lines": s["lines"], "modes": C.sort_modes(line_mode[s["lines"]])}
                  for (nid, s), lo, la in zip(stops.items(), slon, slat)]

    C.render("lines_map.html", C.HERE / "lines_map.html", {
        "__HUBS__": C.dumps(hubs_feats), "__MINHAL__": C.dumps(minhal_feats), "__STOPS__": C.dumps(stop_feats), "__RAIL_STATIONS__": C.dumps(rail_feats),
        "__MODES__": C.dumps(C.MODE_ORDER), "__TOL__": f"{args.tolerance:.0f}", "__RAIL_TOL__": f"{args.rail_tolerance:.0f}",
        "__SUMMARY__": C.dumps(summary.to_dict("records")),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
