-- Copy of SCHEMA in sverdrup/db.py, for the Go tests' fixture database.
-- tests/test_server_schema.py fails when the two differ.
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
