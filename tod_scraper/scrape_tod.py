#!/usr/bin/env python3
"""
Scrape the "TOD Israel" Transit-Oriented Development explorer
(https://tod-israel-d99f0bf7ea5d.herokuapp.com, embedded in
https://www.nsa-hub.com/models-softwares/transit-oriented-development-explorer).

The app is a Mapbox GL single-page app. The station / line / polygon data it
draws is fetched over the network as JSON / GeoJSON (or held in Mapbox GeoJSON
sources). This script drives a real browser with Playwright and:

  1. optionally opens the password-protected NSA-hub portal page, enters the
     password and finds the embedded app URL (iframe);
  2. opens the app and records EVERY JSON / GeoJSON response it downloads;
  3. flips every layer toggle in the sidebar so lazily loaded layers
     (accessibility radiuses, metropolins, municipalities, ...) get fetched too;
  4. dumps every Mapbox GL source that is held in memory (GeoJSON sources
     directly, vector-tile sources via querySourceFeatures at a national zoom);
  5. writes everything to  <out>/raw/  plus a  manifest.json.

Run  build_outputs.py  afterwards to turn the raw captures into shapefiles,
a GeoPackage and an Excel workbook.

Usage
-----
    pip install -r requirements.txt
    playwright install chromium
    python scrape_tod.py                       # direct to the Heroku app
    python scrape_tod.py --via-portal          # go through nsa-hub + password
    python scrape_tod.py --headed              # watch the browser
"""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, Response, TimeoutError as PWTimeout, sync_playwright

APP_URL = "https://tod-israel-d99f0bf7ea5d.herokuapp.com/"
PORTAL_URL = "https://www.nsa-hub.com/models-softwares/transit-oriented-development-explorer"
PORTAL_PASSWORD = "tod@2026"

JSON_CT = ("application/json", "application/geo+json", "text/json", "application/vnd.geo+json")
DATA_EXT = (".json", ".geojson", ".csv", ".topojson")

# Whole of Israel, in lon/lat, used to force vector-tile sources to load nationally
ISRAEL_BOUNDS = [[34.2, 29.4], [35.95, 33.4]]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[scrape] {msg}", flush=True)


def safe_name(url: str) -> str:
    p = urlparse(url)
    base = (p.path.rstrip("/").split("/")[-1] or "index").replace(".", "_")
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", base)[:60]
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{base}_{h}"


TOKEN_RE = re.compile(r"\b(?:sk|pk)\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}")


def redact(text: str) -> str:
    """Strip Mapbox access tokens so captured files can be committed safely."""
    return TOKEN_RE.sub("<MAPBOX_TOKEN_REDACTED>", text)


def looks_like_data(resp: Response) -> bool:
    ct = (resp.headers.get("content-type") or "").lower()
    url = resp.url.lower()
    if any(ct.startswith(c) for c in JSON_CT):
        return True
    path = urlparse(url).path
    return path.endswith(DATA_EXT)


class Capture:
    def __init__(self, out_dir: Path, app_host: str = ""):
        self.raw = out_dir / "raw"
        self.bundles = out_dir / "bundles"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.bundles.mkdir(parents=True, exist_ok=True)
        self.manifest: list[dict] = []
        self.all_responses: list[dict] = []
        self.seen: set[str] = set()
        self.app_host = app_host

    def on_response(self, resp: Response) -> None:
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            if len(self.all_responses) < 1000:
                self.all_responses.append({"url": resp.url, "status": resp.status, "content_type": ct,
                                           "method": resp.request.method, "type": resp.request.resource_type})
            if resp.status != 200 or resp.url in self.seen:
                return
            host = urlparse(resp.url).netloc
            is_script = resp.request.resource_type == "script" or "javascript" in ct
            if is_script and host == self.app_host:
                # keep the app's own bundles: station data is sometimes compiled into them
                body = resp.body()
                self.seen.add(resp.url)
                fname = safe_name(resp.url) + ".js"
                (self.bundles / fname).write_text(redact(body.decode("utf-8", "replace")))
                self.manifest.append({"kind": "bundle", "url": resp.url, "file": fname, "bytes": len(body)})
                log(f"bundle   {len(body):>9,d} B  {resp.url}")
                return
            if not looks_like_data(resp):
                # content-type lies are common: sniff the body of anything from the app itself
                if host != self.app_host or resp.request.resource_type in ("image", "font", "stylesheet", "media"):
                    return
                body = resp.body()
                head = body.lstrip()[:1]
                if head not in (b"{", b"["):
                    return
            else:
                body = resp.body()
            if not body:
                return
            self.seen.add(resp.url)
            ext = ".geojson" if b'"FeatureCollection"' in body[:2000] or b'"features"' in body[:2000] else ".json"
            if urlparse(resp.url).path.lower().endswith(".csv"):
                ext = ".csv"
            fname = safe_name(resp.url) + ext
            (self.raw / fname).write_text(redact(body.decode("utf-8", "replace")))
            self.manifest.append(
                {
                    "kind": "network",
                    "url": resp.url,
                    "file": fname,
                    "status": resp.status,
                    "content_type": resp.headers.get("content-type"),
                    "bytes": len(body),
                    "request_method": resp.request.method,
                }
            )
            log(f"captured {len(body):>9,d} B  {resp.url}")
        except Exception as exc:  # noqa: BLE001 - never let a capture error kill the run
            log(f"warn: could not save {resp.url}: {exc}")

    def add_map_source(self, source_id: str, data: dict | list, kind: str) -> None:
        fname = f"mapsource_{re.sub(r'[^A-Za-z0-9_-]', '_', source_id)}.geojson"
        (self.raw / fname).write_text(redact(json.dumps(data, ensure_ascii=False)))
        n = len(data.get("features", [])) if isinstance(data, dict) else len(data)
        self.manifest.append({"kind": kind, "source_id": source_id, "file": fname, "features": n})
        log(f"map source '{source_id}' -> {fname} ({n} features)")

    def save_manifest(self, extra: dict) -> None:
        (self.raw.parent / "manifest.json").write_text(
            redact(json.dumps({"captured_at": time.strftime("%Y-%m-%d %H:%M:%S"), **extra, "items": self.manifest,
                               "all_responses": self.all_responses}, ensure_ascii=False, indent=2))
        )


# --------------------------------------------------------------------------- #
# portal (Wix-style password page) -> embedded app URL
# --------------------------------------------------------------------------- #
def resolve_app_url_via_portal(page: Page, password: str) -> str:
    log(f"opening portal {PORTAL_URL}")
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(3000)

    pw = page.locator("input[type='password']")
    if pw.count():
        log("password prompt found - submitting")
        pw.first.fill(password)
        pw.first.press("Enter")
        page.wait_for_timeout(4000)

    # look for the embedded explorer (iframe) or a link to it
    cands: list[str] = []
    for fr in page.frames:
        if fr.url and "herokuapp" in fr.url:
            cands.append(fr.url)
    for el in page.locator("iframe, a").all():
        for attr in ("src", "href", "data-src"):
            v = el.get_attribute(attr)
            if v and "herokuapp" in v:
                cands.append(v)
    html = page.content()
    cands += re.findall(r"https?://[a-z0-9\-]+\.herokuapp\.com[^\"'\s<>]*", html)

    if cands:
        url = cands[0]
        log(f"embedded app url: {url}")
        return url
    log("no herokuapp iframe found on portal page - falling back to the default app URL")
    return APP_URL


# --------------------------------------------------------------------------- #
# in-page extraction of Mapbox GL sources
# --------------------------------------------------------------------------- #
FIND_MAP_JS = r"""
() => {
  // Try to locate a live mapbox-gl / maplibre-gl Map instance.
  const isMap = o => o && typeof o === 'object' && typeof o.getStyle === 'function'
                   && typeof o.getSource === 'function' && typeof o.querySourceFeatures === 'function';
  const found = [];
  const names = ['map', 'mapInstance', '_map', 'mapRef', 'todMap', 'mapboxMap'];
  for (const n of names) if (isMap(window[n])) found.push(n);
  for (const k of Object.keys(window)) {
    try { if (isMap(window[k]) && !found.includes(k)) found.push(k); } catch (e) {}
  }
  // React: walk the fiber tree from the map container for a prop / state holding the map
  const containers = document.querySelectorAll('.mapboxgl-map, .maplibregl-map');
  for (const c of containers) {
    for (const k of Object.keys(c)) {
      if (k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')) {
        let f = c[k]; let depth = 0;
        while (f && depth < 60) {
          const st = f.memoizedState, pr = f.memoizedProps;
          for (const cand of [st && st.memoizedState, pr && pr.map, st && st.map]) {
            if (isMap(cand)) { window.__todMap = cand; return ['__todMap']; }
          }
          // hooks list
          let h = st;
          while (h && typeof h === 'object') {
            const v = h.memoizedState;
            if (isMap(v)) { window.__todMap = v; return ['__todMap']; }
            if (v && typeof v === 'object' && isMap(v.current)) { window.__todMap = v.current; return ['__todMap']; }
            h = h.next;
          }
          f = f.return; depth++;
        }
      }
    }
  }
  if (found.length) window.__todMap = window[found[0]];
  return found;
}
"""

DUMP_SOURCES_JS = r"""
async () => {
  const map = window.__todMap;
  if (!map) return {error: 'no map'};
  const style = map.getStyle();
  const out = {sources: {}, layers: []};
  for (const l of (style.layers || [])) {
    out.layers.push({id: l.id, type: l.type, source: l.source, sourceLayer: l['source-layer'],
                     visible: !(l.layout && l.layout.visibility === 'none'), filter: l.filter || null});
  }
  for (const [id, s] of Object.entries(style.sources || {})) {
    const entry = {type: s.type, url: s.url || null, tiles: s.tiles || null, data: null, sourceLayers: []};
    try {
      const src = map.getSource(id);
      if (s.type === 'geojson') {
        // mapbox keeps the original data on _data (object or URL string)
        let d = src && src._data;
        if (typeof d === 'string') {
          try {
            const ac = new AbortController(); const t = setTimeout(() => ac.abort(), 20000);
            d = await (await fetch(d, {signal: ac.signal})).json(); clearTimeout(t);
          } catch (e) { d = {fetchError: String(e), url: d}; }
        }
        if (!d || (d && d.fetchError)) {
          // fall back to what is currently rendered
          const feats = map.querySourceFeatures(id);
          d = {type: 'FeatureCollection', features: feats.map(f => f.toJSON ? f.toJSON() : f)};
        }
        entry.data = d;
      } else if (s.type === 'vector' && !(String(s.url || '').startsWith('mapbox://mapbox.'))) {
        const sl = new Set(out.layers.filter(l => l.source === id && l.sourceLayer).map(l => l.sourceLayer));
        for (const layer of sl) {
          const feats = map.querySourceFeatures(id, {sourceLayer: layer});
          const seen = new Set(); const uniq = [];
          for (const f of feats) {
            const j = f.toJSON ? f.toJSON() : f;
            const key = j.id !== undefined ? String(j.id) : JSON.stringify(j.properties);
            if (!seen.has(key)) { seen.add(key); uniq.push(j); }
          }
          entry.sourceLayers.push({name: layer, data: {type: 'FeatureCollection', features: uniq}});
        }
      }
    } catch (e) { entry.error = String(e); }
    out.sources[id] = entry;
  }
  return out;
}
"""


DUMP_MARKERS_JS = r"""
() => {
  const map = window.__todMap;
  const markers = (map && map._markers) || [];
  const feats = [];
  for (const m of markers) {
    try {
      const ll = m.getLngLat();
      const el = m.getElement ? m.getElement() : m._element;
      const props = {};
      if (el) {
        props.class = el.className || null;
        props.title = el.getAttribute('title') || el.getAttribute('aria-label') || null;
        props.text = (el.innerText || '').trim().slice(0, 500) || null;
        for (const a of el.attributes) if (a.name.startsWith('data-')) props[a.name.slice(5)] = a.value;
        const icons = [...el.querySelectorAll('[class*="icon"],[class*="mode"],svg,img')]
          .map(i => i.getAttribute('class') || i.getAttribute('alt') || i.getAttribute('data-mode') || '').filter(Boolean);
        if (icons.length) props.icons = icons;
      }
      const pop = m.getPopup && m.getPopup();
      if (pop) props.popup_html = (pop._content && pop._content.innerHTML) || pop._content || null;
      feats.push({type: 'Feature', geometry: {type: 'Point', coordinates: [ll.lng, ll.lat]}, properties: props});
    } catch (e) {}
  }
  return {type: 'FeatureCollection', features: feats};
}
"""

# Walk the React fiber tree (and window globals) for arrays of objects that look like
# stations / lines: coordinates, geometry, or lat/lon-like keys.  Catches data kept in
# component state, zustand / redux stores, react-query caches, etc.
SCAN_STATE_JS = r"""
() => {
  const GEO_KEYS = /^(lat|latitude|lng|lon|long|longitude|x|y|coords?|coordinates|geometry|geom|position|lnglat|latlng)$/i;
  const seen = new WeakSet(); const hits = []; let visits = 0;
  const isGeoObj = o => o && typeof o === 'object' && !Array.isArray(o) && Object.keys(o).some(k => GEO_KEYS.test(k));
  const isGeoArr = a => Array.isArray(a) && a.length >= 20 && a.slice(0, 5).every(isGeoObj);
  const isFC = o => o && typeof o === 'object' && o.type === 'FeatureCollection' && Array.isArray(o.features) && o.features.length >= 5;
  function walk(v, path, depth) {
    if (!v || typeof v !== 'object' || depth > 14 || visits++ > 400000) return;
    if (seen.has(v)) return; seen.add(v);
    if (v instanceof Node || v instanceof Window) return;
    if (isFC(v)) { hits.push({path, kind: 'FeatureCollection', n: v.features.length, data: v}); return; }
    if (isGeoArr(v)) { hits.push({path, kind: 'records', n: v.length, data: v}); return; }
    if (Array.isArray(v)) { for (let i = 0; i < Math.min(v.length, 2000); i++) walk(v[i], path + '[' + i + ']', depth + 1); return; }
    for (const k of Object.keys(v)) {
      if (k === 'return' || k === '_owner' || k === 'stateNode' || k === '__reactFiber$' ) continue;
      let c; try { c = v[k]; } catch (e) { continue; }
      if (typeof c === 'function') continue;
      walk(c, path + '.' + k, depth + 1);
    }
    // react fiber links
    for (const k of ['memoizedState', 'memoizedProps', 'child', 'sibling', 'next', 'baseState', 'queue']) {
      if (k in v) { let c; try { c = v[k]; } catch (e) { continue; } if (c && typeof c === 'object') walk(c, path + '.' + k, depth + 1); }
    }
  }
  // 1. react roots
  for (const el of document.querySelectorAll('body *')) {
    for (const k of Object.keys(el)) {
      if (k.startsWith('__reactContainer') || k.startsWith('__reactFiber')) { walk(el[k], 'fiber', 0); break; }
    }
    if (visits > 400000) break;
  }
  // 2. window globals
  for (const k of Object.keys(window)) {
    if (/^(webkit|chrome|on|__todMap)/.test(k)) continue;
    let v; try { v = window[k]; } catch (e) { continue; }
    if (v && typeof v === 'object' && !(v instanceof Node) && !(v instanceof Window)) walk(v, 'window.' + k, 0);
  }
  // de-duplicate by (kind, n) + first item signature; keep the biggest few
  const uniq = new Map();
  for (const h of hits) {
    const first = h.kind === 'FeatureCollection' ? h.data.features[0] : h.data[0];
    const key = h.kind + ':' + h.n + ':' + JSON.stringify(first).slice(0, 200);
    if (!uniq.has(key)) uniq.set(key, h);
  }
  return [...uniq.values()].sort((a, b) => b.n - a.n).slice(0, 25);
}
"""


def dump_markers_and_state(page: Page, cap: Capture) -> None:
    try:
        fc = page.evaluate(DUMP_MARKERS_JS)
        if fc.get("features"):
            cap.add_map_source("markers", fc, "map_markers")
        else:
            log("no mapboxgl.Marker instances on the map")
    except Exception as exc:  # noqa: BLE001
        log(f"warn: marker dump failed: {exc}")
    try:
        hits = page.evaluate(SCAN_STATE_JS)
        log(f"state scan: {len(hits)} candidate datasets")
        for i, h in enumerate(hits):
            sid = f"state{i:02d}_{h['kind']}_{h['n']}"
            data = h["data"] if h["kind"] == "FeatureCollection" else h["data"]
            cap.add_map_source(sid, data, "js_state")
            cap.manifest[-1]["path"] = h["path"]
    except Exception as exc:  # noqa: BLE001
        log(f"warn: state scan failed: {exc}")


def dismiss_modals(page: Page) -> None:
    """Close the 'Beta' notice (and anything similar) so the sidebar is clickable."""
    for txt in ("OK", "Ok", "Got it", "Close", "אישור", "סגור", "המשך"):
        try:
            b = page.get_by_role("button", name=txt, exact=True)
            if b.count() and b.first.is_visible():
                b.first.click(timeout=2000)
                log(f"dismissed a dialog via '{txt}'")
                page.wait_for_timeout(800)
        except Exception:  # noqa: BLE001
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass


def dump_map_sources(page: Page, cap: Capture) -> dict:
    names = page.evaluate(FIND_MAP_JS)
    if not names:
        log("no Mapbox GL map instance reachable from JS - relying on network capture only")
        return {}
    log(f"map instance found as window.{names[0]} - fitting to Israel to load national tiles")
    try:
        page.evaluate("b => window.__todMap.fitBounds(b, {padding: 20, duration: 0})", ISRAEL_BOUNDS)
        page.wait_for_timeout(1500)
        page.evaluate("() => Promise.race([new Promise(r => window.__todMap.once('idle', r)), "
                      "new Promise(r => setTimeout(r, 8000))])")
    except Exception as exc:  # noqa: BLE001
        log(f"warn: fitBounds/idle failed: {exc}")
    dump = page.evaluate(DUMP_SOURCES_JS)
    if "error" in dump:
        log(f"warn: {dump['error']}")
        return {}
    for sid, s in dump["sources"].items():
        if s.get("error"):
            log(f"warn: source '{sid}': {s['error']}")
        if s.get("data"):
            cap.add_map_source(sid, s["data"], "map_geojson_source")
        for sl in s.get("sourceLayers", []):
            cap.add_map_source(f"{sid}__{sl['name']}", sl["data"], "map_vector_source_layer")
    (cap.raw.parent / "map_style.json").write_text(
        json.dumps({"layers": dump["layers"],
                    "sources": {k: {kk: vv for kk, vv in v.items() if kk not in ("data", "sourceLayers")}
                                for k, v in dump["sources"].items()}}, ensure_ascii=False, indent=2))
    return dump


# --------------------------------------------------------------------------- #
# poke the UI so lazily-loaded layers get requested
# --------------------------------------------------------------------------- #
def toggle_everything(page: Page) -> None:
    sel = ("input[type='checkbox'], [role='switch'], [role='checkbox'], "
           "button[aria-pressed], .toggle, .switch")
    els = page.locator(sel)
    n = els.count()
    log(f"found {n} toggle-like controls - switching each one on, then back")
    for i in range(n):
        el = els.nth(i)
        try:
            if not el.is_visible():
                continue
            state = None
            if el.get_attribute("type") == "checkbox":
                state = el.is_checked()
            elif el.get_attribute("aria-checked") is not None:
                state = el.get_attribute("aria-checked") == "true"
            if state is False:          # only turn OFF things ON (that is what triggers lazy loads)
                el.click(timeout=2000)
                page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass
    # planning status buttons ("Detailed only" / "Detailed + Strategic")
    for txt in ("Detailed only", "Detailed + Strategic"):
        try:
            b = page.get_by_text(txt, exact=True)
            if b.count():
                b.first.click(timeout=2000)
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            pass
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PWTimeout:
        pass


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=APP_URL, help="explorer app URL")
    ap.add_argument("--via-portal", action="store_true",
                    help="open the nsa-hub portal page first, enter the password, and follow the iframe")
    ap.add_argument("--password", default=PORTAL_PASSWORD)
    ap.add_argument("--out", default="data", help="output folder (raw/ + manifest.json go here)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--settle", type=int, default=8, help="seconds to wait after load for XHRs to finish")
    ap.add_argument("--no-toggle", action="store_true", help="do not click the layer toggles")
    ap.add_argument("--max-seconds", type=int, default=480,
                    help="hard watchdog: abort the run after this many seconds (captures already on disk are kept)")
    ap.add_argument("--chromium", default=os.environ.get("TOD_CHROMIUM"),
                    help="path to a Chromium/Chrome executable (default: Playwright's own download)")
    args = ap.parse_args()

    out = Path(args.out)
    cap = Capture(out, app_host=urlparse(args.url).netloc)
    if hasattr(signal, "SIGALRM"):
        def _bail(signum, frame):  # noqa: ARG001
            log(f"watchdog: {args.max_seconds}s elapsed - aborting (manifest so far is saved)")
            cap.save_manifest({"app_url": args.url, "aborted": True})
            os._exit(3)
        signal.signal(signal.SIGALRM, _bail)
        signal.alarm(args.max_seconds)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, executable_path=args.chromium or None)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="en-US")
        page = ctx.new_page()
        page.on("response", cap.on_response)

        app_url = args.url
        if args.via_portal:
            app_url = resolve_app_url_via_portal(page, args.password)
            cap.app_host = urlparse(app_url).netloc

        log(f"opening app {app_url}")
        page.goto(app_url, wait_until="domcontentloaded", timeout=120_000)
        try:
            page.wait_for_load_state("networkidle", timeout=60_000)
        except PWTimeout:
            log("networkidle timeout - continuing")
        page.wait_for_timeout(args.settle * 1000)

        # a login form on the app itself? (some deployments protect the app, not the portal)
        pw = page.locator("input[type='password']")
        if pw.count():
            log("password prompt on the app - submitting")
            pw.first.fill(args.password)
            pw.first.press("Enter")
            page.wait_for_timeout(args.settle * 1000)

        dismiss_modals(page)
        if not args.no_toggle:
            toggle_everything(page)
            page.wait_for_timeout(3000)

        page.screenshot(path=str(out / "screenshot.png"), full_page=False)
        dump_map_sources(page, cap)
        dump_markers_and_state(page, cap)

        # save the page + bundle list so endpoints can be inspected offline (tokens redacted)
        (out / "page.html").write_text(redact(page.content()))
        scripts = [s for s in page.evaluate("() => [...document.scripts].map(s => s.src).filter(Boolean)")]
        cap.save_manifest({"app_url": app_url, "scripts": scripts})
        browser.close()

    n_net = sum(1 for m in cap.manifest if m["kind"] == "network")
    n_map = len(cap.manifest) - n_net
    log(f"done: {n_net} network data files, {n_map} map-source dumps -> {out / 'raw'}")
    if not cap.manifest:
        log("NOTHING captured. Run with --headed to see what the page does, and check data/page.html.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
