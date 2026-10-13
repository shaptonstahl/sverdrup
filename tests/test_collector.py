import logging
import sqlite3
import threading

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
    # The last request reaches the live edge: an empty page ends the run.
    assert [r["params"].get("cursor") for r in nextdns.requests] == [
        None,
        "10",
        "20",
        "25",
    ]
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


def test_caught_up_profile_stops_at_the_live_edge(conn, client, nextdns):
    # NextDNS never ends oldest-first paging; past the newest entry it returns
    # an empty page with the same cursor forever.
    nextdns.entries = minute_entries(10, 3)
    collect(conn, client)
    nextdns.requests.clear()

    result = collect(conn, client, now=lambda: utc(2026, 3, 2))

    assert result.inserted == 0
    assert len(nextdns.requests) == 2  # the overlap page, then one empty page
    assert get_meta(conn, META + "last_collect_at") == "2026-03-02T00:00:00.000Z"


def test_paging_stops_when_the_cursor_repeats(nextdns):
    client = NextDNSClient(nextdns.url, API_KEY)
    responses = iter(
        [
            {"data": [{"n": 1}], "meta": {"pagination": {"cursor": "a"}}},
            {"data": [{"n": 2}], "meta": {"pagination": {"cursor": "a"}}},
            {"data": [{"n": 3}], "meta": {"pagination": {"cursor": "b"}}},
        ]
    )
    client._get = lambda url, params: next(responses)

    assert list(client.iter_pages(PROFILE_ID, None)) == [[{"n": 1}], [{"n": 2}]]


def test_page_waits_for_another_writer_instead_of_failing(
    conn, client, nextdns, db_path
):
    # A page with nothing to insert still reads and then writes metadata. It
    # must queue behind a writer such as the processor's rebuild, not fail
    # with "database is locked" (a deferred read-then-write cannot wait).
    nextdns.entries = minute_entries(10, 1)
    collect(conn, client)
    nextdns.entries = [log_entry("2026-03-01T10:05:00.000Z", "not a domain")]
    writer = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    writer.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.3, writer.execute, ["COMMIT"])
    release.start()
    try:
        result = collect(conn, client)
    finally:
        release.join()
        writer.close()

    assert result.skipped == 1
    assert get_meta(conn, META + "last_collect_at") is not None


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
        Config.from_env({"SVERDRUP_ACCOUNTS": "home,family,bad-name"})
    assert len(caught.value.problems) == 3


def test_config_without_a_profile_list_collects_every_visible_profile():
    env = {**GOOD_ENV, "NEXTDNS_FAMILY_PROFILES": " "}
    del env["NEXTDNS_HOME_PROFILES"]
    assert [a.profiles for a in Config.from_env(env).accounts] == [(), ()]


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


def test_main_treats_a_locked_database_as_a_profile_failure(
    nextdns, db_path, conn, monkeypatch, caplog
):
    env = two_account_env(nextdns, db_path)
    nextdns.logs = {"def456": minute_entries(11, 3), "fam001": minute_entries(12, 4)}
    real = collector.collect_profile

    def locked_for_first_profile(conn, client, account, profile_id, **kwargs):
        if profile_id == PROFILE_ID:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, client, account, profile_id, **kwargs)

    monkeypatch.setattr(collector, "collect_profile", locked_for_first_profile)
    with caplog.at_level(logging.ERROR):
        assert collector.main(env) == 2
    assert f"home/{PROFILE_ID}: collection stopped" in caplog.text
    assert conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 7


def test_main_still_fails_on_other_database_errors(nextdns, db_path, monkeypatch):
    env = two_account_env(nextdns, db_path)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(collector, "collect_profile", broken)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        collector.main(env)


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


# --- profile discovery -----------------------------------------------------


def discovery_env(nextdns, db_path):
    """Two accounts without profile lists; the family key also sees abc123."""
    nextdns.accounts = {
        API_KEY: {PROFILE_ID, "def456"},
        "family-key-not-a-real-secret": {"fam001", "kids99", PROFILE_ID},
    }
    nextdns.names = {PROFILE_ID: "Stephen", "def456": "Julie", "kids99": "Kids"}
    nextdns.logs = {
        PROFILE_ID: minute_entries(10, 2),
        "def456": minute_entries(11, 3),
        "fam001": minute_entries(12, 4),
        "kids99": minute_entries(13, 5),
    }
    return {
        "NEXTDNS_API_URL": nextdns.url,
        "SVERDRUP_ACCOUNTS": "home,family",
        "NEXTDNS_HOME_API_KEY": API_KEY,
        "NEXTDNS_FAMILY_API_KEY": "family-key-not-a-real-secret",
        "DB_PATH": db_path,
    }


def stored_profiles(conn):
    return conn.execute(
        "SELECT profile_id, account, name FROM profiles ORDER BY profile_id"
    ).fetchall()


def query_counts(conn):
    return conn.execute(
        "SELECT account, profile_id, COUNT(*) FROM queries"
        " GROUP BY account, profile_id ORDER BY profile_id"
    ).fetchall()


def test_list_profiles_follows_pages_and_skips_malformed(nextdns, client):
    nextdns.accounts = {API_KEY: {"a1", "b2", "c3"}}
    nextdns.names = {"b2": "Kids"}
    nextdns.profiles_page_size = 2

    assert collector.NextDNSClient.list_profiles(client) == [
        collector.Profile("a1", "a1"),
        collector.Profile("b2", "Kids"),
        collector.Profile("c3", "c3"),
    ]
    assert len(nextdns.profile_requests) == 2


def test_main_without_lists_collects_every_visible_profile_once(nextdns, db_path, conn):
    env = discovery_env(nextdns, db_path)

    assert collector.main(env) == 0

    # abc123 is visible to both keys; the first account collects it.
    assert query_counts(conn) == [
        ("home", PROFILE_ID, 2),
        ("home", "def456", 3),
        ("family", "fam001", 4),
        ("family", "kids99", 5),
    ]
    assert stored_profiles(conn) == [
        (PROFILE_ID, "home", "Stephen"),
        ("def456", "home", "Julie"),
        ("fam001", "family", "fam001"),
        ("kids99", "family", "Kids"),
    ]
    assert (PROFILE_ID, "family-key-not-a-real-secret") not in {
        (r["profile"], r["api_key"]) for r in nextdns.requests
    }


def test_a_listed_profile_is_never_collected_by_a_discovering_account(
    nextdns, db_path, conn
):
    env = discovery_env(nextdns, db_path)
    env["SVERDRUP_ACCOUNTS"] = "family,home"
    env["NEXTDNS_HOME_PROFILES"] = PROFILE_ID

    assert collector.main(env) == 0

    # home lists abc123, so family (first, discovering) leaves it, and home
    # collects only its list, not def456.
    assert query_counts(conn) == [
        ("home", PROFILE_ID, 2),
        ("family", "fam001", 4),
        ("family", "kids99", 5),
    ]
    assert (PROFILE_ID, "home", "Stephen") in stored_profiles(conn)


def test_a_new_profile_is_backfilled_while_others_poll_incrementally(
    nextdns, db_path, conn
):
    env = discovery_env(nextdns, db_path)
    assert collector.main(env) == 0
    nextdns.requests.clear()

    nextdns.accounts[API_KEY].add("new789")
    nextdns.names["new789"] = "Guest"
    nextdns.logs["new789"] = minute_entries(14, 6)
    assert collector.main(env) == 0

    first = {}
    for request in nextdns.requests:
        first.setdefault(request["profile"], request["params"])
    assert "from" not in first["new789"]
    assert "from" in first[PROFILE_ID]
    assert ("home", "new789", 6) in query_counts(conn)
    assert ("new789", "home", "Guest") in stored_profiles(conn)


def test_profile_names_follow_renames(nextdns, db_path, conn):
    env = discovery_env(nextdns, db_path)
    assert collector.main(env) == 0
    nextdns.names["def456"] = "Julie's house"

    assert collector.main(env) == 0

    assert ("def456", "home", "Julie's house") in stored_profiles(conn)


def test_listing_failure_skips_a_discovering_account_but_not_a_listed_one(
    nextdns, db_path, conn, caplog
):
    env = discovery_env(nextdns, db_path)
    env["NEXTDNS_HOME_PROFILES"] = "def456"
    nextdns.profiles_failure = 404

    with caplog.at_level(logging.WARNING):
        assert collector.main(env) == 2

    # home still collects its list, without names; family has nothing to go on.
    assert query_counts(conn) == [("home", "def456", 3)]
    assert stored_profiles(conn) == [("def456", "home", None)]
    assert "family: could not list profiles" in caplog.text


def test_a_listed_profile_the_key_cannot_see_is_warned_about(
    nextdns, db_path, conn, caplog
):
    env = discovery_env(nextdns, db_path)
    env["NEXTDNS_HOME_PROFILES"] = "def456,gone99"

    with caplog.at_level(logging.WARNING):
        assert collector.main(env) == 2

    assert "home/gone99: listed in NEXTDNS_HOME_PROFILES but not visible" in (
        caplog.text
    )
    assert ("home", "def456", 3) in query_counts(conn)
