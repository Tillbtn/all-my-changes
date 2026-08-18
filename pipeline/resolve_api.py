"""Step 4b: resolve node and way geometries via the main OSM API multi-fetch.

`GET /api/0.6/nodes?nodes=1,2,3` (and the ways equivalent) is purpose-built
for bulk id lookups and much cheaper for everyone than batch queries against
the shared Overpass instances. Deleted elements come back with
visible=false, so deletions by other users are detected in the same pass.

Ways need a second round of node multi-fetches for their coordinates.
Relations are left to resolve_qlever.py / resolve_overpass.py, which
return their member geometry fully assembled.

Every multi-fetch is timed via common.api_get, so the step summary separates
request time from the time lost to the API's rolling bandwidth quota (the
429/509 waits) - on a big backlog that throttling usually dominates.
"""

import json
import time

from common import (
    API_SLEEP,
    OSM_API,
    HttpStats,
    Phase,
    Progress,
    api_get,
    fmt_dur,
    log,
    loads_lenient,
    open_db,
    run,
)
from resolve_overpass import is_area

BATCH = 300

STATS = HttpStats("OSM API")
SLEPT = 0.0


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def pause():
    global SLEPT
    time.sleep(API_SLEEP)
    SLEPT += API_SLEEP


def multifetch(otype, ids, label="multifetch"):
    """Fetch current versions; recursively bisect on 404 (unknown id)."""
    idstr = ",".join(map(str, ids))
    r = api_get(f"{OSM_API}/{otype}s.json", params={f"{otype}s": idstr},
                stats=STATS, label=f"{label} ({len(ids)} {otype}s)")
    if r.status_code == 404 and len(ids) > 1:
        mid = len(ids) // 2
        return multifetch(otype, ids[:mid], label) + multifetch(otype, ids[mid:], label)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    pause()
    return loads_lenient(r.content, label).get("elements", [])


def store_ok(db, otype, oid, geom, tags, now):
    db.execute(
        "INSERT OR REPLACE INTO geoms"
        "(otype, oid, status, source, geojson, tags, resolved_at)"
        " VALUES (?,?,'ok','api',?,?,?)",
        (otype, oid, json.dumps(geom), json.dumps(tags, ensure_ascii=False), now),
    )


def store_gone(db, otype, oid, now):
    """Deleted upstream: fall back to last known coordinates from my osc."""
    row = db.execute(
        "SELECT osc_lat, osc_lon FROM objects WHERE otype=? AND oid=?",
        (otype, oid),
    ).fetchone()
    geom = None
    if row and row[0] is not None:
        geom = json.dumps({"type": "Point", "coordinates": [row[1], row[0]]})
    db.execute(
        "INSERT OR REPLACE INTO geoms"
        "(otype, oid, status, source, geojson, tags, resolved_at)"
        " VALUES (?,?,?,'osc',?,'{}',?)",
        (otype, oid, "deleted" if geom else "missing", geom, now),
    )


def load_todo(db, otype):
    return [
        oid
        for (oid,) in db.execute(
            """
            SELECT o.oid FROM objects o
            LEFT JOIN geoms g ON g.otype=o.otype AND g.oid=o.oid
            WHERE g.oid IS NULL AND o.otype=? ORDER BY o.oid
            """,
            (otype,),
        )
    ]


def resolve_nodes(db, node_ids):
    n_batches = -(-len(node_ids) // BATCH)
    log(f"resolving {len(node_ids)} nodes via API multi-fetch "
        f"({n_batches} batches of {BATCH})")
    prog = Progress(len(node_ids), "nodes", unit="nodes")
    ok = gone = 0
    for i, chunk in enumerate(chunks(node_ids, BATCH), 1):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        seen = set()
        for el in multifetch("node", chunk, f"node batch {i}/{n_batches}"):
            seen.add(el["id"])
            if el.get("visible", True) and "lat" in el:
                geom = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
                store_ok(db, "node", el["id"], geom, el.get("tags", {}), now)
                ok += 1
            else:
                store_gone(db, "node", el["id"], now)
                gone += 1
        for oid in set(chunk) - seen:
            store_gone(db, "node", oid, now)
            gone += 1
        db.commit()
        prog.advance(len(chunk), extra=f"{ok} live, {gone} deleted/unknown")
    prog.finish(extra=f"{ok} live, {gone} deleted/unknown")


def resolve_ways(db, way_ids):
    n_batches = -(-len(way_ids) // BATCH)
    log(f"resolving {len(way_ids)} ways via API multi-fetch "
        f"({n_batches} batches of {BATCH}, plus their node coordinates)")
    ways, needed_nodes = {}, set()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prog = Progress(len(way_ids), "ways", unit="ways")
    gone = 0
    for i, chunk in enumerate(chunks(way_ids, BATCH), 1):
        seen = set()
        for el in multifetch("way", chunk, f"way batch {i}/{n_batches}"):
            seen.add(el["id"])
            if el.get("visible", True) and el.get("nodes"):
                ways[el["id"]] = (el["nodes"], el.get("tags", {}))
                needed_nodes.update(el["nodes"])
            else:
                store_gone(db, "way", el["id"], now)
                gone += 1
        for oid in set(chunk) - seen:
            store_gone(db, "way", oid, now)
            gone += 1
        db.commit()
        prog.advance(len(chunk),
                     extra=f"{len(needed_nodes)} distinct nodes referenced")
    prog.finish(extra=f"{len(ways)} live, {gone} deleted/unknown, "
                      f"{len(needed_nodes)} node coordinates needed")

    coords = {}
    node_list = sorted(needed_nodes)
    n_batches = -(-len(node_list) // BATCH)
    log(f"fetching {len(node_list)} referenced node coordinates "
        f"({n_batches} batches)", 1)
    prog = Progress(len(node_list), "way nodes", unit="nodes")
    for i, chunk in enumerate(chunks(node_list, BATCH), 1):
        for el in multifetch("node", chunk, f"way-node batch {i}/{n_batches}"):
            if "lat" in el:
                coords[el["id"]] = [el["lon"], el["lat"]]
        prog.advance(len(chunk), extra=f"{len(coords)} located")
    prog.finish(extra=f"{len(coords)} located")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with Phase("assembling way geometries", indent=1) as p:
        incomplete = 0
        for wid, (nds, tags) in ways.items():
            pts = [coords[n] for n in nds if n in coords]
            if len(pts) < 2:
                incomplete += 1
                store_gone(db, "way", wid, now)
                continue
            closed = len(pts) >= 4 and pts[0] == pts[-1]
            if is_area(tags, closed):
                geom = {"type": "Polygon", "coordinates": [pts]}
            else:
                geom = {"type": "LineString", "coordinates": pts}
            store_ok(db, "way", wid, geom, tags, now)
        db.commit()
        p.note(f"{len(ways) - incomplete} ways")
        if incomplete:
            p.note(f"{incomplete} with fewer than 2 resolvable nodes")


def main():
    db = open_db()
    with Phase("selecting nodes/ways still without geometry") as p:
        node_ids = load_todo(db, "node")
        way_ids = load_todo(db, "way")
        p.note(f"{len(node_ids)} nodes, {len(way_ids)} ways")
    if not node_ids and not way_ids:
        log("No nodes/ways left for the API.")
        return
    if node_ids:
        resolve_nodes(db, node_ids)
    if way_ids:
        resolve_ways(db, way_ids)
    STATS.summary()
    if SLEPT:
        log(f"politeness sleeps between requests: {fmt_dur(SLEPT)} "
            f"(API_SLEEP={API_SLEEP}s)", 1)


if __name__ == "__main__":
    run(main)
