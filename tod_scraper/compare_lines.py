#!/usr/bin/env python3
"""
Compare the transit network of the Hubs project (HubsPrioritizing: csv of node x line
memberships, ITM coordinates) with the lines and stations of Minhal (the TOD Israel map
of מנהל התכנון scraped by this repository), by geometry only.

The Hubs project file (Data/All_nodes+lines_V1.01.csv) lists, per line id, the model
nodes the line stops at. It has no route geometry, no stop order and its line names do
not follow Minhal's naming, so nothing here relies on names:

1. offset  - the two sources do not sit exactly on top of each other. A local shift
             field (median node -> station offset of the matched pairs within
             --shift-radius) is estimated and applied to the Hubs nodes before
             anything else is measured; the raw and corrected distances are both kept.
2. mode    - the mode of a Hubs line is taken from Minhal: the mode whose Minhal
             lines run within --tol-line of most of its nodes (the line-name prefix is
             reported only as a hint).
3. nodes   - every Hubs node is matched to the nearest Minhal station (<= --tol-station)
             and to the nearest Minhal line of each mode (<= --tol-line).
4. routing - the nodes of a line are ordered (shortest open path through them) and
             each consecutive pair is routed along the Minhal network of the line's mode.
             A pair with no path, or whose Minhal route is much longer than the straight
             distance, is a routing difference; a node that is not on the Minhal network
             is reported as off-network.
5. reverse - Minhal stations with no Hubs node nearby, and stretches of the Minhal
             network (>= --min-gap metres) that no routed Hubs line uses.

Outputs data/out/line_comparison.xlsx, data/out/line_comparison.gpkg and
data/out/line_comparison_map.html (an interactive map of all of the above).

    python compare_lines.py ../Data/All_nodes+lines_V1.01.csv --leaflet-css leaflet.css \
        [--tol-station 150] [--tol-line 100] [--min-gap 300] [--copy-map-to ../compare_lines.html]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree

ITM = "EPSG:2039"
MODES = ["Metro", "LRT", "BRT", "Rail", "Funicular"]
# line-name prefix -> mode hint (only reported, never used for matching)
HINTS = [(r"^rail", "Rail"), (r"^(brt|BRT|BluRT|RH-GY)", "BRT"), (r"^(lrt|LRT)", "LRT"), (r"_LRT_", "LRT"),
         (r"^(M1|Me0)", "Metro"), (r"^(c0|s0)", "Funicular")]      # m0xxx are Haifa's Metronit (BRT), no hint


def mode_hint(line_id: str) -> str:
    for pat, m in HINTS:
        if re.search(pat, line_id):
            return m
    return ""


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def read_hubs(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1255", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    cols = {c.lower(): c for c in df.columns}
    node = cols.get("node") or cols.get("node_id") or cols.get("n")
    line = cols.get("line_id") or cols.get("line") or cols.get("lineid")
    x = cols.get("x") or cols.get("x_itm") or cols.get("east")
    y = cols.get("y") or cols.get("y_itm") or cols.get("north")
    df = df.rename(columns={node: "node", line: "LINE_ID", x: "X", y: "Y"})[["node", "LINE_ID", "X", "Y"]]
    df["LINE_ID"] = df["LINE_ID"].astype(str).str.strip()
    df = df.dropna(subset=["X", "Y"]).drop_duplicates(["node", "LINE_ID"]).reset_index(drop=True)
    return df


def local_shift(N: np.ndarray, S: np.ndarray, tol: float, radius: float, min_pairs: int, passes: int = 2):
    """Median offset (station - node) of the matched pairs around every node; global median elsewhere."""
    tree = cKDTree(S)
    shift = np.zeros_like(N)
    glob = np.zeros(2)
    for _ in range(passes):
        d, i = tree.query(N + shift)
        m = d <= tol
        if m.sum() < min_pairs:
            break
        off = S[i[m]] - N[m]                       # offset relative to the raw node position
        glob = np.median(off, axis=0)
        pt = cKDTree(N[m])
        new = np.tile(glob, (len(N), 1))
        for k in range(len(N)):
            idx = pt.query_ball_point(N[k], radius)
            if len(idx) >= min_pairs:
                new[k] = np.median(off[idx], axis=0)
        shift = new
    return shift, glob


def order_points(xy: np.ndarray) -> list[int]:
    """Shortest open path through the points: greedy chains from a few starts, improved by 2-opt
    (closed-tour 2-opt with a dummy node that is free to reach, then cut at the dummy)."""
    n = len(xy)
    if n <= 2:
        return list(range(n))
    D = np.sqrt(((xy[:, None, :] - xy[None, :, :]) ** 2).sum(-1))
    Dd = np.zeros((n + 1, n + 1))
    Dd[:n, :n] = D                                     # node n is the dummy (distance 0 to everything)
    c = xy.mean(axis=0)
    starts = list(np.argsort(-np.hypot(*(xy - c).T))[:4]) + [int(np.argmin(xy[:, 1])), int(np.argmin(xy[:, 0]))]
    best, best_len = None, np.inf
    for s in dict.fromkeys(int(v) for v in starts):
        path, used = [s], {s}
        while len(path) < n:
            cur = path[-1]
            cand = [j for j in range(n) if j not in used]
            j = cand[int(np.argmin(D[cur, cand]))]
            path.append(j)
            used.add(j)
        tour = path + [n]
        improved = True
        while improved:
            improved = False
            for i in range(0, n):
                for j in range(i + 2, n + 1):
                    a, b, cc, d = tour[i], tour[i + 1], tour[j], tour[(j + 1) % (n + 1)]
                    if a == d:
                        continue
                    if Dd[a, cc] + Dd[b, d] < Dd[a, b] + Dd[cc, d] - 1e-6:
                        tour[i + 1:j + 1] = tour[i + 1:j + 1][::-1]
                        improved = True
        k = tour.index(n)
        open_path = tour[k + 1:] + tour[:k]
        L = sum(D[open_path[t], open_path[t + 1]] for t in range(n - 1))
        if L < best_len:
            best, best_len = open_path, L
    return best


class ModeNetwork:
    """The Minhal lines of one mode as a routable graph (noded, endpoints within join_tol joined)."""

    def __init__(self, geoms, join_tol: float = 20.0):
        u = unary_union(list(geoms))
        pieces = list(u.geoms) if hasattr(u, "geoms") else [u]
        vid: dict[tuple, int] = {}
        coords: list[tuple] = []
        eu, ev, ew = [], [], []

        def vertex(p):
            key = (round(p[0], 2), round(p[1], 2))
            if key not in vid:
                vid[key] = len(coords)
                coords.append(key)
            return vid[key]

        for p in pieces:
            cs = list(p.coords)
            for a, b in zip(cs[:-1], cs[1:]):
                i, j = vertex(a), vertex(b)
                if i != j:
                    eu.append(i); ev.append(j); ew.append(float(np.hypot(a[0] - b[0], a[1] - b[1])))
        self.coords = np.array(coords)
        deg = np.bincount(np.r_[eu, ev], minlength=len(coords))
        ends = np.where(deg == 1)[0]
        if len(ends) and join_tol > 0:                 # join dangling ends to a vertex nearby (fragmented layer)
            tree = cKDTree(self.coords)
            for i in ends:
                for j in tree.query_ball_point(self.coords[i], join_tol):
                    if j != i:
                        eu.append(int(i)); ev.append(int(j)); ew.append(float(np.hypot(*(self.coords[i] - self.coords[j]))))
                        break
        self.eu, self.ev, self.ew = np.array(eu), np.array(ev), np.array(ew)
        self.n = len(coords)
        self.segs = shapely.linestrings(np.stack([self.coords[self.eu], self.coords[self.ev]], axis=1))
        self.tree = STRtree(self.segs)
        self.edge_id = {(min(a, b), max(a, b)): k for k, (a, b) in enumerate(zip(eu, ev))}

    def snap(self, xy: np.ndarray, tol: float):
        """nearest edge for every point: (edge index, projected point, distance) or None beyond tol."""
        out = []
        pts = shapely.points(xy)
        for p in pts:
            k = int(self.tree.nearest(p))
            s = self.segs[k]
            d = s.distance(p)
            out.append((k, np.array(s.interpolate(s.project(p)).coords[0]), d) if d <= tol else None)
        return out

    def route(self, snaps):
        """Dijkstra between consecutive snapped stops. Returns per pair: (length or None, vertex path coords)."""
        virt = [k for k, s in enumerate(snaps) if s is not None]
        if len(virt) < 2:
            return {}
        nv = len(virt)
        vu, vv, vw = [], [], []
        vcoord = {}
        for t, k in enumerate(virt):
            e, P, _ = snaps[k]
            a, b = int(self.eu[e]), int(self.ev[e])
            vidx = self.n + t
            vcoord[vidx] = P
            vu += [vidx, vidx]; vv += [a, b]; vw += [float(np.hypot(*(P - self.coords[a]))), float(np.hypot(*(P - self.coords[b])))]
        u = np.r_[self.eu, vu]; v = np.r_[self.ev, vv]; w = np.r_[self.ew, vw] + 1e-6
        N = self.n + nv
        g = coo_matrix((np.r_[w, w], (np.r_[u, v], np.r_[v, u])), shape=(N, N)).tocsr()
        src = [self.n + t for t in range(nv)]
        dist, pred = dijkstra(g, directed=False, indices=src, return_predecessors=True)
        out = {}
        for t in range(nv - 1):
            k0, k1 = virt[t], virt[t + 1]
            if k1 != k0 + 1:
                continue                               # an unsnapped stop sits between them
            tgt = self.n + t + 1
            L = dist[t, tgt]
            if not np.isfinite(L):
                out[k0] = (None, None, [])
                continue
            path = [tgt]
            while path[-1] != src[t]:
                path.append(int(pred[t, path[-1]]))
            path = path[::-1]
            pts = [vcoord[i] if i >= self.n else self.coords[i] for i in path]
            edges = [self.edge_id.get((min(a, b), max(a, b))) for a, b in zip(path[:-1], path[1:]) if a < self.n and b < self.n]
            out[k0] = (float(L), np.array(pts), [e for e in edges if e is not None])
        return out


def to_wgs(geoms, crs=ITM):
    return gpd.GeoSeries(geoms, crs=crs).to_crs(4326)


def rc(g, nd=5):
    """polyline -> [[lat, lng], ...] rounded"""
    return [[round(y, nd), round(x, nd)] for x, y in g.coords]


# ---------------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="Hubs project node x line csv (node, LINE_ID, X, Y in EPSG:2039)")
    ap.add_argument("--data", default="data")
    ap.add_argument("--tol-station", type=float, default=150, help="Hubs node <-> Minhal station match radius (m, after shift)")
    ap.add_argument("--tol-line", type=float, default=100, help="Hubs node <-> Minhal line radius (m): on the network / mode vote")
    ap.add_argument("--tol-line-rail", type=float, default=250, help="same for Rail (stations sit further from the drawn track)")
    ap.add_argument("--parallel-tol", type=float, default=60, help="an unused map stretch this close to a used one of the same mode is a parallel duplicate, not a gap")
    ap.add_argument("--near", type=float, default=400, help="upper radius for 'possible' station matches (m)")
    ap.add_argument("--detour", type=float, default=1.5, help="Minhal route / straight distance above which a stop pair is a routing difference")
    ap.add_argument("--detour-min", type=float, default=300, help="... and the extra length must exceed this many metres")
    ap.add_argument("--min-gap", type=float, default=300, help="report Minhal network stretches no Hubs line uses from this length (m)")
    ap.add_argument("--shift", choices=["local", "global", "none"], default="local")
    ap.add_argument("--shift-radius", type=float, default=10000)
    ap.add_argument("--shift-min-pairs", type=int, default=15)
    ap.add_argument("--leaflet-css", required=True)
    ap.add_argument("--leaflet-js", default="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js")
    ap.add_argument("--copy-map-to", default=None)
    args = ap.parse_args()
    data = Path(args.data)
    out_dir = data / "out"

    # ---- inputs ---------------------------------------------------------------------------------
    ext = read_hubs(Path(args.csv))
    nodes = ext.drop_duplicates("node")[["node", "X", "Y"]].reset_index(drop=True)
    nodes["lines"] = nodes["node"].map(ext.groupby("node")["LINE_ID"].agg(list))
    N = nodes[["X", "Y"]].to_numpy(float)

    st = pd.read_excel(out_dir / "tod_israel.xlsx", sheet_name="stations")
    if "x_itm" not in st.columns:
        g = gpd.GeoSeries(gpd.points_from_xy(st["lng"], st["lat"]), crs=4326).to_crs(ITM)
        st["x_itm"], st["y_itm"] = g.x, g.y
    S = st[["x_itm", "y_itm"]].to_numpy(float)
    ln = gpd.read_file(out_dir / "tod_israel.gpkg", layer="lines").to_crs(ITM)
    ln = ln[ln.geometry.notna() & (ln.geometry.length > 0)].reset_index(drop=True)
    nets = {m: ModeNetwork(ln.loc[ln["mode"] == m, "geometry"]) for m in MODES if (ln["mode"] == m).any()}
    unions = {m: unary_union(list(ln.loc[ln["mode"] == m, "geometry"])) for m in nets}
    print(f"Hubs project: {len(ext)} node x line rows, {len(nodes)} nodes, {ext['LINE_ID'].nunique()} lines; "
          f"Minhal: {len(st)} stations, {len(ln)} line segments ({', '.join(f'{m} {len(nets[m].coords)} vertices' for m in nets)})")

    # ---- 1. offset between the sources --------------------------------------------------------
    if args.shift == "none":
        shift, glob = np.zeros_like(N), np.zeros(2)
    elif args.shift == "global":
        _, glob = local_shift(N, S, args.tol_station, 1e9, args.shift_min_pairs)
        shift = np.tile(glob, (len(N), 1))
    else:
        shift, glob = local_shift(N, S, args.tol_station, args.shift_radius, args.shift_min_pairs)
    A = N + shift                                          # node coordinates in "map space"
    nodes["shift_dx"], nodes["shift_dy"] = shift[:, 0].round(1), shift[:, 1].round(1)

    # ---- 2+3. nodes vs stations / lines -------------------------------------------------------
    stree = cKDTree(S)
    d_raw, _ = stree.query(N)
    d_adj, i_adj = stree.query(A)
    nodes["x_adj"], nodes["y_adj"] = A[:, 0].round(1), A[:, 1].round(1)
    nodes["dist_minhal_station_raw_m"] = d_raw.round(0)
    nodes["dist_minhal_station_m"] = d_adj.round(0)
    nodes["minhal_station_id"] = st.loc[i_adj, "id"].values
    nodes["minhal_station_name"] = st.loc[i_adj, "name"].values
    nodes["minhal_station_modes"] = st.loc[i_adj, "modes"].values
    nodes["minhal_station_status"] = st.loc[i_adj, "planning_status"].values
    nodes["minhal_station_metropolin"] = st.loc[i_adj, "metropolin"].values if "metropolin" in st.columns else None
    nodes["minhal_station_match"] = nodes["dist_minhal_station_m"] <= args.tol_station
    nodes["minhal_station_possible"] = (~nodes["minhal_station_match"]) & (nodes["dist_minhal_station_m"] <= args.near)
    TOL = {m: (args.tol_line_rail if m == "Rail" else args.tol_line) for m in unions}
    apts = shapely.points(A)
    for m, u in unions.items():
        nodes[f"dist_minhal_line_{m}_m"] = shapely.distance(apts, u).round(0)
    dl = nodes[[f"dist_minhal_line_{m}_m" for m in unions]]
    nodes["dist_minhal_line_m"] = dl.min(axis=1)
    nodes["minhal_modes_near"] = [" | ".join(m for m in unions if r[f"dist_minhal_line_{m}_m"] <= TOL[m]) for _, r in nodes.iterrows()]
    nodes["on_minhal_network"] = nodes["minhal_modes_near"] != ""
    nodes["n_lines"] = nodes["lines"].map(len)
    nodes["lines_str"] = nodes["lines"].map(" | ".join)

    # shift by metropolitan area (for the summary)
    region = nodes["minhal_station_metropolin"].fillna("other")
    matched = nodes["minhal_station_match"]
    shift_rows = []
    for reg, grp in nodes[matched].groupby(region[matched]):
        off = S[i_adj[grp.index]] - N[grp.index]
        shift_rows.append({"area": reg, "matched nodes": len(grp), "median dx (m)": round(float(np.median(off[:, 0])), 1),
                           "median dy (m)": round(float(np.median(off[:, 1])), 1),
                           "offset after shift, median (m)": round(float(np.median(grp["dist_minhal_station_m"])), 1),
                           "offset after shift, 90th pct (m)": round(float(np.percentile(grp["dist_minhal_station_m"], 90)), 1)})
    shift_tbl = pd.DataFrame(shift_rows)

    # ---- 4. lines: mode vote, order, routing ---------------------------------------------------
    nidx = {n: k for k, n in enumerate(nodes["node"])}
    members = ext.groupby("LINE_ID")["node"].agg(list)
    routes: dict[frozenset, dict] = {}                       # identical node sets (the two directions) share a route
    line_rows, seg_rows = [], []
    used_edges: dict[str, dict[int, set]] = {m: defaultdict(set) for m in nets}
    for lid, mem in members.items():
        key = frozenset(mem)
        idx = [nidx[n] for n in mem]
        sub = nodes.loc[idx]
        hint = mode_hint(lid)
        votes = {m: int((sub[f"dist_minhal_line_{m}_m"] <= TOL[m]).sum()) for m in unions}
        top = max(votes.values()) if votes else 0
        cands = [m for m, v in votes.items() if v == top and v > 0]
        st_modes = " | ".join(sub.loc[sub["minhal_station_match"], "minhal_station_modes"].astype(str))
        if not cands:
            mode = ""
        elif len(cands) == 1 or hint in cands:
            mode = hint if hint in cands else cands[0]
        else:
            mode = max(cands, key=lambda m: (st_modes.count(m), -MODES.index(m)))
        if key in routes:
            r = routes[key]
            r["lines"].append(lid)
        else:
            order = order_points(A[idx])
            oidx = [idx[o] for o in order]
            r = {"route_id": len(routes) + 1, "lines": [lid], "mode": mode, "order": oidx, "segs": []}
            net = nets.get(mode)
            snaps = net.snap(A[oidx], TOL[mode]) if net else [None] * len(oidx)
            paths = net.route(snaps) if net else {}
            for t in range(len(oidx) - 1):
                a, b = oidx[t], oidx[t + 1]
                straight = float(np.hypot(*(A[a] - A[b])))
                seg = {"from": nodes.at[a, "node"], "to": nodes.at[b, "node"], "straight_m": round(straight)}
                if snaps[t] is None or snaps[t + 1] is None:
                    seg.update(kind="off-network", routed_m=None, geom=LineString([A[a], A[b]]))
                elif t not in paths or paths[t][0] is None:
                    seg.update(kind="no path", routed_m=None, geom=LineString([A[a], A[b]]))
                else:
                    L, pts, edges = paths[t]
                    detour = L / straight if straight > 0 else 1
                    if detour > args.detour and L - straight > args.detour_min:
                        seg.update(kind="routing differs", routed_m=round(L), geom=LineString([A[a], A[b]]))
                    else:
                        seg.update(kind="routed", routed_m=round(L), geom=LineString(pts) if len(pts) > 1 else LineString([A[a], A[b]]))
                        for e in edges:
                            used_edges[mode][e].add(r["route_id"])
                seg["ratio"] = round(seg["routed_m"] / straight, 2) if seg["routed_m"] and straight > 0 else None
                r["segs"].append(seg)
            routes[key] = r
        n = len(idx)
        n_st = int(sub["minhal_station_match"].sum())
        n_on = votes.get(mode, 0) if mode else 0
        n_any = int(sub["on_minhal_network"].sum())
        kinds = pd.Series([s["kind"] for s in r["segs"]])
        n_seg = len(r["segs"])
        n_routed = int((kinds == "routed").sum())
        share = n_on / n if n else 0
        if n_any == 0:
            status = "missing in Minhal"
        elif share >= 0.9 and (n_seg == 0 or n_routed / n_seg >= 0.9):
            status = "match"
        elif share >= 0.5:
            status = "partial"
        else:
            status = "mostly missing"
        st_share = n_st / n if n else 0
        stations_status = "all" if st_share == 1 else "most" if st_share >= 0.75 else "some" if st_share >= 0.25 else "few"
        line_rows.append({
            "LINE_ID": lid, "route_id": r["route_id"], "same_nodes_as": "", "name_mode_hint": hint, "minhal_mode": mode,
            "mode_hint_differs": bool(hint and mode and hint != mode), "status": status, "stations_status": stations_status, "n_nodes": n,
            "nodes_with_minhal_station": n_st, "pct_nodes_with_minhal_station": round(100 * n_st / n) if n else None,
            "nodes_on_minhal_line_of_mode": n_on, "pct_nodes_on_minhal_line": round(100 * share) if n else None,
            "nodes_on_any_minhal_line": n_any, "nodes_off_network": n - n_on,
            "minhal_station_dist_median_m": round(float(sub["dist_minhal_station_m"].median())) if n else None,
            "minhal_station_dist_max_m": round(float(sub["dist_minhal_station_m"].max())) if n else None,
            "minhal_line_dist_median_m": round(float(sub[f"dist_minhal_line_{mode}_m"].median())) if mode else None,
            "minhal_line_dist_max_m": round(float(sub[f"dist_minhal_line_{mode}_m"].max())) if mode else None,
            "segments": n_seg, "segments_routed": n_routed,
            "segments_routing_differs": int((kinds == "routing differs").sum()),
            "segments_no_path": int((kinds == "no path").sum()),
            "segments_off_network": int((kinds == "off-network").sum()),
            "straight_length_m": int(sum(s["straight_m"] for s in r["segs"])),
            "routed_length_m": int(sum(s["geom"].length for s in r["segs"])),
            "nodes_without_minhal_station": " | ".join(str(v) for v in sub.loc[~sub["minhal_station_match"], "node"]),
            "nodes_off_minhal_line": " | ".join(str(v) for v in sub.loc[sub[f"dist_minhal_line_{mode}_m"] > args.tol_line, "node"]) if mode else " | ".join(str(v) for v in sub["node"]),
            "mode_votes": ", ".join(f"{m} {v}" for m, v in sorted(votes.items(), key=lambda kv: -kv[1]) if v),
        })
    lines = pd.DataFrame(line_rows)
    same = lines.groupby("route_id")["LINE_ID"].agg(list)
    lines["same_nodes_as"] = [" | ".join(x for x in same[r] if x != l) for r, l in zip(lines["route_id"], lines["LINE_ID"])]
    status_order = {"missing in Minhal": 0, "mostly missing": 1, "partial": 2, "match": 3}
    lines = lines.sort_values(["status", "pct_nodes_on_minhal_line", "LINE_ID"], key=lambda s: s.map(status_order) if s.name == "status" else s)
    for key, r in routes.items():
        r["status"] = lines.loc[lines["route_id"] == r["route_id"], "status"].iloc[0]
        for t, s in enumerate(r["segs"]):
            seg_rows.append({"route_id": r["route_id"], "lines": " | ".join(r["lines"]), "minhal_mode": r["mode"], "seq": t + 1,
                             "from_node": s["from"], "to_node": s["to"], "kind": s["kind"], "straight_m": s["straight_m"],
                             "minhal_route_m": s["routed_m"], "ratio": s["ratio"]})
    segments = pd.DataFrame(seg_rows)

    # ---- 5. reverse: Minhal -> Hubs project ------------------------------------------------------------
    atree = cKDTree(A)
    d_s2n, i_s2n = atree.query(S)
    st["dist_hubs_node_m"] = d_s2n.round(0)
    st["nearest_hubs_node"] = nodes.loc[i_s2n, "node"].values
    st["nearest_hubs_node_lines"] = nodes.loc[i_s2n, "lines_str"].values
    only_map_st = st.loc[st["dist_hubs_node_m"] > args.tol_station,
                         [c for c in ["id", "name", "modes", "n_modes", "planning_status", "yearOperation", "hub_id", "municipality",
                                      "metropolin", "lng", "lat", "dist_hubs_node_m", "nearest_hubs_node", "nearest_hubs_node_lines"] if c in st.columns]]
    only_map_st = only_map_st.sort_values(["planning_status", "dist_hubs_node_m"], ascending=[True, False])

    cov_rows, gap_geoms, used_geoms = [], [], []
    route_by_id = {r["route_id"]: r for r in routes.values()}
    for m, net in nets.items():
        used = used_edges[m]
        n_edges = len(net.ew)
        cnt = np.array([len(used.get(k, ())) for k in range(n_edges)])
        used_len = float(net.ew[cnt > 0].sum())
        cov_rows.append({"Minhal mode": m, "Minhal network km": round(net.ew.sum() / 1000, 1), "km used by a Hubs line": round(used_len / 1000, 1),
                         "pct used": round(100 * used_len / net.ew.sum()) if net.ew.sum() else None})
        used_union = None
        if (cnt > 0).any():
            mu = linemerge(MultiLineString(list(net.segs[cnt > 0])))
            used_union = mu
            for g in (mu.geoms if hasattr(mu, "geoms") else [mu]):
                used_geoms.append({"mode": m, "geometry": g})
        par_len = 0.0
        if (cnt == 0).any():
            mg = linemerge(MultiLineString(list(net.segs[cnt == 0])))
            for g in (mg.geoms if hasattr(mg, "geoms") else [mg]):
                if g.length < args.min_gap:
                    continue
                parallel = False
                if used_union is not None and args.parallel_tol > 0:   # the other carriageway of a line already used
                    k = max(3, int(g.length // 50) + 1)
                    pts = shapely.points([g.interpolate(t, normalized=True).coords[0] for t in np.linspace(0, 1, k)])
                    parallel = float(np.mean(shapely.distance(pts, used_union) <= args.parallel_tol)) >= 0.8
                c = g.interpolate(0.5, normalized=True)
                d, k = atree.query([c.x, c.y])
                gap_geoms.append({"mode": m, "length_m": round(g.length), "parallel_to_used": parallel, "nearest_hubs_node": nodes.at[k, "node"],
                                  "nearest_hubs_node_lines": nodes.at[k, "lines_str"], "dist_nearest_node_m": round(float(d)), "geometry": g})
                par_len += g.length if parallel else 0
        cov_rows[-1]["km parallel to a used line"] = round(par_len / 1000, 1)
    gaps_all = gpd.GeoDataFrame(gap_geoms, geometry="geometry", crs=ITM) if gap_geoms else \
        gpd.GeoDataFrame(columns=["mode", "length_m", "parallel_to_used", "geometry"], geometry="geometry", crs=ITM)
    gaps_all = gaps_all.sort_values("length_m", ascending=False).reset_index(drop=True)
    gaps_all.insert(0, "gap_id", gaps_all.index + 1)
    gaps = gaps_all[~gaps_all["parallel_to_used"].astype(bool)].reset_index(drop=True)
    coverage = pd.DataFrame(cov_rows)

    # ---- summary --------------------------------------------------------------------------------
    n_lines, n_nodes = len(lines), len(nodes)
    summary = pd.DataFrame([
        {"item": "Hubs project lines", "value": n_lines, "note": f"{len(routes)} distinct node sets (both directions of a line share one)"},
        {"item": "Hubs project nodes", "value": n_nodes, "note": Path(args.csv).name},
        {"item": "Minhal stations / line segments", "value": f"{len(st)} / {len(ln)}", "note": "tod_israel.xlsx 'stations', tod_israel.gpkg 'lines'"},
        {"item": "offset Hubs -> Minhal, global median (m)", "value": f"dx {glob[0]:+.1f}, dy {glob[1]:+.1f}",
         "note": f"shift mode '{args.shift}'; the sheet 'shift' lists it per metropolitan area; applied to the Hubs nodes before matching"},
        {"item": "Hubs nodes with a Minhal station", "value": int(nodes["minhal_station_match"].sum()), "note": f"within {args.tol_station:.0f} m after shift ({int((d_raw <= args.tol_station).sum())} before shift)"},
        {"item": "Hubs nodes with a possible Minhal station", "value": int(nodes["minhal_station_possible"].sum()), "note": f"{args.tol_station:.0f}-{args.near:.0f} m, review"},
        {"item": "Hubs nodes without a Minhal station", "value": int((nodes["dist_minhal_station_m"] > args.near).sum()), "note": f"nearest Minhal station more than {args.near:.0f} m away"},
        {"item": "Hubs nodes on the Minhal network", "value": int(nodes["on_minhal_network"].sum()), "note": f"a Minhal line of any mode within {args.tol_line:.0f} m (Rail {args.tol_line_rail:.0f} m)"},
        {"item": "Hubs lines: match", "value": int((lines["status"] == "match").sum()), "note": ">= 90% of nodes on a Minhal line of the mode and >= 90% of stop pairs routed along it"},
        {"item": "Hubs lines: partial", "value": int((lines["status"] == "partial").sum()), "note": ">= 50% of nodes on a Minhal line of the mode"},
        {"item": "Hubs lines: mostly missing", "value": int((lines["status"] == "mostly missing").sum()), "note": "< 50% of nodes on a Minhal line of the mode"},
        {"item": "Hubs lines: missing in Minhal", "value": int((lines["status"] == "missing in Minhal").sum()), "note": "no node on any Minhal line"},
        {"item": "Hubs lines: Minhal stations all / most / some / few", "value": " / ".join(str(int((lines["stations_status"] == s).sum())) for s in ("all", "most", "some", "few")),
         "note": "share of the line's nodes with a Minhal station: 100% / >= 75% / >= 25% / < 25%"},
        {"item": "Hubs lines whose name hints another mode", "value": int(lines["mode_hint_differs"].sum()), "note": "name prefix vs the mode of the Minhal lines under the nodes"},
        {"item": "stop pairs: routing differs / no path / off-network", "value": f"{int((segments['kind'] == 'routing differs').sum())} / {int((segments['kind'] == 'no path').sum())} / {int((segments['kind'] == 'off-network').sum())}" if len(segments) else "0 / 0 / 0",
         "note": f"Minhal route > {args.detour:g} x straight (+{args.detour_min:.0f} m) / not connected / a stop off the Minhal network"},
        {"item": "Minhal stations without a Hubs node", "value": len(only_map_st), "note": f"no Hubs node within {args.tol_station:.0f} m"},
        {"item": "Minhal network stretches no Hubs line uses", "value": f"{len(gaps)} ({gaps['length_m'].sum() / 1000:.0f} km)" if len(gaps) else "0",
         "note": f">= {args.min_gap:.0f} m long, of the Minhal network of the line's mode; {int(gaps_all['parallel_to_used'].sum())} more stretches ({gaps_all.loc[gaps_all['parallel_to_used'], 'length_m'].sum() / 1000:.0f} km) run parallel (<= {args.parallel_tol:.0f} m) to a used line and are left out"},
    ])

    # ---- write xlsx + gpkg -----------------------------------------------------------------------
    node_cols = ["node", "n_lines", "lines_str", "X", "Y", "shift_dx", "shift_dy", "x_adj", "y_adj", "minhal_station_match", "minhal_station_possible",
                 "dist_minhal_station_m", "dist_minhal_station_raw_m", "minhal_station_id", "minhal_station_name", "minhal_station_modes", "minhal_station_status", "minhal_station_metropolin",
                 "on_minhal_network", "minhal_modes_near", "dist_minhal_line_m"] + [f"dist_minhal_line_{m}_m" for m in unions]
    nodes_out = nodes[node_cols].rename(columns={"lines_str": "lines"}).sort_values(["minhal_station_match", "dist_minhal_station_m"], ascending=[True, False])
    out_x = out_dir / "line_comparison.xlsx"
    with pd.ExcelWriter(out_x, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="summary", index=False)
        shift_tbl.to_excel(xw, sheet_name="shift", index=False)
        lines.to_excel(xw, sheet_name="hubs_lines", index=False)
        segments.to_excel(xw, sheet_name="hubs_stop_pairs", index=False)
        nodes_out.to_excel(xw, sheet_name="hubs_nodes", index=False)
        only_map_st.to_excel(xw, sheet_name="only_minhal_stations", index=False)
        gaps.drop(columns="geometry").to_excel(xw, sheet_name="only_minhal_lines", index=False)
        coverage.to_excel(xw, sheet_name="minhal_coverage", index=False)
    print(f"wrote {out_x}")
    print(summary.to_string(index=False))

    out_g = out_dir / "line_comparison.gpkg"
    if out_g.exists():
        out_g.unlink()
    g_routes = gpd.GeoDataFrame([{"route_id": r["route_id"], "lines": " | ".join(r["lines"]), "minhal_mode": r["mode"], "status": r["status"],
                                  "n_nodes": len(r["order"]),
                                  "geometry": MultiLineString([s["geom"] for s in r["segs"]]) if r["segs"] else Point(A[r["order"][0]])}
                                 for r in routes.values()], geometry="geometry", crs=ITM)
    g_routes[g_routes.geom_type == "MultiLineString"].to_file(out_g, layer="hubs_routes", driver="GPKG")
    g_segs = gpd.GeoDataFrame(segments.assign(geometry=[s["geom"] for r in routes.values() for s in r["segs"]]), geometry="geometry", crs=ITM)
    if len(g_segs):
        g_segs.to_file(out_g, layer="hubs_stop_pairs", driver="GPKG")
    gpd.GeoDataFrame(nodes_out, geometry=gpd.points_from_xy(nodes_out["x_adj"], nodes_out["y_adj"]), crs=ITM).to_file(out_g, layer="hubs_nodes", driver="GPKG")
    gpd.GeoDataFrame(nodes_out, geometry=gpd.points_from_xy(nodes_out["X"], nodes_out["Y"]), crs=ITM).to_file(out_g, layer="hubs_nodes_raw", driver="GPKG")
    if used_geoms:
        gpd.GeoDataFrame(used_geoms, geometry="geometry", crs=ITM).to_file(out_g, layer="minhal_lines_used", driver="GPKG")
    if len(gaps_all):
        gaps_all.to_file(out_g, layer="only_minhal_lines", driver="GPKG")       # parallel duplicates kept, flagged
    gpd.GeoDataFrame(only_map_st, geometry=gpd.points_from_xy(only_map_st["lng"], only_map_st["lat"]), crs=4326).to_file(out_g, layer="only_minhal_stations", driver="GPKG")
    print(f"wrote {out_g}")

    # ---- map ----------------------------------------------------------------------------------------
    A4 = np.c_[to_wgs(shapely.points(A)).x, to_wgs(shapely.points(A)).y]
    S4 = np.c_[st["lng"], st["lat"]]
    j_routes = []
    for r in routes.values():
        lr = lines[lines["route_id"] == r["route_id"]].iloc[0]
        segs4 = to_wgs([s["geom"].simplify(3) for s in r["segs"]])
        j_routes.append({"id": r["route_id"], "lines": r["lines"], "mode": r["mode"] or "?", "hint": mode_hint(r["lines"][0]), "status": r["status"], "sst": lr["stations_status"],
                         "n": int(lr["n_nodes"]), "nst": int(lr["nodes_with_minhal_station"]), "non": int(lr["nodes_on_minhal_line_of_mode"]),
                         "nseg": int(lr["segments"]), "nrt": int(lr["segments_routed"]), "ndiff": int(lr["segments_routing_differs"] + lr["segments_no_path"]),
                         "noff": int(lr["segments_off_network"]), "votes": lr["mode_votes"],
                         "segs": [{"k": {"routed": "r", "routing differs": "d", "no path": "n", "off-network": "o"}[s["kind"]], "c": rc(g),
                                   "a": s["from"], "b": s["to"], "s": s["straight_m"], "m": s["routed_m"]} for s, g in zip(r["segs"], segs4)],
                         "pt": [round(A4[r["order"][0], 1], 5), round(A4[r["order"][0], 0], 5)]})
    j_nodes = []
    for k, r in nodes.iterrows():
        o = {"id": int(r["node"]) if str(r["node"]).isdigit() else str(r["node"]), "lat": round(A4[k, 1], 5), "lng": round(A4[k, 0], 5),
             "lines": r["lines"], "d": int(r["dist_minhal_station_m"]), "dl": int(r["dist_minhal_line_m"]), "near": r["minhal_modes_near"],
             "sname": str(r["minhal_station_name"]), "smodes": str(r["minhal_station_modes"]), "sstat": str(r["minhal_station_status"]),
             "m": int(r["minhal_station_match"]) + (2 if r["minhal_station_possible"] else 0), "on": bool(r["on_minhal_network"])}
        if r["minhal_station_match"] or r["minhal_station_possible"]:
            o["slat"], o["slng"] = round(S4[i_adj[k], 1], 5), round(S4[i_adj[k], 0], 5)
        j_nodes.append(o)
    j_used = [{"mode": u["mode"], "c": rc(g)} for u, g in zip(used_geoms, to_wgs([u["geometry"].simplify(5) for u in used_geoms]))] if used_geoms else []
    j_gaps = [{"id": int(r["gap_id"]), "mode": r["mode"], "len": int(r["length_m"]), "near": str(r["nearest_hubs_node_lines"]), "nd": int(r["dist_nearest_node_m"]), "c": rc(g)}
              for (_, r), g in zip(gaps.iterrows(), to_wgs(gaps.geometry.simplify(5)))] if len(gaps) else []
    j_oms = [{"id": int(r["id"]), "name": str(r["name"]), "modes": str(r["modes"]), "stat": str(r["planning_status"]), "lat": round(float(r["lat"]), 5),
              "lng": round(float(r["lng"]), 5), "d": int(r["dist_hubs_node_m"]), "near": str(r["nearest_hubs_node_lines"])} for _, r in only_map_st.iterrows()]
    j_shift = shift_tbl.to_dict("records")

    def clean(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if np.isnan(o) else float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        return str(o)
    dumps = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=clean)

    css = re.sub(r"url\([^)]*\)", "none", Path(args.leaflet_css).read_text(encoding="utf-8"))
    html = TEMPLATE
    for k, v in {"__LEAFLET_CSS__": css, "__LEAFLET_JS__": args.leaflet_js, "__ROUTES__": dumps(j_routes), "__NODES__": dumps(j_nodes),
                 "__USED__": dumps(j_used), "__GAPS__": dumps(j_gaps), "__OMS__": dumps(j_oms), "__SHIFT__": dumps(j_shift),
                 "__TOLS__": f"{args.tol_station:.0f}", "__TOLL__": f"{args.tol_line:.0f}", "__TOLR__": f"{args.tol_line_rail:.0f}", "__MINGAP__": f"{args.min_gap:.0f}",
                 "__NEAR__": f"{args.near:.0f}",
                 "__CSV_NAME__": Path(args.csv).name, "__N_LINES__": str(n_lines), "__N_ROUTES__": str(len(routes)), "__N_NODES__": str(n_nodes),
                 "__N_ST__": str(len(st)), "__GDX__": f"{glob[0]:+.0f}", "__GDY__": f"{glob[1]:+.0f}"}.items():
        html = html.replace(k, v)
    html = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            + html.replace('\n<div id="map"', '\n</head>\n<body>\n<div id="map"', 1) + "\n</body>\n</html>\n")
    out_h = out_dir / "line_comparison_map.html"
    out_h.write_text(html, encoding="utf-8")
    if args.copy_map_to:
        Path(args.copy_map_to).write_text(html, encoding="utf-8")
    print(f"wrote {out_h} ({out_h.stat().st_size / 1e6:.2f} MB)")
    return 0


TEMPLATE = r"""<title>Hubs vs Minhal Lines</title>
<meta name="description" content="Transit lines of the Hubs project (פרויקט תעדוף מתחמים, model nodes per line) routed over the Minhal network (מנהל התכנון TOD map): matched stops, missing stops, routing differences and Minhal lines the Hubs project does not use.">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;700&display=swap">
<style>
__LEAFLET_CSS__
:root {
  color-scheme: light;
  --bg: #f4f3ef; --panel: #fffefb; --panel-border: #dcd9d0; --ink: #1c1b18; --ink-2: #5b5950; --ink-3: #8a877c;
  --map-bg: #e9e9e9; --shadow: 0 4px 18px rgba(30,28,20,.16); --accent: #2a78d6; --focus: #2a78d6; --ring: #ffffff;
  --routed: #2a78d6; --diff: #eb6834; --off: #c8102e; --node-ok: #2a78d6; --node-maybe: #d9a300; --node-no: #c8102e;
  --used: #9a978c; --gap: #b038c9; --onlym: #1baf7a; --tie: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #17171a; --panel: #212125; --panel-border: #3a3a40; --ink: #f2f1ea; --ink-2: #c3c2b7; --ink-3: #8b8a82;
    --map-bg: #e9e9e9; --shadow: 0 4px 18px rgba(0,0,0,.5); --accent: #3987e5; --focus: #3987e5; --ring: #ffffff;
    --routed: #2a78d6; --diff: #d95926; --off: #c8102e; --node-ok: #2a78d6; --node-maybe: #d9a300; --node-no: #c8102e;
    --used: #8c8a80; --gap: #a02fb8; --onlym: #199e70; --tie: #2a78d6;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #17171a; --panel: #212125; --panel-border: #3a3a40; --ink: #f2f1ea; --ink-2: #c3c2b7; --ink-3: #8b8a82;
  --map-bg: #e9e9e9; --shadow: 0 4px 18px rgba(0,0,0,.5); --accent: #3987e5; --focus: #3987e5; --ring: #ffffff;
  --routed: #2a78d6; --diff: #d95926; --off: #c8102e; --node-ok: #2a78d6; --node-maybe: #d9a300; --node-no: #c8102e;
  --used: #8c8a80; --gap: #a02fb8; --onlym: #199e70; --tie: #2a78d6;
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
.panel { position: absolute; z-index: 1000; top: calc(12px + env(safe-area-inset-top, 0px)); left: 12px; width: 336px; max-width: calc(100% - 24px); max-height: calc(100% - 24px); overflow: auto;
  background: var(--panel); border: 1px solid var(--panel-border); border-radius: 10px; box-shadow: var(--shadow); padding: 14px 16px 12px; box-sizing: border-box; }
.panel h1 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -.01em; text-wrap: balance; }
.panel .sub { margin: 2px 0 0; color: var(--ink-2); font-size: 12.5px; }
.eyebrow { margin: 14px 0 6px; font-size: 11px; font-weight: 500; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }
.sets { display: grid; gap: 2px; margin: 0; padding: 0; list-style: none; }
.sets label { display: grid; grid-template-columns: 18px 26px 1fr auto; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 6px; cursor: pointer; }
.sets label:hover { background: var(--bg); }
.sets input { margin: 0; width: 15px; height: 15px; accent-color: var(--accent); }
.sets input:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.sets .lbl { font-weight: 500; line-height: 1.2; }
.sets .lbl small { display: block; font-weight: 400; color: var(--ink-3); font-size: 11.5px; }
.sets .cnt { color: var(--ink-2); font-variant-numeric: tabular-nums; font-size: 13px; text-align: right; }
.sym { justify-self: center; width: 22px; height: 16px; }
.pick { display: grid; grid-template-columns: 1fr auto; gap: 6px; margin-top: 6px; }
.pick select, .pick input { font: inherit; font-size: 12.5px; padding: 5px 6px; border-radius: 6px; border: 1px solid var(--panel-border); background: var(--bg); color: var(--ink); min-width: 0; }
.pick select:focus-visible, .pick input:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.actions button { flex: 1 1 auto; white-space: nowrap; font: inherit; font-size: 12.5px; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--panel-border); background: var(--bg); color: var(--ink); cursor: pointer; }
.actions button:hover { border-color: var(--ink-3); }
.actions button:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.note { margin: 10px 0 0; font-size: 11.5px; color: var(--ink-3); }
.st { display: inline-block; font-size: 11px; font-weight: 500; letter-spacing: .04em; padding: 1px 7px; border-radius: 99px; color: #fff; }
.st-match { background: var(--onlym); } .st-partial { background: var(--diff); } .st-missing { background: var(--off); }
.pp h2 { margin: 0 0 6px; font-size: 14px; font-weight: 700; line-height: 1.25; }
.pp .tag { display: inline-block; font-size: 11px; font-weight: 500; letter-spacing: .04em; padding: 1px 7px; border-radius: 99px; color: #fff; margin-bottom: 6px; }
.pp .kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; font-size: 12.5px; margin-top: 6px; }
.pp .kv dt { color: var(--ink-3); } .pp .kv dd { margin: 0; font-variant-numeric: tabular-nums; }
.pp .diff { color: var(--diff); } .pp .same { color: var(--onlym); } .pp .bad { color: var(--off); }
.pp button { font: inherit; font-size: 12px; margin-top: 8px; padding: 4px 8px; border-radius: 6px; border: 1px solid var(--panel-border); background: var(--bg); color: var(--ink); cursor: pointer; }
.hl { filter: drop-shadow(0 0 3px var(--ink)); }
@media (max-width: 560px) {
  .panel { top: auto; bottom: calc(12px + env(safe-area-inset-bottom, 0px)); left: 12px; right: 12px; width: auto; max-height: 50%; }
  .leaflet-top.leaflet-right { top: env(safe-area-inset-top, 0px); }
}
@media (prefers-reduced-motion: reduce) { .leaflet-zoom-anim .leaflet-zoom-animated, .leaflet-fade-anim .leaflet-popup { transition: none; } }
</style>

<div id="map" role="application" aria-label="Map comparing the transit lines of the Hubs project with the Minhal network"></div>

<section class="panel" id="panel" aria-label="Layers and legend">
  <h1>Hubs project vs Minhal: transit lines</h1>
  <p class="sub">__N_LINES__ Hubs project lines (<bdi>פרויקט תעדוף מתחמים</bdi>; __N_ROUTES__ distinct node sets, __N_NODES__ nodes) routed over the Minhal network (<bdi>מנהל התכנון</bdi>) · stations matched within __TOLS__ m, lines within __TOLL__ m (Rail __TOLR__ m) · nodes shifted by a local median offset (overall __GDX__ m, __GDY__ m)</p>
  <p class="eyebrow">Hubs project lines (routed over Minhal)</p>
  <ul class="sets">
    <li><label><input type="checkbox" id="set-routed" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><path d="M1 12 C 6 2, 12 14, 21 4" fill="none" stroke="var(--routed)" stroke-width="3"/></svg>
      <span class="lbl">Stop pair routed on Minhal<small>follows a Minhal line of the same mode</small></span><span class="cnt" id="cnt-routed"></span></label></li>
    <li><label><input type="checkbox" id="set-diff" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><line x1="1" y1="8" x2="21" y2="8" stroke="var(--diff)" stroke-width="3" stroke-dasharray="4 3"/></svg>
      <span class="lbl">Routing differs<small>Minhal route much longer than the straight line, or no path</small></span><span class="cnt" id="cnt-diff"></span></label></li>
    <li><label><input type="checkbox" id="set-off" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><line x1="1" y1="8" x2="21" y2="8" stroke="var(--off)" stroke-width="3" stroke-dasharray="4 3"/></svg>
      <span class="lbl">Off the Minhal network<small>a Hubs stop with no Minhal line of the mode within __TOLL__ m (Rail __TOLR__ m)</small></span><span class="cnt" id="cnt-off"></span></label></li>
  </ul>
  <p class="eyebrow">Hubs project nodes</p>
  <ul class="sets">
    <li><label><input type="checkbox" id="set-nok" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><line x1="6" y1="8" x2="16" y2="8" stroke="var(--tie)" stroke-width="1.5"/><circle cx="6" cy="8" r="5" fill="var(--node-ok)" stroke="var(--ring)" stroke-width="1.5"/><circle cx="16" cy="8" r="5" fill="none" stroke="var(--node-ok)" stroke-width="2"/></svg>
      <span class="lbl">Node with a Minhal station<small>filled = Hubs node · ring = Minhal station · line = offset</small></span><span class="cnt" id="cnt-nok"></span></label></li>
    <li><label><input type="checkbox" id="set-nmaybe" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><circle cx="11" cy="8" r="5" fill="var(--node-maybe)" stroke="var(--ring)" stroke-width="1.5"/></svg>
      <span class="lbl">Possible Minhal station<small>nearest Minhal station __TOLS__-__NEAR__ m away</small></span><span class="cnt" id="cnt-nmaybe"></span></label></li>
    <li><label><input type="checkbox" id="set-nno" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><circle cx="11" cy="8" r="5" fill="var(--node-no)" stroke="var(--ring)" stroke-width="1.5"/></svg>
      <span class="lbl">No Minhal station<small>none within __NEAR__ m</small></span><span class="cnt" id="cnt-nno"></span></label></li>
  </ul>
  <p class="eyebrow">Minhal network</p>
  <ul class="sets">
    <li><label><input type="checkbox" id="set-used" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><line x1="1" y1="8" x2="21" y2="8" stroke="var(--used)" stroke-width="2"/></svg>
      <span class="lbl">Minhal lines used by a Hubs line</span><span class="cnt" id="cnt-used"></span></label></li>
    <li><label><input type="checkbox" id="set-gap" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><line x1="1" y1="8" x2="21" y2="8" stroke="var(--gap)" stroke-width="3.5"/></svg>
      <span class="lbl">Minhal lines no Hubs line uses<small>stretches of __MINGAP__ m or more</small></span><span class="cnt" id="cnt-gap"></span></label></li>
    <li><label><input type="checkbox" id="set-oms" checked>
      <svg class="sym" viewBox="0 0 22 16" aria-hidden="true"><circle cx="11" cy="8" r="5" fill="var(--onlym)" stroke="var(--ring)" stroke-width="1.5"/></svg>
      <span class="lbl">Minhal stations without a Hubs node<small>no Hubs node within __TOLS__ m</small></span><span class="cnt" id="cnt-oms"></span></label></li>
  </ul>
  <p class="eyebrow">Find a Hubs line</p>
  <div class="pick">
    <select id="pick-line" aria-label="Hubs line"><option value="">all lines</option></select>
    <select id="pick-status" aria-label="Line status"><option value="">any status</option><option value="match">match</option><option value="partial">partial</option><option value="mostly missing">mostly missing</option><option value="missing in Minhal">missing in Minhal</option></select>
  </div>
  <div class="actions">
    <button type="button" id="btn-fit">Zoom to all</button>
    <button type="button" id="btn-tlv">Tel Aviv</button>
    <button type="button" id="btn-hfa">Haifa</button>
    <button type="button" id="btn-jlm">Jerusalem</button>
    <button type="button" id="btn-bsh">Be'er Sheva</button>
  </div>
  <p class="note">Basemap: OpenStreetMap (light gray). Matching is by geometry only; line names are not compared. The order of the stops of each Hubs line is reconstructed (shortest path through its nodes), so a routing difference on a line with loops or branches may come from the ordering. Source: __CSV_NAME__.</p>
</section>

<script src="__LEAFLET_JS__"></script>
<script>
(function () {
  const ROUTES = __ROUTES__, NODES = __NODES__, USED = __USED__, GAPS = __GAPS__, OMS = __OMS__;
  const css = getComputedStyle(document.documentElement), tok = n => css.getPropertyValue(n).trim();
  const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = v => v == null ? '–' : Number(v).toLocaleString();
  const map = L.map('map', {zoomControl: false, preferCanvas: true});
  L.control.zoom({position: 'topright'}).addTo(map);
  map.attributionControl.setPrefix('');
  map.attributionControl.addAttribution('© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · TOD Israel explorer (beta)');
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, className: 'osm-gray'}).addTo(map);

  const groups = {};
  ['used','gap','oms','routed','diff','off','nok','nmaybe','nno'].forEach(k => groups[k] = L.layerGroup().addTo(map));
  const stCls = s => s === 'match' ? 'st-match' : s === 'partial' ? 'st-partial' : 'st-missing';
  const kindName = {r: 'routed on Minhal', d: 'routing differs', n: 'no path on the Minhal network', o: 'off the Minhal network'};

  // ---- Minhal network ---------------------------------------------------------------------------
  USED.forEach(u => groups.used.addLayer(L.polyline(u.c, {color: tok('--used'), weight: 2, opacity: .7, interactive: false})));
  GAPS.forEach(g => {
    const pl = L.polyline(g.c, {color: tok('--gap'), weight: 4, opacity: .85});
    pl.bindPopup(`<div class="pp" dir="auto"><span class="tag" style="background:var(--gap)">Minhal line no Hubs line uses</span>
      <dl class="kv"><dt>mode</dt><dd>${esc(g.mode)}</dd><dt>length</dt><dd>${fmt(g.len)} m</dd><dt>nearest Hubs node</dt><dd>${fmt(g.nd)} m · lines ${esc(g.near)}</dd></dl></div>`, {maxWidth: 320});
    pl.bindTooltip(`${g.mode} · ${fmt(g.len)} m not used by any Hubs line`, {sticky: true});
    groups.gap.addLayer(pl);
  });
  OMS.forEach(p => {
    const m = L.circleMarker([p.lat, p.lng], {radius: 6, color: tok('--ring'), weight: 1.5, fillColor: tok('--onlym'), fillOpacity: .95});
    m.bindPopup(`<div class="pp" dir="auto"><span class="tag" style="background:var(--onlym)">Minhal station without a Hubs node</span>
      <h2>${esc(p.name)}</h2><dl class="kv"><dt>station id</dt><dd>${p.id}</dd><dt>modes</dt><dd>${esc(p.modes)}</dd><dt>status</dt><dd>${esc(p.stat)}</dd>
      <dt>nearest Hubs node</dt><dd>${fmt(p.d)} m · lines ${esc(p.near)}</dd></dl></div>`, {maxWidth: 320});
    m.bindTooltip(`${p.name} (Minhal only)`, {direction: 'top', offset: [0, -6]});
    groups.oms.addLayer(m);
  });

  // ---- Hubs project lines ------------------------------------------------------------------------
  const routeLayers = {};                      // route id -> [polyline]
  const counts = {r: 0, d: 0, o: 0};
  const routePopup = (r, s) => `<div class="pp" dir="auto"><span class="st ${stCls(r.status)}">alignment: ${esc(r.status)}</span> <span class="st ${r.sst === 'all' || r.sst === 'most' ? 'st-match' : r.sst === 'some' ? 'st-partial' : 'st-missing'}">stations: ${esc(r.sst)}</span>
      <h2 style="margin-top:6px">${esc(r.lines.join(' / '))}</h2>
      <dl class="kv"><dt>Minhal mode</dt><dd>${esc(r.mode)}${r.hint && r.hint !== r.mode ? ` <span class="diff">(name hints ${esc(r.hint)})</span>` : ''} · votes ${esc(r.votes)}</dd>
      <dt>nodes</dt><dd>${r.n} · <span class="${r.nst < r.n ? 'diff' : 'same'}">${r.nst} with a Minhal station</span> · ${r.non} on a Minhal ${esc(r.mode)} line</dd>
      <dt>stop pairs</dt><dd>${r.nseg} · ${r.nrt} routed · <span class="${r.ndiff ? 'diff' : ''}">${r.ndiff} routing differ</span> · <span class="${r.noff ? 'bad' : ''}">${r.noff} off network</span></dd>
      ${s ? `<dt>this pair</dt><dd>${esc(kindName[s.k])} · nodes ${esc(s.a)} → ${esc(s.b)} · straight ${fmt(s.s)} m${s.m != null ? ` · Minhal route ${fmt(s.m)} m` : ''}</dd>` : ''}</dl>
      <button type="button" data-zoom="${r.id}">Zoom to line</button></div>`;
  ROUTES.forEach(r => {
    routeLayers[r.id] = [];
    r.segs.forEach(s => {
      const k = s.k === 'r' ? 'routed' : s.k === 'o' ? 'off' : 'diff';
      counts[s.k === 'r' ? 'r' : s.k === 'o' ? 'o' : 'd']++;
      const pl = L.polyline(s.c, k === 'routed' ? {color: tok('--routed'), weight: 3.5, opacity: .8}
        : {color: tok(k === 'off' ? '--off' : '--diff'), weight: 3, opacity: .9, dashArray: '6 5'});
      pl.bindPopup(routePopup(r, s), {maxWidth: 360});
      pl.bindTooltip(`${r.lines.join(' / ')} · ${r.mode} · ${kindName[s.k]}`, {sticky: true});
      pl._route = r.id; pl._kind = k;
      groups[k].addLayer(pl);
      routeLayers[r.id].push(pl);
    });
  });
  document.getElementById('cnt-routed').textContent = counts.r;
  document.getElementById('cnt-diff').textContent = counts.d;
  document.getElementById('cnt-off').textContent = counts.o;

  // ---- nodes -----------------------------------------------------------------------------------
  const nc = {nok: 0, nmaybe: 0, nno: 0};
  NODES.forEach(p => {
    const set = p.m === 1 ? 'nok' : p.m === 2 ? 'nmaybe' : 'nno';
    nc[set]++;
    const col = tok(set === 'nok' ? '--node-ok' : set === 'nmaybe' ? '--node-maybe' : '--node-no');
    const m = L.circleMarker([p.lat, p.lng], {radius: 5.5, color: tok('--ring'), weight: 1.2, fillColor: col, fillOpacity: .95});
    const stTxt = set === 'nno' ? `<span class="bad">none within __NEAR__ m</span> (nearest ${esc(p.sname)} · ${fmt(p.d)} m)`
      : `${esc(p.sname)} · ${esc(p.smodes)} · ${esc(p.sstat)} · <span class="${set === 'nok' ? 'same' : 'diff'}">${fmt(p.d)} m</span>`;
    m.bindPopup(`<div class="pp" dir="auto"><span class="tag" style="background:${col}">${set === 'nok' ? 'Node with a Minhal station' : set === 'nmaybe' ? 'Possible Minhal station' : 'No Minhal station'}</span>
      <h2>Hubs node ${esc(p.id)}</h2><dl class="kv"><dt>Hubs lines</dt><dd>${esc(p.lines.join(' | '))}</dd><dt>Minhal station</dt><dd>${stTxt}</dd>
      <dt>Minhal line</dt><dd>${p.on ? `<span class="same">${esc(p.near)} within ${fmt(p.dl)} m</span>` : `<span class="bad">none within __TOLL__ m (Rail __TOLR__ m)</span> (nearest ${fmt(p.dl)} m)`}</dd></dl></div>`, {maxWidth: 340});
    m.bindTooltip(`Hubs node ${p.id} · ${p.lines.join(' | ')}`, {direction: 'top', offset: [0, -6]});
    m._lines = p.lines;
    if (p.slat != null) {
      groups[set].addLayer(L.polyline([[p.lat, p.lng], [p.slat, p.slng]], {color: tok('--tie'), weight: 1.2, opacity: .8, interactive: false}));
      groups[set].addLayer(L.circleMarker([p.slat, p.slng], {radius: 6, color: col, weight: 2, fillOpacity: 0, interactive: false}));
    }
    groups[set].addLayer(m);
  });
  document.getElementById('cnt-nok').textContent = nc.nok;
  document.getElementById('cnt-nmaybe').textContent = nc.nmaybe;
  document.getElementById('cnt-nno').textContent = nc.nno;
  document.getElementById('cnt-used').textContent = USED.length ? 'on' : '0';
  document.getElementById('cnt-gap').textContent = GAPS.length;
  document.getElementById('cnt-oms').textContent = OMS.length;

  const wire = (id, layer) => document.getElementById(id).addEventListener('change', e => e.target.checked ? layer.addTo(map) : map.removeLayer(layer));
  Object.keys(groups).forEach(k => wire('set-' + k, groups[k]));

  // ---- line picker ---------------------------------------------------------------------------
  const sel = document.getElementById('pick-line'), selSt = document.getElementById('pick-status');
  const byStatus = {};
  ROUTES.slice().sort((a, b) => a.lines[0].localeCompare(b.lines[0])).forEach(r => {
    const o = document.createElement('option'); o.value = r.id; o.textContent = `${r.lines.join(' / ')} · ${r.mode} · ${r.status} · stations ${r.sst}`; o.dataset.status = r.status; sel.appendChild(o);
    (byStatus[r.status] = byStatus[r.status] || []).push(r.id);
  });
  const showRoutes = ids => {
    const all = ids == null;
    const set = all ? null : new Set(ids.map(String));
    ROUTES.forEach(r => routeLayers[r.id].forEach(pl => {
      const on = all || set.has(String(r.id));
      pl.setStyle({opacity: on ? (pl._kind === 'routed' ? .8 : .9) : .08});
      if (on) pl.bringToFront();
    }));
    Object.values(groups).forEach(g => g.eachLayer(l => {
      if (l._lines) {
        const on = all || l._lines.some(x => ROUTES.some(r => set.has(String(r.id)) && r.lines.includes(x)));
        l.setStyle({fillOpacity: on ? .95 : .12, opacity: on ? 1 : .12});
      }
    }));
  };
  const zoomTo = id => {
    const b = L.latLngBounds([]);
    routeLayers[id].forEach(pl => b.extend(pl.getBounds()));
    const r = ROUTES.find(x => String(x.id) === String(id));
    if (b.isValid()) map.fitBounds(b, {padding: [40, 40]}); else map.setView(r.pt, 14);
  };
  sel.addEventListener('change', () => { selSt.value = ''; if (sel.value) { showRoutes([sel.value]); zoomTo(sel.value); } else showRoutes(null); });
  selSt.addEventListener('change', () => { sel.value = ''; showRoutes(selSt.value ? (byStatus[selSt.value] || []) : null); });
  map.on('popupopen', e => {
    const b = e.popup.getElement().querySelector('button[data-zoom]');
    if (b) b.addEventListener('click', () => { sel.value = b.dataset.zoom; selSt.value = ''; showRoutes([b.dataset.zoom]); zoomTo(b.dataset.zoom); });
  });

  const all = [...NODES.map(p => [p.lat, p.lng]), ...OMS.map(p => [p.lat, p.lng])];
  const bounds = L.latLngBounds(all);
  document.getElementById('btn-fit').addEventListener('click', () => map.fitBounds(bounds, {padding: [30, 30]}));
  document.getElementById('btn-tlv').addEventListener('click', () => map.setView([32.08, 34.82], 12));
  document.getElementById('btn-hfa').addEventListener('click', () => map.setView([32.80, 35.02], 12));
  document.getElementById('btn-jlm').addEventListener('click', () => map.setView([31.78, 35.21], 12));
  document.getElementById('btn-bsh').addEventListener('click', () => map.setView([31.25, 34.79], 12));
  const panel = document.getElementById('panel');
  L.DomEvent.disableClickPropagation(panel); L.DomEvent.disableScrollPropagation(panel);
  map.fitBounds(bounds, {padding: [30, 30]});
})();
</script>
"""

if __name__ == "__main__":
    raise SystemExit(main())
