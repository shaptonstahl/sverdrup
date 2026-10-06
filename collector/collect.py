#!/usr/bin/env python3
"""
Collector: Fetches DNS logs from NextDNS API and stores in SQLite.

Logs come from one or more NextDNS accounts (one API key each), and from
only the profiles listed for each account. A profile with no stored rows is
backfilled with every log NextDNS still retains; after that it is polled
incrementally from its last stored timestamp. Pages are requested in ascending
time order and committed one at a time, so an interrupted backfill resumes
where it stopped. Overlapping fetch windows never double-insert: each row
carries a unique dedup key that includes its profile.

Environment variables:
  SVERDRUP_ACCOUNTS: comma-separated short account names, e.g. home,family
  NEXTDNS_<NAME>_API_KEY: API key of account NAME (name uppercased)
  NEXTDNS_<NAME>_PROFILES: comma-separated profile IDs to collect for NAME
  NEXTDNS_API_URL: NextDNS API endpoint (default: https://api.nextdns.io)
  DB_PATH: SQLite database path (default: /data/sverdrup.db)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sverdrup.db import (  # noqa: E402
    connect,
    format_timestamp,
    get_meta,
    init_db,
    parse_timestamp,
    set_meta,
)
from sverdrup.runlock import run_lock  # noqa: E402
from sverdrup.services import normalize_domain  # noqa: E402

log = logging.getLogger("sverdrup.collector")

DEFAULT_API_URL = "https://api.nextdns.io"
DEFAULT_DB_PATH = "/data/sverdrup.db"

# Incremental polls re-read this much history before the newest stored row,
# to pick up log entries NextDNS records late; dedup drops the repeats.
OVERLAP = timedelta(minutes=10)

_DOMAIN_RE = re.compile(r"^[a-z0-9_-]{1,63}(\.[a-z0-9_-]{1,63})*$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# device_name for queries NextDNS could not tie to a device; they stay in the
# device slice as their own bucket rather than being dropped.
UNIDENTIFIED_DEVICE = "unidentified"


class ConfigError(Exception):
    """Configuration is missing or invalid; ``problems`` lists every issue."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class NextDNSError(Exception):
    """The NextDNS API could not be read; retry on the next scheduled run."""


@dataclass(frozen=True)
class Account:
    """A NextDNS account: its API key and the profiles to collect from it."""

    name: str
    api_key: str = field(repr=False)
    profiles: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    """Collector settings read from the environment."""

    api_url: str
    accounts: tuple[Account, ...]
    db_path: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Config:
        """Build a Config, raising ConfigError that names each bad variable.

        Messages never include an API key's value.
        """
        problems: list[str] = []
        if environ.get("NEXTDNS_API_KEY") or environ.get("NEXTDNS_PROFILE_ID"):
            problems.append(
                "NEXTDNS_API_KEY and NEXTDNS_PROFILE_ID are no longer read; set"
                " SVERDRUP_ACCOUNTS plus NEXTDNS_<NAME>_API_KEY and"
                " NEXTDNS_<NAME>_PROFILES for each account instead"
            )
        names = _split_list(environ.get("SVERDRUP_ACCOUNTS", ""))
        if not names:
            problems.append(
                "SVERDRUP_ACCOUNTS is not set; list your NextDNS account short"
                " names, e.g. SVERDRUP_ACCOUNTS=home,family"
            )
        accounts: list[Account] = []
        seen_names: set[str] = set()
        profile_owner: dict[str, str] = {}
        for name in names:
            if not name:
                problems.append(
                    "SVERDRUP_ACCOUNTS has an empty name; remove the extra comma"
                )
                continue
            if not _ACCOUNT_RE.match(name):
                problems.append(
                    f"SVERDRUP_ACCOUNTS: account name {name!r} must start with a letter"
                    " and use only letters, digits and underscores"
                )
                continue
            name = name.lower()
            if name in seen_names:
                problems.append(
                    f"SVERDRUP_ACCOUNTS lists account {name!r} more than once"
                )
                continue
            seen_names.add(name)
            key_var = f"NEXTDNS_{name.upper()}_API_KEY"
            profiles_var = f"NEXTDNS_{name.upper()}_PROFILES"
            api_key = environ.get(key_var, "").strip()
            if not api_key:
                problems.append(
                    f"{key_var} is not set; give the API key of account {name!r}"
                )
            profiles = _split_list(environ.get(profiles_var, ""))
            if not profiles:
                problems.append(
                    f"{profiles_var} is not set; list the profile IDs to collect"
                    f" for account {name!r}, e.g. {profiles_var}=abc123,def456"
                )
            for profile in profiles:
                if not profile:
                    problems.append(
                        f"{profiles_var} has an empty profile ID; remove the extra comma"
                    )
                elif not _PROFILE_RE.match(profile):
                    problems.append(
                        f"{profiles_var}: profile ID {profile!r} is not a valid ID"
                    )
                elif profile in profile_owner:
                    problems.append(
                        f"{profiles_var}: profile ID {profile!r} is already listed in"
                        f" {profile_owner[profile]}; list each profile once"
                    )
                else:
                    profile_owner[profile] = profiles_var
            accounts.append(Account(name, api_key, tuple(profiles)))

        api_url = environ.get("NEXTDNS_API_URL", "").strip() or DEFAULT_API_URL
        if urllib.parse.urlparse(api_url).scheme not in ("http", "https"):
            problems.append("NEXTDNS_API_URL must be an http(s) URL")
        if problems:
            raise ConfigError(problems)
        db_path = environ.get("DB_PATH", "").strip() or DEFAULT_DB_PATH
        return cls(api_url.rstrip("/"), tuple(accounts), db_path)


def _split_list(value: str) -> list[str]:
    """Split a comma-separated variable; blank means empty, blanks inside stay."""
    return [] if not value.strip() else [item.strip() for item in value.split(",")]


class NextDNSClient:
    """Minimal client for ``GET /profiles/:profile/logs`` with one account's key."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        page_size: int = 1000,
        max_retries: int = 6,
        backoff_base: float = 2.0,
        backoff_max: float = 300.0,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._page_size = page_size
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._timeout = timeout
        self._sleep = sleep

    def iter_pages(self, profile_id: str, since: str | None) -> Iterator[list[object]]:
        """Yield a profile's log entries a page at a time, oldest first.

        ``since`` bounds the oldest entry; None fetches everything retained.
        """
        url = f"{self._api_url}/profiles/{urllib.parse.quote(profile_id, safe='')}/logs"
        params: dict[str, str] = {"sort": "asc", "limit": str(self._page_size)}
        if since is not None:
            params["from"] = since
        while True:
            body = self._get(url, params)
            data = body.get("data")
            if not isinstance(data, list):
                raise NextDNSError("NextDNS response has no data list")
            yield data
            meta = body.get("meta")
            pagination = meta.get("pagination") if isinstance(meta, dict) else None
            cursor = pagination.get("cursor") if isinstance(pagination, dict) else None
            if not cursor:
                return
            params["cursor"] = str(cursor)

    def _get(self, url: str, params: dict[str, str]) -> dict:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={
                "X-Api-Key": self._api_key,
                "Accept": "application/json",
                "User-Agent": "sverdrup-collector",
            },
        )
        for attempt in range(self._max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    body = json.loads(response.read())
            except urllib.error.HTTPError as err:
                retryable = err.code == 429 or 500 <= err.code < 600
                if not retryable or attempt == self._max_retries:
                    raise NextDNSError(
                        f"NextDNS API returned HTTP {err.code}"
                    ) from None
                delay = self._backoff_delay(attempt, err.headers.get("Retry-After"))
                log.warning(
                    "NextDNS API returned HTTP %d; retrying in %.1fs", err.code, delay
                )
                self._sleep(delay)
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                reason = getattr(err, "reason", err)
                raise NextDNSError(
                    f"network error contacting NextDNS: {reason}"
                ) from None
            except ValueError:
                raise NextDNSError("NextDNS response is not valid JSON") from None
            if not isinstance(body, dict):
                raise NextDNSError("NextDNS response is not a JSON object")
            return body
        raise AssertionError("unreachable")

    def _backoff_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(self._backoff_max, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(self._backoff_max, self._backoff_base * 2**attempt)


def entry_to_row(
    entry: object, account: str, profile_id: str
) -> tuple[str, str, str, str, str | None, str | None, str, str, str] | None:
    """Validate one log entry and return a queries row, or None if malformed.

    The row is (account, profile_id, timestamp, device_name, device_id,
    device_model, domain_queried, raw_json, dedup_key).
    """
    if not isinstance(entry, dict):
        return None
    timestamp, domain = entry.get("timestamp"), entry.get("domain")
    if not isinstance(timestamp, str) or not isinstance(domain, str):
        return None
    try:
        timestamp = format_timestamp(parse_timestamp(timestamp))
    except ValueError:
        return None
    domain = normalize_domain(domain)
    if len(domain) > 253 or not _DOMAIN_RE.match(domain):
        return None
    device = entry.get("device")
    device = device if isinstance(device, dict) else {}
    device_id = _text(device.get("id"))
    device_model = _text(device.get("model"))
    if device_id == "__UNIDENTIFIED__":  # NextDNS's own unidentified marker
        device_id = None
    device_name = _text(device.get("name")) or device_id or UNIDENTIFIED_DEVICE
    identity = [
        profile_id,
        timestamp,
        domain,
        device_id,
        device_name,
        entry.get("clientIp"),
        entry.get("protocol"),
        entry.get("type"),
    ]
    dedup_key = hashlib.sha256(json.dumps(identity, default=str).encode()).hexdigest()
    raw_json = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return (
        account,
        profile_id,
        timestamp,
        device_name,
        device_id,
        device_model,
        domain,
        raw_json,
        dedup_key,
    )


def _text(value: object) -> str | None:
    """Return a stripped non-empty string, or None."""
    return (value.strip() or None) if isinstance(value, str) else None


@dataclass
class CollectResult:
    """Counts from one collector run."""

    mode: str
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0


def collect_profile(
    conn: sqlite3.Connection,
    client: NextDNSClient,
    account: str,
    profile_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CollectResult:
    """Fetch a profile's new logs: backfill if it has no rows, else incremental."""
    latest = conn.execute(
        "SELECT MAX(timestamp) FROM queries WHERE profile_id = ?", (profile_id,)
    ).fetchone()[0]
    if latest is None:
        result, since = CollectResult("backfill"), None
    else:
        result = CollectResult("incremental")
        since = format_timestamp(parse_timestamp(latest) - OVERLAP)
    log.info(
        "%s/%s: starting %s%s",
        account,
        profile_id,
        result.mode,
        f" from {since}" if since else "",
    )
    meta = f"profile:{profile_id}:"

    for page in client.iter_pages(profile_id, since):
        rows = []
        for entry in page:
            row = entry_to_row(entry, account, profile_id)
            if row is None:
                result.skipped += 1
            else:
                rows.append(row)
        if len(rows) < len(page):
            log.warning(
                "%s/%s: skipped %d malformed log entries",
                account,
                profile_id,
                len(page) - len(rows),
            )
        result.fetched += len(page)
        with conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO queries (account, profile_id, timestamp,"
                " device_name, device_id, device_model, domain_queried, raw_json,"
                " dedup_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            result.inserted += conn.total_changes - before
            earliest, newest = conn.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM queries WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if earliest is not None:
                set_meta(conn, meta + "earliest_timestamp", earliest)
                set_meta(conn, meta + "latest_timestamp", newest)

    with conn:
        stamp = format_timestamp(now())
        set_meta(conn, meta + "last_collect_at", stamp)
        if get_meta(conn, meta + "backfill_completed_at") is None:
            set_meta(conn, meta + "backfill_completed_at", stamp)
    return result


class _RedactFilter(logging.Filter):
    """Replace secrets wherever they would appear in a log line."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def main(environ: Mapping[str, str] | None = None) -> int:
    """Run one collection pass. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        config = Config.from_env(os.environ if environ is None else environ)
    except ConfigError as err:
        for problem in err.problems:
            log.error("configuration: %s", problem)
        return 1
    redact = _RedactFilter([account.api_key for account in config.accounts])
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)

    with run_lock(f"{config.db_path}.collect.lock") as acquired:
        if not acquired:
            log.info("another collector run is in progress; exiting")
            return 0
        failed = False
        conn = connect(config.db_path)
        try:
            init_db(conn)
            for account in config.accounts:
                client = NextDNSClient(config.api_url, account.api_key)
                for profile_id in account.profiles:
                    try:
                        result = collect_profile(conn, client, account.name, profile_id)
                    except NextDNSError as err:
                        log.error(
                            "%s/%s: collection stopped, will resume next run: %s",
                            account.name,
                            profile_id,
                            err,
                        )
                        failed = True
                        continue
                    log.info(
                        "%s/%s: %s done: fetched=%d inserted=%d skipped=%d",
                        account.name,
                        profile_id,
                        result.mode,
                        result.fetched,
                        result.inserted,
                        result.skipped,
                    )
        finally:
            conn.close()
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
