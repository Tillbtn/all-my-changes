"""Step 5: emit web/data/<user>/*.geojson(.gz) + meta.json for the frontend.

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
  l   last edit (YYYY-MM-DD)         c   my edit count
  f   first edit, only when it       b   1 = building
      differs from l                 d   1 = deleted
                                     op  1 = outline modeled by 3D parts

Buildings additionally carry Simple-3D-Buildings keys for the roof
renderer - see building_props().

Output size matters: GitHub Pages rejects single files over 100 MB and asks
sites to stay under 1 GB, and a prolific mapper's polygon layer alone can
pass both. Three knobs keep it down, all lossless enough that the map looks
identical:

  AMC_GZIP=1        write <name>.geojson.gz instead of plain (default; ~4x
                    on areas, ~12x on vertices). The frontend reads
                    meta.json's "gzip" flag and inflates in the browser.
  AMC_PRECISION=6   coordinate decimals; 6 is ~11 cm, well below anything
                    visible on a map, and cuts ~7% of the raw bytes.
  AMC_SIMPLIFY=0    Douglas-Peucker tolerance in degrees, applied before
                    rounding. 0 = off; 3e-6 (~0.3 m) buys another ~20% on
                    traced outlines but does change geometry.
  AMC_VERTEX_CAP    untagged way-vertices are sampled down to this many.
"""

import json
import os
import time

from common import (
    WEB_DATA_DIR,
    WEB_DATA_ROOT,
    OSM_USER,
    Phase,
    fmt_bytes,
    get_meta,
    log,
    open_db,
    open_text_out,
    run,
)

# untagged way-vertices above this count are evenly sampled down: a
# multi-million-point geojson is unusable in the browser however well it
# compresses (the layer is off by default anyway)
VERTEX_CAP = int(os.environ.get("AMC_VERTEX_CAP", "300000"))

GZIP = os.environ.get("AMC_GZIP", "1") != "0"
PRECISION = int(os.environ.get("AMC_PRECISION", "6"))
SIMPLIFY = float(os.environ.get("AMC_SIMPLIFY", "0"))

# GitHub's hard per-file push limit, its warning threshold, and the size
# GitHub Pages asks sites to stay below
FILE_LIMIT = 100 * 1024**2
FILE_WARN = 50 * 1024**2
SITE_LIMIT = 1024**3

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
    """Short property set. `f` is omitted when it equals `l` and the year is
    left out entirely - the frontend derives both from `l`, and on a
    single-edit-heavy dataset those two keys alone are megabytes."""
    first_edit, last_edit, edit_count, status = row
    first, last = (first_edit or "")[:10], (last_edit or "")[:10]
    props = {"t": otype[0], "id": oid, "l": last, "c": edit_count}
    if first and first != last:
        props["f"] = first
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


def round_coords(coords, nd=PRECISION):
    """Round every coordinate to nd decimals.

    Rounding can collapse neighbouring points onto each other; those
    zero-length segments render as nothing and upset the roof triangulation,
    so they are dropped - unless that would leave a ring with fewer than its
    four points, in which case the geometry stays as it is.
    """
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], nd), round(coords[1], nd)]
    if isinstance(coords[0], (list, tuple)) and isinstance(
        coords[0][0], (int, float)
    ):
        pts = [[round(p[0], nd), round(p[1], nd)] for p in coords]
        out = [pts[0]]
        for p in pts[1:]:
            if p != out[-1]:
                out.append(p)
        closed = pts[0] == pts[-1]
        if closed and out[-1] != out[0]:
            out.append(out[0])
        return out if len(out) >= (4 if closed else 2) else pts
    return [round_coords(c, nd) for c in coords]


def shrink(geom):
    """Return a smaller copy of one geometry: optionally simplified, always
    rounded. QLever hands out GeometryCollections for mixed relations, so
    recurse into their parts."""
    if geom.get("type") == "GeometryCollection":
        return dict(geom, geometries=[shrink(g) for g in geom.get("geometries", [])])
    if SIMPLIFY > 0 and geom["type"] != "Point":
        from shapely.geometry import mapping, shape

        try:
            simplified = shape(geom).simplify(SIMPLIFY, preserve_topology=True)
            if not simplified.is_empty:
                geom = mapping(simplified)
        except Exception:
            pass
    if PRECISION >= 0 and geom.get("coordinates"):
        geom = dict(geom, coordinates=round_coords(geom["coordinates"]))
    return geom


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
    """Streams one FeatureCollection out, plain or gzipped, and remembers
    both what it wrote and what it cost on disk."""

    def __init__(self, base_path, gzipped=GZIP):
        self.base = base_path
        self.f, self.path = open_text_out(base_path, gzipped)
        self.f.write('{"type":"FeatureCollection","features":[\n')
        self.count = 0
        self.raw = 0
        self.size = 0
        self.bbox = [180.0, 90.0, -180.0, -90.0]

    def add(self, geom, props):
        feat = {"type": "Feature", "geometry": geom, "properties": props}
        text = json.dumps(feat, ensure_ascii=False, separators=(",", ":"))
        if self.count:
            self.f.write(",\n")
            self.raw += 2
        self.f.write(text)
        self.raw += len(text)
        self.count += 1
        self._grow(geom)

    def _grow(self, geom):
        """Widen the bbox by one geometry (GeometryCollections included)."""
        if geom.get("type") == "GeometryCollection":
            for g in geom.get("geometries", []):
                self._grow(g)
        elif geom.get("coordinates"):
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
        self.size = self.path.stat().st_size
        # a switch between plain and gzipped output would otherwise leave the
        # old variant behind and have deploy.sh publish both
        stale = self.base if self.path != self.base else self.base.with_suffix(
            self.base.suffix + ".gz"
        )
        if stale.exists():
            stale.unlink()

    def report(self):
        line = (f"{self.path.name}: {self.count} features, "
                f"{fmt_bytes(self.size)}")
        if self.path != self.base and self.raw and self.size:
            line += (f" gzipped from {fmt_bytes(self.raw)} "
                     f"({self.raw / self.size:.1f}x)")
        log(line, 1)


def main():
    db = open_db()
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    log(f"building web data for {OSM_USER} "
        f"(gzip={'on' if GZIP else 'off'}, {PRECISION} decimals, "
        f"simplify={SIMPLIFY or 'off'})")

    writers = {
        name: StreamWriter(WEB_DATA_DIR / f"{name}.geojson")
        for name in ("polygons", "lines", "relations", "points", "vertices", "deleted")
    }
    counts = {"missing": 0}
    with Phase("finding outlines modeled by building parts", indent=1) as p:
        part_outlines = find_part_outlines(db)
        p.note(f"{len(part_outlines)} flagged")

    n_vertices = db.execute(
        "SELECT COUNT(*) FROM geoms WHERE otype='node' AND status='ok'"
        " AND (tags='{}' OR tags IS NULL)"
    ).fetchone()[0]
    stride = max(1, -(-n_vertices // VERTEX_CAP))
    if stride > 1:
        log(f"sampling vertices: keeping 1 in {stride} of {n_vertices}", 1)
    vertex_seen = 0

    with Phase("writing feature files", indent=1) as phase:
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
                target = "deleted"
            elif otype == "relation":
                target = "relations"
            elif geom["type"] in ("Polygon", "MultiPolygon"):
                target = "polygons"
            elif geom["type"] in ("LineString", "MultiLineString"):
                target = "lines"
            elif geom["type"] in ("Point", "MultiPoint"):
                if tags:
                    target = "points"
                else:
                    vertex_seen += 1
                    if (vertex_seen - 1) % stride != 0:
                        continue
                    target = "vertices"
            elif geom["type"] == "GeometryCollection":
                target = "relations"
            else:
                continue
            writers[target].add(shrink(geom), props)
        phase.note(f"{sum(w.count for w in writers.values())} features")

    bbox = [180.0, 90.0, -180.0, -90.0]
    total_size = total_raw = 0
    files = {}
    for name, w in writers.items():
        w.close()
        counts[name] = w.count
        total_size += w.size
        total_raw += w.raw
        files[name] = {"features": w.count, "bytes": w.size}
        if w.count and name != "deleted":
            bbox = [
                min(bbox[0], w.bbox[0]), min(bbox[1], w.bbox[1]),
                max(bbox[2], w.bbox[2]), max(bbox[3], w.bbox[3]),
            ]
        w.report()

    total_objects = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    n_changesets = db.execute(
        "SELECT COUNT(*), MAX(closed_at) FROM changesets WHERE parsed=1"
    ).fetchone()
    if stride > 1:
        counts["vertices_total"] = n_vertices
    meta = {
        "user": OSM_USER,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "changesets": n_changesets[0],
        "latest_changeset": n_changesets[1],
        "objects": total_objects,
        "counts": counts,
        "bbox": bbox,
        "pbf_timestamp": get_meta(db, "pbf_timestamp"),
        "mode": get_meta(db, "mode", "full"),
        "gzip": GZIP,
        "precision": PRECISION,
        "files": files,
    }
    with open(WEB_DATA_DIR / "meta.json", "w") as f:
        json.dump(meta, f, indent=1)
    log(f"{WEB_DATA_DIR.name}: {total_objects} objects, "
        f"{counts['missing']} without geometry, "
        f"{fmt_bytes(total_size)} published"
        + (f" (from {fmt_bytes(total_raw)} of JSON, "
           f"{total_raw / total_size:.1f}x)" if GZIP and total_size else ""))
    check_limits(writers)
    write_user_index()


def check_limits(writers):
    """Warn before a push or a Pages deploy runs into GitHub's limits."""
    for w in writers.values():
        if w.size > FILE_LIMIT:
            log(f"ERROR: {w.path.name} is {fmt_bytes(w.size)} - git refuses "
                f"files over {fmt_bytes(FILE_LIMIT)}. Raise AMC_SIMPLIFY or "
                f"lower AMC_VERTEX_CAP.", 1)
        elif w.size > FILE_WARN:
            log(f"warning: {w.path.name} is {fmt_bytes(w.size)}; GitHub warns "
                f"above {fmt_bytes(FILE_WARN)} per file.", 1)
    site = sum(f.stat().st_size for f in WEB_DATA_ROOT.rglob("*") if f.is_file())
    note = f"web/data total: {fmt_bytes(site)}"
    if site > SITE_LIMIT:
        log(f"ERROR: {note} - over the {fmt_bytes(SITE_LIMIT)} GitHub Pages "
            f"limit.", 1)
    elif site > 0.8 * SITE_LIMIT:
        log(f"warning: {note}, approaching the {fmt_bytes(SITE_LIMIT)} "
            f"GitHub Pages limit.", 1)
    else:
        log(note, 1)


def write_user_index():
    """web/data/users.json: one entry per user directory, feeds the
    frontend's user switcher."""
    users = []
    for mf in sorted(WEB_DATA_ROOT.glob("*/meta.json")):
        try:
            m = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue
        users.append({
            "dir": mf.parent.name,
            "user": m.get("user", mf.parent.name),
            "objects": m.get("objects", 0),
            "updated": (m.get("generated_at") or "")[:10],
        })
    users.sort(key=lambda u: str(u["user"]).lower())
    with open(WEB_DATA_ROOT / "users.json", "w") as f:
        json.dump(users, f, ensure_ascii=False, indent=1)
    log(f"users.json: {len(users)} users", 1)


if __name__ == "__main__":
    run(main)
