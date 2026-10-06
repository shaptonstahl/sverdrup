import logging

import pytest

from collector import collect as collector
from collector.collect import (
    Config,
    ConfigError,
    NextDNSClient,
    NextDNSError,
    collect_profile,
    entry_to_row,
)
from sverdrup.db import get_meta
from tests.nextdns_mock import API_KEY, PROFILE_ID, log_entry, utc

META = f"profile:{PROFILE_ID}:"


def collect(conn, client, **kwargs):
    return collect_profile(conn, client, "home", PROFILE_ID, **kwargs)


def stored(conn):
    return conn.execute(
        "SELECT timestamp, device_name, domain_queried FROM queries ORDER BY timestamp, id"
    ).fetchall()


def minute_entries(hour, count, domain="www.netflix.com"):
    return [
        log_entry(f"2026-03-01T{hour:02d}:{m:02d}:00.000Z", domain)
        for m in range(count)
    ]


def test_backfill_on_empty_database_fetches_everything(conn, client, nextdns):
    nextdns.entries = minute_entries(10, 25)

    result = collect(conn, client)

    assert result.mode == "backfill"
    assert (result.fetched, result.inserted) == (25, 25)
    assert len(stored(conn)) == 25
    # Backfill sends no lower bound, asks oldest-first, and follows the cursor.
    assert "from" not in nextdns.requests[0]["params"]
    assert nextdns.requests[0]["params"]["sort"] == "asc"
    assert [r["params"].get("cursor") for r in nextdns.requests] == [None, "10", "20"]
    assert get_meta(conn, META + "earliest_timestamp") == "2026-03-01T10:00:00.000Z"
    assert get_meta(conn, META + "latest_timestamp") == "2026-03-01T10:24:00.000Z"
    assert get_meta(conn, META + "backfill_completed_at") is not None


def test_incremental_polls_from_last_stored_timestamp(conn, client, nextdns):
    nextdns.entries = minute_entries(10, 5)
    collect(conn, client)
    nextdns.requests.clear()
    nextdns.entries += minute_entries(11, 3)

    result = collect(conn, client)

    assert result.mode == "incremental"
    # Starts at the newest stored row (10:04) minus the 10-minute overlap.
    assert nextdns.requests[0]["params"]["from"] == "2026-03-01T09:54:00.000Z"
    assert result.inserted == 3
    assert len(stored(conn)) == 8


def test_overlapping_windows_never_double_insert(conn, client, nextdns):
    nextdns.entries = minute_entries(10, 12)
    collect(conn, client)

    # The overlap re-fetches 10:01-10:11 (newest row minus 10 minutes).
    again = collect(conn, client)
    assert again.fetched == 11
    assert again.inserted == 0
    assert len(stored(conn)) == 12


def test_dedup_key_ignores_field_order_but_not_identity():
    def key(entry, profile=PROFILE_ID):
        return entry_to_row(entry, "home", profile)[8]

    a = log_entry("2026-03-01T10:00:00.000Z", "netflix.com")
    reordered = dict(reversed(list(a.items())))
    other_device = log_entry("2026-03-01T10:00:00.000Z", "netflix.com", device="Phone")
    assert key(a) == key(reordered)
    assert key(a) != key(other_device)
    assert key(a) != key(a, profile="other1")


def test_interrupted_backfill_resumes_without_gaps(conn, nextdns, sleeps):
    nextdns.entries = minute_entries(10, 30)
    client = NextDNSClient(
        nextdns.url, API_KEY, page_size=10, max_retries=0, sleep=sleeps.append
    )
    pages = client.iter_pages
    calls = []

    def failing_pages(profile_id, since):
        for page in pages(profile_id, since):
            calls.append(page)
            if len(calls) == 2:
                raise NextDNSError("simulated outage")
            yield page

    client.iter_pages = failing_pages
    with pytest.raises(NextDNSError):
        collect(conn, client)
    assert len(stored(conn)) == 10  # first page committed
    assert get_meta(conn, META + "backfill_completed_at") is None

    client.iter_pages = pages
    result = collect(conn, client, now=lambda: utc(2026, 3, 2))
    assert result.mode == "incremental"
    assert len(stored(conn)) == 30
    assert get_meta(conn, META + "backfill_completed_at") == "2026-03-02T00:00:00.000Z"

    collect(conn, client, now=lambda: utc(2026, 3, 3))
    assert get_meta(conn, META + "backfill_completed_at") == "2026-03-02T00:00:00.000Z"


def test_rate_limit_backs_off_then_succeeds(conn, client, nextdns, sleeps):
    nextdns.entries = minute_entries(10, 3)
    nextdns.failures.extend([(429, {}), (429, {"Retry-After": "7"}), (503, {})])

    result = collect(conn, client)

    assert result.inserted == 3
    assert sleeps == [2.0, 7.0, 8.0]  # exponential, but Retry-After wins


def test_rate_limit_gives_up_after_max_retries(conn, nextdns, sleeps):
    nextdns.entries = minute_entries(10, 3)
    nextdns.failures.extend([(429, {})] * 3)
    client = NextDNSClient(nextdns.url, API_KEY, max_retries=2, sleep=sleeps.append)

    with pytest.raises(NextDNSError, match="HTTP 429"):
        collect(conn, client)
    assert sleeps == [2.0, 4.0]
    assert stored(conn) == []


def test_auth_failure_is_not_retried(conn, nextdns, sleeps):
    client = NextDNSClient(nextdns.url, "wrong-key", sleep=sleeps.append)
    with pytest.raises(NextDNSError, match="HTTP 403"):
        collect(conn, client)
    assert sleeps == []


def test_sends_api_key_header(conn, client, nextdns):
    nextdns.entries = minute_entries(10, 1)
    collect(conn, client)
    assert nextdns.requests[0]["api_key"] == API_KEY
    assert nextdns.requests[0]["profile"] == PROFILE_ID


def test_malformed_entries_are_skipped_and_values_sanitized(conn, client, nextdns):
    nextdns.entries = [
        log_entry("2026-03-01T10:00:00.000Z", " WWW.Netflix.COM. "),
        log_entry("2026-03-01T10:01:00Z", "spotify.com", device=None),
        log_entry("not a time", "netflix.com"),
        log_entry("2026-03-01T10:02:00.000Z", "bad domain!"),
        {"timestamp": "2026-03-01T10:03:00.000Z"},
        {
            "timestamp": "2026-03-01T10:04:00.000Z",
            "domain": "netflix.com",
            "device": {"id": "__UNIDENTIFIED__"},
        },
    ]

    result = collect(conn, client)

    assert (result.fetched, result.inserted, result.skipped) == (6, 3, 3)
    assert stored(conn) == [
        ("2026-03-01T10:00:00.000Z", "Living Room TV", "www.netflix.com"),
        ("2026-03-01T10:01:00.000Z", "unidentified", "spotify.com"),
        ("2026-03-01T10:04:00.000Z", "unidentified", "netflix.com"),
    ]
    assert conn.execute(
        "SELECT DISTINCT account, profile_id FROM queries"
    ).fetchall() == [("home", PROFILE_ID)]
    assert conn.execute(
        "SELECT device_id, device_model FROM queries ORDER BY timestamp"
    ).fetchall() == [("living-room-tv", None), (None, None), (None, None)]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-03-01T10:00:00.123456Z", "2026-03-01T10:00:00.123Z"),
        ("2026-03-01T05:00:00-05:00", "2026-03-01T10:00:00.000Z"),
        ("2026-03-01T10:00:00", "2026-03-01T10:00:00.000Z"),
    ],
)
def test_timestamps_normalize_to_utc_milliseconds(value, expected):
    assert (
        entry_to_row(log_entry(value, "netflix.com"), "home", PROFILE_ID)[2] == expected
    )


# --- configuration -------------------------------------------------------

GOOD_ENV = {
    "SVERDRUP_ACCOUNTS": "home, Family",
    "NEXTDNS_HOME_API_KEY": "key-home",
    "NEXTDNS_HOME_PROFILES": "abc123,def456",
    "NEXTDNS_FAMILY_API_KEY": "key-family",
    "NEXTDNS_FAMILY_PROFILES": "fam001",
}


def test_config_reads_nested_accounts_and_profiles():
    config = Config.from_env(GOOD_ENV)
    assert [(a.name, a.api_key, a.profiles) for a in config.accounts] == [
        ("home", "key-home", ("abc123", "def456")),
        ("family", "key-family", ("fam001",)),
    ]
    assert config.api_url == "https://api.nextdns.io"
    assert config.db_path == "/data/sverdrup.db"
    assert "key-home" not in repr(config)


@pytest.mark.parametrize(
    "changes, variable",
    [
        ({"SVERDRUP_ACCOUNTS": ""}, "SVERDRUP_ACCOUNTS is not set"),
        ({"SVERDRUP_ACCOUNTS": "home,,family"}, "SVERDRUP_ACCOUNTS has an empty name"),
        ({"SVERDRUP_ACCOUNTS": "home,my-family"}, "account name 'my-family'"),
        ({"SVERDRUP_ACCOUNTS": "home,HOME"}, "lists account 'home' more than once"),
        ({"NEXTDNS_HOME_API_KEY": " "}, "NEXTDNS_HOME_API_KEY is not set"),
        ({"NEXTDNS_FAMILY_PROFILES": ""}, "NEXTDNS_FAMILY_PROFILES is not set"),
        ({"NEXTDNS_HOME_PROFILES": "abc123,"}, "NEXTDNS_HOME_PROFILES has an empty"),
        ({"NEXTDNS_HOME_PROFILES": "abc123,abc123"}, "'abc123' is already listed"),
        (
            {"NEXTDNS_FAMILY_PROFILES": "abc123"},
            "already listed in NEXTDNS_HOME_PROFILES",
        ),
        ({"NEXTDNS_HOME_PROFILES": "abc/123"}, "'abc/123' is not a valid ID"),
        (
            {"NEXTDNS_API_KEY": "old"},
            "NEXTDNS_API_KEY and NEXTDNS_PROFILE_ID are no longer",
        ),
        ({"NEXTDNS_API_URL": "ftp://x"}, "NEXTDNS_API_URL must be an http(s) URL"),
    ],
)
def test_config_rejects_bad_settings_naming_the_variable(changes, variable):
    with pytest.raises(ConfigError) as caught:
        Config.from_env({**GOOD_ENV, **changes})
    assert any(variable in problem for problem in caught.value.problems)
    for key in ("key-home", "key-family"):
        assert key not in str(caught.value)


def test_config_reports_every_problem_at_once():
    with pytest.raises(ConfigError) as caught:
        Config.from_env({"SVERDRUP_ACCOUNTS": "home,family"})
    assert len(caught.value.problems) == 4


# --- main ----------------------------------------------------------------


def two_account_env(nextdns, db_path):
    """Two accounts; the family account owns a profile we choose not to collect."""
    nextdns.accounts = {
        API_KEY: {PROFILE_ID, "def456"},
        "family-key-not-a-real-secret": {"fam001", "kids99"},
    }
    return {
        "NEXTDNS_API_URL": nextdns.url,
        "SVERDRUP_ACCOUNTS": "home,family",
        "NEXTDNS_HOME_API_KEY": API_KEY,
        "NEXTDNS_HOME_PROFILES": f"{PROFILE_ID},def456",
        "NEXTDNS_FAMILY_API_KEY": "family-key-not-a-real-secret",
        "NEXTDNS_FAMILY_PROFILES": "fam001",
        "DB_PATH": db_path,
    }


def test_main_collects_only_listed_profiles_of_each_account(nextdns, db_path, conn):
    env = two_account_env(nextdns, db_path)
    nextdns.logs = {
        PROFILE_ID: minute_entries(10, 2),
        "def456": minute_entries(11, 3),
        "fam001": minute_entries(12, 4),
        "kids99": minute_entries(13, 5),  # owned by the family account, not listed
    }

    assert collector.main(env) == 0

    counts = conn.execute(
        "SELECT account, profile_id, COUNT(*) FROM queries"
        " GROUP BY account, profile_id ORDER BY profile_id"
    ).fetchall()
    assert counts == [
        ("home", PROFILE_ID, 2),
        ("home", "def456", 3),
        ("family", "fam001", 4),
    ]
    assert {(r["profile"], r["api_key"]) for r in nextdns.requests} == {
        (PROFILE_ID, API_KEY),
        ("def456", API_KEY),
        ("fam001", "family-key-not-a-real-secret"),
    }


def test_main_keeps_collecting_other_profiles_when_one_fails(nextdns, db_path, conn):
    env = two_account_env(nextdns, db_path)
    env["NEXTDNS_HOME_PROFILES"] = "notmine,def456"
    nextdns.logs = {"def456": minute_entries(11, 3), "fam001": minute_entries(12, 4)}

    assert collector.main(env) == 2
    assert conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 7


def run_main(env, caplog, capsys):
    with caplog.at_level(logging.DEBUG):
        code = collector.main(env)
    captured = capsys.readouterr()
    return code, caplog.text + captured.out + captured.err


def test_main_never_logs_an_api_key(nextdns, db_path, caplog, capsys):
    env = two_account_env(nextdns, db_path)
    code, output = run_main(env, caplog, capsys)
    assert code == 0
    assert "home/abc123: backfill done" in output

    nextdns.failures.append((401, {}))
    code, output = run_main(env, caplog, capsys)
    assert code == 2
    assert "HTTP 401" in output
    for key in (API_KEY, "family-key-not-a-real-secret"):
        assert key not in output


def test_main_redacts_keys_if_anything_logs_them(nextdns, db_path, capsys):
    env = two_account_env(nextdns, db_path)
    handler = logging.StreamHandler()
    logging.getLogger().addHandler(handler)
    try:
        collector.main(env)
        logging.getLogger("sverdrup.test").warning(
            "keys %s", env["NEXTDNS_FAMILY_API_KEY"]
        )
    finally:
        logging.getLogger().removeHandler(handler)
    err = capsys.readouterr().err
    assert "keys [REDACTED]" in err
    assert env["NEXTDNS_FAMILY_API_KEY"] not in err


def test_main_exits_nonzero_on_bad_config(db_path, caplog):
    with caplog.at_level(logging.ERROR):
        assert collector.main({"DB_PATH": db_path}) == 1
    assert "SVERDRUP_ACCOUNTS is not set" in caplog.text
