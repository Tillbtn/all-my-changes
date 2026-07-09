"""Step 4: resolve remaining geometries via the Overpass API.

Everything the pbf pass could not answer lands here: objects outside the
extract region and objects edited after the pbf snapshot. Queries are
batched by id and throttled. Objects Overpass does not return no longer
exist; deleted nodes fall back to their last coordinates from my changeset.
"""

import json
import time

import osm2geojson
import requests

from common import OVERPASS_API, OVERPASS_SLEEP, USER_AGENT, open_db

# rotated round-robin when an instance is overloaded (429/504)
ENDPOINTS = [
    OVERPASS_API,
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
_endpoint = 0

BATCH = {"node": 500, "way": 250, "relation": 40}

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


def overpass(query, retries=8):
    global _endpoint
    for attempt in range(retries):
        url = ENDPOINTS[_endpoint]
        try:
            r = requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=300,
            )
        except requests.RequestException as e:
            print(f"  overpass error on {url} ({e}), rotating...")
            _endpoint = (_endpoint + 1) % len(ENDPOINTS)
            time.sleep(10)
            continue
        if r.status_code in (429, 504, 502, 503):
            print(f"  HTTP {r.status_code} from {url}, rotating mirror...")
            _endpoint = (_endpoint + 1) % len(ENDPOINTS)
            time.sleep(10 if attempt < len(ENDPOINTS) else 60)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("all overpass mirrors keep failing")


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
        print(f"  osm2geojson failed: {e}")
        return
    for feat in fc.get("features", []):
        props = feat.get("properties", {})
        if props.get("type") != "relation":
            continue
        yield props["id"], feat["geometry"], props.get("tags", {})


def store(db, otype, found, requested, now):
    for oid, geom, tags in found:
        db.execute(
            "INSERT OR REPLACE INTO geoms"
            "(otype, oid, status, source, geojson, tags, resolved_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (otype, oid, "ok", "overpass", json.dumps(geom),
             json.dumps(tags, ensure_ascii=False), now),
        )
    found_ids = {oid for oid, _, _ in found}
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
    db.commit()


def main():
    db = open_db()
    todo = {"node": [], "way": [], "relation": []}
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
    total = sum(len(v) for v in todo.values())
    if not total:
        print("Nothing left for Overpass.")
        return
    print(
        f"Overpass fallback for {total} objects "
        f"({len(todo['node'])} nodes, {len(todo['way'])} ways, "
        f"{len(todo['relation'])} relations)..."
    )

    done = 0
    for otype in ("node", "way", "relation"):
        for chunk in chunks(todo[otype], BATCH[otype]):
            ids = ",".join(map(str, chunk))
            query = f"[out:json][timeout:180];{otype}(id:{ids});out geom;"
            resp = overpass(query)
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if otype == "node":
                found = list(node_features(resp.get("elements", [])))
            elif otype == "way":
                found = list(way_features(resp.get("elements", [])))
            else:
                found = list(relation_features(resp))
            store(db, otype, found, set(chunk), now)
            done += len(chunk)
            print(f"  {done}/{total} ({len(found)}/{len(chunk)} found in batch)")
            time.sleep(OVERPASS_SLEEP)
    print("Overpass resolution done.")


if __name__ == "__main__":
    main()
