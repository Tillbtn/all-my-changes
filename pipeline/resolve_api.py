"""Step 4a: resolve node and way geometries via the main OSM API multi-fetch.

`GET /api/0.6/nodes?nodes=1,2,3` (and the ways equivalent) is purpose-built
for bulk id lookups and much cheaper for everyone than batch queries against
the shared Overpass instances. Deleted elements come back with
visible=false, so deletions by other users are detected in the same pass.

Ways need a second round of node multi-fetches for their coordinates.
Relations are left to resolve_overpass.py, which assembles their member
geometry in one query.
"""

import json
import time

from common import API_SLEEP, OSM_API, api_get, open_db
from resolve_overpass import is_area

BATCH = 300


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def multifetch(otype, ids):
    """Fetch current versions; recursively bisect on 404 (unknown id)."""
    idstr = ",".join(map(str, ids))
    r = api_get(f"{OSM_API}/{otype}s.json", params={f"{otype}s": idstr})
    if r.status_code == 404 and len(ids) > 1:
        mid = len(ids) // 2
        return multifetch(otype, ids[:mid]) + multifetch(otype, ids[mid:])
    if r.status_code == 404:
        return []
    r.raise_for_status()
    time.sleep(API_SLEEP)
    return r.json().get("elements", [])


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
    print(f"Resolving {len(node_ids)} nodes via API multi-fetch...")
    done = 0
    for chunk in chunks(node_ids, BATCH):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        seen = set()
        for el in multifetch("node", chunk):
            seen.add(el["id"])
            if el.get("visible", True) and "lat" in el:
                geom = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
                store_ok(db, "node", el["id"], geom, el.get("tags", {}), now)
            else:
                store_gone(db, "node", el["id"], now)
        for oid in set(chunk) - seen:
            store_gone(db, "node", oid, now)
        db.commit()
        done += len(chunk)
        if done % 3000 < BATCH:
            print(f"  {done}/{len(node_ids)}")


def resolve_ways(db, way_ids):
    print(f"Resolving {len(way_ids)} ways via API multi-fetch...")
    ways, needed_nodes = {}, set()
    done = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for chunk in chunks(way_ids, BATCH):
        seen = set()
        for el in multifetch("way", chunk):
            seen.add(el["id"])
            if el.get("visible", True) and el.get("nodes"):
                ways[el["id"]] = (el["nodes"], el.get("tags", {}))
                needed_nodes.update(el["nodes"])
            else:
                store_gone(db, "way", el["id"], now)
        for oid in set(chunk) - seen:
            store_gone(db, "way", oid, now)
        db.commit()
        done += len(chunk)
        print(f"  {done}/{len(way_ids)} ways fetched")

    coords = {}
    node_list = sorted(needed_nodes)
    print(f"  fetching {len(node_list)} referenced node coordinates...")
    done = 0
    for chunk in chunks(node_list, BATCH):
        for el in multifetch("node", chunk):
            if "lat" in el:
                coords[el["id"]] = [el["lon"], el["lat"]]
        done += len(chunk)
        if done % 6000 < BATCH:
            print(f"  {done}/{len(node_list)} nodes")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
    if incomplete:
        print(f"  {incomplete} ways had fewer than 2 resolvable nodes")


def main():
    db = open_db()
    node_ids = load_todo(db, "node")
    way_ids = load_todo(db, "way")
    if not node_ids and not way_ids:
        print("No nodes/ways left for the API.")
        return
    if node_ids:
        resolve_nodes(db, node_ids)
    if way_ids:
        resolve_ways(db, way_ids)
    print("API resolution done.")


if __name__ == "__main__":
    main()
