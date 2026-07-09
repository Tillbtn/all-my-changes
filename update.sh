#!/usr/bin/env bash
# Run the full pipeline: fetch new changesets, resolve geometries, rebuild
# the web data. Safe to run daily (e.g. via cron or a systemd timer) - all
# fetched changesets are cached, so incremental runs only cost a couple of
# API requests.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python

$PY -u pipeline/fetch_changesets.py
$PY -u pipeline/parse_changesets.py
$PY -u pipeline/resolve_pbf.py
$PY -u pipeline/resolve_api.py
$PY -u pipeline/resolve_overpass.py
$PY -u pipeline/build_output.py

echo "Done. Serve the map with: ./serve.sh"
