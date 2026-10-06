"""Synthetic NextDNS logs -> collector -> processor -> metrics rows."""

from datetime import timedelta

from collector import collect as collector
from processor import process as processor
from sverdrup.db import connect, format_timestamp
from tests.nextdns_mock import API_KEY, PROFILE_ID, log_entry, utc

FAMILY_KEY = "family-key-not-a-real-secret"


def watching(start, minutes, every, domain, device):
    """Queries every ``every`` minutes for ``minutes`` minutes, inclusive."""
    return [
        log_entry(format_timestamp(start + timedelta(minutes=m)), domain, device)
        for m in range(0, minutes + 1, every)
    ]


def test_logs_flow_through_to_metrics(nextdns, db_path):
    # Two accounts; the family account also owns kids99, which is not listed.
    nextdns.accounts = {API_KEY: {PROFILE_ID}, FAMILY_KEY: {"fam001", "kids99"}}
    nextdns.logs = {
        PROFILE_ID: (
            # Netflix in the living room, 19:30-20:30 New York time (EST, UTC-5)
            watching(utc(2026, 3, 2, 0, 30), 60, 5, "ipv4-c1.oca.nflxvideo.net", "TV")
            # Late YouTube on the phone that crosses local midnight: 23:45-00:15
            + watching(
                utc(2026, 3, 2, 4, 45), 30, 10, "rr1---sn-x.googlevideo.com", "Phone"
            )
            # Background noise that matches no service
            + watching(utc(2026, 3, 2, 1, 0), 120, 7, "time.example.net", "Phone")
            + [log_entry("garbage", "netflix.com")]
        ),
        # Netflix on the family profile, 20:00-20:20 local, seen only at the
        # router: NextDNS logs no device at all
        "fam001": watching(utc(2026, 3, 2, 1, 0), 20, 10, "netflix.com", None),
        "kids99": watching(utc(2026, 3, 2, 1, 0), 60, 5, "netflix.com", "Tablet"),
    }
    env = {
        "NEXTDNS_API_URL": nextdns.url,
        "SVERDRUP_ACCOUNTS": "home,family",
        "NEXTDNS_HOME_API_KEY": API_KEY,
        "NEXTDNS_HOME_PROFILES": PROFILE_ID,
        "NEXTDNS_FAMILY_API_KEY": FAMILY_KEY,
        "NEXTDNS_FAMILY_PROFILES": "fam001",
        "DB_PATH": db_path,
        "TZ": "America/New_York",
        "SESSION_GAP_MINUTES": "15",
    }

    assert collector.main(env) == 0
    assert processor.main(env) == 0

    # The next evening's Netflix arrives on a later, incremental poll.
    nextdns.entries += watching(utc(2026, 3, 3, 1, 0), 45, 15, "netflix.com", "TV")
    assert collector.main(env) == 0
    assert processor.main(env) == 0
    assert processor.main(env) == 0  # re-running changes nothing

    conn = connect(db_path)
    try:
        metrics = conn.execute(
            "SELECT s.service_code, m.account, m.profile_id, m.date, m.total_minutes,"
            " m.session_count, m.last_seen"
            " FROM metrics m JOIN services s ON s.id = m.service_id"
            " ORDER BY m.date, s.service_code, m.profile_id"
        ).fetchall()
        across_profiles = conn.execute(
            "SELECT s.service_code, m.date, SUM(m.total_minutes), SUM(m.session_count)"
            " FROM metrics m JOIN services s ON s.id = m.service_id"
            " GROUP BY s.service_code, m.date ORDER BY m.date, s.service_code"
        ).fetchall()
        family_sessions = conn.execute(
            "SELECT profile_ids, device_names, session_length_minutes FROM sessions"
            " WHERE account = 'family'"
        ).fetchall()
        device_slice = conn.execute(
            "SELECT s.service_code, m.account, m.device_name, m.date, m.total_minutes"
            " FROM device_metrics m JOIN services s ON s.id = m.service_id"
            " ORDER BY m.date, m.account, m.device_name"
        ).fetchall()
        queries = dict(
            conn.execute("SELECT profile_id, COUNT(*) FROM queries GROUP BY profile_id")
        )
    finally:
        conn.close()

    assert metrics == [
        (
            "netflix",
            "home",
            "abc123",
            "2026-03-01",
            60.0,
            1,
            "2026-03-02T01:30:00.000Z",
        ),
        (
            "netflix",
            "family",
            "fam001",
            "2026-03-01",
            20.0,
            1,
            "2026-03-02T01:20:00.000Z",
        ),
        (
            "youtube",
            "home",
            "abc123",
            "2026-03-01",
            15.0,
            1,
            "2026-03-02T04:55:00.000Z",
        ),
        (
            "netflix",
            "home",
            "abc123",
            "2026-03-02",
            45.0,
            1,
            "2026-03-03T01:45:00.000Z",
        ),
        (
            "youtube",
            "home",
            "abc123",
            "2026-03-02",
            15.0,
            0,
            "2026-03-02T05:15:00.000Z",
        ),
    ]
    assert across_profiles == [
        ("netflix", "2026-03-01", 80.0, 2),
        ("youtube", "2026-03-01", 15.0, 1),
        ("netflix", "2026-03-02", 45.0, 1),
        ("youtube", "2026-03-02", 15.0, 0),
    ]
    assert queries == {"abc123": 13 + 4 + 18 + 4, "fam001": 3}
    assert family_sessions == [('["fam001"]', '["unidentified"]', 20.0)]
    # Device slice: the router-only family profile lands in "unidentified".
    assert device_slice == [
        ("netflix", "family", "unidentified", "2026-03-01", 20.0),
        ("youtube", "home", "Phone", "2026-03-01", 15.0),
        ("netflix", "home", "TV", "2026-03-01", 60.0),
        ("youtube", "home", "Phone", "2026-03-02", 15.0),
        ("netflix", "home", "TV", "2026-03-02", 45.0),
    ]
