"""Show pipeline progress for every user: changeset fetch state, object
counts, which source resolved each geometry (pbf / qlever / api / overpass /
osc fallback) and how old the published web output is.

The last part answers the question quick mode cannot: "is this dataset
actually up to date?" - a full dataset can be months old and still be a
complete one, in which case only ./update.sh (without --quick) refreshes it.

Opens the databases read-only, so it is safe to run while update.sh works.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

from common import DATA_DIR, WEB_DATA_ROOT, fmt_bytes, fmt_dur, user_slug


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def age_of(iso: str) -> str:
    """Human age of an ISO timestamp like 2026-07-21T17:06:32Z."""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            t = time.mktime(time.strptime(iso[:len(time.strftime(fmt))], fmt))
        except (ValueError, TypeError):
            continue
        return fmt_dur(max(0.0, time.time() - t)) + " ago"
    return "unknown age"


def show_web_output(db, name):
    """Published output for this user: when it was built and how big it is."""
    for slug in {name, user_slug(name)}:
        d = WEB_DATA_ROOT / slug
        if (d / "meta.json").exists():
            break
    else:
        print("web output:  none yet - run build_output.py")
        return
    try:
        meta = json.loads((d / "meta.json").read_text())
    except (OSError, ValueError) as e:
        print(f"web output:  unreadable meta.json ({e})")
        return
    size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
    built = meta.get("generated_at", "")
    mode = f"mode={meta['mode']}, " if meta.get("mode") else ""
    print(f"web output:  {d.name}, built {built} ({age_of(built)}), "
          f"{mode}{fmt_bytes(size)}"
          f"{' gzipped' if meta.get('gzip') else ''}")
    latest_db = db.execute(
        "SELECT MAX(closed_at) FROM changesets WHERE parsed=1"
    ).fetchone()[0]
    if latest_db and latest_db != meta.get("latest_changeset"):
        print(f"             stale: database holds changesets up to "
              f"{latest_db}, output only to {meta.get('latest_changeset')}")


def show(user_dir: Path):
    db = open_ro(user_dir / "mychanges.sqlite")
    print(f"===== {user_dir.name} =====")

    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    if meta.get("last_fetch"):
        print(f"last fetch:  {meta['last_fetch']} ({age_of(meta['last_fetch'])})")
    if meta.get("mode") == "quick":
        print("mode:        quick preview (no edit history, no deletions)")

    total, downloaded, parsed = db.execute(
        "SELECT COUNT(*), SUM(downloaded), SUM(parsed) FROM changesets"
    ).fetchone()
    print(f"changesets:  {total} discovered, {downloaded or 0} downloaded, "
          f"{parsed or 0} parsed")

    objs = dict(db.execute(
        "SELECT otype, COUNT(*) FROM objects GROUP BY otype"
    ).fetchall())
    print(f"objects:     {objs.get('node', 0)} nodes, {objs.get('way', 0)} ways, "
          f"{objs.get('relation', 0)} relations")

    rows = db.execute(
        "SELECT source, status, COUNT(*) FROM geoms GROUP BY source, status"
        " ORDER BY 3 DESC"
    ).fetchall()
    if rows:
        print("geometries:")
        for source, status, n in rows:
            print(f"  {source:<9} {status:<8} {n}")

    todo = dict(db.execute(
        """
        SELECT o.otype, COUNT(*) FROM objects o
        LEFT JOIN geoms g ON g.otype=o.otype AND g.oid=o.oid
        WHERE g.oid IS NULL GROUP BY o.otype
        """
    ).fetchall())
    if todo:
        print(f"unresolved:  {todo.get('node', 0)} nodes, {todo.get('way', 0)} ways, "
              f"{todo.get('relation', 0)} relations")

    show_web_output(db, user_dir.name)
    db.close()


def main():
    users = sys.argv[1:]
    dirs = (
        [DATA_DIR / "users" / u for u in users]
        if users
        else sorted((DATA_DIR / "users").glob("*"))
    )
    for d in dirs:
        if (d / "mychanges.sqlite").exists():
            show(d)
        else:
            print(f"===== {d.name} ===== (no database yet)")
    if WEB_DATA_ROOT.exists():
        site = sum(f.stat().st_size for f in WEB_DATA_ROOT.rglob("*") if f.is_file())
        print(f"\nweb/data total: {fmt_bytes(site)} "
              f"(GitHub Pages asks for < 1.0 GB)")


if __name__ == "__main__":
    main()
