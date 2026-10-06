#!/usr/bin/env python3
"""
Processor: Sessionizes DNS queries and computes metrics.

Each run classifies newly collected queries against the services table, then
rebuilds the sessions and metrics tables from every matched query in one
transaction. Sessions are per service within an account, ignoring profile and
device: NextDNS often sees only the router, and clients switch profiles, so
neither may split a session. Metrics are per service, profile and local day,
so they sum across profiles. A device slice (device_sessions, device_metrics)
is derived the same way per NextDNS device, for accounts where NextDNS runs on
each client; it never feeds the household totals. Rebuilding instead of
appending makes re-runs safe: nothing is double-counted, and late-arriving
queries land in the right session.

Environment variables:
  DB_PATH: SQLite database path (default: /data/sverdrup.db)
  SESSION_GAP_MINUTES: a gap longer than this starts a new session (default: 15)
  TZ: IANA time zone that defines calendar days for metrics (default: UTC)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from sverdrup.services import DomainMatcher  # noqa: E402

log = logging.getLogger("sverdrup.processor")

DEFAULT_DB_PATH = "/data/sverdrup.db"
DEFAULT_GAP_MINUTES = 15.0
CLASSIFY_BATCH = 5000


class ConfigError(Exception):
    """Configuration is invalid."""


@dataclass(frozen=True)
class Config:
    """Processor settings read from the environment."""

    db_path: str
    gap: timedelta
    tz: tzinfo

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Config:
        """Build a Config, raising ConfigError when it is unusable."""
        db_path = environ.get("DB_PATH", "").strip() or DEFAULT_DB_PATH
        raw_gap = environ.get("SESSION_GAP_MINUTES", "").strip()
        try:
            gap_minutes = float(raw_gap) if raw_gap else DEFAULT_GAP_MINUTES
        except ValueError:
            raise ConfigError("SESSION_GAP_MINUTES must be a number") from None
        if not gap_minutes > 0:
            raise ConfigError("SESSION_GAP_MINUTES must be positive")
        return cls(
            db_path, timedelta(minutes=gap_minutes), load_tz(environ.get("TZ", ""))
        )


def load_tz(name: str) -> tzinfo:
    """Resolve an IANA zone name (POSIX ``:Zone`` form allowed); empty is UTC."""
    name = name.strip().lstrip(":")
    if name in ("", "UTC", "Etc/UTC"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"TZ {name!r} is not a known IANA time zone") from None


class Track(NamedTuple):
    """Queries that sessionize together: one service within one account.

    Profile and device are deliberately not part of a track. NextDNS usually
    sits at the router, so the logged device is unreliable, and clients move
    between profiles (say, onto a VPN profile) mid-viewing. Concurrent viewers
    of one service therefore merge into one session: an undercount, never an
    overcount.
    """

    service_id: int
    account: str


class DeviceTrack(NamedTuple):
    """Queries that sessionize together in the device slice.

    Meaningful only where NextDNS runs on each client, so device labels are
    consistent; behind a router, most queries land in one device bucket.
    """

    service_id: int
    account: str
    device_name: str


# (service_id, account, profile_id or device_name, local date): the grain of
# the metrics and device_metrics tables.
MetricKey = tuple[int, str, str, date]


class RebuildResult(NamedTuple):
    """Row counts written by one rebuild."""

    sessions: int
    metrics: int
    device_sessions: int
    device_metrics: int


class Segment(NamedTuple):
    """The part of a session spent under one profile."""

    profile_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Session:
    """A run of queries on one track without a gap longer than the threshold.

    ``segments`` partition [start, end] by the profile in effect: each stretch
    between two queries belongs to the profile of the earlier query.
    """

    track: Track | DeviceTrack
    segments: tuple[Segment, ...]
    device_names: tuple[str, ...]

    @property
    def start(self) -> datetime:
        return self.segments[0].start

    @property
    def end(self) -> datetime:
        return self.segments[-1].end

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    @property
    def profile_ids(self) -> list[str]:
        """Every profile the session touched, sorted."""
        return sorted({segment.profile_id for segment in self.segments})


@dataclass
class DailyMetric:
    """One service's usage under one profile on one local calendar day."""

    total_minutes: float = 0.0
    session_count: int = 0
    last_seen: datetime | None = None


def classify(conn: sqlite3.Connection) -> int:
    """Tag unprocessed queries with their service. Returns the number tagged.

    When the services table differs from the one used for earlier runs, every
    query is reclassified so mapping changes apply to history too.
    """
    services = conn.execute("SELECT id, domains FROM services ORDER BY id").fetchall()
    fingerprint = hashlib.sha256(json.dumps(services).encode()).hexdigest()
    matcher = DomainMatcher(
        {domain: sid for sid, domains in services for domain in json.loads(domains)}
    )
    with conn:
        previous = get_meta(conn, "classifier_fingerprint")
        if previous != fingerprint:
            if previous is not None:
                log.info("service mappings changed; reclassifying all queries")
            conn.execute("UPDATE queries SET processed = 0, service_id = NULL")
            set_meta(conn, "classifier_fingerprint", fingerprint)

    tagged = 0
    while True:
        batch = conn.execute(
            "SELECT id, domain_queried FROM queries WHERE processed = 0"
            " ORDER BY id LIMIT ?",
            (CLASSIFY_BATCH,),
        ).fetchall()
        if not batch:
            return tagged
        with conn:
            conn.executemany(
                "UPDATE queries SET processed = 1, service_id = ? WHERE id = ?",
                [(matcher.match(domain), qid) for qid, domain in batch],
            )
        tagged += len(batch)


def sessionize(
    queries: Iterable[tuple[Track | DeviceTrack, str, str, datetime]], gap: timedelta
) -> Iterator[Session]:
    """Group (track, profile_id, device_name, time) rows into sessions.

    Rows must be sorted by track, then time. A new session starts when the
    track changes or the gap since the previous query exceeds ``gap``; a gap
    exactly equal to ``gap`` continues the session. A profile change inside a
    session starts a new segment, not a new session.
    """
    track: Track | DeviceTrack | None = None
    segments: list[Segment] = []
    devices: set[str] = set()
    for row_track, profile_id, device_name, at in queries:
        if track == row_track and at - segments[-1].end <= gap:
            last = segments[-1]
            segments[-1] = last._replace(end=at)
            if last.profile_id != profile_id:
                segments.append(Segment(profile_id, at, at))
            devices.add(device_name)
            continue
        if track is not None:
            yield Session(track, tuple(segments), tuple(sorted(devices)))
        track, segments, devices = (
            row_track,
            [Segment(profile_id, at, at)],
            {device_name},
        )
    if track is not None:
        yield Session(track, tuple(segments), tuple(sorted(devices)))


def split_by_day(
    start: datetime, end: datetime, tz: tzinfo
) -> Iterator[tuple[date, float]]:
    """Yield (local date, minutes) for each local calendar day [start, end) covers."""
    cursor = start
    while cursor < end:
        day = cursor.astimezone(tz).date()
        next_midnight = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
        segment_end = min(end, next_midnight.astimezone(timezone.utc))
        yield day, (segment_end - cursor).total_seconds() / 60
        cursor = segment_end


def daily_metrics(
    sessions: Iterable[Session],
    last_seen: Mapping[MetricKey, datetime],
    tz: tzinfo,
) -> dict[MetricKey, DailyMetric]:
    """Aggregate household sessions by service, account, profile and local date.

    Each segment's minutes go to its profile, split at local midnight; a
    session counts once, under the profile and on the day it starts.
    ``last_seen`` maps each key to the latest matched query.
    """
    metrics: dict[MetricKey, DailyMetric] = {}
    for session in sessions:
        service_id, account = session.track[:2]
        first = session.segments[0]
        key = (service_id, account, first.profile_id, first.start.astimezone(tz).date())
        metrics.setdefault(key, DailyMetric()).session_count += 1
        for segment in session.segments:
            for day, minutes in split_by_day(segment.start, segment.end, tz):
                key = (service_id, account, segment.profile_id, day)
                metrics.setdefault(key, DailyMetric()).total_minutes += minutes
    for key, at in last_seen.items():
        metrics.setdefault(key, DailyMetric()).last_seen = at
    return metrics


def device_daily_metrics(
    sessions: Iterable[Session],
    last_seen: Mapping[MetricKey, datetime],
    tz: tzinfo,
) -> dict[MetricKey, DailyMetric]:
    """Aggregate device sessions by service, account, device and local date.

    Minutes are split at local midnight; a session counts on the day it starts.
    """
    metrics: dict[MetricKey, DailyMetric] = {}
    for session in sessions:
        start_day = session.start.astimezone(tz).date()
        metrics.setdefault(
            (*session.track, start_day), DailyMetric()
        ).session_count += 1
        for day, minutes in split_by_day(session.start, session.end, tz):
            metrics.setdefault(
                (*session.track, day), DailyMetric()
            ).total_minutes += minutes
    for key, at in last_seen.items():
        metrics.setdefault(key, DailyMetric()).last_seen = at
    return metrics


def _matched(
    conn: sqlite3.Connection,
    by_device: bool,
    tz: tzinfo,
    last_seen: dict[MetricKey, datetime],
) -> Iterator[tuple[Track | DeviceTrack, str, str, datetime]]:
    """Stream matched queries in track order, recording last_seen per day.

    Household tracks key last_seen by profile; device tracks by device.
    """
    order = "service_id, account, device_name" if by_device else "service_id, account"
    for service_id, account, profile_id, device_name, ts in conn.execute(
        "SELECT service_id, account, profile_id, device_name, timestamp"
        f" FROM queries WHERE service_id IS NOT NULL ORDER BY {order}, timestamp, id"
    ):
        at = parse_timestamp(ts)
        day = at.astimezone(tz).date()
        if by_device:
            track = DeviceTrack(service_id, account, device_name)
            key = (service_id, account, device_name, day)
        else:
            track = Track(service_id, account)
            key = (service_id, account, profile_id, day)
        if key not in last_seen or at > last_seen[key]:
            last_seen[key] = at
        yield track, profile_id, device_name, at


def _metric_rows(metrics: dict[MetricKey, DailyMetric]) -> list[tuple]:
    return [
        (
            service_id,
            account,
            slice_value,
            day.isoformat(),
            round(m.total_minutes, 3),
            m.session_count,
            format_timestamp(m.last_seen),
        )
        for (service_id, account, slice_value, day), m in sorted(metrics.items())
    ]


def rebuild(conn: sqlite3.Connection, gap: timedelta, tz: tzinfo) -> RebuildResult:
    """Recompute household and device sessions and metrics, atomically.

    Household sessions (account + service) are the totals; the device slice
    is derived separately and is never summed into them.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        last_seen: dict[MetricKey, datetime] = {}
        sessions = list(sessionize(_matched(conn, False, tz, last_seen), gap))
        metrics = daily_metrics(sessions, last_seen, tz)
        device_last_seen: dict[MetricKey, datetime] = {}
        device_sessions = list(
            sessionize(_matched(conn, True, tz, device_last_seen), gap)
        )
        device_metrics = device_daily_metrics(device_sessions, device_last_seen, tz)

        for table in ("sessions", "metrics", "device_sessions", "device_metrics"):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            "INSERT INTO sessions (service_id, account, profile_ids, device_names,"
            " start_time, end_time, session_length_minutes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    *s.track,
                    json.dumps(s.profile_ids),
                    json.dumps(list(s.device_names)),
                    format_timestamp(s.start),
                    format_timestamp(s.end),
                    round(s.minutes, 3),
                )
                for s in sessions
            ],
        )
        conn.executemany(
            "INSERT INTO metrics (service_id, account, profile_id, date,"
            " total_minutes, session_count, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
            _metric_rows(metrics),
        )
        conn.executemany(
            "INSERT INTO device_sessions (service_id, account, device_name,"
            " profile_ids, start_time, end_time, session_length_minutes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    *s.track,
                    json.dumps(s.profile_ids),
                    format_timestamp(s.start),
                    format_timestamp(s.end),
                    round(s.minutes, 3),
                )
                for s in device_sessions
            ],
        )
        conn.executemany(
            "INSERT INTO device_metrics (service_id, account, device_name, date,"
            " total_minutes, session_count, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
            _metric_rows(device_metrics),
        )
        set_meta(conn, "last_process_at", format_timestamp(datetime.now(timezone.utc)))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return RebuildResult(
        len(sessions), len(metrics), len(device_sessions), len(device_metrics)
    )


def main(environ: Mapping[str, str] | None = None) -> int:
    """Run one processing pass. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        config = Config.from_env(os.environ if environ is None else environ)
    except ConfigError as err:
        log.error("%s", err)
        return 1

    with run_lock(f"{config.db_path}.process.lock") as acquired:
        if not acquired:
            log.info("another processor run is in progress; exiting")
            return 0
        conn = connect(config.db_path)
        try:
            init_db(conn)
            tagged = classify(conn)
            result = rebuild(conn, config.gap, config.tz)
        finally:
            conn.close()
    log.info(
        "done: classified=%d sessions=%d metric_rows=%d"
        " device_sessions=%d device_metric_rows=%d",
        tagged,
        *result,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
