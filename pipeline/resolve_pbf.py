"""Step 3: resolve current geometries from the local Geofabrik pbf extract.

Works like `osmium getid -r` followed by an export, entirely in pyosmium:

  pass 1: my ways   -> collect node refs, write to temp file
  pass 2: my nodes + referenced nodes, write to temp file
  pass 3: geometry assembly on the small merged file (with areas)

Relations always go to QLever/Overpass (see load_targets), so only nodes
and ways are resolved here.

Only objects whose last edit is older than the pbf snapshot are resolved
here; anything newer (or not found, e.g. outside the extract) is left for
the Overpass fallback. Objects whose last action was my own delete are
marked deleted directly.

Each of the three passes is timed and reported with its throughput over the
pbf, since a full scan of a country extract is minutes of pure I/O and is
usually the single longest part of an incremental run.
"""

import json
import time
from pathlib import Path

import osmium
from osmium.filter import IdFilter
from osmium.osm import NODE, WAY

from common import (
    DATA_DIR,
    Phase,
    find_pbf,
    fmt_bytes,
    fmt_dur,
    fmt_rate,
    log,
    open_db,
    run,
    set_meta,
)

HEADER_CACHE = DATA_DIR / "cache" / "pbf_header.json"


def read_pbf_timestamp(pbf: Path) -> str:
    """Snapshot date straight from the pbf header."""
    reader = osmium.io.Reader(str(pbf))
    try:
        ts = reader.header().get("osmosis_replication_timestamp", "")
    finally:
        reader.close()
    if not ts:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pbf.stat().st_mtime))
    return ts


def pbf_timestamp(pbf: Path) -> str:
    """Cached snapshot date for the extract.

    Opening the reader costs anywhere between nothing and a minute and a half
    on a 4.5 GB extract depending on how much of it the page cache still
    holds, and the answer is a property of the file rather than of the user
    being processed - so it is cached against the file's size and mtime and
    shared by every user in an update run.
    """
    st = pbf.stat()
    key = f"{pbf.name}:{st.st_size}:{int(st.st_mtime)}"
    try:
        cached = json.loads(HEADER_CACHE.read_text())
        if cached.get("key") == key:
            return cached["timestamp"]
    except (OSError, ValueError, KeyError):
        pass
    with Phase(f"reading the pbf header ({fmt_bytes(st.st_size)}, "
               "cached afterwards)", indent=1) as p:
        ts = read_pbf_timestamp(pbf)
        p.note(f"snapshot {ts}")
    HEADER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    HEADER_CACHE.write_text(json.dumps({"key": key, "timestamp": ts}))
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
        log(f"marked {cur.rowcount} objects as deleted (my own deletions)")


def load_targets(db, pbf_ts):
    """Objects needing geometry whose last edit predates the pbf snapshot.

    Relations are deliberately excluded: routes and boundaries often span
    beyond the extract and would be silently clipped at its edge. They are
    few, so QLever (or Overpass as last resort) resolves them completely.
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


def scan_note(phase, pbf_size, hits, what):
    """Annotate a full-pbf pass with what it found and how fast it read."""
    seconds = time.monotonic() - phase.t0
    phase.note(f"{hits} {what}")
    phase.note(f"{fmt_rate(pbf_size / 1e6, seconds, ' MB/s').strip()} "
               f"over {fmt_bytes(pbf_size)}")


def extract_small_file(pbf, targets, tmpdir):
    """Filter the big pbf down to my objects + everything they reference."""
    way_file = tmpdir / "ways.osm.pbf"
    node_file = tmpdir / "nodes.osm.pbf"
    small = tmpdir / "small.osm.pbf"
    for f in (way_file, node_file, small):
        f.unlink(missing_ok=True)
    pbf_size = pbf.stat().st_size

    node_refs = set()
    with Phase(f"scan 1/3: full pbf pass for {len(targets['way'])} ways",
               indent=1) as p:
        with osmium.SimpleWriter(str(way_file)) as w:
            if targets["way"]:
                fp = osmium.FileProcessor(str(pbf), WAY).with_filter(
                    IdFilter(targets["way"])
                )
                for way in fp:
                    node_refs.update(n.ref for n in way.nodes)
                    w.add_way(way)
        scan_note(p, pbf_size, len(node_refs), "referenced nodes collected")

    node_ids = targets["node"] | node_refs
    with Phase(f"scan 2/3: full pbf pass for {len(node_ids)} nodes",
               indent=1) as p:
        with osmium.SimpleWriter(str(node_file)) as w:
            if node_ids:
                fp = osmium.FileProcessor(str(pbf), NODE).with_filter(
                    IdFilter(node_ids)
                )
                for node in fp:
                    w.add_node(node)
        scan_note(p, pbf_size, len(node_ids), "nodes requested")

    # merge in node-before-way order so location indexing works
    with Phase("scan 3/3: merging the extract", indent=1) as p:
        with osmium.SimpleWriter(str(small)) as w:
            for part in (node_file, way_file):
                for obj in osmium.FileProcessor(str(part)):
                    w.add(obj)
        p.note(f"{fmt_bytes(small.stat().st_size)} working file")
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
        log(f"{errors} geometry errors (left for Overpass fallback)", 1)
    return results


def main():
    db = open_db()
    pbf = find_pbf()
    with Phase("marking my own deletions"):
        mark_deleted(db)
    if not pbf:
        log("no .osm.pbf in data/ - skipping pbf resolution")
        return
    pbf_ts = pbf_timestamp(pbf)
    with Phase("selecting objects older than the pbf snapshot") as p:
        targets = load_targets(db, pbf_ts)
        total = sum(len(v) for v in targets.values())
        p.note(f"{len(targets['node'])} nodes, {len(targets['way'])} ways")
    log(f"pbf: {pbf.name}, {fmt_bytes(pbf.stat().st_size)}, snapshot "
        f"{pbf_ts} - resolving {total} objects "
        f"(relations go to QLever/Overpass)")
    if not total:
        return

    tmpdir = DATA_DIR / "cache" / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    small = extract_small_file(pbf, targets, tmpdir)
    with Phase("assembling geometries from the extract", indent=1) as p:
        results = assemble_geometries(small, targets)
        p.note(f"{len(results)} geometries")

    with Phase("storing geometries", indent=1):
        for (otype, oid), (geojson, tags) in results.items():
            db.execute(
                "INSERT OR REPLACE INTO geoms"
                "(otype, oid, status, source, geojson, tags, resolved_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (otype, oid, "ok", "pbf", geojson,
                 json.dumps(tags, ensure_ascii=False), pbf_ts),
            )
        set_meta(db, "pbf_timestamp", pbf_ts)
        db.commit()
    log(f"resolved {len(results)}/{total} from pbf in "
        f"{fmt_dur(time.monotonic() - t0)}; "
        f"{total - len(results)} left for QLever/API/Overpass")


if __name__ == "__main__":
    run(main)
