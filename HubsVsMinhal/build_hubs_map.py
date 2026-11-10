#!/usr/bin/env python3
"""
Hubs map: the hubs the Hubs project ranked next to the hubs of the Minhal project.

Hubs project   hub_prioritization_results.csv, one row per scored hub group, with tier (HubType),
               score and ranks. Rows with HubType = "Not Hub" passed eligibility but got no tier;
               they are a separate, optional layer.
Minhal         stations of MT_Network.gpkg that share a hub_id. A hub with two or more modes
               (--min-modes) is compared; single-mode hubs are a separate, optional layer.

Matching is by location: a Hubs hub and a Minhal hub are the same place when any stop of the one
is within --tolerance metres of any station of the other. That gives three sets:

    both         Hubs hub <-> Minhal hub pairs
    only_hubs    Hubs hubs with no Minhal multi-mode hub within the tolerance
    only_minhal  Minhal multi-mode hubs with no Hubs hub within the tolerance

hub_scoring_calculation.xlsx lists all candidate groups but without coordinates, so only its
eligibility funnel is reported (panel, and sheet `eligibility` of the workbook); the stops that
belong to no scored group are drawn as an optional layer instead.

    python build_hubs_map.py [--tolerance 400] [--min-modes 2]

Writes hubs_map.html and hubs_comparison.xlsx next to this script.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

TIERS = ["ארצי", "מטרופוליני", "עירוני"]
TIER_EN = {"ארצי": "National", "מטרופוליני": "Metropolitan", "עירוני": "Local", "Not Hub": "Not Hub"}
CRITERIA = [("RegionLocation_Norm", "Location"), ("bus_terminal_Norm", "Bus terminal"), ("score_Norm", "Service & modes"),
            ("TotalDemand_Norm", "Passenger activity"), ("PopEmp_Score_Norm", "Population & jobs")]


def load_hubs_results() -> gpd.GeoDataFrame:
    res = C.read_csv_he(C.HUBS_RESULTS)
    res["nodes"] = res["node"].map(ast.literal_eval)
    res["modes_src"] = res["Mode_Planned"].map(ast.literal_eval)
    res["modes"] = res["modes_src"].map(lambda ms: C.sort_modes(C.MODE_MAP[m] for m in ms))
    res["is_hub"] = res["HubType"].isin(TIERS)
    unknown = set(res["HubType"]) - set(TIERS) - {"Not Hub"}
    if unknown:
        raise ValueError(f"unexpected HubType value(s): {unknown}")
    return gpd.GeoDataFrame(res, geometry=gpd.points_from_xy(res["x"], res["y"]), crs=C.WGS).to_crs(C.ITM)


def load_minhal_hubs() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """One row per Minhal hub_id (centroid of its stations) and the member stations."""
    _, st = C.load_minhal()
    st = st[st["hub_id"].notna() & st["active"]].copy()
    st["hub_id"] = st["hub_id"].astype(int)
    st["clean_name"] = st["station_name"].fillna("").map(lambda s: re.sub(r"\s*-\s*\d+$", "", s).strip())
    rows = []
    for hid, g in st.groupby("hub_id"):
        names = [n for n in g["clean_name"].value_counts().index if n]
        line_names = sorted({n for n in g["line_name"].dropna()})
        rows.append({"hub_id": hid, "name": " / ".join(names[:3]) or f"hub {hid}", "modes": C.sort_modes(g["mode"]),
                     "n_modes": g["type"].nunique(), "n_stations": len(g), "n_lines": g["line_id"].nunique(),
                     "line_ids": sorted(map(int, g["line_id"].unique())), "line_names": line_names,
                     "year_first": int(g["year_operation"].min()), "year_last": int(g["year_operation"].max()),
                     "rail_hub": bool(g["rail_hub"].notna().any()), "plans": sorted(g["source_plan"].dropna().unique()),
                     "x": g.geometry.x.mean(), "y": g.geometry.y.mean()})
    hubs = pd.DataFrame(rows)
    return gpd.GeoDataFrame(hubs, geometry=gpd.points_from_xy(hubs["x"], hubs["y"]), crs=C.ITM), st


def eligibility_funnel() -> tuple[pd.DataFrame, dict]:
    """Re-run step 1 of hub_scoring_calculation.xlsx (its formulas hold no cached values)."""
    par = pd.read_excel(C.HUBS_SCORING, sheet_name="Parameters", header=None)
    P = {str(n): v for n, v in zip(par[2], par[1]) if isinstance(n, str)}
    e = pd.read_excel(C.HUBS_SCORING, sheet_name="1_Eligibility", header=3)
    e = e[e["Group"].notna()].copy()
    e["Group"] = e["Group"].astype(int)
    pax, n_modes, rail_only_modes, lines = (e[c] for c in ("Passengers / day (2050)", "Planned modes (count)", "Of which rail-only modes", "Unique lines"))
    e["enough_passengers"] = pax >= P["MinPassengers"]
    e["enough_modes"] = n_modes >= P["MinModes"]
    e["rail_only"] = (n_modes > 0) & (rail_only_modes == n_modes)
    e["eligible"] = e["enough_passengers"] & e["enough_modes"] & ((P["RequireNonRail"] == 0) | ~e["rail_only"])
    hs, iu, sub, metro, lrt, brt = (e[f"Has {m}"] == 1 for m in ("HighSpeed Rail", "Interurban Rail", "Suburban Rail", "Metro", "LRT", "BRT"))
    many = lines >= P["MinLinesTier"]
    e["hub_type"] = np.select(
        [(hs | iu) & many & (pax >= P["NationalMinDemand"]), (sub | metro | iu | hs) & many & (pax >= P["MetroMinDemand"]),
         (brt | lrt) & many & (pax >= P["LocalMinDemand"]), (iu | hs | sub) & ~many],
        ["ארצי", "מטרופוליני", "עירוני", "Train Station"], default="Not Hub")
    e["why_not_eligible"] = np.select(
        [e["eligible"], ~e["enough_modes"], e["rail_only"] & (P["RequireNonRail"] != 0), ~e["enough_passengers"]],
        ["", f"fewer than {P['MinModes']} planned modes", "rail-only modes", f"under {P['MinPassengers']:,} passengers / day"], default="")
    funnel = {"groups": len(e), "eligible": int(e["eligible"].sum()), "min_modes": int(P["MinModes"]), "min_pax": int(P["MinPassengers"]),
              "few_modes": int((e["why_not_eligible"].str.startswith("fewer")).sum()),
              "rail_only": int((e["why_not_eligible"] == "rail-only modes").sum()),
              "few_pax": int((e["why_not_eligible"].str.startswith("under")).sum())}
    return e, funnel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tolerance", type=float, default=400, help="match radius in metres, stop to station")
    ap.add_argument("--near", type=float, default=1000, help="upper radius for 'possible' matches listed for review")
    ap.add_argument("--min-modes", type=int, default=2, help="Minhal hubs need this many modes to be compared")
    args = ap.parse_args()

    res = load_hubs_results()
    mh, mst = load_minhal_hubs()
    mh["multi"] = mh["n_modes"] >= args.min_modes
    nodes = C.read_csv_he(C.HUBS_NODES).groupby("node")[["X", "Y"]].mean()

    elig, funnel = eligibility_funnel()
    if set(elig.loc[elig["eligible"], "Group"]) != set(res["group"]):
        raise ValueError("eligible groups in hub_scoring_calculation.xlsx differ from hub_prioritization_results.csv")
    tier_check = res.set_index("group")["HubType"].eq(elig.set_index("Group")["hub_type"].reindex(res["group"]).values)
    if not tier_check.all():
        raise ValueError(f"hub type differs between the two Hubs files for groups {list(tier_check[~tier_check].index)}")

    # ---- distance matrix: Hubs group (its stops + its point) x Minhal hub (its stations) --------------
    trees = {hid: cKDTree(np.c_[g.geometry.x, g.geometry.y]) for hid, g in mst.groupby("hub_id")}
    pts = [np.vstack([nodes.loc[r.nodes].values, [[r.geometry.x, r.geometry.y]]]) for r in res.itertuples()]
    D = np.array([[trees[hid].query(p)[0].min() for hid in mh["hub_id"]] for p in pts])
    multi, is_hub = mh["multi"].values, res["is_hub"].values
    near = D <= args.tolerance

    def nearest(dist_row_or_col, names, ids):
        if not len(dist_row_or_col):
            return None, None, None
        i = int(np.argmin(dist_row_or_col))
        return ids[i], names[i], int(round(dist_row_or_col[i]))

    res["matched"] = (near[:, multi]).any(axis=1)
    res["single_mode_minhal_hub_here"] = (near[:, ~multi]).any(axis=1)
    mh["matched"] = (near[is_hub, :]).any(axis=0)
    mh["not_hub_group_here"] = (near[~is_hub, :]).any(axis=0)
    m_ids, m_names = mh["hub_id"].values, mh["name"].values
    h_ids, h_names = res["group"].values, res["HubNameHE"].values
    res[["nearest_minhal_hub_id", "nearest_minhal_hub", "dist_m"]] = [nearest(D[i, multi], m_names[multi], m_ids[multi]) for i in range(len(res))]
    mh[["nearest_hubs_group", "nearest_hubs_hub", "dist_m"]] = [nearest(D[is_hub, j], h_names[is_hub], h_ids[is_hub]) for j in range(len(mh))]

    res["set"] = np.where(~res["is_hub"], "not_hub", np.where(res["matched"], "both", "only_hubs"))
    mh["set"] = np.where(~mh["multi"], "single_mode", np.where(mh["matched"], "both", "only_minhal"))

    pairs = [{"group": int(res["group"].iat[i]), "hub_id": int(mh["hub_id"].iat[j]), "dist_m": int(round(D[i, j]))}
             for i, j in zip(*np.nonzero(near)) if is_hub[i] and multi[j]]
    pair_tbl = pd.DataFrame(pairs)
    # Everything within the tolerance, compared or not: the popups name all of it, so that "only Hubs project"
    # next to a single-mode Minhal hub, or "only Minhal" next to a Not Hub group, says what is there.
    around = [{"group": int(res["group"].iat[i]), "hub_id": int(mh["hub_id"].iat[j]), "dist_m": int(round(D[i, j]))}
              for i, j in zip(*np.nonzero(near))]

    # ---- workbook ---------------------------------------------------------------------------------------
    res_ll, mh_ll = res.to_crs(C.WGS), mh.to_crs(C.WGS)
    res["lon"], res["lat"] = res_ll.geometry.x.round(6), res_ll.geometry.y.round(6)
    mh["lon"], mh["lat"] = mh_ll.geometry.x.round(6), mh_ll.geometry.y.round(6)
    res["TotalDemand_100"] = (res["TotalDemand"] / 100).round() * 100
    hub_cols = ["group", "HubNameHE", "HubType", "Metro", "LocationForChart", "Overall_Rank", "Rank_within_HubType", "RankByHubTypeMetro",
                "TotalScore_MC"] + [c for c, _ in CRITERIA] + ["TotalDemand_100", "Num_Modes", "Line_Nunique", "bus_terminal", "modes",
                "nearest_minhal_hub_id", "nearest_minhal_hub", "dist_m", "single_mode_minhal_hub_here", "lon", "lat"]
    m_cols = ["hub_id", "name", "n_modes", "modes", "n_stations", "n_lines", "line_names", "year_first", "year_last", "rail_hub",
              "nearest_hubs_group", "nearest_hubs_hub", "dist_m", "not_hub_group_here", "lon", "lat"]

    def flat(df: pd.DataFrame) -> pd.DataFrame:
        """List-valued cells -> ' | '-joined text, so they survive in Excel."""
        join = lambda v: " | ".join(map(str, v)) if isinstance(v, list) else v
        return df.assign(**{c: df[c].map(join) for c in df.columns if df[c].map(lambda v: isinstance(v, list)).any()})

    both = pair_tbl.merge(res[hub_cols].drop(columns=["nearest_minhal_hub_id", "nearest_minhal_hub", "dist_m"]), on="group") \
                   .merge(mh[m_cols].drop(columns=["nearest_hubs_group", "nearest_hubs_hub", "dist_m"]), on="hub_id", suffixes=("_hubs", "_minhal"))
    both["modes_equal"] = both["modes_hubs"].map(set) == both["modes_minhal"].map(set)
    both["modes_only_in_hubs"] = [" | ".join(m for m in a if m not in b) for a, b in zip(both["modes_hubs"], both["modes_minhal"])]
    both["modes_only_in_minhal"] = [" | ".join(m for m in b if m not in a) for a, b in zip(both["modes_hubs"], both["modes_minhal"])]
    only_h = res.loc[res["set"] == "only_hubs", hub_cols].sort_values("Overall_Rank")
    only_m = mh.loc[mh["set"] == "only_minhal", m_cols].sort_values("dist_m")
    possible = pd.concat([only_h[only_h["dist_m"] <= args.near].assign(side="Hubs hub -> nearest Minhal hub"),
                          only_m[only_m["dist_m"] <= args.near].assign(side="Minhal hub -> nearest Hubs hub")], ignore_index=True)
    n = lambda df, s: int((df["set"] == s).sum())
    summary = pd.DataFrame([
        {"set": "in both", "hubs_project": n(res, "both"), "minhal": n(mh, "both"),
         "note": f"a stop of the Hubs hub within {args.tolerance:.0f} m of a station of the Minhal hub; {len(pair_tbl)} pairs, one hub can pair with several"},
        {"set": "only in Hubs", "hubs_project": n(res, "only_hubs"), "minhal": None,
         "note": f"no Minhal hub with >= {args.min_modes} modes within the tolerance; {int(res.loc[res['set'] == 'only_hubs', 'single_mode_minhal_hub_here'].sum())} of them have a single-mode Minhal hub there"},
        {"set": "only in Minhal", "hubs_project": None, "minhal": n(mh, "only_minhal"),
         "note": f"no Hubs hub within the tolerance; {int(mh.loc[mh['set'] == 'only_minhal', 'not_hub_group_here'].sum())} of them have a scored 'Not Hub' group there"},
        {"set": "compared", "hubs_project": int(res["is_hub"].sum()), "minhal": int(mh["multi"].sum()),
         "note": f"Hubs: tier {', '.join(TIERS)}; Minhal: hub_id with >= {args.min_modes} modes"},
        {"set": "not compared", "hubs_project": n(res, "not_hub"), "minhal": n(mh, "single_mode"),
         "note": "Hubs: scored groups with HubType = Not Hub; Minhal: single-mode hubs"},
        {"set": "modes agree (pairs)", "hubs_project": int(both["modes_equal"].sum()), "minhal": None,
         "note": "same mode set after mapping both vocabularies (Suburban/Interurban -> Rail, HighSpeed -> Rail fast, FUNI/Cable/Funicular together)"},
        {"set": "possible (review)", "hubs_project": int(possible["side"].str.startswith("Hubs").sum()),
         "minhal": int(possible["side"].str.startswith("Minhal").sum()), "note": f"nearest neighbour {args.tolerance:.0f}-{args.near:.0f} m away"},
        {"set": "Hubs candidates", "hubs_project": funnel["groups"], "minhal": None,
         "note": f"{funnel['eligible']} eligible; out: {funnel['few_modes']} fewer than {funnel['min_modes']} modes, {funnel['rail_only']} rail-only, {funnel['few_pax']} under {funnel['min_pax']:,} passengers/day. No coordinates in the workbook, so not on the map."},
    ])
    summary[["hubs_project", "minhal"]] = summary[["hubs_project", "minhal"]].astype("Int64")
    out_x = C.HERE / "hubs_comparison.xlsx"
    with pd.ExcelWriter(out_x, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="summary", index=False)
        flat(both).sort_values(["Overall_Rank", "dist_m"]).to_excel(xw, sheet_name="both", index=False)
        flat(only_h).to_excel(xw, sheet_name="only_hubs", index=False)
        flat(only_m).to_excel(xw, sheet_name="only_minhal", index=False)
        flat(possible).to_excel(xw, sheet_name="possible_matches", index=False)
        flat(res.loc[res["set"] == "not_hub", hub_cols]).to_excel(xw, sheet_name="hubs_not_hub", index=False)
        flat(mh.loc[mh["set"] == "single_mode", m_cols]).to_excel(xw, sheet_name="minhal_single_mode", index=False)
        elig.drop(columns=[c for c in elig.columns if c.endswith("?") or c in ("ELIGIBLE", "Hub type (tier)")]).to_excel(xw, sheet_name="eligibility", index=False)
    print(f"wrote {out_x.relative_to(C.HERE.parent)}")
    print(summary.to_string(index=False))

    # ---- map ----------------------------------------------------------------------------------------------
    num = lambda v, nd=2: None if pd.isna(v) else round(float(v), nd)
    hubs_feats = [{"g": int(r.group), "name": r.HubNameHE, "tier": r.HubType, "tier_en": TIER_EN[r.HubType], "metro": r.Metro, "pos": r.LocationForChart,
                   "rank": int(r.Overall_Rank), "rank_tier": int(r.Rank_within_HubType), "rank_tier_metro": int(r.RankByHubTypeMetro),
                   "score": num(r.TotalScore_MC), "crit": [num(getattr(r, c), 1) for c, _ in CRITERIA], "pax": num(r.TotalDemand_100, 0),
                   "modes": r.modes, "modes_src": r.modes_src, "lines": int(r.Line_Nunique), "bus": int(r.bus_terminal), "set": r.set,
                   "single": bool(r.single_mode_minhal_hub_here), "near": r.nearest_minhal_hub, "d": r.dist_m, "ll": [r.lat, r.lon]}
                  for r in res.itertuples()]
    minhal_feats = [{"id": int(r.hub_id), "name": r.name, "modes": r.modes, "n_modes": int(r.n_modes), "stations": int(r.n_stations),
                     "lines": int(r.n_lines), "line_names": r.line_names, "y0": r.year_first, "y1": r.year_last, "rail_hub": r.rail_hub,
                     "set": r.set, "not_hub": bool(r.not_hub_group_here), "near": r.nearest_hubs_hub, "d": r.dist_m, "ll": [r.lat, r.lon]}
                    for r in mh.itertuples()]
    scored = {n for ns in res["nodes"] for n in ns}
    rest = nodes[~nodes.index.isin(scored)]
    rest_ll = gpd.GeoSeries(gpd.points_from_xy(rest["X"], rest["Y"]), crs=C.ITM).to_crs(C.WGS)
    # what stops there: the node file gives the lines of each stop, the mode table their mode and name
    line_info = C.read_csv_he(C.HUBS_MODES).drop_duplicates("Line_ModelName").set_index("Line_ModelName")
    stop_lines = C.read_csv_he(C.HUBS_NODES).groupby("node")["LINE_ID"].agg(lambda s: sorted(set(s)))
    missing = {l for ls in stop_lines for l in ls} - set(line_info.index)
    if missing:
        raise ValueError(f"lines without a row in {C.HUBS_MODES.name}: {sorted(missing)}")
    uniq = lambda vals: list(dict.fromkeys(vals))
    other_stops = [{"id": int(n), "ll": [round(p.y, 5), round(p.x, 5)],
                    "modes": uniq(line_info.loc[stop_lines[n], "Mode_Planned"]),
                    "lines": uniq(line_info.loc[stop_lines[n], "Line_Name"]), "ids": stop_lines[n]}
                   for n, p in zip(rest.index, rest_ll)]
    funnel.update(scored_nodes=len(scored), other_nodes=len(rest),
                  tiers={t: int((res["HubType"] == t).sum()) for t in TIERS + ["Not Hub"]})

    C.render("hubs_map.html", C.HERE / "hubs_map.html", {
        "__HUBS__": C.dumps(hubs_feats), "__MINHAL__": C.dumps(minhal_feats), "__AROUND__": C.dumps(around),
        "__OTHER_STOPS__": C.dumps(other_stops), "__FUNNEL__": C.dumps(funnel), "__CRITERIA__": C.dumps([n for _, n in CRITERIA]),
        "__TOL__": f"{args.tolerance:.0f}", "__MINM__": str(args.min_modes),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
