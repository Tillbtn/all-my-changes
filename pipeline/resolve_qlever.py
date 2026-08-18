"""Step 4a: resolve node, way and relation geometries via QLever osm-planet.

The Uni Freiburg QLever instance indexes the whole planet with osm2rdf
geometries precomputed: ways arrive with area typing applied (buildings as
POLYGON) and relations fully assembled - no member stitching, no country
extracts for edits abroad. Two quirks shape this step:

  - untagged nodes are indexed under the http:// scheme while tagged
    objects use https://, so node lookups query both id spellings;
  - the snapshot lags a few weeks behind. Only objects last edited before
    the snapshot are resolved here, and ids QLever does not return are left
    unresolved - the live-API steps that follow can tell "deleted" from
    "too new", QLever cannot.

The snapshot date is the MAX over all object timestamps. That aggregation
is expensive for the server, so its result is cached for a day and shared
by all users.

Every SPARQL call is timed and its server-side `query-time-ms` recorded, so
the step summary shows whether a slow run was the query engine, the network
or our own politeness sleeps. AMC_VERBOSE=2 logs each batch individually.
"""

import json
import time

import requests
from shapely import from_wkt
from shapely.geometry import GeometryCollection, MultiLineString, MultiPolygon, mapping

from common import (
    DATA_DIR,
    QLEVER_API,
    QLEVER_SLEEP,
    USER_AGENT,
    HttpStats,
    Phase,
    Progress,
    fmt_dur,
    log,
    loads_lenient,
    open_db,
    run,
    vlog,
)

BATCH = {"node": 1000, "way": 1000, "relation": 50}

OSM_PREFIX = "https://www.openstreetmap.org"
KEY_PREFIX = f"{OSM_PREFIX}/wiki/Key:"

SNAPSHOT_CACHE = DATA_DIR / "cache" / "qlever_snapshot.json"
SNAPSHOT_TTL = 24 * 3600

STATS = HttpStats("QLever")
# how much wall time went into the QLEVER_SLEEP pauses between batches
SLEPT = 0.0


def sparql(query, retries=5, timeout=300, label="query"):
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            r = requests.post(
                QLEVER_API,
                data={"query": query},
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/sparql-results+json",
                },
                timeout=timeout,
            )
        except requests.RequestException as e:
            STATS.retries += 1
            if attempt == retries - 1:
                raise
            log(f"qlever error after {fmt_dur(time.monotonic() - t0)} ({e}), "
                "retrying...", 1)
            time.sleep(10 * (attempt + 1))
            STATS.wait(10 * (attempt + 1))
            continue
        dur = time.monotonic() - t0
        nbytes = len(r.content)
        if r.status_code in (429, 502, 503, 504):
            STATS.retries += 1
            log(f"HTTP {r.status_code} from qlever after {fmt_dur(dur)}, "
                "waiting...", 1)
            time.sleep(15 * (attempt + 1))
            STATS.wait(15 * (attempt + 1))
            continue
        r.raise_for_status()
        data = loads_lenient(r.content, label)
        meta = data.get("meta") or {}
        STATS.record(label, dur, nbytes=nbytes,
                     server_ms=meta.get("query-time-ms"),
                     rows=meta.get("result-size-total"))
        return data
    raise RuntimeError("qlever keeps failing")


def pause():
    """Politeness sleep between batches, accounted for in the summary."""
    global SLEPT
    time.sleep(QLEVER_SLEEP)
    SLEPT += QLEVER_SLEEP


def snapshot_date():
    """Newest object timestamp in the QLever dataset (cached for a day)."""
    try:
        cached = json.loads(SNAPSHOT_CACHE.read_text())
        age = time.time() - cached["checked"]
        if age < SNAPSHOT_TTL:
            vlog(f"snapshot date from cache ({fmt_dur(age)} old)")
            return cached["snapshot"]
    except (OSError, ValueError, KeyError):
        pass
    with Phase("asking QLever for its snapshot date (slow MAX aggregation)"):
        resp = sparql(
            f"PREFIX osmmeta: <{OSM_PREFIX}/meta/> "
            "SELECT (MAX(?t) AS ?newest) WHERE { ?s osmmeta:timestamp ?t }",
            label="snapshot MAX(timestamp)",
        )
    value = resp["results"]["bindings"][0]["newest"]["value"]
    snap = value if value.endswith("Z") else value + "Z"
    SNAPSHOT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_CACHE.write_text(
        json.dumps({"snapshot": snap, "checked": time.time()})
    )
    return snap


def load_todo(db, otype, snap):
    """Unresolved objects old enough for the snapshot to know them."""
    return [
        oid
        for (oid,) in db.execute(
            """
            SELECT o.oid FROM objects o
            LEFT JOIN geoms g ON g.otype=o.otype AND g.oid=o.oid
            WHERE g.oid IS NULL AND o.otype=? AND o.last_edit <= ?
            ORDER BY o.oid
            """,
            (otype, snap),
        )
    ]


def values_clause(otype, ids):
    iris = [f"<{OSM_PREFIX}/{otype}/{oid}>" for oid in ids]
    if otype == "node":
        # untagged nodes live under the http:// scheme (osm2rdf quirk)
        iris += [f"<http://www.openstreetmap.org/{otype}/{oid}>" for oid in ids]
    return " ".join(iris)


def fetch_wkts(otype, ids, label):
    resp = sparql(
        "PREFIX geo: <http://www.opengis.net/ont/geosparql#> "
        "SELECT ?id ?wkt WHERE { "
        f"VALUES ?id {{ {values_clause(otype, ids)} }} "
        "?id geo:hasGeometry/geo:asWKT ?wkt . }",
        label=f"{label} geometry",
    )
    return {
        int(b["id"]["value"].rsplit("/", 1)[1]): b["wkt"]["value"]
        for b in resp["results"]["bindings"]
    }


def fetch_tags(otype, ids, label):
    resp = sparql(
        "SELECT ?id ?p ?v WHERE { "
        f"VALUES ?id {{ {values_clause(otype, ids)} }} "
        f'?id ?p ?v . FILTER(STRSTARTS(STR(?p), "{KEY_PREFIX}")) }}',
        label=f"{label} tags",
    )
    tags = {}
    for b in resp["results"]["bindings"]:
        oid = int(b["id"]["value"].rsplit("/", 1)[1])
        key = b["p"]["value"][len(KEY_PREFIX):]
        tags.setdefault(oid, {})[key] = b["v"]["value"]
    return tags


def to_geojson(wkt):
    """WKT -> GeoJSON dict; collections are normalized like the other
    resolvers emit them (Multi* per part kind, point members dropped)."""
    geom = from_wkt(wkt)
    if geom.is_empty:
        return None
    if geom.geom_type == "GeometryCollection":
        polys, lines = [], []
        for g in geom.geoms:
            if g.geom_type == "Polygon":
                polys.append(g)
            elif g.geom_type == "MultiPolygon":
                polys.extend(g.geoms)
            elif g.geom_type == "LineString":
                lines.append(g)
            elif g.geom_type == "MultiLineString":
                lines.extend(g.geoms)
        parts = []
        if polys:
            parts.append(polys[0] if len(polys) == 1 else MultiPolygon(polys))
        if lines:
            parts.append(lines[0] if len(lines) == 1 else MultiLineString(lines))
        if not parts:
            return None
        geom = parts[0] if len(parts) == 1 else GeometryCollection(parts)
    return mapping(geom)


def resolve(db, otype, ids, snap):
    batch = BATCH[otype]
    n_batches = -(-len(ids) // batch)
    log(f"resolving {len(ids)} {otype}s via QLever "
        f"({n_batches} batches of {batch})")
    prog = Progress(len(ids), f"{otype}s", unit=otype + "s")
    found = failed = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        label = f"{otype} batch {i // batch + 1}/{n_batches}"
        wkts = fetch_wkts(otype, chunk, label)
        pause()
        tags = fetch_tags(otype, chunk, label) if wkts else {}
        pause()
        for oid, wkt in wkts.items():
            try:
                geom = to_geojson(wkt)
            except Exception:
                geom = None
            if geom is None:
                failed += 1
                continue
            db.execute(
                "INSERT OR REPLACE INTO geoms"
                "(otype, oid, status, source, geojson, tags, resolved_at)"
                " VALUES (?,?,'ok','qlever',?,?,?)",
                (
                    otype,
                    oid,
                    json.dumps(geom),
                    json.dumps(tags.get(oid, {}), ensure_ascii=False),
                    snap,
                ),
            )
            found += 1
        db.commit()
        prog.advance(len(chunk), extra=f"{found} resolved, {len(wkts)} in batch")
    prog.finish(extra=f"{found} resolved")
    left = len(ids) - found
    if left:
        log(f"{left} {otype}s not in QLever ({failed} bad geometries) "
            "- left for the API/Overpass steps.", 1)
    return found


def main():
    db = open_db()
    todo = {}
    try:
        snap = snapshot_date()
    except Exception as e:
        log(f"QLever unavailable ({e}) - leaving everything to API/Overpass.")
        return
    log(f"QLever snapshot: {snap}")
    with Phase("selecting unresolved objects old enough for the snapshot") as p:
        for otype in ("node", "way", "relation"):
            todo[otype] = load_todo(db, otype, snap)
        p.note(", ".join(f"{len(v)} {k}s" for k, v in todo.items()))
    if not any(todo.values()):
        log("Nothing old enough left for QLever.")
        STATS.summary()
        return
    total = 0
    for otype, ids in todo.items():
        if ids:
            total += resolve(db, otype, ids, snap)
    log(f"QLever resolved {total}/{sum(len(v) for v in todo.values())} objects.")
    STATS.summary()
    if SLEPT:
        log(f"politeness sleeps between batches: {fmt_dur(SLEPT)} "
            f"(QLEVER_SLEEP={QLEVER_SLEEP}s)", 1)


if __name__ == "__main__":
    run(main)
