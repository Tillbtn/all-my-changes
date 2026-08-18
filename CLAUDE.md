# all-my-changes

MapLibre globe of every OSM object a set of users (`users.txt`) ever edited.

- Python venv: `.venv/` (pyosmium, requests, osm2geojson, shapely). Run
  scripts with `.venv/bin/python -u pipeline/<step>.py` from the repo root;
  the `OSM_USER` env var selects the user (default `Till_btn`).
- Pipeline order: fetch_changesets → parse_changesets → resolve_pbf →
  resolve_qlever → resolve_api → resolve_overpass → build_output (all
  incremental; `./update.sh` loops them over users.txt). Geometry priority:
  local pbf, then QLever osm-planet SPARQL (planet-wide; untagged nodes
  sit under http:// ids, tagged objects under https://; snapshot lags
  weeks), then OSM API multi-fetch (recent objects + authoritative
  deletion detection), Overpass only for leftover relations.
- `./update.sh --quick` builds instant previews from QLever alone
  (last-editor semantics, no deletions/history; quick_qlever.py). It never
  overwrites a finished full dataset, and parse_changesets wipes
  quick-marked data (meta mode=quick) before a real run. For a user who
  already has a full dataset it only reports that dataset's age and stops -
  it is not a freshness check. QLever bulk TSV streams can be cut silently
  - the expected-row COUNT + re-stream loop in quick_qlever.py is what
  guards against half data; keep it.
- State lives in `data/users/<user>/mychanges.sqlite`; web output in
  `web/data/<user>/` plus a `users.json` index; changeset dumps cached
  immutably in the shared `data/cache/changesets/*.osc.gz` — never delete,
  they replace API load. `./deploy.sh` publishes `web/` to GitHub Pages
  (gh-pages branch, single commit).
- Output is gzipped GeoJSON (`*.geojson.gz`, ~5x; 545 MB -> 79 MB for all
  users) - app.js sniffs the gzip magic number and inflates with
  DecompressionStream, so it works whether or not a host sets
  Content-Encoding. Size knobs: AMC_GZIP / AMC_PRECISION (6 decimals) /
  AMC_SIMPLIFY (degrees, off) / AMC_VERTEX_CAP. Features carry no year
  prop - config.js derives it from `l`; `f` is omitted when it equals `l`.
- `web/` is a plain static site (no build step); visual config in
  `web/config.js`; generated data in `web/data/` (gitignored).
- Logging lives in common.py: `log`/`vlog`, `Phase` (timed block),
  `Progress` (rate + ETA), `HttpStats` (requests, bytes, server time,
  throttling, slowest call) and `run(main)` as each step's entry point.
  AMC_VERBOSE=2 logs every request, 0 only summaries. `status.py` reports
  how old each user's web output is.
- 3D mode: `web/roofs3d.js` models Simple-3D-Buildings roof shapes with
  three.js (props from `building_props()` in build_output.py); custom
  layers never appear in `map.getStyle().layers` — use `map.getLayer()`.
- Be polite to APIs: keep the sleeps in `pipeline/common.py`; the pbf in
  `data/` answers geometry lookups so Overpass only gets leftovers.
- Opening `osmium.io.Reader` on the 4.5 GB extract can stall up to ~90s
  (page-cache dependent), so the pbf snapshot date is cached per file
  size+mtime in `data/cache/pbf_header.json` - do not inline it back into
  every user's run.
