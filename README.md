# All my OSM changes

An interactive MapLibre **globe** showing every OpenStreetMap object ever
edited by [`Till_btn`](https://www.openstreetmap.org/user/Till_btn) —
highlighted with configurable styles on configurable basemaps.

## How it works

```
OSM API (changeset dumps, cached forever)      Geofabrik internal .pbf
        │                                              │
        ▼                                              ▼
  data/cache/changesets/*.osc.gz  ──►  SQLite index  ──►  geometries
                                       (objects)          (pbf first,
                                                           Overpass fallback)
                                              │
                                              ▼
                                   web/data/*.geojson + meta.json
                                              │
                                              ▼
                                   web/ MapLibre globe frontend
```

1. **`pipeline/fetch_changesets.py`** pages through my changeset listing and
   downloads each changeset's osmChange dump from the OSM API. Dumps are
   immutable and cached in `data/cache/changesets/` — the initial backfill
   happens once, afterwards a daily run costs only a couple of requests.
2. **`pipeline/parse_changesets.py`** builds the object index: every
   node/way/relation I ever touched, with first/last edit date, edit count
   and last action.
3. **`pipeline/resolve_pbf.py`** resolves current geometries from the local
   Geofabrik extract in `data/` (no API load at all). Works like
   `osmium getid -r` + export, implemented with pyosmium id filters.
4. **`pipeline/resolve_api.py`** resolves leftover nodes/ways (outside the
   extract or newer than its snapshot) with the main API's bulk multi-fetch
   endpoints. Objects deleted by others are detected here (visible=false).
5. **`pipeline/resolve_overpass.py`** fetches relations via Overpass in
   throttled batches (rotating between public mirrors), because Overpass
   assembles full member geometry in one query.
6. **`pipeline/build_output.py`** writes `web/data/{polygons,lines,points,
   vertices,deleted}.geojson` and `meta.json`.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install osmium requests osm2geojson
# put a Geofabrik .osm.pbf covering your main editing area into data/
# (the *internal* flavour is not required - the file is only used to look
#  up geometries by id; user metadata is taken from the changeset dumps)
./update.sh
./serve.sh          # then open http://localhost:8080
```

## Daily updates

`./update.sh` is fully incremental — schedule it, e.g. with cron:

```cron
15 5 * * *  /home/Till/coding/OSM/all-my-changes/update.sh >> /tmp/amc-update.log 2>&1
```

or a systemd user timer. New changesets are discovered with 1–2 API
requests, their dumps downloaded once, geometries fetched from Overpass
(the pbf is only consulted for objects older than its snapshot), and the
GeoJSON rebuilt.

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
- If the data outgrows GeoJSON comfort (~100 MB), the natural next step is
  piping `build_output.py`'s files through tippecanoe into PMTiles and using
  the `pmtiles` protocol in MapLibre — the frontend structure stays the same.
