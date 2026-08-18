"""Step 1: discover all changesets of OSM_USER and cache their osmChange dumps.

Discovery pages the changeset listing newest-first. In incremental runs it
stops as soon as a whole page is already known, so a daily run costs only a
couple of requests. The osmChange downloads are immutable and cached forever
under data/cache/changesets/<id>.osc.gz.

Both phases report request timings via common.HttpStats: on a first full run
the dump downloads are thousands of API calls, and the summary makes it
obvious how much of the runtime was the API's rate limit rather than us.
"""

import gzip
import sys
import time

from common import (
    API_SLEEP,
    CACHE_DIR,
    OSM_API,
    OSM_USER,
    HttpStats,
    Phase,
    Progress,
    api_get,
    fmt_bytes,
    fmt_dur,
    log,
    loads_lenient,
    open_db,
    run,
    set_meta,
)

STATS = HttpStats("OSM API")
SLEPT = 0.0


def pause():
    global SLEPT
    time.sleep(API_SLEEP)
    SLEPT += API_SLEEP


def discover(db, full=False):
    """Page through the changeset listing, inserting unknown changesets."""
    log(f"discovering changesets for {OSM_USER} (100 per page, newest first)")
    known = {row[0] for row in db.execute("SELECT id FROM changesets")}
    new_total = 0
    pages = 0
    time_upper = None  # created_at of oldest changeset seen so far

    while True:
        params = {"display_name": OSM_USER, "limit": 100}
        if time_upper:
            # closed after T1 and created before T2
            params["time"] = f"2001-01-01T00:00:00Z,{time_upper}"
        r = api_get(f"{OSM_API}/changesets.json", params, stats=STATS,
                    label=f"changeset page {pages + 1}")
        r.raise_for_status()
        page = loads_lenient(r.content, "changeset listing").get("changesets", [])
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

        pages += 1
        oldest = min(cs["created_at"] for cs in page)
        log(f"page {pages} down to {oldest}: {fresh} new "
            f"(total new {new_total})", 1)

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
        pause()

    log(f"discovery done: {new_total} new changesets from {pages} pages", 1)
    return new_total


def download(db):
    """Download the osmChange for every closed, not-yet-cached changeset."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    todo = db.execute(
        "SELECT id FROM changesets WHERE downloaded=0 AND open=0 ORDER BY id"
    ).fetchall()
    if not todo:
        log("all changesets already cached")
        return 0
    log(f"downloading {len(todo)} osmChange dumps "
        f"(cached ones are skipped without a request)")
    prog = Progress(len(todo), "dumps", unit="changesets")
    done = fetched = cached = written = 0
    for (cid,) in todo:
        path = CACHE_DIR / f"{cid}.osc.gz"
        if not path.exists():
            r = api_get(f"{OSM_API}/changeset/{cid}/download", stats=STATS,
                        label=f"changeset/{cid}/download")
            r.raise_for_status()
            tmp = path.with_suffix(".tmp")
            with gzip.open(tmp, "wb") as f:
                f.write(r.content)
            tmp.replace(path)
            fetched += 1
            written += path.stat().st_size
            pause()
        else:
            cached += 1
        db.execute("UPDATE changesets SET downloaded=1 WHERE id=?", (cid,))
        done += 1
        if done % 100 == 0:
            db.commit()
        prog.advance(extra=f"{fetched} fetched, {cached} already cached")
    db.commit()
    prog.finish(extra=f"{fetched} fetched ({fmt_bytes(written)} gzipped), "
                      f"{cached} already cached")
    return done


def main():
    full = "--full" in sys.argv
    db = open_db()
    with Phase("discovery"):
        discover(db, full=full)
    with Phase("dump downloads"):
        download(db)
    set_meta(db, "last_fetch", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    db.commit()
    db.close()
    STATS.summary()
    if SLEPT:
        log(f"politeness sleeps between requests: {fmt_dur(SLEPT)} "
            f"(API_SLEEP={API_SLEEP}s)", 1)


if __name__ == "__main__":
    run(main)
