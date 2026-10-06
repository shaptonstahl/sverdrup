"""SQLite schema and helpers shared by the collector and processor."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from sverdrup.services import SERVICES, Service

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id           INTEGER PRIMARY KEY,
    service_code TEXT NOT NULL UNIQUE,
    service_name TEXT NOT NULL,
    domains      TEXT NOT NULL  -- JSON array of DNS domains
);

CREATE TABLE IF NOT EXISTS queries (
    id             INTEGER PRIMARY KEY,
    account        TEXT NOT NULL,  -- short name from SVERDRUP_ACCOUNTS
    profile_id     TEXT NOT NULL,  -- NextDNS profile the query was logged under
    timestamp      TEXT NOT NULL,  -- ISO 8601 UTC, YYYY-MM-DDTHH:MM:SS.mmmZ
    device_name    TEXT NOT NULL,  -- NextDNS device name, else its ID, else 'unidentified'
    device_id      TEXT,           -- NextDNS device ID (scoped to the profile)
    device_model   TEXT,
    domain_queried TEXT NOT NULL,
    raw_json       TEXT NOT NULL,
    dedup_key      TEXT NOT NULL UNIQUE,
    processed      INTEGER NOT NULL DEFAULT 0,
    service_id     INTEGER REFERENCES services(id)
);
CREATE INDEX IF NOT EXISTS idx_queries_profile_timestamp ON queries(profile_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_queries_unprocessed ON queries(id) WHERE processed = 0;
CREATE INDEX IF NOT EXISTS idx_queries_matched
    ON queries(service_id, account, timestamp)
    WHERE service_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_queries_matched_device
    ON queries(service_id, account, device_name, timestamp)
    WHERE service_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    id                     INTEGER PRIMARY KEY,
    service_id             INTEGER NOT NULL REFERENCES services(id),
    account                TEXT NOT NULL,
    profile_ids            TEXT NOT NULL,  -- JSON array of profiles the session touched
    device_names           TEXT NOT NULL,  -- JSON array of device names it touched
    start_time             TEXT NOT NULL,
    end_time               TEXT NOT NULL,
    session_length_minutes REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_service_start ON sessions(service_id, start_time);

CREATE TABLE IF NOT EXISTS metrics (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id),
    account       TEXT NOT NULL,
    profile_id    TEXT NOT NULL,
    date          TEXT NOT NULL,  -- local calendar date, YYYY-MM-DD
    total_minutes REAL NOT NULL,
    session_count INTEGER NOT NULL,
    last_seen     TEXT NOT NULL,  -- latest matched query that date, ISO 8601 UTC
    UNIQUE (service_id, account, profile_id, date)
);

-- Device slice: the same derivations keyed by NextDNS device as well. Only
-- meaningful where NextDNS runs on each client; household figures above are
-- never the sum of these.
CREATE TABLE IF NOT EXISTS device_sessions (
    id                     INTEGER PRIMARY KEY,
    service_id             INTEGER NOT NULL REFERENCES services(id),
    account                TEXT NOT NULL,
    device_name            TEXT NOT NULL,
    profile_ids            TEXT NOT NULL,  -- JSON array of profiles the session touched
    start_time             TEXT NOT NULL,
    end_time               TEXT NOT NULL,
    session_length_minutes REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_sessions_service_start
    ON device_sessions(service_id, start_time);

CREATE TABLE IF NOT EXISTS device_metrics (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id),
    account       TEXT NOT NULL,
    device_name   TEXT NOT NULL,
    date          TEXT NOT NULL,  -- local calendar date, YYYY-MM-DD
    total_minutes REAL NOT NULL,
    session_count INTEGER NOT NULL,
    last_seen     TEXT NOT NULL,
    UNIQUE (service_id, account, device_name, date)
);
"""

_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


def connect(db_path: str) -> sqlite3.Connection:
    """Open the database with settings that let collector and processor overlap."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection, services: tuple[Service, ...] = SERVICES) -> None:
    """Create tables if missing and sync the services table. Safe to repeat."""
    with conn:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        for service in services:
            conn.execute(
                """
                INSERT INTO services (service_code, service_name, domains)
                VALUES (?, ?, ?)
                ON CONFLICT (service_code) DO UPDATE SET
                    service_name = excluded.service_name,
                    domains = excluded.domains
                """,
                (service.code, service.name, json.dumps(list(service.domains))),
            )
        codes = [s.code for s in services]
        conn.execute(
            f"DELETE FROM services WHERE service_code NOT IN ({','.join('?' * len(codes))})",
            codes,
        )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Return a metadata value, or None when unset."""
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a metadata value (caller owns the transaction)."""
    conn.execute(
        "INSERT INTO metadata (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into an aware UTC datetime.

    Accepts any fractional-second precision and a ``Z`` or numeric offset;
    a timestamp without an offset is taken as UTC. Raises ValueError.
    """
    match = _TIMESTAMP_RE.match(value.strip())
    if not match:
        raise ValueError(f"not an ISO 8601 timestamp: {value!r}")
    date, time, fraction, offset = match.groups()
    micros = (fraction or "0")[:6].ljust(6, "0")
    if offset in (None, "Z"):
        offset = "+00:00"
    elif ":" not in offset:
        offset = f"{offset[:3]}:{offset[3:]}"
    parsed = datetime.fromisoformat(f"{date}T{time}.{micros}{offset}")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """Format an aware datetime as the stored UTC form, millisecond precision.

    The fixed width keeps lexicographic order equal to chronological order.
    """
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
