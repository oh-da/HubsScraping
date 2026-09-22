#!/usr/bin/env python3
"""
Browser-free fallback: discover and download the explorer's data endpoints.

Downloads the app's HTML and JS bundles, greps them for anything that looks
like a data URL (/api/..., *.json, *.geojson, *.csv, mapbox dataset ids) and
tries to GET each candidate.  Every JSON-ish 200 response is saved to
<out>/raw/ in the same layout scrape_tod.py uses, so build_outputs.py can
consume it directly.

Use this when Playwright / Chromium is not available.  It cannot see data
that the app only requests after user interaction, so prefer scrape_tod.py.

    python probe_endpoints.py [--url URL] [--out data]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

APP_URL = "https://tod-israel-d99f0bf7ea5d.herokuapp.com/"
COMMON = ["api/stations", "api/lines", "api/data", "api/layers", "api/geojson", "api/network",
          "data/stations.geojson", "data/lines.geojson", "data/stations.json", "data/lines.json",
          "stations.geojson", "lines.geojson", "stations.json", "lines.json", "data.json", "data.geojson",
          "static/data/stations.geojson", "static/data/lines.geojson", "api/metropolins", "api/municipalities",
          "api/functional_areas", "api/radiuses", "api/accessibility"]
URL_RE = re.compile(r"""["'`]((?:https?:)?//[^"'`\s]+?|/?[A-Za-z0-9_\-./]+?)\.(?:geo)?json["'`]|["'`](/api/[A-Za-z0-9_\-./]*)["'`]""")


def log(m): print(f"[probe] {m}", flush=True)


def safe_name(url: str) -> str:
    p = urlparse(url)
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", (p.path.rstrip("/").split("/")[-1] or "index").replace(".", "_"))[:60]
    return f"{base}_{hashlib.md5(url.encode()).hexdigest()[:8]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=APP_URL)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()
    raw = Path(args.out) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (tod-scraper)"

    log(f"GET {args.url}")
    html = s.get(args.url, timeout=60).text
    scripts = re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", html)
    texts = [html]
    for src in scripts:
        u = urljoin(args.url, src)
        try:
            r = s.get(u, timeout=60)
            texts.append(r.text)
            log(f"bundle {len(r.text):>10,d} chars  {u}")
        except Exception as exc:  # noqa: BLE001
            log(f"warn: {u}: {exc}")

    cands: set[str] = set()
    for t in texts:
        for m in URL_RE.finditer(t):
            g = m.group(1) or m.group(2)
            if not g:
                continue
            full = m.group(0).strip("\"'`")
            if "node_modules" in full or full.startswith("data:"):
                continue
            cands.add(full)
    cands |= set(COMMON)
    log(f"{len(cands)} candidate endpoints")

    manifest = []
    tried: set[str] = set()
    for c in sorted(cands):
        u = urljoin(args.url, c)
        if u in tried:
            continue
        tried.add(u)
        try:
            r = s.get(u, timeout=60)
        except Exception:  # noqa: BLE001
            continue
        ct = (r.headers.get("content-type") or "").lower()
        if r.status_code != 200 or not ("json" in ct or r.text.lstrip().startswith(("{", "["))):
            continue
        try:
            json.loads(r.text)
        except Exception:  # noqa: BLE001
            continue
        ext = ".geojson" if '"features"' in r.text[:2000] else ".json"
        f = raw / (safe_name(u) + ext)
        f.write_bytes(r.content)
        manifest.append({"kind": "network", "url": u, "file": f.name, "bytes": len(r.content), "content_type": ct})
        log(f"saved {len(r.content):>9,d} B  {u}")

    (Path(args.out) / "manifest.json").write_text(json.dumps({"app_url": args.url, "scripts": scripts, "items": manifest}, indent=2))
    log(f"done: {len(manifest)} data files -> {raw}")
    return 0 if manifest else 2


if __name__ == "__main__":
    sys.exit(main())
