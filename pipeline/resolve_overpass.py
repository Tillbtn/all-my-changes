"""Step 4c: resolve remaining geometries via the Overpass API (last resort).

After pbf, QLever and API passes this is only relations QLever did not
know (edited after its snapshot, or deleted). Queries are batched by id
and throttled. Objects Overpass does not return no longer exist; deleted
nodes fall back to their last coordinates from the changeset dumps.

Overpass is the slowest and most fragile source, so every batch is logged
with the mirror it hit, its wall time and the response size; the step
summary adds up request time, throttling and mirror rotations. Overpass
`remark` lines (timeouts, memory limits) are surfaced instead of silently
producing an empty batch.
"""

import json
import time

import osm2geojson
import requests

from common import (
    OVERPASS_API,
    OVERPASS_SLEEP,
    USER_AGENT,
    HttpStats,
    Phase,
    Progress,
    fmt_dur,
    log,
    loads_lenient,
    open_db,
    run,
)

# rotated round-robin when an instance is overloaded (429/504)
ENDPOINTS = [
    OVERPASS_API,
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
_endpoint = 0

BATCH = {"node": 500, "way": 250, "relation": 40}

STATS = HttpStats("Overpass")
SLEPT = 0.0
ROTATIONS = 0

# tag keys that make a closed way an area (approximation of osmium's default)
AREA_KEYS = {
    "building", "building:part", "landuse", "natural", "leisure", "amenity",
    "shop", "tourism", "man_made", "place", "boundary", "area:highway",
    "craft", "office", "historic", "military", "ruins", "aeroway",
    "power", "sport", "waterway", "water", "indoor",
}


def is_area(tags, closed):
    if not closed:
        return False
    if tags.get("area") == "yes":
        return True
    if tags.get("area") == "no":
        return False
    return any(k in tags for k in AREA_KEYS)


def host(url):
    return url.split("/")[2]


def overpass(query, retries=8, label="query"):
    global _endpoint, ROTATIONS
    for attempt in range(retries):
        url = ENDPOINTS[_endpoint]
        t0 = time.monotonic()
        try:
            r = requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=300,
            )
        except requests.RequestException as e:
            STATS.retries += 1
            ROTATIONS += 1
            log(f"overpass error on {host(url)} after "
                f"{fmt_dur(time.monotonic() - t0)} ({e}), rotating...", 1)
            _endpoint = (_endpoint + 1) % len(ENDPOINTS)
            time.sleep(10)
            STATS.wait(10)
            continue
        dur = time.monotonic() - t0
        if r.status_code in (429, 504, 502, 503):
            STATS.retries += 1
            ROTATIONS += 1
            log(f"HTTP {r.status_code} from {host(url)} after {fmt_dur(dur)}, "
                "rotating mirror...", 1)
            _endpoint = (_endpoint + 1) % len(ENDPOINTS)
            wait = 10 if attempt < len(ENDPOINTS) else 60
            time.sleep(wait)
            STATS.wait(wait)
            continue
        r.raise_for_status()
        data = loads_lenient(r.content, label)
        STATS.record(f"{label} @{host(url)}", dur, nbytes=len(r.content),
                     rows=len(data.get("elements", [])))
        if data.get("remark"):
            log(f"overpass remark on {label}: {data['remark']}", 1)
        return data
    raise RuntimeError("all overpass mirrors keep failing")


def pause():
    global SLEPT
    time.sleep(OVERPASS_SLEEP)
    SLEPT += OVERPASS_SLEEP


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def node_features(elements):
    for el in elements:
        if el["type"] != "node":
            continue
        geom = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
        yield el["id"], geom, el.get("tags", {})


def way_features(elements):
    for el in elements:
        if el["type"] != "way" or not el.get("geometry"):
            continue
        coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
        tags = el.get("tags", {})
        closed = len(coords) >= 4 and coords[0] == coords[-1]
        if is_area(tags, closed):
            geom = {"type": "Polygon", "coordinates": [coords]}
        else:
            geom = {"type": "LineString", "coordinates": coords}
        yield el["id"], geom, tags


def relation_features(response):
    """Use osm2geojson for proper multipolygon ring assembly."""
    try:
        fc = osm2geojson.json2geojson(response)
    except Exception as e:
        log(f"osm2geojson failed: {e}", 1)
        return
    for feat in fc.get("features", []):
        props = feat.get("properties", {})
        if props.get("type") != "relation":
            continue
        yield props["id"], feat["geometry"], props.get("tags", {})


def store(db, otype, found, requested, now):
    """Store the batch; ids Overpass did not return are gone from the live
    database. Returns how many of those were recorded as deleted."""
    for oid, geom, tags in found:
        db.execute(
            "INSERT OR REPLACE INTO geoms"
            "(otype, oid, status, source, geojson, tags, resolved_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (otype, oid, "ok", "overpass", json.dumps(geom),
             json.dumps(tags, ensure_ascii=False), now),
        )
    found_ids = {oid for oid, _, _ in found}
    gone = 0
    for oid in requested - found_ids:
        # gone from the live db: deleted by someone else (or redacted)
        row = db.execute(
            "SELECT osc_lat, osc_lon FROM objects WHERE otype=? AND oid=?",
            (otype, oid),
        ).fetchone()
        geom = None
        if row and row[0] is not None:
            geom = json.dumps(
                {"type": "Point", "coordinates": [row[1], row[0]]}
            )
        db.execute(
            "INSERT OR REPLACE INTO geoms"
            "(otype, oid, status, source, geojson, tags, resolved_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (otype, oid, "deleted" if geom else "missing", "osc", geom, "{}", now),
        )
        gone += 1
    db.commit()
    return gone


def main():
    db = open_db()
    todo = {"node": [], "way": [], "relation": []}
    with Phase("selecting objects no earlier source could resolve") as p:
        rows = db.execute(
            """
            SELECT o.otype, o.oid FROM objects o
            LEFT JOIN geoms g ON g.otype=o.otype AND g.oid=o.oid
            WHERE g.oid IS NULL
            ORDER BY o.otype, o.oid
            """
        )
        for otype, oid in rows:
            todo[otype].append(oid)
        p.note(", ".join(f"{len(v)} {k}s" for k, v in todo.items()))
    total = sum(len(v) for v in todo.values())
    if not total:
        log("Nothing left for Overpass.")
        return
    n_batches = sum(-(-len(v) // BATCH[k]) for k, v in todo.items())
    log(f"Overpass fallback for {total} objects in {n_batches} batches "
        f"({OVERPASS_SLEEP}s between them, so at least "
        f"{fmt_dur(n_batches * OVERPASS_SLEEP)} of sleeping)")

    prog = Progress(total, "overpass", unit="objects")
    ok = gone = 0
    for otype in ("node", "way", "relation"):
        batch = BATCH[otype]
        for i, chunk in enumerate(chunks(todo[otype], batch), 1):
            ids = ",".join(map(str, chunk))
            query = f"[out:json][timeout:180];{otype}(id:{ids});out geom;"
            label = f"{otype} batch {i} ({len(chunk)} ids)"
            resp = overpass(query, label=label)
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if otype == "node":
                found = list(node_features(resp.get("elements", [])))
            elif otype == "way":
                found = list(way_features(resp.get("elements", [])))
            else:
                found = list(relation_features(resp))
            gone += store(db, otype, found, set(chunk), now)
            ok += len(found)
            prog.advance(len(chunk),
                         extra=f"{ok} resolved, {gone} gone "
                               f"(last batch {len(found)}/{len(chunk)})")
            pause()
    prog.finish(extra=f"{ok} resolved, {gone} recorded as deleted/missing")
    STATS.summary()
    if SLEPT:
        log(f"politeness sleeps between batches: {fmt_dur(SLEPT)} "
            f"(OVERPASS_SLEEP={OVERPASS_SLEEP}s)", 1)
    if ROTATIONS:
        log(f"mirror rotations after errors: {ROTATIONS}", 1)


if __name__ == "__main__":
    run(main)
