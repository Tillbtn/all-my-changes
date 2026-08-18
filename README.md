# All my OSM changes

An interactive MapLibre **globe** showing every OpenStreetMap object ever
edited by a set of users (see `users.txt`) — highlighted with configurable
styles on configurable basemaps, switchable between mappers.

## How it works

```
OSM API (changeset dumps, cached forever)      Geofabrik .pbf, QLever
        │                                              │
        ▼                                              ▼
  data/cache/changesets/*.osc.gz  ──►  SQLite index  ──►  geometries
                                    (per-user objects)    (pbf → QLever →
                                              │            API → Overpass)
                                              ▼
                                web/data/<user>/*.geojson + meta.json
                                              │
                                              ▼
                                   web/ MapLibre globe frontend
```

Every step runs once per user (`OSM_USER` env var, looped by `update.sh`);
state lives in `data/users/<user>/mychanges.sqlite`, the changeset dump
cache is shared.

1. **`pipeline/fetch_changesets.py`** pages through the user's changeset
   listing and downloads each changeset's osmChange dump from the OSM API.
   Dumps are immutable and cached in `data/cache/changesets/` — the initial
   backfill happens once, afterwards a daily run costs only a couple of
   requests.
2. **`pipeline/parse_changesets.py`** builds the object index: every
   node/way/relation the user ever touched, with first/last edit date,
   edit count and last action.
3. **`pipeline/resolve_pbf.py`** resolves current geometries from the local
   Geofabrik extract in `data/` (no API load at all). Works like
   `osmium getid -r` + export, implemented with pyosmium id filters. The
   extract's snapshot date is cached in `data/cache/pbf_header.json` — just
   opening a reader on a 4.5 GB file can stall for a minute, and the answer
   is the same for every user.
4. **`pipeline/resolve_qlever.py`** resolves leftover nodes/ways and all
   relations from the [QLever osm-planet](https://qlever.dev/osm-planet)
   SPARQL endpoint: planet-wide precomputed geometries, so edits abroad
   need no extra country extracts and relations arrive fully assembled.
   Its snapshot lags a few weeks — anything newer falls through.
5. **`pipeline/resolve_api.py`** resolves the rest (anything newer than
   the snapshots) with the main API's bulk multi-fetch endpoints. Objects
   deleted by others are detected here (visible=false).
6. **`pipeline/resolve_overpass.py`** fetches the last relations (newer
   than the QLever snapshot, or deleted) via Overpass in throttled batches,
   rotating between public mirrors.
7. **`pipeline/build_output.py`** writes `web/data/<user>/{polygons,lines,
   relations,points,vertices,deleted}.geojson.gz` + `meta.json`, and the
   `users.json` index that feeds the frontend's mapper dropdown. See
   [Output size](#output-size) for what it does to keep those files small.

`.venv/bin/python pipeline/status.py [user...]` prints where every user
stands: changesets fetched/parsed, which source resolved how many
geometries, what is still unresolved, and **how old the published web
output is**.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install osmium requests osm2geojson shapely
# put a Geofabrik .osm.pbf covering the main editing area into data/
# (the *internal* flavour is not required - the file is only used to look
#  up geometries by id; user metadata is taken from the changeset dumps)
# list the OSM usernames in users.txt, then:
./update.sh          # or: ./update.sh SomeUser   for a single user
./serve.sh           # then open http://localhost:8080
```

### Logging

Every step timestamps its lines, reports its own runtime and ends with a
summary of what the remote services cost — request count, time spent
waiting, bytes transferred, QLever's server-side `query-time-ms`, time lost
to rate limiting, and the slowest single request. Long loops print progress
with a rate and an ETA. `update.sh` adds per-step, per-user and total wall
times.

```
[19:12:44]   way batch 12/141 geometry  2.1s, server 1.4s, 1000 rows, 4.2 MB (2.0 MB/s)
[19:12:51]   ways 12000/141184 (8.5%) 640 ways/s  elapsed 18.8s  eta 3m22s
[19:20:02] QLever: 282 requests, 9m14s waiting for responses, 4m01s of that
           server-side, 1.1 GB transferred, 11m00s throttled/sleeping
```

- `AMC_VERBOSE=2` logs *every* API/QLever/Overpass request (default `1`
  logs only those slower than `AMC_SLOW_REQUEST=10` seconds).
- `AMC_VERBOSE=0` keeps just the phase banners and summaries.
- `AMC_PROGRESS_EVERY=5` is the seconds between progress lines.

### Quick previews

`./update.sh --quick` skips the changeset pipeline and pulls each user's
objects straight from QLever (two bulk queries, seconds to minutes instead
of hours). The trade-offs: only objects whose *current* version is by the
user (anything later retouched by others is missing — bites hardest for
relations), no deleted objects, no per-user edit history, and a snapshot a
few weeks old. Finished full datasets are never overwritten, and a later
full `./update.sh` replaces a preview cleanly — for a user who already has
one, `--quick` prints how old that dataset is and does nothing else, since
*"a full dataset exists"* is not the same as *"the data is current"*.

## Publishing (GitHub Pages)

`./deploy.sh` force-pushes the `web/` directory (including the generated
data) as a single-commit `gh-pages` branch. One-time setup on GitHub:
*Settings → Pages → Deploy from a branch → `gh-pages` / root*. Re-run it
after each `./update.sh` to refresh the published data. It refuses to push
if a file exceeds git's 100 MB limit or the payload exceeds the 1 GB
GitHub Pages limit.

### Output size

GeoJSON is verbose, and a prolific mapper is easily hundreds of megabytes
of it — `hca` alone was 275 MB raw, with a 147 MB `polygons.geojson` that
git would have refused outright. `build_output.py` therefore writes
**gzipped** GeoJSON and trims what it can; the same dataset now publishes
at 50 MB, and all ten users together at 79 MB instead of 545 MB.

| knob | default | effect |
| --- | --- | --- |
| `AMC_GZIP` | `1` | write `<name>.geojson.gz`; ~4x on areas, ~11x on vertices. `meta.json` carries a `gzip` flag and the frontend inflates via `DecompressionStream`. |
| `AMC_PRECISION` | `6` | coordinate decimals (6 ≈ 11 cm). ~7% of the raw bytes, invisible on a map. |
| `AMC_SIMPLIFY` | `0` | Douglas-Peucker tolerance in **degrees**, applied before rounding. `3e-6` (≈0.3 m) buys another ~20% on traced outlines, but does change geometry. |
| `AMC_VERTEX_CAP` | `300000` | untagged vertices are evenly sampled down to this many. |

Two properties were also dropped from every feature: the year (`y`, now
derived from `l` in `web/config.js`) and `f` when it equals `l`.

`build_output.py` prints the size of each file with its compression ratio
and warns before either GitHub limit is hit.

## Daily updates

`./update.sh` is fully incremental — schedule it, e.g. with cron:

```cron
15 5 * * *  /home/Till/coding/OSM/all-my-changes/update.sh >> /tmp/amc-update.log 2>&1
```

or a systemd user timer. New changesets are discovered with 1–2 API
requests per user, their dumps downloaded once, geometries resolved
(pbf → QLever → API → Overpass, each pass only handles what the previous
ones could not), and the GeoJSON rebuilt.

## Configuration

Everything visual lives in **`web/config.js`**:

- **`basemaps`** — vector styles (any MapLibre style URL, e.g. OpenFreeMap),
  raster xyz tiles (OSM, Carto, Esri imagery) or a plain colored globe.
- **`highlightStyles`** — paint properties per layer kind
  (`polygonFill`, `polygonLine`, `line`, `point`, `vertex`, `deleted`,
  optional `…Halo` layers and `extrusionColor` for 3D). Ships with:
  *Red outline*, *Neon glow*, *Solid fill* and *Edit age* (colored by year
  of my last edit).
- **`defaults`** — initial basemap/style; user choices persist in
  localStorage.

Layer toggles in the UI: areas, ways, relations (routes/boundaries with
their full member geometry), points, untagged vertices (every node I
moved), deleted objects (last known position), and **3D buildings**.

### 3D buildings

`web/roofs3d.js` is a three.js custom layer that models the
[Simple 3D Buildings](https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings)
schema instead of just extruding flat boxes:

- **roof shapes**: gabled, hipped, skillion, pyramidal, dome, onion, flat —
  plus aliases (gambrel/saltbox→gabled, half-hipped/mansard→hipped, ...).
  Ridges are real geometry (the footprint is split along the ridge line),
  so gables and hip faces render crisply.
- honors `height`, `building:levels`, `min_height`/`building:min_level`,
  `roof:height`, `roof:levels`, `roof:direction`, `roof:orientation`,
  and `building:colour`/`roof:colour` (roofs default to a darker wall tone).
- building outlines that are modeled by `building:part`s are detected in
  the pipeline (`op` flag) and not extruded, per the schema.
- ridge orientation comes from the footprint's minimum-area bounding box,
  overridden by `roof:orientation=across` / `roof:direction`.

The **Roof shapes** sub-toggle in the layer menu switches between modeled
roofs and plain flat-top fill-extrusion at runtime (some prefer MapLibre's
softer shading); `use3dRoofs: false` in `web/config.js` disables the
three.js renderer entirely. The 3D layer only draws at zoom ≥ 12.

Pipeline knobs (user name, API endpoints, throttling) are at the top of
`pipeline/common.py`.

## Notes

- The changeset dumps are the source of truth for *"did I ever edit this?"*;
  the pbf and Overpass only supply the object's **current** geometry. An
  object heavily reshaped by others since my edit still counts.
- Deleted ways/relations have no live geometry and are counted but not shown;
  deleted nodes are shown at their last known position (grey circles).
- If gzipped GeoJSON ever stops being enough (a single file approaching
  100 MB, or the site the 1 GB Pages limit), the natural next step is piping
  `build_output.py`'s files through tippecanoe into PMTiles and using the
  `pmtiles` protocol in MapLibre — the frontend structure stays the same,
  and it would also fix the fact that the browser currently parses whole
  layers at once.
