"""Step 1: discover all changesets of OSM_USER and cache their osmChange dumps.

Discovery pages the changeset listing newest-first. In incremental runs it
stops as soon as a whole page is already known, so a daily run costs only a
couple of requests. The osmChange downloads are immutable and cached forever
under data/cache/changesets/<id>.osc.gz.
"""

import gzip
import sys
import time

from common import (
    API_SLEEP,
    CACHE_DIR,
    OSM_API,
    OSM_USER,
    api_get,
    open_db,
    set_meta,
)


def discover(db, full=False):
    """Page through the changeset listing, inserting unknown changesets."""
    print(f"Discovering changesets for {OSM_USER}...")
    known = {row[0] for row in db.execute("SELECT id FROM changesets")}
    new_total = 0
    time_upper = None  # created_at of oldest changeset seen so far

    while True:
        params = {"display_name": OSM_USER, "limit": 100}
        if time_upper:
            # closed after T1 and created before T2
            params["time"] = f"2001-01-01T00:00:00Z,{time_upper}"
        r = api_get(f"{OSM_API}/changesets.json", params)
        r.raise_for_status()
        page = r.json().get("changesets", [])
        if not page:
            break

        fresh = 0
        for cs in page:
            if cs["id"] not in known:
                fresh += 1
                known.add(cs["id"])
                new_total += 1
                db.execute(
                    "INSERT OR IGNORE INTO changesets"
                    "(id, created_at, closed_at, open, changes_count)"
                    " VALUES (?,?,?,?,?)",
                    (
                        cs["id"],
                        cs["created_at"],
                        cs.get("closed_at"),
                        1 if cs.get("open") else 0,
                        cs.get("changes_count", 0),
                    ),
                )
            else:
                # refresh changesets we saw while they were still open, so
                # they get downloaded once closed
                db.execute(
                    "UPDATE changesets SET open=?, closed_at=?, changes_count=?,"
                    " downloaded=0, parsed=0 WHERE id=? AND open=1",
                    (
                        1 if cs.get("open") else 0,
                        cs.get("closed_at"),
                        cs.get("changes_count", 0),
                        cs["id"],
                    ),
                )
        db.commit()

        oldest = min(cs["created_at"] for cs in page)
        print(f"  page down to {oldest}: {fresh} new (total new {new_total})")

        if fresh == 0 and not full and time_upper is None:
            # first page fully known -> nothing newer than what we have
            break
        if len(page) < 100 and fresh == 0:
            break
        if len(page) < 100 and time_upper == oldest:
            break
        if fresh == 0 and time_upper == oldest:
            # no progress: everything at this timestamp is known
            break
        time_upper = oldest
        time.sleep(API_SLEEP)

    print(f"Discovery done: {new_total} new changesets.")
    return new_total


def download(db):
    """Download the osmChange for every closed, not-yet-cached changeset."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    todo = db.execute(
        "SELECT id FROM changesets WHERE downloaded=0 AND open=0 ORDER BY id"
    ).fetchall()
    if not todo:
        print("All changesets already cached.")
        return 0
    print(f"Downloading {len(todo)} osmChange files...")
    done = 0
    for (cid,) in todo:
        path = CACHE_DIR / f"{cid}.osc.gz"
        if not path.exists():
            r = api_get(f"{OSM_API}/changeset/{cid}/download")
            r.raise_for_status()
            tmp = path.with_suffix(".tmp")
            with gzip.open(tmp, "wb") as f:
                f.write(r.content)
            tmp.replace(path)
            time.sleep(API_SLEEP)
        db.execute("UPDATE changesets SET downloaded=1 WHERE id=?", (cid,))
        done += 1
        if done % 100 == 0:
            db.commit()
            print(f"  {done}/{len(todo)}")
    db.commit()
    print(f"Downloaded {done} changesets.")
    return done


def main():
    full = "--full" in sys.argv
    db = open_db()
    discover(db, full=full)
    download(db)
    set_meta(db, "last_fetch", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
