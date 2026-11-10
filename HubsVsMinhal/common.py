"""Shared loaders and helpers for the Hubs-vs-Minhal comparison maps."""
from __future__ import annotations

import json
import re
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"                    # inputs that only these maps use
SHARED = HERE.parent / "Data"           # inputs the repository already holds
ITM = "EPSG:2039"
WGS = "EPSG:4326"

HUBS_NODES = SHARED / "All_nodes+lines_V1.1.csv"
HUBS_MODES = DATA / "Lines_and_Planned_Mode_V1.1.csv"
HUBS_ROUTES = DATA / "Routes_and_Nodes_V1.1.xlsx"
HUBS_RESULTS = DATA / "hub_prioritization_results.csv"
HUBS_SCORING = DATA / "hub_scoring_calculation.xlsx"
MINHAL_GPKG = SHARED / "MT_Network.gpkg"

# Both projects' mode vocabularies mapped to one display vocabulary ...
MODE_MAP = {
    "Metro": "Metro", "LRT": "LRT", "BRT": "BRT",
    "Rail": "Rail", "Suburban Rail": "Rail", "Interurban Rail": "Rail",
    "Rail fast": "Rail fast", "HighSpeed Rail": "Rail fast",
    "FUNI": "Funicular / cable", "Funicular": "Funicular / cable", "Cable Line": "Funicular / cable",
}
MODE_ORDER = ["Metro", "LRT", "BRT", "Rail", "Rail fast", "Funicular / cable"]
# ... and to the coarser family used when asking "does the other project have this mode here?"
MODE_FAMILY = {"Metro": "Metro", "LRT": "LRT", "BRT": "BRT", "Rail": "Rail", "Rail fast": "Rail",
               "Funicular / cable": "Funicular / cable"}


def read_csv_he(path: Path) -> pd.DataFrame:
    """The DataPack CSVs are a mix of UTF-8 (with BOM) and Windows-1255."""
    for enc in ("utf-8-sig", "cp1255"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("csv", b"", 0, 1, f"{path} is neither UTF-8 nor cp1255")


def sort_modes(modes) -> list[str]:
    return sorted(set(modes), key=MODE_ORDER.index)


def load_minhal():
    """Minhal lines and stations (EPSG:2039). `active` = no year_cancel: as in the rest of this
    repository, lines and stops that Minhal cancels (by 2050, the Haifa metro by 2060) are left out
    of the Minhal network."""
    lines = gpd.read_file(MINHAL_GPKG, layer="lines")
    stations = gpd.read_file(MINHAL_GPKG, layer="stations")
    for df in (lines, stations):
        df["mode"] = df["type"].map(MODE_MAP)
        if df["mode"].isna().any():
            raise ValueError(f"unmapped Minhal type(s): {sorted(df.loc[df['mode'].isna(), 'type'].unique())}")
        df["year_cancel"] = pd.to_numeric(df["year_cancel"], errors="coerce")
        df["active"] = df["year_cancel"].isna()
    return lines, stations


def round_coords(coords, nd: int = 5):
    """ITM/WGS coordinate array -> [[lat, lng], ...] rounded, consecutive duplicates dropped."""
    out = []
    for x, y in coords:
        p = [round(float(y), nd), round(float(x), nd)]
        if not out or out[-1] != p:
            out.append(p)
    return out


def _clean(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


def dumps(v) -> str:
    """Compact JSON, NaN -> null, safe to embed inside a <script> element."""
    def fix(o):
        if isinstance(o, float) and np.isnan(o):
            return None
        if isinstance(o, dict):
            return {k: fix(x) for k, x in o.items()}
        if isinstance(o, (list, tuple)):
            return [fix(x) for x in o]
        if isinstance(o, (np.integer, np.floating, np.bool_)):
            return fix(_clean(o))
        return o
    return json.dumps(fix(v), ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")


def render(template: str, out: Path, values: dict[str, str]) -> None:
    """Fill a templates/*.html page: {{base_css}} plus the page's own __KEY__ placeholders."""
    html = (HERE / "templates" / template).read_text(encoding="utf-8")
    html = html.replace("/*__BASE_CSS__*/", (HERE / "templates" / "base.css").read_text(encoding="utf-8"))
    for k, v in values.items():
        html = html.replace(k, v)
    left = sorted(set(re.findall(r"__[A-Z0-9_]+__", html)))
    if left:
        raise ValueError(f"{template}: unfilled placeholders {left}")
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out.relative_to(HERE.parent)} ({out.stat().st_size / 1e6:.2f} MB)")
