"""Step 3: resolve current geometries from the local Geofabrik pbf extract.

Works like `osmium getid -r` followed by an export, entirely in pyosmium:

  pass 1: my ways   -> collect node refs, write to temp file
  pass 2: my nodes + referenced nodes, write to temp file
  pass 3: geometry assembly on the small merged file (with areas)

Relations always go to Overpass (see load_targets), so only nodes and ways
are resolved here.

Only objects whose last edit is older than the pbf snapshot are resolved
here; anything newer (or not found, e.g. outside the extract) is left for
the Overpass fallback. Objects whose last action was my own delete are
marked deleted directly.
"""

import json
import time
from pathlib import Path

import osmium
from osmium.filter import IdFilter
from osmium.osm import NODE, WAY

from common import DATA_DIR, find_pbf, open_db, set_meta

def pbf_timestamp(pbf: Path) -> str:
    header = osmium.io.Reader(str(pbf)).header()
    ts = header.get("osmosis_replication_timestamp", "")
    if not ts:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pbf.stat().st_mtime))
    return ts


def mark_deleted(db):
    """Objects whose last action was my delete: no live geometry to fetch."""
    cur = db.execute(
        """
        INSERT OR IGNORE INTO geoms(otype, oid, status, source, geojson, tags, resolved_at)
        SELECT o.otype, o.oid, 'deleted', 'osc',
               CASE WHEN o.osc_lat IS NOT NULL THEN
                   json_object('type','Point','coordinates', json_array(o.osc_lon, o.osc_lat))
               END,
               '{}', o.last_edit
        FROM objects o
        LEFT JOIN geoms g ON g.otype=o.otype AND g.oid=o.oid
        WHERE g.oid IS NULL AND o.last_action='delete'
        """
    )
    db.commit()
    if cur.rowcount:
        print(f"Marked {cur.rowcount} objects as deleted (my own deletions).")


def load_targets(db, pbf_ts):
    """Objects needing geometry whose last edit predates the pbf snapshot.

    Relations are deliberately excluded: routes and boundaries often span
    beyond the extract and would be silently clipped at its edge. They are
    few, so Overpass resolves them completely instead.
    """
    targets = {"node": set(), "way": set()}
    rows = db.execute(
        """
        SELECT o.otype, o.oid FROM objects o
        LEFT JOIN geoms g ON g.otype=o.otype AND g.oid=o.oid
        WHERE g.oid IS NULL AND o.last_edit <= ? AND o.otype != 'relation'
        """,
        (pbf_ts,),
    )
    for otype, oid in rows:
        targets[otype].add(oid)
    return targets


def extract_small_file(pbf, targets, tmpdir):
    """Filter the big pbf down to my objects + everything they reference."""
    way_file = tmpdir / "ways.osm.pbf"
    node_file = tmpdir / "nodes.osm.pbf"
    small = tmpdir / "small.osm.pbf"
    for f in (way_file, node_file, small):
        f.unlink(missing_ok=True)

    node_refs = set()
    print(f"  scan 1/2: {len(targets['way'])} ways...")
    with osmium.SimpleWriter(str(way_file)) as w:
        if targets["way"]:
            fp = osmium.FileProcessor(str(pbf), WAY).with_filter(
                IdFilter(targets["way"])
            )
            for way in fp:
                node_refs.update(n.ref for n in way.nodes)
                w.add_way(way)

    node_ids = targets["node"] | node_refs
    print(f"  scan 2/2: {len(node_ids)} nodes...")
    with osmium.SimpleWriter(str(node_file)) as w:
        if node_ids:
            fp = osmium.FileProcessor(str(pbf), NODE).with_filter(IdFilter(node_ids))
            for node in fp:
                w.add_node(node)

    # merge in node-before-way order so location indexing works
    with osmium.SimpleWriter(str(small)) as w:
        for part in (node_file, way_file):
            for obj in osmium.FileProcessor(str(part)):
                w.add(obj)
    return small


def assemble_geometries(small, targets):
    """Build GeoJSON geometries for all target objects from the small file."""
    factory = osmium.geom.GeoJSONFactory()
    results = {}  # (otype, oid) -> (geojson_str, tags_dict)
    errors = 0

    fp = osmium.FileProcessor(str(small)).with_locations().with_areas()
    for obj in fp:
        try:
            if obj.is_node():
                if obj.id in targets["node"] and obj.location.valid():
                    results[("node", obj.id)] = (
                        factory.create_point(obj),
                        dict(obj.tags),
                    )
            elif obj.is_way():
                if len(obj.nodes) < 2:
                    continue
                if obj.id in targets["way"]:
                    results[("way", obj.id)] = (
                        factory.create_linestring(obj),
                        dict(obj.tags),
                    )
            elif obj.is_area():
                # closed ways with area tags become polygons instead
                oid = obj.orig_id()
                if obj.from_way() and oid in targets["way"]:
                    results[("way", oid)] = (
                        factory.create_multipolygon(obj),
                        dict(obj.tags),
                    )
        except (osmium.InvalidLocationError, RuntimeError):
            errors += 1
    if errors:
        print(f"  {errors} geometry errors (left for Overpass fallback)")
    return results


def main():
    db = open_db()
    pbf = find_pbf()
    mark_deleted(db)
    if not pbf:
        print("No .osm.pbf in data/ - skipping pbf resolution.")
        return
    pbf_ts = pbf_timestamp(pbf)
    targets = load_targets(db, pbf_ts)
    total = sum(len(v) for v in targets.values())
    print(f"pbf: {pbf.name} (snapshot {pbf_ts})")
    print(
        f"Resolving {total} objects "
        f"({len(targets['node'])} nodes, {len(targets['way'])} ways; "
        f"relations go to Overpass)..."
    )
    if not total:
        return

    tmpdir = DATA_DIR / "cache" / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    small = extract_small_file(pbf, targets, tmpdir)
    print("  assembling geometries...")
    results = assemble_geometries(small, targets)

    for (otype, oid), (geojson, tags) in results.items():
        db.execute(
            "INSERT OR REPLACE INTO geoms"
            "(otype, oid, status, source, geojson, tags, resolved_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (otype, oid, "ok", "pbf", geojson, json.dumps(tags, ensure_ascii=False), pbf_ts),
        )
    set_meta(db, "pbf_timestamp", pbf_ts)
    db.commit()
    print(f"Resolved {len(results)}/{total} from pbf. Rest goes to Overpass.")


if __name__ == "__main__":
    main()
