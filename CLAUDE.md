# all-my-changes

MapLibre globe of every OSM object user `Till_btn` ever edited.

- Python venv: `.venv/` (pyosmium, requests, osm2geojson). Run scripts with
  `.venv/bin/python -u pipeline/<step>.py` from the repo root.
- Pipeline order: fetch_changesets → parse_changesets → resolve_pbf →
  resolve_api → resolve_overpass → build_output (all incremental;
  `./update.sh` runs all). Geometry priority: local pbf, then OSM API
  multi-fetch (nodes/ways), Overpass only for relations.
- State lives in `data/mychanges.sqlite`; changeset dumps cached immutably
  in `data/cache/changesets/*.osc.gz` — never delete, they replace API load.
- `web/` is a plain static site (no build step); visual config in
  `web/config.js`; generated data in `web/data/` (gitignored).
- 3D mode: `web/roofs3d.js` models Simple-3D-Buildings roof shapes with
  three.js (props from `building_props()` in build_output.py); custom
  layers never appear in `map.getStyle().layers` — use `map.getLayer()`.
- Be polite to APIs: keep the sleeps in `pipeline/common.py`; the pbf in
  `data/` answers geometry lookups so Overpass only gets leftovers.
