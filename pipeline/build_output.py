"""Step 5: emit web/data/*.geojson + meta.json for the MapLibre frontend.

Features are split into files by geometry kind so the map can style and
lazy-load them independently:

  polygons.geojson   areas (buildings, landuse, ...)
  lines.geojson      ways
  relations.geojson  relations with their full member geometry
                     (routes, boundaries, multipolygons)
  points.geojson     tagged nodes (POIs, ...)
  vertices.geojson   untagged nodes (way vertices I moved/created)
  deleted.geojson    objects that no longer exist (last known position)

Feature properties (kept short, the files carry many features):
  t   n|w|r                          k   primary tag "key=value"
  id  osm id                         n   name
  f/l first/last edit (YYYY-MM-DD)   y   year of last edit
  c   my edit count                  b   1 = building
  d   1 = deleted                    op  1 = outline modeled by 3D parts

Buildings additionally carry Simple-3D-Buildings keys for the roof
renderer - see building_props().
"""

import json
import time

from common import WEB_DATA_DIR, OSM_USER, get_meta, open_db

PRIMARY_KEYS = (
    "building", "highway", "amenity", "shop", "leisure", "tourism", "craft",
    "office", "natural", "landuse", "waterway", "railway", "power",
    "man_made", "barrier", "boundary", "place", "public_transport", "route",
    "addr:housenumber", "entrance", "emergency", "historic", "type",
)


def parse_len(value):
    """Parse a length like '12', '12.5 m', '12,5'."""
    try:
        return float(str(value).split()[0].replace("m", "").replace(",", "."))
    except (ValueError, IndexError):
        return None


def building_props(tags):
    """Simple-3D-Buildings properties for the 3D renderer.

    h  height in m (hl=1: derived from levels, excludes the roof)
    rs/rh/rl/rd/ro  roof shape/height/levels/direction/orientation
    mh min_height   bp building:part   bc/rc building/roof colour
    """
    p = {}
    h = next((parse_len(tags[k]) for k in ("height", "building:height")
              if k in tags), None)
    if h is not None:
        p["h"] = round(h, 1)
    else:
        lv = next((parse_len(tags[k]) for k in ("building:levels", "levels")
                   if k in tags), None)
        if lv is not None:
            p["h"] = round(lv * 3.0, 1)
            p["hl"] = 1
    if tags.get("roof:shape"):
        p["rs"] = tags["roof:shape"][:24]
    rh = parse_len(tags.get("roof:height")) if "roof:height" in tags else None
    if rh is not None:
        p["rh"] = round(rh, 1)
    rl = parse_len(tags.get("roof:levels")) if "roof:levels" in tags else None
    if rl is not None:
        p["rl"] = rl
    rd = parse_len(tags.get("roof:direction")) if "roof:direction" in tags else None
    if rd is not None:
        p["rd"] = rd
    if tags.get("roof:orientation") in ("along", "across"):
        p["ro"] = tags["roof:orientation"]
    mh = parse_len(tags.get("min_height")) if "min_height" in tags else None
    if mh is None and "building:min_level" in tags:
        lv = parse_len(tags["building:min_level"])
        mh = lv * 3.0 if lv is not None else None
    if mh:
        p["mh"] = round(mh, 1)
    if tags.get("building:part", "no") != "no":
        p["bp"] = 1
    for pk, tk in (("bc", "building:colour"), ("rc", "roof:colour")):
        if tags.get(tk):
            p[pk] = tags[tk][:24]
    return p


def primary_tag(tags):
    for k in PRIMARY_KEYS:
        if k in tags:
            return f"{k}={tags[k]}"
    for k, v in tags.items():
        if k not in ("name", "source") and not k.startswith("addr:"):
            return f"{k}={v}"
    return next((f"{k}={v}" for k, v in tags.items()), None)


def make_props(otype, oid, row, tags):
    first_edit, last_edit, edit_count, status = row
    props = {
        "t": otype[0],
        "id": oid,
        "f": (first_edit or "")[:10],
        "l": (last_edit or "")[:10],
        "y": int(last_edit[:4]) if last_edit else None,
        "c": edit_count,
    }
    if tags.get("name"):
        props["n"] = tags["name"]
    k = primary_tag(tags)
    if k:
        props["k"] = k[:80]
    if tags.get("building", "no") != "no" or tags.get("building:part", "no") != "no":
        props["b"] = 1
        props.update(building_props(tags))
    if status == "deleted":
        props["d"] = 1
    return {k: v for k, v in props.items() if v is not None}


def find_part_outlines(db):
    """Building outlines whose interior contains my building:part objects.

    Per Simple 3D Buildings, a building modeled with parts gets its volume
    from the parts - the outline is just the footprint. Flag such outlines
    (prop op=1) so the 3D renderer can skip extruding them.
    """
    from shapely.geometry import shape

    part_points = []
    rows = db.execute(
        "SELECT geojson, tags FROM geoms WHERE status='ok'"
        " AND tags LIKE '%building:part%'"
    )
    for gj, tj in rows:
        tags = json.loads(tj)
        if tags.get("building:part", "no") == "no":
            continue
        g = json.loads(gj)
        if g["type"] in ("Polygon", "MultiPolygon"):
            p = shape(g).representative_point()
            part_points.append((p.x, p.y, p))
    if not part_points:
        return set()

    flagged = set()
    rows = db.execute(
        "SELECT otype, oid, geojson, tags FROM geoms WHERE status='ok'"
        " AND tags LIKE '%building%'"
    )
    for otype, oid, gj, tj in rows:
        tags = json.loads(tj)
        if tags.get("building", "no") == "no" or tags.get("building:part", "no") != "no":
            continue
        g = json.loads(gj)
        if g["type"] not in ("Polygon", "MultiPolygon"):
            continue
        poly = shape(g)
        minx, miny, maxx, maxy = poly.bounds
        for x, y, p in part_points:
            if minx <= x <= maxx and miny <= y <= maxy and poly.contains(p):
                flagged.add((otype, oid))
                break
    return flagged


class StreamWriter:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "w")
        self.f.write('{"type":"FeatureCollection","features":[\n')
        self.count = 0
        self.bbox = [180.0, 90.0, -180.0, -90.0]

    def add(self, geom, props):
        feat = {"type": "Feature", "geometry": geom, "properties": props}
        if self.count:
            self.f.write(",\n")
        self.f.write(json.dumps(feat, ensure_ascii=False, separators=(",", ":")))
        self.count += 1
        self._grow_bbox(geom["coordinates"])

    def _grow_bbox(self, coords):
        if isinstance(coords[0], (int, float)):
            self.bbox[0] = min(self.bbox[0], coords[0])
            self.bbox[1] = min(self.bbox[1], coords[1])
            self.bbox[2] = max(self.bbox[2], coords[0])
            self.bbox[3] = max(self.bbox[3], coords[1])
        else:
            for c in coords:
                self._grow_bbox(c)

    def close(self):
        self.f.write("\n]}")
        self.f.close()


def main():
    db = open_db()
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    writers = {
        name: StreamWriter(WEB_DATA_DIR / f"{name}.geojson")
        for name in ("polygons", "lines", "relations", "points", "vertices", "deleted")
    }
    counts = {"missing": 0}
    part_outlines = find_part_outlines(db)

    rows = db.execute(
        """
        SELECT o.otype, o.oid, o.first_edit, o.last_edit, o.edit_count,
               g.status, g.geojson, g.tags
        FROM objects o
        JOIN geoms g ON g.otype=o.otype AND g.oid=o.oid
        """
    )
    for otype, oid, first, last, count, status, geojson, tags_json in rows:
        if not geojson:
            counts["missing"] += 1
            continue
        geom = json.loads(geojson)
        tags = json.loads(tags_json) if tags_json else {}
        props = make_props(otype, oid, (first, last, count, status), tags)
        if (otype, oid) in part_outlines:
            props["op"] = 1

        if status == "deleted":
            writers["deleted"].add(geom, props)
        elif otype == "relation":
            writers["relations"].add(geom, props)
        elif geom["type"] in ("Polygon", "MultiPolygon"):
            writers["polygons"].add(geom, props)
        elif geom["type"] in ("LineString", "MultiLineString"):
            writers["lines"].add(geom, props)
        elif geom["type"] in ("Point", "MultiPoint"):
            writers["points" if tags else "vertices"].add(geom, props)

    bbox = [180.0, 90.0, -180.0, -90.0]
    for name, w in writers.items():
        w.close()
        counts[name] = w.count
        if w.count and name != "deleted":
            bbox = [
                min(bbox[0], w.bbox[0]), min(bbox[1], w.bbox[1]),
                max(bbox[2], w.bbox[2]), max(bbox[3], w.bbox[3]),
            ]
        print(f"  {name}: {w.count} features")

    total_objects = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    n_changesets = db.execute(
        "SELECT COUNT(*), MAX(closed_at) FROM changesets WHERE parsed=1"
    ).fetchone()
    meta = {
        "user": OSM_USER,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "changesets": n_changesets[0],
        "latest_changeset": n_changesets[1],
        "objects": total_objects,
        "counts": counts,
        "bbox": bbox,
        "pbf_timestamp": get_meta(db, "pbf_timestamp"),
    }
    with open(WEB_DATA_DIR / "meta.json", "w") as f:
        json.dump(meta, f, indent=1)
    print(f"Wrote {WEB_DATA_DIR}/meta.json: {total_objects} objects, "
          f"{counts['missing']} without geometry.")


if __name__ == "__main__":
    main()
