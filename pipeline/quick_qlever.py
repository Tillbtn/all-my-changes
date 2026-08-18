"""Quick mode: build a user's map dataset purely from QLever's osm-planet
("objects whose current version is by this user") - seconds instead of
hours, at the price of missing anything someone else edited since, plus
deleted objects and the per-user edit history (first edit, edit count).

Intended as an instant preview for a fresh user. It never touches a
completed changeset-based dataset (web/data/<user>/meta.json marks one),
and it keeps the changesets table intact, so the full pipeline can take
over later - parse_changesets recognizes the quick marker and rebuilds
the object index from scratch.

The object rows stream in as TSV (id, timestamp, version, WKT contain no
tabs), so even a multi-million-object user needs no big in-memory JSON.
The stream is the slow part of this step, so it reports rows/s, transfer
rate and an ETA against the expected row count while it runs.
"""

import json
import sqlite3
import time

import requests

import build_output
from common import (
    OSM_USER,
    QLEVER_API,
    USER_AGENT,
    WEB_DATA_DIR,
    Phase,
    Progress,
    fmt_bytes,
    fmt_dur,
    fmt_rate,
    fmt_size_rate,
    get_meta,
    loads_lenient,
    log,
    open_db,
    run,
    set_meta,
)
from resolve_qlever import KEY_PREFIX, to_geojson

META_PREFIX = "https://www.openstreetmap.org/meta/"


def clean(value):
    """TSV export: literals with non-standard datatypes (e.g. WKT) arrive
    as "..."^^<datatype> - reduce them to their lexical form."""
    if value.startswith('"'):
        value = value[1 : value.rfind('"')]
    return value


class Meter:
    """Counts what a streamed response actually costs: rows, decoded
    characters and (when urllib3 exposes it) compressed wire bytes."""

    def __init__(self):
        self.chars = 0
        self.wire = 0

    def report(self, label, seconds, rows):
        parts = [f"{rows} rows in {fmt_dur(seconds)}",
                 fmt_rate(rows, seconds, " rows/s").strip(),
                 f"{fmt_bytes(self.chars)} decoded"]
        if self.wire:
            parts.append(f"{fmt_size_rate(self.wire, seconds)} on the wire")
        log(f"{label}: " + ", ".join(parts), 1)


def stream_tsv(query, meter):
    """POST a SPARQL query, yield rows of QLever's TSV export."""
    with requests.post(
        QLEVER_API,
        data={"query": query},
        headers={"User-Agent": USER_AGENT, "Accept": "text/tab-separated-values"},
        timeout=900,
        stream=True,
    ) as r:
        r.raise_for_status()
        lines = r.iter_lines(decode_unicode=True)
        next(lines, None)  # header row
        for line in lines:
            if line:
                meter.chars += len(line) + 1
                yield [clean(v) for v in line.split("\t")]
        try:
            meter.wire = r.raw.tell() or 0
        except Exception:
            pass


def fetch_all_tags():
    """uri -> tags dict for every tagged object of the user (JSON: tag
    values may contain characters that would break TSV rows)."""
    t0 = time.monotonic()
    r = requests.post(
        QLEVER_API,
        data={"query": f'''
PREFIX osmmeta: <{META_PREFIX}>
SELECT ?id ?p ?v WHERE {{
  ?id osmmeta:user "{OSM_USER}" .
  ?id ?p ?v .
  FILTER(STRSTARTS(STR(?p), "{KEY_PREFIX}"))
}}'''},
        headers={"User-Agent": USER_AGENT,
                 "Accept": "application/sparql-results+json"},
        timeout=900,
    )
    r.raise_for_status()
    nbytes = len(r.content)
    data = loads_lenient(r.content, "tag query")
    bindings = data["results"]["bindings"]
    tags = {}
    for b in bindings:
        uri = b["id"]["value"]
        tags.setdefault(uri, {})[b["p"]["value"][len(KEY_PREFIX):]] = b["v"]["value"]
    dur = time.monotonic() - t0
    server = (data.get("meta") or {}).get("query-time-ms")
    log(f"tags: {len(tags)} tagged objects from {len(bindings)} tag triples "
        f"in {fmt_dur(dur)}"
        + (f" (server {fmt_dur(server / 1000)})" if server is not None else "")
        + f", {fmt_bytes(nbytes)} transferred", 1)
    return tags


def objects_query():
    return f'''
PREFIX osmmeta: <{META_PREFIX}>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
SELECT ?id ?ts ?v ?wkt WHERE {{
  ?id osmmeta:user "{OSM_USER}" .
  ?id osmmeta:timestamp ?ts .
  ?id osmmeta:version ?v .
  ?id geo:hasGeometry/geo:asWKT ?wkt .
}}'''


def expected_rows():
    """How many rows the objects query must deliver - the reference that
    tells a silently truncated stream apart from a complete one."""
    query = objects_query().replace(
        "SELECT ?id ?ts ?v ?wkt", "SELECT (COUNT(?id) AS ?n)", 1
    )
    t0 = time.monotonic()
    r = requests.post(
        QLEVER_API, data={"query": query},
        headers={"User-Agent": USER_AGENT,
                 "Accept": "application/sparql-results+json"},
        timeout=300,
    )
    r.raise_for_status()
    data = loads_lenient(r.content, "COUNT query")
    server = (data.get("meta") or {}).get("query-time-ms")
    n = int(data["results"]["bindings"][0]["n"]["value"])
    log(f"expected row count: {n} (COUNT query took "
        f"{fmt_dur(time.monotonic() - t0)}"
        + (f", server {fmt_dur(server / 1000)}" if server is not None else "")
        + ")", 1)
    return n


def consume(db, tags, now, expected, log_collisions, collisions):
    """One full pass over the objects stream; replayed rows just overwrite
    themselves. Returns (rows_seen, per-type counts, bad, aborted)."""
    counts = {"node": 0, "way": 0, "relation": 0}
    seen = bad = 0
    aborted = None
    meter = Meter()
    prog = Progress(expected, "stream", unit="rows")
    t0 = time.monotonic()
    try:
        for iri, ts, version, wkt in stream_tsv(objects_query(), meter):
            seen += 1
            uri = iri.strip("<>")
            otype, oid = uri.rsplit("/", 2)[-2:]
            try:
                geom = to_geojson(wkt)
            except Exception:
                geom = None
            if geom is None:
                bad += 1
                prog.advance(extra=f"{bad} unusable")
                continue
            try:
                db.execute(
                    "INSERT INTO objects"
                    "(otype, oid, first_edit, last_edit, edit_count,"
                    " last_action, last_version)"
                    " VALUES (?,?,?,?,1,'modify',?)",
                    (otype, int(oid), ts, ts, int(version)),
                )
            except sqlite3.IntegrityError:
                if log_collisions and len(collisions) < 5:
                    old = db.execute(
                        "SELECT first_edit, last_version FROM objects"
                        " WHERE otype=? AND oid=?", (otype, int(oid)),
                    ).fetchone()
                    collisions.append((uri, ts, version, old))
                db.execute(
                    "INSERT OR REPLACE INTO objects"
                    "(otype, oid, first_edit, last_edit, edit_count,"
                    " last_action, last_version)"
                    " VALUES (?,?,?,?,1,'modify',?)",
                    (otype, int(oid), ts, ts, int(version)),
                )
            db.execute(
                "INSERT OR REPLACE INTO geoms"
                "(otype, oid, status, source, geojson, tags, resolved_at)"
                " VALUES (?,?,'ok','qlever',?,?,?)",
                (otype, int(oid), json.dumps(geom),
                 json.dumps(tags.get(uri, {}), ensure_ascii=False), now),
            )
            counts[otype] += 1
            prog.advance(extra=f"{fmt_bytes(meter.chars)} decoded")
            if seen % 50000 == 0:
                db.commit()
    except requests.RequestException as e:
        aborted = e
    db.commit()
    meter.report("objects stream", time.monotonic() - t0, seen)
    return seen, counts, bad, aborted


def main():
    db = open_db()
    if (WEB_DATA_DIR / "meta.json").exists() and get_meta(db, "mode") != "quick":
        report_existing(db)
        return

    log(f"Quick build for {OSM_USER} from QLever (last-editor semantics)")
    with Phase("asking QLever what to expect", indent=1):
        expected = expected_rows()
        tags = fetch_all_tags()

    db.execute("DELETE FROM objects")
    db.execute("DELETE FROM geoms")
    set_meta(db, "mode", "quick")
    db.commit()

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    collisions = []
    for attempt in range(1, 5):
        seen, counts, bad, aborted = consume(
            db, tags, now, expected, attempt == 1, collisions
        )
        if aborted is None and seen >= expected:
            break
        why = f"stream error: {aborted}" if aborted else "silently truncated"
        log(f"attempt {attempt}: {seen}/{expected} rows ({why})", 1)
        if attempt == 4:
            raise RuntimeError(
                f"objects stream stayed incomplete ({seen}/{expected} rows)"
            )
        log("re-streaming (already stored rows just repeat)...", 1)
        time.sleep(10)
    db.close()

    log(f"{counts['node']} nodes, {counts['way']} ways, "
        f"{counts['relation']} relations ({bad} unusable geometries)", 1)
    if collisions:
        log("WARNING: duplicate ids in stream, first examples:", 1)
        for uri, ts, version, old in collisions:
            log(f"{uri} ts={ts} v={version} (already stored: {old})", 2)

    build_output.main()


def report_existing(db):
    """Explain *why* quick mode is a no-op here: a finished full dataset is
    present. Its age is what tells you whether it still needs refreshing -
    quick mode says nothing about that, so spell it out."""
    try:
        meta = json.loads((WEB_DATA_DIR / "meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    built = (meta.get("generated_at") or "")[:10]
    age = ""
    if built:
        try:
            days = int((time.time() - time.mktime(
                time.strptime(built, "%Y-%m-%d"))) // 86400)
            age = f", {days} day{'s' if days != 1 else ''} old"
        except ValueError:
            pass
    log(f"{OSM_USER}: full dataset already present "
        f"(built {built or 'at an unknown date'}{age}, "
        f"{meta.get('objects', '?')} objects from "
        f"{meta.get('changesets', '?')} changesets).")
    log("Quick mode only builds previews for users without one and will not "
        "touch it - this says nothing about whether it is up to date.", 1)
    log(f"Run  ./update.sh {OSM_USER}  for a full incremental update "
        f"(latest changeset in it: {meta.get('latest_changeset') or 'unknown'}).", 1)


if __name__ == "__main__":
    run(main)
