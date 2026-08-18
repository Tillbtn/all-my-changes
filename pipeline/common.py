"""Shared config, database, logging and HTTP helpers for the all-my-changes
pipeline."""

import gzip as _gzip
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------- config

# The user whose edits are processed; every pipeline step reads this, so
# `OSM_USER=SomeName ./update.sh` (or the users.txt loop) switches users.
OSM_USER = os.environ.get("OSM_USER", "Till_btn")


def user_slug(name: str) -> str:
    """Filesystem/URL-safe directory name for an OSM display name."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


USER_SLUG = user_slug(OSM_USER)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
# changeset ids are globally unique, so the dump cache is shared by all users
CACHE_DIR = DATA_DIR / "cache" / "changesets"
DB_PATH = DATA_DIR / "users" / USER_SLUG / "mychanges.sqlite"
WEB_DATA_ROOT = PROJECT_DIR / "web" / "data"
WEB_DATA_DIR = WEB_DATA_ROOT / USER_SLUG

OSM_API = "https://api.openstreetmap.org/api/0.6"
OVERPASS_API = "https://overpass-api.de/api/interpreter"
QLEVER_API = "https://qlever.dev/api/osm-planet"
USER_AGENT = (
    "all-my-changes/1.0 (edit history map; operator OSM user Till_btn; "
    f"processing {OSM_USER})"
)

# seconds between OSM API requests / Overpass / QLever batches
API_SLEEP = 0.2
OVERPASS_SLEEP = 5.0
QLEVER_SLEEP = 2.0


def find_pbf() -> Path | None:
    pbfs = sorted(DATA_DIR.glob("*.osm.pbf"))
    return pbfs[-1] if pbfs else None


# ---------------------------------------------------------------- logging

# 0 = only phase banners and summaries, 1 = also throttled progress lines
# (default), 2 = also one line per remote request - use that when a step
# feels stuck and you want to see exactly which query is slow.
VERBOSE = int(os.environ.get("AMC_VERBOSE", "1"))
# requests slower than this are always logged, whatever the level: they are
# what makes a long run long
SLOW_REQUEST = float(os.environ.get("AMC_SLOW_REQUEST", "10"))
# seconds between progress lines
PROGRESS_EVERY = float(os.environ.get("AMC_PROGRESS_EVERY", "5"))


def fmt_dur(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h >= 48:  # ages of datasets, where hours stop being readable
        return f"{h // 24}d{h % 24:02d}h"
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_rate(n: float, seconds: float, unit: str = "/s") -> str:
    if seconds <= 0:
        return f"-{unit}"
    r = n / seconds
    return f"{r:.1f}{unit}" if r < 10 else f"{r:.0f}{unit}"


# below this a transfer rate says nothing useful, so it is left out
RATE_FLOOR = 1024**2


def fmt_size_rate(nbytes: float, seconds: float) -> str:
    """Size plus transfer rate, or just the size for small payloads."""
    if nbytes < RATE_FLOOR:
        return fmt_bytes(nbytes)
    return f"{fmt_bytes(nbytes)} ({fmt_rate(nbytes / 1e6, seconds, ' MB/s')})"


def log(msg: str, indent: int = 0):
    print(f"[{time.strftime('%H:%M:%S')}] {'  ' * indent}{msg}", flush=True)


def vlog(msg: str, indent: int = 1, level: int = 1):
    if VERBOSE >= level:
        log(msg, indent)


class Phase:
    """Context manager timing one named part of a step.

    with Phase("scanning ways") as p:
        ...
        p.note("1234 hits")
    """

    def __init__(self, label: str, indent: int = 0):
        self.label = label
        self.indent = indent
        self.notes: list[str] = []

    def note(self, text: str):
        self.notes.append(text)

    def __enter__(self):
        self.t0 = time.monotonic()
        log(f"{self.label}...", self.indent)
        return self

    def __exit__(self, exc_type, exc, tb):
        dur = fmt_dur(time.monotonic() - self.t0)
        tail = f" - {', '.join(self.notes)}" if self.notes else ""
        if exc_type is None:
            log(f"{self.label}: {dur}{tail}", self.indent)
        else:
            log(f"{self.label}: FAILED after {dur} ({exc_type.__name__}: {exc})",
                self.indent)
        return False


class Progress:
    """done/total counter printing rate and ETA at most every few seconds."""

    def __init__(self, total: int, label: str, unit: str = "objects",
                 every: float | None = None, indent: int = 1):
        self.total = total
        self.label = label
        self.unit = unit
        self.every = PROGRESS_EVERY if every is None else every
        self.indent = indent
        self.done = 0
        self.t0 = time.monotonic()
        self.last = self.t0

    def advance(self, n: int = 1, extra: str = ""):
        self.done += n
        now = time.monotonic()
        if now - self.last >= self.every and self.done < self.total:
            self.last = now
            self._line(extra)

    def _line(self, extra: str = ""):
        el = time.monotonic() - self.t0
        pct = 100 * self.done / self.total if self.total else 100.0
        rate = self.done / el if el > 0 else 0
        parts = [
            f"{self.label} {self.done}/{self.total} ({pct:.1f}%)",
            fmt_rate(self.done, el, f" {self.unit}/s"),
            f"elapsed {fmt_dur(el)}",
        ]
        if rate > 0 and self.done < self.total:
            parts.append(f"eta {fmt_dur((self.total - self.done) / rate)}")
        if extra:
            parts.append(extra)
        log("  ".join(parts), self.indent)

    def finish(self, extra: str = ""):
        el = time.monotonic() - self.t0
        line = (f"{self.label}: {self.done} {self.unit} in {fmt_dur(el)} "
                f"({fmt_rate(self.done, el, f' {self.unit}/s').strip()})")
        log(line + (f" - {extra}" if extra else ""), self.indent)


class HttpStats:
    """Accounting for one remote service: how many calls, how much data and
    where the time went (waiting on the server vs. throttled by it)."""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0
        self.retries = 0
        self.bytes = 0
        self.rows = 0
        self.wall = 0.0
        self.server = 0.0
        self.throttled = 0.0
        self.slowest = (0.0, "")

    def record(self, label, seconds, nbytes=0, server_ms=None, rows=None):
        self.calls += 1
        self.wall += seconds
        self.bytes += nbytes
        if server_ms is not None:
            self.server += server_ms / 1000.0
        if rows is not None:
            self.rows += rows
        if seconds > self.slowest[0]:
            self.slowest = (seconds, label)
        detail = [fmt_dur(seconds)]
        if server_ms is not None:
            detail.append(f"server {fmt_dur(server_ms / 1000.0)}")
        if rows is not None:
            detail.append(f"{rows} rows")
        if nbytes:
            detail.append(fmt_size_rate(nbytes, seconds))
        line = f"{label}  {', '.join(detail)}"
        if seconds >= SLOW_REQUEST:
            vlog(f"slow: {line}", indent=2, level=1)
        else:
            vlog(line, indent=2, level=2)

    def wait(self, seconds: float):
        """Time spent sleeping because the service asked us to."""
        self.throttled += seconds

    def summary(self, indent: int = 1):
        if not self.calls:
            return
        parts = [f"{self.calls} requests",
                 f"{fmt_dur(self.wall)} waiting for responses"]
        if self.server:
            parts.append(f"{fmt_dur(self.server)} of that server-side")
        if self.bytes:
            parts.append(f"{fmt_size_rate(self.bytes, self.wall)} transferred")
        if self.rows:
            parts.append(f"{self.rows} result rows")
        if self.throttled:
            parts.append(f"{fmt_dur(self.throttled)} throttled/sleeping")
        if self.retries:
            parts.append(f"{self.retries} retries")
        log(f"{self.name}: " + ", ".join(parts), indent)
        if self.slowest[1]:
            log(f"slowest request: {self.slowest[1]} "
                f"({fmt_dur(self.slowest[0])})", indent + 1)


def run(main):
    """Entry point for a pipeline step: banner plus total runtime, so a long
    ./update.sh log shows at a glance which step ate the hours."""
    name = Path(sys.argv[0]).stem
    log(f"--- {name} [{OSM_USER}]")
    t0 = time.monotonic()
    try:
        main()
    except BaseException as e:
        log(f"--- {name} FAILED after {fmt_dur(time.monotonic() - t0)}: "
            f"{type(e).__name__}: {e}")
        raise
    log(f"--- {name} done in {fmt_dur(time.monotonic() - t0)}")


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS changesets(
    id INTEGER PRIMARY KEY,
    created_at TEXT,
    closed_at TEXT,
    open INTEGER DEFAULT 0,
    changes_count INTEGER,
    downloaded INTEGER DEFAULT 0,
    parsed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS objects(
    otype TEXT NOT NULL,
    oid INTEGER NOT NULL,
    first_edit TEXT,
    last_edit TEXT,
    edit_count INTEGER DEFAULT 0,
    last_action TEXT,
    last_version INTEGER,
    osc_lat REAL,
    osc_lon REAL,
    PRIMARY KEY (otype, oid)
);
CREATE TABLE IF NOT EXISTS geoms(
    otype TEXT NOT NULL,
    oid INTEGER NOT NULL,
    status TEXT,             -- ok | missing | deleted
    source TEXT,             -- pbf | qlever | api | overpass | osc
    geojson TEXT,
    tags TEXT,
    resolved_at TEXT,
    PRIMARY KEY (otype, oid)
);
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------- http

_session = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = USER_AGENT
    return _session


def api_get(url, params=None, retries=5, timeout=180, stats=None, label=None):
    """GET with retry/backoff for rate limits and transient errors.

    429/509 are OSM's rolling bandwidth quota, not failures: it refills
    over time and often answers with Retry-After: 1 while still draining.
    Those wait with growing patience and never give up; only real errors
    count against the retry budget.

    Pass `stats` (an HttpStats) to have request timings and the time lost
    to throttling accounted for in the step summary.
    """
    errors = throttles = 0
    while True:
        t0 = time.monotonic()
        try:
            r = session().get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            errors += 1
            if stats:
                stats.retries += 1
            if errors >= retries:
                raise
            log(f"request error ({e}), retrying...", 1)
            time.sleep(5 * errors)
            continue
        dur = time.monotonic() - t0
        if r.status_code in (429, 509):
            throttles += 1
            if stats:
                stats.retries += 1
            wait = max(int(r.headers.get("Retry-After", 0) or 0),
                       min(300, 2 ** min(throttles, 9)))
            if throttles == 1 or wait >= 30:
                log(f"HTTP {r.status_code} (rate limited), waiting {wait}s...", 1)
            time.sleep(wait)
            if stats:
                stats.wait(wait)
            continue
        if r.status_code in (502, 503, 504):
            errors += 1
            if stats:
                stats.retries += 1
            if errors >= retries:
                raise RuntimeError(f"giving up on {url}")
            wait = int(r.headers.get("Retry-After", 0)) or 15 * errors
            log(f"HTTP {r.status_code}, waiting {wait}s...", 1)
            time.sleep(wait)
            if stats:
                stats.wait(wait)
            continue
        if stats:
            stats.record(label or url.rsplit("/", 2)[-1], dur,
                         nbytes=len(r.content))
        return r


def loads_lenient(payload, label="response"):
    """Parse a JSON response that may carry raw control characters.

    OSM tag values legitimately contain literal newlines and tabs, and both
    QLever and Overpass serialize them verbatim - which is invalid JSON that
    Python's strict parser refuses mid-stream, after minutes of waiting.
    strict=False accepts control characters inside strings; json.dumps
    escapes them again on the way out, so the published GeoJSON stays valid.
    """
    if isinstance(payload, (bytes, bytearray)):
        # JSON is UTF-8 by definition; don't let a bad byte lose the response
        payload = payload.decode("utf-8", "replace")
    try:
        return json.loads(payload, strict=False)
    except json.JSONDecodeError as e:
        start = max(0, e.pos - 120)
        log(f"invalid JSON in {label} at char {e.pos} of {len(payload)}: {e.msg}")
        log(f"context: {payload[start:e.pos + 120]!r}", 1)
        raise


def dump_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    tmp.replace(path)


def open_text_out(path: Path, gzipped: bool):
    """Text writer for `path` (plain) or `path`.gz (gzip). Returns
    (file object, actual path)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if gzipped:
        target = path.with_suffix(path.suffix + ".gz")
        return _gzip.open(target, "wt", encoding="utf-8", compresslevel=6), target
    return open(path, "w"), path
