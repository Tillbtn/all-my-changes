"""Shared config, database and HTTP helpers for the all-my-changes pipeline."""

import json
import sqlite3
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------- config

OSM_USER = "Till_btn"
OSM_UID = 16371836

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache" / "changesets"
DB_PATH = DATA_DIR / "mychanges.sqlite"
WEB_DATA_DIR = PROJECT_DIR / "web" / "data"

OSM_API = "https://api.openstreetmap.org/api/0.6"
OVERPASS_API = "https://overpass-api.de/api/interpreter"
USER_AGENT = f"all-my-changes/1.0 (personal edit map; OSM user {OSM_USER})"

# seconds between OSM API requests / Overpass batches
API_SLEEP = 0.2
OVERPASS_SLEEP = 5.0


def find_pbf() -> Path | None:
    pbfs = sorted(DATA_DIR.glob("*.osm.pbf"))
    return pbfs[-1] if pbfs else None


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
    source TEXT,             -- pbf | overpass | osc
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


def api_get(url, params=None, retries=5, timeout=180):
    """GET with retry/backoff for rate limits and transient errors."""
    for attempt in range(retries):
        try:
            r = session().get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            print(f"  request error ({e}), retrying...")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 502, 503, 504, 509):
            wait = int(r.headers.get("Retry-After", 0)) or 15 * (attempt + 1)
            print(f"  HTTP {r.status_code}, waiting {wait}s...")
            time.sleep(wait)
            continue
        return r
    raise RuntimeError(f"giving up on {url}")


def dump_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    tmp.replace(path)
