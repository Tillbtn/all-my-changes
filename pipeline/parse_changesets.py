"""Step 2: parse cached osmChange files into the object index.

Every node/way/relation that appears in one of my changesets is recorded in
the `objects` table with first/last edit time, my edit count and the last
action (create/modify/delete). For nodes the coordinates from the osmChange
are kept as a fallback geometry for objects that were deleted later.

Objects whose latest edit is new (or newly re-edited) get their cached
geometry dropped so the resolve steps fetch a fresh one.
"""

import gzip
import xml.etree.ElementTree as ET

from common import CACHE_DIR, open_db

OTYPES = {"node", "way", "relation"}
ACTIONS = {"create", "modify", "delete"}


def parse_osc(path):
    """Yield (action, otype, id, version, timestamp, lat, lon) per element."""
    with gzip.open(path, "rb") as f:
        action = None
        for event, elem in ET.iterparse(f, events=("start", "end")):
            if event == "start":
                if elem.tag in ACTIONS:
                    action = elem.tag
                continue
            if elem.tag in OTYPES and action:
                yield (
                    action,
                    elem.tag,
                    int(elem.get("id")),
                    int(elem.get("version", 0)),
                    elem.get("timestamp", ""),
                    float(elem.get("lat")) if elem.get("lat") else None,
                    float(elem.get("lon")) if elem.get("lon") else None,
                )
                elem.clear()
            elif elem.tag in ACTIONS:
                action = None
                elem.clear()


def main():
    db = open_db()
    todo = db.execute(
        "SELECT id FROM changesets WHERE downloaded=1 AND parsed=0 ORDER BY id"
    ).fetchall()
    if not todo:
        print("No new changesets to parse.")
        return
    print(f"Parsing {len(todo)} changesets...")

    n_elements = 0
    touched = set()
    for i, (cid,) in enumerate(todo, 1):
        path = CACHE_DIR / f"{cid}.osc.gz"
        if not path.exists():
            print(f"  warning: missing cache file for changeset {cid}, skipping")
            continue
        for action, otype, oid, version, ts, lat, lon in parse_osc(path):
            n_elements += 1
            touched.add((otype, oid, ts))
            db.execute(
                """
                INSERT INTO objects
                    (otype, oid, first_edit, last_edit, edit_count,
                     last_action, last_version, osc_lat, osc_lon)
                VALUES (?,?,?,?,1,?,?,?,?)
                ON CONFLICT(otype, oid) DO UPDATE SET
                    edit_count = edit_count + 1,
                    first_edit = MIN(first_edit, excluded.first_edit),
                    last_action = CASE WHEN excluded.last_edit >= last_edit
                                       THEN excluded.last_action ELSE last_action END,
                    last_version = CASE WHEN excluded.last_edit >= last_edit
                                        THEN excluded.last_version ELSE last_version END,
                    osc_lat = CASE WHEN excluded.last_edit >= last_edit
                                        AND excluded.osc_lat IS NOT NULL
                                   THEN excluded.osc_lat ELSE osc_lat END,
                    osc_lon = CASE WHEN excluded.last_edit >= last_edit
                                        AND excluded.osc_lon IS NOT NULL
                                   THEN excluded.osc_lon ELSE osc_lon END,
                    last_edit = MAX(last_edit, excluded.last_edit)
                """,
                (otype, oid, ts, ts, action, version, lat, lon),
            )
        db.execute("UPDATE changesets SET parsed=1 WHERE id=?", (cid,))
        if i % 200 == 0:
            db.commit()
            print(f"  {i}/{len(todo)} changesets, {n_elements} elements")
    db.commit()

    # drop cached geometry for objects whose known last edit is newer than
    # what was resolved -> they get re-resolved (via Overpass, the pbf only
    # serves objects older than its snapshot date)
    dropped = 0
    for otype, oid, ts in touched:
        cur = db.execute(
            "DELETE FROM geoms WHERE otype=? AND oid=? AND resolved_at < ?",
            (otype, oid, ts),
        )
        dropped += cur.rowcount
    db.commit()

    total = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    print(
        f"Parsed {len(todo)} changesets, {n_elements} elements. "
        f"{total} distinct objects, {dropped} geometries invalidated."
    )


if __name__ == "__main__":
    main()
