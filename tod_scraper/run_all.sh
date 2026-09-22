#!/usr/bin/env bash
# One-shot: scrape the TOD Israel explorer and build shapefiles + xlsx.
set -euo pipefail
cd "$(dirname "$0")"
python scrape_tod.py "$@" || python probe_endpoints.py
python build_outputs.py
