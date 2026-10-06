from datetime import date, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from processor import process as processor
from processor.process import (
    Config,
    ConfigError,
    Segment,
    Session,
    Track,
    classify,
    daily_metrics,
    rebuild,
    sessionize,
    split_by_day,
)
from sverdrup.db import format_timestamp, get_meta
from tests.nextdns_mock import utc

GAP = timedelta(minutes=15)
NEW_YORK = ZoneInfo("America/New_York")


def query_row(at, domain, device, profile="p1", account="home"):
    dedup_key = f"{profile}|{at.isoformat()}|{domain}|{device}"
    return account, profile, format_timestamp(at), device, domain, dedup_key


def insert_queries(conn, rows):
    """Insert (datetime, domain, device[, profile[, account]]) rows directly."""
    with conn:
        conn.executemany(
            "INSERT INTO queries (account, profile_id, timestamp, device_name,"
            " domain_queried, raw_json, dedup_key) VALUES (?, ?, ?, ?, ?, '{}', ?)",
            [query_row(*row) for row in rows],
        )


def service_id(conn, code):
    return conn.execute(
        "SELECT id FROM services WHERE service_code = ?", (code,)
    ).fetchone()[0]


def sessions_table(conn):
    return conn.execute(
        "SELECT s.service_code, x.device_names, x.start_time, x.end_time,"
        " x.session_length_minutes FROM sessions x JOIN services s ON s.id = x.service_id"
        " ORDER BY x.start_time, s.service_code"
    ).fetchall()


def metrics_table(conn):
    return conn.execute(
        "SELECT s.service_code, m.date, m.total_minutes, m.session_count, m.last_seen"
        " FROM metrics m JOIN services s ON s.id = m.service_id ORDER BY m.date, s.service_code"
    ).fetchall()


def process(conn, gap=GAP, tz=timezone.utc):
    classify(conn)
    return rebuild(conn, gap, tz)


# --- sessionization -------------------------------------------------------


def rows(*minutes, service=1, device="tv", profile="p1"):
    track = Track(service, "home")
    start = utc(2026, 3, 1, 20, 0)
    return [(track, profile, device, start + timedelta(minutes=m)) for m in minutes]


def test_gap_at_threshold_continues_and_beyond_splits():
    sessions = list(sessionize(rows(0, 5, 20, 35, 50.01), GAP))
    assert [(s.start, s.end) for s in sessions] == [
        (utc(2026, 3, 1, 20, 0), utc(2026, 3, 1, 20, 35)),
        (
            utc(2026, 3, 1, 20, 50, 0) + timedelta(seconds=0.6),
            utc(2026, 3, 1, 20, 50, 0, 600000),
        ),
    ]
    assert sessions[0].minutes == 35
    assert sessions[1].minutes == 0  # a lone query is a zero-length session


def test_service_change_splits_but_device_change_does_not():
    sessions = list(
        sessionize(
            rows(0, 1, service=1, device="phone")
            + rows(2, 3, service=1, device="tv")
            + rows(4, service=2, device="tv"),
            GAP,
        )
    )
    assert [(s.track.service_id, s.device_names, s.minutes) for s in sessions] == [
        (1, ("phone", "tv"), 3),
        (2, ("tv",), 0),
    ]


def test_profile_switch_mid_session_keeps_one_session():
    # The TV moves from profile p1 to a VPN profile p2 while still watching.
    sessions = list(
        sessionize(rows(0, 10, profile="p1") + rows(20, 30, profile="p2"), GAP)
    )
    assert len(sessions) == 1
    [session] = sessions
    assert session.minutes == 30
    assert session.profile_ids == ["p1", "p2"]
    # The stretch between the last p1 query and the first p2 query is p1's.
    assert [
        (s.profile_id, (s.end - s.start).seconds // 60) for s in session.segments
    ] == [
        ("p1", 20),
        ("p2", 10),
    ]


def test_split_by_day_at_utc_midnight():
    parts = list(
        split_by_day(utc(2026, 3, 1, 23, 50), utc(2026, 3, 2, 0, 20), timezone.utc)
    )
    assert parts == [(date(2026, 3, 1), 10), (date(2026, 3, 2), 20)]


def test_split_by_day_uses_local_midnight_and_dst():
    # 2026-03-08 is the US spring-forward day: the local day is 23 hours long.
    start = utc(2026, 3, 8, 4, 30)  # 23:30 EST on 03-07
    end = utc(2026, 3, 9, 4, 30)  # 00:30 EDT on 03-09
    parts = list(split_by_day(start, end, NEW_YORK))
    assert parts == [
        (date(2026, 3, 7), 30),
        (date(2026, 3, 8), 23 * 60),
        (date(2026, 3, 9), 30),
    ]


def test_daily_metrics_split_minutes_and_count_sessions_on_start_day():
    netflix = Track(1, "home")

    def session(start, end):
        return Session(netflix, (Segment("p1", start, end),), ("tv",))

    sessions = [
        session(utc(2026, 3, 1, 23, 40), utc(2026, 3, 2, 0, 30)),
        session(utc(2026, 3, 2, 9, 0), utc(2026, 3, 2, 9, 45)),
    ]
    day1_key = (1, "home", "p1", date(2026, 3, 1))
    day2_key = (1, "home", "p1", date(2026, 3, 2))
    last_seen = {
        day1_key: utc(2026, 3, 1, 23, 55),
        day2_key: utc(2026, 3, 2, 9, 45),
    }
    metrics = daily_metrics(sessions, last_seen, timezone.utc)
    day1, day2 = metrics[day1_key], metrics[day2_key]
    assert (day1.total_minutes, day1.session_count, day1.last_seen) == (
        20,
        1,
        last_seen[day1_key],
    )
    assert (day2.total_minutes, day2.session_count) == (30 + 45, 1)


def test_daily_metrics_attribute_minutes_to_each_profile():
    session = Session(
        Track(1, "home"),
        (
            Segment("p1", utc(2026, 3, 1, 20, 0), utc(2026, 3, 1, 20, 20)),
            Segment("p2", utc(2026, 3, 1, 20, 20), utc(2026, 3, 1, 20, 30)),
        ),
        ("tv",),
    )
    metrics = daily_metrics([session], {}, timezone.utc)
    day = date(2026, 3, 1)
    p1, p2 = metrics[(1, "home", "p1", day)], metrics[(1, "home", "p2", day)]
    assert (p1.total_minutes, p1.session_count) == (20, 1)
    assert (p2.total_minutes, p2.session_count) == (10, 0)


# --- classify + rebuild against SQLite -----------------------------------


def test_metrics_accuracy(conn):
    insert_queries(
        conn,
        [
            # Netflix on the TV: 20:00-20:30 (gaps of exactly 15), then 21:00-21:10
            (utc(2026, 3, 1, 20, 0), "www.netflix.com", "tv"),
            (utc(2026, 3, 1, 20, 15), "ipv4-c1.oca.nflxvideo.net", "tv"),
            (utc(2026, 3, 1, 20, 30), "ipv4-c1.oca.nflxvideo.net", "tv"),
            (utc(2026, 3, 1, 21, 0), "www.netflix.com", "tv"),
            (utc(2026, 3, 1, 21, 10), "nflxso.net", "tv"),
            # Netflix on a phone at the same time merges into the TV's session
            (utc(2026, 3, 1, 20, 5), "netflix.com", "phone"),
            (utc(2026, 3, 1, 20, 15), "netflix.com", "phone"),
            # Spotify, plus unrelated traffic that must be ignored
            (utc(2026, 3, 1, 8, 0), "spclient.wg.spotify.com", "phone"),
            (utc(2026, 3, 1, 8, 12), "audio-sp.pscdn.co", "phone"),
            (utc(2026, 3, 1, 8, 5), "example.org", "phone"),
            (utc(2026, 3, 1, 8, 6), "ocsp.apple.com", "tv"),
        ],
    )

    assert process(conn)[:2] == (3, 2)

    assert metrics_table(conn) == [
        ("netflix", "2026-03-01", 40.0, 2, "2026-03-01T21:10:00.000Z"),
        ("spotify", "2026-03-01", 12.0, 1, "2026-03-01T08:12:00.000Z"),
    ]
    # Data consistency: metrics agree with the sessions table.
    totals = conn.execute(
        "SELECT SUM(session_length_minutes), COUNT(*) FROM sessions"
    ).fetchone()
    assert (
        totals
        == conn.execute(
            "SELECT SUM(total_minutes), SUM(session_count) FROM metrics"
        ).fetchone()
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM queries WHERE processed = 0").fetchone()[0]
        == 0
    )


def test_profile_switch_mid_session_is_one_session_with_per_profile_minutes(conn):
    insert_queries(
        conn,
        [
            # A client switches from p1 to a VPN profile p2 mid-viewing
            (utc(2026, 3, 1, 20, 0), "netflix.com", "tv", "p1"),
            (utc(2026, 3, 1, 20, 10), "netflix.com", "tv", "p1"),
            (utc(2026, 3, 1, 20, 20), "netflix.com", "router", "p2"),
            (utc(2026, 3, 1, 20, 30), "netflix.com", "router", "p2"),
            # Another account is kept apart even at the same time
            (utc(2026, 3, 1, 20, 5), "netflix.com", "tv", "f1", "family"),
            (utc(2026, 3, 1, 20, 20), "netflix.com", "tv", "f1", "family"),
        ],
    )
    assert process(conn)[:2] == (2, 3)

    sessions = conn.execute(
        "SELECT account, profile_ids, device_names, start_time, end_time,"
        " session_length_minutes FROM sessions ORDER BY account"
    ).fetchall()
    assert sessions == [
        (
            "family",
            '["f1"]',
            '["tv"]',
            "2026-03-01T20:05:00.000Z",
            "2026-03-01T20:20:00.000Z",
            15.0,
        ),
        (
            "home",
            '["p1", "p2"]',
            '["router", "tv"]',
            "2026-03-01T20:00:00.000Z",
            "2026-03-01T20:30:00.000Z",
            30.0,
        ),
    ]
    per_profile = conn.execute(
        "SELECT account, profile_id, total_minutes, session_count, last_seen"
        " FROM metrics ORDER BY profile_id"
    ).fetchall()
    assert per_profile == [
        ("family", "f1", 15.0, 1, "2026-03-01T20:20:00.000Z"),
        ("home", "p1", 20.0, 1, "2026-03-01T20:10:00.000Z"),
        ("home", "p2", 10.0, 0, "2026-03-01T20:30:00.000Z"),
    ]
    across = conn.execute(
        "SELECT date, SUM(total_minutes), SUM(session_count), MAX(last_seen)"
        " FROM metrics GROUP BY service_id, date"
    ).fetchall()
    assert across == [("2026-03-01", 45.0, 2, "2026-03-01T20:30:00.000Z")]


def test_queries_without_a_device_sessionize_normally(conn):
    # The collector stores a missing or unidentified device as "unidentified".
    insert_queries(
        conn,
        [(utc(2026, 3, 1, 20, m), "netflix.com", "unidentified") for m in (0, 10, 20)]
        + [(utc(2026, 3, 1, 21, 0), "netflix.com", "unidentified")],
    )
    assert process(conn)[:2] == (2, 1)
    assert sessions_table(conn) == [
        (
            "netflix",
            '["unidentified"]',
            "2026-03-01T20:00:00.000Z",
            "2026-03-01T20:20:00.000Z",
            20.0,
        ),
        (
            "netflix",
            '["unidentified"]',
            "2026-03-01T21:00:00.000Z",
            "2026-03-01T21:00:00.000Z",
            0.0,
        ),
    ]
    assert metrics_table(conn)[0][2:4] == (20.0, 2)


def test_device_slice_with_consistent_labels(conn):
    tv, phone = "Living Room TV", "Phone"
    insert_queries(
        conn,
        [
            # NextDNS runs on each client, so device labels are consistent
            (utc(2026, 3, 1, 20, 0), "netflix.com", tv),
            (utc(2026, 3, 1, 20, 10), "netflix.com", tv),
            (utc(2026, 3, 1, 20, 20), "netflix.com", tv),
            (utc(2026, 3, 1, 20, 5), "netflix.com", phone),
            (utc(2026, 3, 1, 20, 15), "netflix.com", phone, "p2"),
            (utc(2026, 3, 1, 20, 40), "netflix.com", phone),
            (utc(2026, 3, 1, 21, 30), "netflix.com", "unidentified"),
        ],
    )

    assert process(conn) == (3, 2, 4, 3)

    device_sessions = conn.execute(
        "SELECT device_name, profile_ids, start_time, end_time,"
        " session_length_minutes FROM device_sessions ORDER BY start_time"
    ).fetchall()
    assert device_sessions == [
        (tv, '["p1"]', "2026-03-01T20:00:00.000Z", "2026-03-01T20:20:00.000Z", 20.0),
        (
            phone,
            '["p1", "p2"]',
            "2026-03-01T20:05:00.000Z",
            "2026-03-01T20:15:00.000Z",
            10.0,
        ),
        (phone, '["p1"]', "2026-03-01T20:40:00.000Z", "2026-03-01T20:40:00.000Z", 0.0),
        (
            "unidentified",
            '["p1"]',
            "2026-03-01T21:30:00.000Z",
            "2026-03-01T21:30:00.000Z",
            0.0,
        ),
    ]
    device_metrics = conn.execute(
        "SELECT account, device_name, date, total_minutes, session_count, last_seen"
        " FROM device_metrics ORDER BY device_name"
    ).fetchall()
    assert device_metrics == [
        ("home", tv, "2026-03-01", 20.0, 1, "2026-03-01T20:20:00.000Z"),
        ("home", phone, "2026-03-01", 10.0, 2, "2026-03-01T20:40:00.000Z"),
        ("home", "unidentified", "2026-03-01", 0.0, 1, "2026-03-01T21:30:00.000Z"),
    ]
    # Household totals come from the account+service sessions (concurrent
    # viewing merged), never from summing the device slice.
    household = conn.execute(
        "SELECT SUM(total_minutes), SUM(session_count) FROM metrics"
    ).fetchone()
    devices = conn.execute(
        "SELECT SUM(total_minutes), SUM(session_count) FROM device_metrics"
    ).fetchone()
    assert household == (20.0, 3)
    assert devices == (30.0, 4)


def test_session_crossing_midnight_in_local_time(conn):
    # 23:50-00:20 New York time on the night of 2026-03-01 (EST, UTC-5).
    insert_queries(
        conn,
        [
            (utc(2026, 3, 2, 4, 50), "netflix.com", "tv"),
            (utc(2026, 3, 2, 5, 5), "netflix.com", "tv"),
            (utc(2026, 3, 2, 5, 20), "netflix.com", "tv"),
        ],
    )
    process(conn, tz=NEW_YORK)

    assert sessions_table(conn) == [
        (
            "netflix",
            '["tv"]',
            "2026-03-02T04:50:00.000Z",
            "2026-03-02T05:20:00.000Z",
            30.0,
        )
    ]
    assert metrics_table(conn) == [
        ("netflix", "2026-03-01", 10.0, 1, "2026-03-02T04:50:00.000Z"),
        ("netflix", "2026-03-02", 20.0, 0, "2026-03-02T05:20:00.000Z"),
    ]


def test_rerun_recomputes_without_double_counting(conn):
    insert_queries(
        conn,
        [(utc(2026, 3, 1, 20, m), "netflix.com", "tv") for m in (0, 10, 20)],
    )
    process(conn)
    first = (sessions_table(conn), metrics_table(conn))

    process(conn)
    assert (sessions_table(conn), metrics_table(conn)) == first


def test_late_queries_extend_an_existing_session(conn):
    insert_queries(
        conn, [(utc(2026, 3, 1, 20, m), "netflix.com", "tv") for m in (0, 10)]
    )
    process(conn)
    insert_queries(
        conn, [(utc(2026, 3, 1, 20, m), "netflix.com", "tv") for m in (20, 30)]
    )
    process(conn)

    assert sessions_table(conn) == [
        (
            "netflix",
            '["tv"]',
            "2026-03-01T20:00:00.000Z",
            "2026-03-01T20:30:00.000Z",
            30.0,
        )
    ]
    assert metrics_table(conn)[0][2:4] == (30.0, 1)


def test_classify_only_touches_new_queries_until_mappings_change(conn):
    insert_queries(conn, [(utc(2026, 3, 1, 20, 0), "watch.example", "tv")])
    assert classify(conn) == 1
    assert classify(conn) == 0

    with conn:
        conn.execute(
            'UPDATE services SET domains = \'["netflix.com", "watch.example"]\''
            " WHERE service_code = 'netflix'"
        )
    assert classify(conn) == 1
    matched = conn.execute("SELECT service_id FROM queries").fetchone()[0]
    assert matched == service_id(conn, "netflix")
    assert get_meta(conn, "classifier_fingerprint") is not None


def test_config_from_env():
    config = Config.from_env({"SESSION_GAP_MINUTES": "30", "TZ": ":America/New_York"})
    assert config.gap == timedelta(minutes=30)
    assert config.tz == NEW_YORK
    assert Config.from_env({}).tz is timezone.utc
    for bad in (
        {"SESSION_GAP_MINUTES": "0"},
        {"SESSION_GAP_MINUTES": "x"},
        {"TZ": "Mars/Base"},
    ):
        with pytest.raises(ConfigError):
            Config.from_env(bad)


def test_main_runs_against_database(conn, db_path):
    insert_queries(conn, [(utc(2026, 3, 1, 20, 0), "netflix.com", "tv")])
    assert processor.main({"DB_PATH": db_path}) == 0
    assert len(metrics_table(conn)) == 1
    assert processor.main({"DB_PATH": db_path, "SESSION_GAP_MINUTES": "-1"}) == 1
