import json
import logging
from datetime import timedelta, timezone

import pytest

from processor import process as processor
from processor.process import Config, ConfigError, classify, rebuild
from sverdrup.groups import Group, GroupConfigError, parse_groups
from tests.nextdns_mock import utc
from tests.test_processor import GAP, insert_queries

# The live household: HapLan is every profile except two other households and
# the calibration profile.
PROFILES = {
    "4c6947": "Stephen",
    "cfb9e1": "Kids",
    "ce2122": "Julie",
    "887843": "Cumberland",
    "69aa36": "calibration",
}
HAPLAN_ENV = {
    "SVERDRUP_GROUPS": "HapLan",
    "SVERDRUP_GROUP_HAPLAN_EXCLUDE": "ce2122,887843,69aa36",
}
HAPLAN_BY_NAME_ENV = {
    "SVERDRUP_GROUPS": "HapLan",
    "SVERDRUP_GROUP_HAPLAN_EXCLUDE": "Julie,Cumberland,calibration",
}


# --- configuration ----------------------------------------------------------


def test_no_groups_configured():
    assert parse_groups({}) == ()
    assert parse_groups({"SVERDRUP_GROUPS": " "}) == ()


def test_parse_exclusion_and_explicit_groups_in_order():
    groups = parse_groups(
        {
            "SVERDRUP_GROUPS": "HapLan, Kids",
            "SVERDRUP_GROUP_HAPLAN_EXCLUDE": "Julie, Cumberland,calibration",
            "SVERDRUP_GROUP_KIDS_PROFILES": "cfb9e1",
        }
    )
    assert groups == (
        Group("HapLan", True, ("Julie", "Cumberland", "calibration")),
        Group("Kids", False, ("cfb9e1",)),
    )
    assert [g.rule for g in groups] == ["exclude", "include"]


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({"SVERDRUP_GROUPS": "HapLan"}, "SVERDRUP_GROUP_HAPLAN_PROFILES"),
        (
            {
                "SVERDRUP_GROUPS": "HapLan",
                "SVERDRUP_GROUP_HAPLAN_PROFILES": "a",
                "SVERDRUP_GROUP_HAPLAN_EXCLUDE": "b",
            },
            "both set",
        ),
        ({"SVERDRUP_GROUPS": "1st"}, "must start with a letter"),
        ({"SVERDRUP_GROUPS": "All"}, "reserved"),
        ({"SVERDRUP_GROUPS": "a,,b"}, "empty name"),
        (
            {
                "SVERDRUP_GROUPS": "HapLan,haplan",
                "SVERDRUP_GROUP_HAPLAN_PROFILES": "a",
            },
            "more than once",
        ),
        (
            {"SVERDRUP_GROUPS": "Kids", "SVERDRUP_GROUP_KIDS_PROFILES": "a,,b"},
            "SVERDRUP_GROUP_KIDS_PROFILES has an empty entry",
        ),
    ],
)
def test_invalid_group_config_names_the_problem(env, fragment):
    with pytest.raises(GroupConfigError) as err:
        parse_groups(env)
    assert any(fragment in problem for problem in err.value.problems)


def test_every_problem_is_reported_at_once():
    with pytest.raises(GroupConfigError) as err:
        parse_groups({"SVERDRUP_GROUPS": "A,B"})
    assert len(err.value.problems) == 2


def test_processor_config_reads_groups_and_rejects_bad_ones():
    assert Config.from_env(HAPLAN_ENV).groups[0].name == "HapLan"
    with pytest.raises(ConfigError, match="SVERDRUP_GROUP_HAPLAN"):
        Config.from_env({"SVERDRUP_GROUPS": "HapLan"})


# --- resolution -------------------------------------------------------------


def test_exclusion_group_is_every_profile_but_the_excluded():
    (haplan,) = parse_groups(HAPLAN_ENV)
    assert haplan.resolve(PROFILES) == (["4c6947", "cfb9e1"], [], [])


def test_exclusion_group_takes_in_profiles_that_appear_later():
    (haplan,) = parse_groups(HAPLAN_ENV)
    later = {**PROFILES, "a1b2c3": "Guest"}
    assert haplan.resolve(later)[0] == ["4c6947", "a1b2c3", "cfb9e1"]


def test_entries_match_ids_or_names_ignoring_case_and_report_unmatched():
    group = Group("Mixed", False, ("4C6947", "kids", "ce2122", "Nobody"))
    # IDs match exactly; names match without regard to case.
    assert group.resolve(PROFILES) == (
        ["ce2122", "cfb9e1"],
        ["4C6947", "Nobody"],
        [],
    )


def test_profile_without_a_name_matches_by_id_only():
    group = Group("G", True, ("calibration",))
    assert group.resolve({"69aa36": None, "4c6947": "Stephen"}) == (
        ["4c6947", "69aa36"],
        ["calibration"],
        ["69aa36"],
    )


def test_unnamed_profiles_only_matter_to_exclusions_by_name():
    profiles = {**PROFILES, "a1b2c3": None}
    (by_id,) = parse_groups(HAPLAN_ENV)
    (by_name,) = parse_groups(HAPLAN_BY_NAME_ENV)
    assert by_id.resolve(profiles).unnamed == []
    assert by_name.resolve(profiles).unnamed == ["a1b2c3"]


# --- per-group aggregation ---------------------------------------------------


def add_profiles(conn, profiles=PROFILES, account="stephen"):
    with conn:
        conn.executemany(
            "INSERT INTO profiles (profile_id, account, name, updated_at)"
            " VALUES (?, ?, ?, '2026-03-01T00:00:00.000Z')",
            [(pid, account, name) for pid, name in profiles.items()],
        )


def watch(start, minutes, profile, device="tv", account="stephen", every=5):
    return [
        (start + timedelta(minutes=m), "netflix.com", device, profile, account)
        for m in range(0, minutes + 1, every)
    ]


def group_sessions(conn, group="HapLan"):
    return conn.execute(
        "SELECT profile_ids, start_time, end_time, session_length_minutes"
        " FROM group_sessions WHERE group_name = ? ORDER BY start_time",
        (group,),
    ).fetchall()


def group_metrics(conn, group="HapLan"):
    return conn.execute(
        "SELECT date, total_minutes, session_count, last_seen FROM group_metrics"
        " WHERE group_name = ? ORDER BY date",
        (group,),
    ).fetchall()


def process(conn, groups, tz=timezone.utc):
    classify(conn)
    return rebuild(conn, GAP, tz, parse_groups(groups))


def test_concurrent_viewing_in_two_member_profiles_counts_once(conn):
    add_profiles(conn)
    start = utc(2026, 3, 1, 20, 0)
    # Two HapLan profiles watch Netflix at the same time, 20:00-21:00 and
    # 20:30-21:30: one group session of 90 minutes, not 120.
    insert_queries(
        conn,
        watch(start, 60, "4c6947")
        + watch(start + timedelta(minutes=30), 60, "cfb9e1", device="phone"),
    )

    result = process(conn, HAPLAN_ENV)

    assert (result.group_sessions, result.group_metrics) == (1, 1)
    ((profiles, begin, end, minutes),) = group_sessions(conn)
    assert json.loads(profiles) == ["4c6947", "cfb9e1"]
    assert (begin, end, minutes) == (
        "2026-03-01T20:00:00.000Z",
        "2026-03-01T21:30:00.000Z",
        90.0,
    )
    assert group_metrics(conn) == [("2026-03-01", 90.0, 1, "2026-03-01T21:30:00.000Z")]


def test_excluded_profiles_never_reach_the_group(conn):
    add_profiles(conn)
    start = utc(2026, 3, 1, 20, 0)
    # Julie's viewing bridges two short HapLan sessions in the account-wide
    # household track, but not in HapLan's.
    insert_queries(
        conn,
        watch(start, 10, "4c6947")
        + watch(start + timedelta(minutes=10), 60, "ce2122")
        + watch(start + timedelta(minutes=60), 10, "4c6947")
        + watch(start, 120, "69aa36", device="laptop"),
    )

    process(conn, HAPLAN_ENV)

    assert [row[1:] for row in group_sessions(conn)] == [
        ("2026-03-01T20:00:00.000Z", "2026-03-01T20:10:00.000Z", 10.0),
        ("2026-03-01T21:00:00.000Z", "2026-03-01T21:10:00.000Z", 10.0),
    ]
    assert group_metrics(conn) == [("2026-03-01", 20.0, 2, "2026-03-01T21:10:00.000Z")]
    # The household (all profiles) view still has everything, as one session.
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (1,)


def test_group_merges_its_profiles_across_accounts(conn):
    add_profiles(conn, {"4c6947": "Stephen"}, account="stephen")
    add_profiles(conn, {"fed321": "Den"}, account="family")
    start = utc(2026, 3, 1, 20, 0)
    insert_queries(
        conn,
        watch(start, 30, "4c6947")
        + watch(start + timedelta(minutes=20), 30, "fed321", account="family"),
    )

    process(
        conn,
        {"SVERDRUP_GROUPS": "Both", "SVERDRUP_GROUP_BOTH_PROFILES": "Stephen,Den"},
    )

    assert [row[3] for row in group_sessions(conn, "Both")] == [50.0]
    # The household view keeps accounts apart: one session in each.
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (2,)


def test_group_metrics_split_at_local_midnight_and_count_on_start_day(conn):
    add_profiles(conn)
    # 23:45-00:15 New York time (EST, UTC-5).
    insert_queries(conn, watch(utc(2026, 3, 2, 4, 45), 30, "4c6947"))

    process(conn, HAPLAN_ENV, tz=processor.load_tz("America/New_York"))

    assert group_metrics(conn) == [
        ("2026-03-01", 15.0, 1, "2026-03-02T04:55:00.000Z"),
        ("2026-03-02", 15.0, 0, "2026-03-02T05:15:00.000Z"),
    ]


def test_group_definitions_and_members_are_stored(conn):
    add_profiles(conn)
    process(
        conn,
        {
            **HAPLAN_ENV,
            "SVERDRUP_GROUPS": "HapLan,Others",
            "SVERDRUP_GROUP_OTHERS_PROFILES": "Julie,Cumberland",
        },
    )

    assert conn.execute(
        "SELECT name, position, rule, entries FROM profile_groups ORDER BY position"
    ).fetchall() == [
        ("HapLan", 0, "exclude", '["ce2122", "887843", "69aa36"]'),
        ("Others", 1, "include", '["Julie", "Cumberland"]'),
    ]
    assert conn.execute(
        "SELECT group_name, profile_id FROM group_members ORDER BY 1, 2"
    ).fetchall() == [
        ("HapLan", "4c6947"),
        ("HapLan", "cfb9e1"),
        ("Others", "887843"),
        ("Others", "ce2122"),
    ]


def test_profiles_known_only_from_queries_join_exclusion_groups(conn):
    # A profile with queries but no profiles row has no recorded name; an
    # exclusion by ID still takes it in.
    add_profiles(conn, {pid: PROFILES[pid] for pid in ("ce2122", "887843", "69aa36")})
    insert_queries(conn, watch(utc(2026, 3, 1, 20, 0), 10, "4c6947"))

    process(conn, HAPLAN_ENV)

    assert conn.execute("SELECT profile_id FROM group_members").fetchall() == [
        ("4c6947",)
    ]


def test_removing_a_group_clears_its_rows_and_unmatched_entries_warn(conn, caplog):
    add_profiles(conn)
    insert_queries(conn, watch(utc(2026, 3, 1, 20, 0), 10, "4c6947"))
    process(conn, HAPLAN_ENV)

    with caplog.at_level(logging.WARNING, logger="sverdrup.processor"):
        process(
            conn,
            {
                "SVERDRUP_GROUPS": "Typo",
                "SVERDRUP_GROUP_TYPO_PROFILES": "Stephen,Juile",
            },
        )

    assert "'Juile'" in caplog.text
    assert conn.execute("SELECT DISTINCT group_name FROM group_metrics").fetchall() == [
        ("Typo",)
    ]
    assert conn.execute("SELECT name FROM profile_groups").fetchall() == [("Typo",)]


def test_main_rejects_bad_group_config(conn, db_path):
    assert processor.main({"DB_PATH": db_path, "SVERDRUP_GROUPS": "HapLan"}) == 1
    assert processor.main({"DB_PATH": db_path, **HAPLAN_ENV}) == 0


def test_excluded_hosts_never_make_group_sessions(conn):
    add_profiles(conn)
    # An idle Google tab refreshing accounts.youtube.com every 10 minutes.
    insert_queries(
        conn,
        [
            (utc(2026, 3, 1, 20, m), "accounts.youtube.com", "laptop", "4c6947")
            for m in range(0, 60, 10)
        ],
    )

    process(conn, HAPLAN_ENV)

    assert group_sessions(conn) == []
    assert group_metrics(conn) == []


# --- exclusion groups fail closed --------------------------------------------


def group_rows(conn, group="HapLan"):
    members = conn.execute(
        "SELECT profile_id FROM group_members WHERE group_name = ? ORDER BY 1",
        (group,),
    ).fetchall()
    return group_sessions(conn, group), group_metrics(conn, group), members


def two_groups_by_name():
    return {
        **HAPLAN_BY_NAME_ENV,
        "SVERDRUP_GROUPS": "HapLan,Kids",
        "SVERDRUP_GROUP_KIDS_PROFILES": "cfb9e1",
    }


def test_renamed_excluded_profile_keeps_the_group_from_rebuilding(conn, caplog):
    add_profiles(conn)
    start = utc(2026, 3, 1, 20, 0)
    insert_queries(conn, watch(start, 30, "4c6947") + watch(start, 30, "cfb9e1"))
    process(conn, two_groups_by_name())
    before = group_rows(conn)

    # Julie's profile is renamed in NextDNS, then she watches for two hours.
    with conn:
        conn.execute(
            "UPDATE profiles SET name = 'Julie iPad' WHERE profile_id = 'ce2122'"
        )
    later = utc(2026, 3, 2, 20, 0)
    insert_queries(conn, watch(later, 120, "ce2122") + watch(later, 20, "cfb9e1"))
    with caplog.at_level(logging.ERROR, logger="sverdrup.processor"):
        result = process(conn, two_groups_by_name())

    assert "group HapLan: not rebuilt" in caplog.text
    assert "'Julie'" in caplog.text
    assert group_rows(conn) == before
    # Other groups and the per-profile metrics still pick up the new viewing.
    assert [row[0] for row in group_metrics(conn, "Kids")] == [
        "2026-03-01",
        "2026-03-02",
    ]
    assert result.group_sessions == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM metrics WHERE profile_id = 'ce2122'"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT name FROM profile_groups ORDER BY position"
    ).fetchall() == [
        ("HapLan",),
        ("Kids",),
    ]


def test_unnamed_profile_keeps_a_by_name_exclusion_from_rebuilding(conn, caplog):
    add_profiles(conn)
    start = utc(2026, 3, 1, 20, 0)
    insert_queries(conn, watch(start, 30, "4c6947"))
    process(conn, HAPLAN_BY_NAME_ENV)
    before = group_rows(conn)

    # A new profile's queries arrive before the collector stored its name: it
    # may be one HapLan excludes.
    insert_queries(conn, watch(start + timedelta(minutes=20), 60, "a1b2c3"))
    with caplog.at_level(logging.ERROR, logger="sverdrup.processor"):
        process(conn, HAPLAN_BY_NAME_ENV)

    assert "profile a1b2c3 has no recorded name" in caplog.text
    assert group_rows(conn) == before

    # Once the name is recorded, the group is rebuilt.
    add_profiles(conn, {"a1b2c3": "Guest"})
    process(conn, HAPLAN_BY_NAME_ENV)
    assert group_rows(conn)[2] == [("4c6947",), ("a1b2c3",), ("cfb9e1",)]
