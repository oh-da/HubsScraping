#!/usr/bin/env python3
"""
Build the node x line csv that compare_lines.py reads from a Hubs project routes workbook.

The routes workbook (e.g. Data/Routes_and_Nodes_V1.1.xlsx, sheet "All") lists, per line, the
model nodes it stops at (Line, Node, IsStop, Mode, Include, PlannedMode) but carries no
coordinates, and its row order is not the stop order. Coordinates are taken from an earlier
node x line csv with X/Y in EPSG:2039 (Data/All_nodes+lines_V1.01.csv):

* rows with Include = 0 are dropped (blank Include counts as included);
* line names are stripped of surrounding spaces;
* a node id with more than one position in the coordinates csv (the regional models reuse
  some ids) gets the position nearest the other nodes of the lines that use it;
* nodes with no position are kept with blank X/Y, so compare_lines.py can report them.

    python prepare_hubs_lines.py ../Data/Routes_and_Nodes_V1.1.xlsx ../Data/All_nodes+lines_V1.01.csv \
        -o ../Data/Routes_and_Nodes_V1.1_nodes+lines.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def read_coords(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1255", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    cols = {c.lower(): c for c in df.columns}
    df = df.rename(columns={cols["node"]: "node", cols["x"]: "X", cols["y"]: "Y"})
    df = df.dropna(subset=["X", "Y"])
    df["X"], df["Y"] = df["X"].round(1), df["Y"].round(1)
    return df[["node", "X", "Y"]].drop_duplicates()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("routes", help="routes workbook (Line, Node, Include, PlannedMode)")
    ap.add_argument("coords", help="csv with node, X, Y (EPSG:2039)")
    ap.add_argument("--sheet", default="All")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    r = pd.read_excel(args.routes, sheet_name=args.sheet)
    n_rows, n_lines = len(r), r["Line"].nunique()
    r["LINE_ID"] = r["Line"].astype(str).str.strip()
    excluded = sorted(r.loc[r["Include"] == 0, "LINE_ID"].unique())
    r = r[r["Include"] != 0]
    r = r.rename(columns={"Node": "node"})
    r["mode"] = r["PlannedMode"] if "PlannedMode" in r.columns else ""
    r = r[["node", "LINE_ID", "mode"]].drop_duplicates(["node", "LINE_ID"])

    xy = read_coords(Path(args.coords))
    multi = xy["node"].duplicated(keep=False)
    single = xy[~multi].set_index("node")[["X", "Y"]]
    pos = single.to_dict("index")
    for node, cand in xy[multi].groupby("node"):
        lines = r.loc[r["node"] == node, "LINE_ID"]
        others = r.loc[r["LINE_ID"].isin(lines) & (r["node"] != node), "node"]
        ref = single.reindex(others).dropna()
        c = cand[["X", "Y"]].to_numpy(float)
        k = int(np.argmin(np.hypot(*(c - ref.median().to_numpy(float)).T))) if len(ref) else 0
        pos[node] = {"X": c[k, 0], "Y": c[k, 1]}
    r["X"] = r["node"].map(lambda n: pos.get(n, {}).get("X"))
    r["Y"] = r["node"].map(lambda n: pos.get(n, {}).get("Y"))
    r.to_csv(args.out, index=False, encoding="utf-8-sig")

    miss = r[r["X"].isna()]
    per_line = r.assign(has=r["X"].notna()).groupby("LINE_ID")["has"].agg(["size", "sum"])
    print(f"{args.routes} [{args.sheet}]: {n_rows} rows, {n_lines} lines; {len(excluded)} lines with Include = 0 dropped")
    print(f"kept {len(r)} node x line rows, {r['LINE_ID'].nunique()} lines, {r['node'].nunique()} nodes; "
          f"{miss['node'].nunique()} nodes ({len(miss)} rows) have no coordinates")
    print(f"lines with no coordinates at all: {', '.join(per_line.index[per_line['sum'] == 0])}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
