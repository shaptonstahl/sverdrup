import json

import pytest

from sverdrup.db import init_db
from sverdrup.services import SERVICES, DomainMatcher, Service, matcher_by_code


@pytest.mark.parametrize(
    "domain, expected",
    [
        ("netflix.com", "netflix"),
        ("www.netflix.com", "netflix"),
        ("ipv4-c001-sea001-ix.1.oca.nflxvideo.net", "netflix"),
        ("NETFLIX.COM.", "netflix"),
        ("audio-sp-ash.pscdn.co", "spotify"),
        ("rr3---sn-abc.googlevideo.com", "youtube"),
        ("tv.apple.com", "apple_tv_plus"),
        ("play.hbomax.com", "hbo_max"),
        ("www.paramountplus.com", "paramount_plus"),
        ("disney.api.edge.bamgrid.com", "disney_plus"),
        ("atv-ps.primevideo.com", "prime_video"),
        # Documented ambiguity: any amazon.com host counts as Prime Video.
        ("www.amazon.com", "prime_video"),
    ],
)
def test_matches_service_domains_and_subdomains(domain, expected):
    assert matcher_by_code().match(domain) == expected


@pytest.mark.parametrize(
    "domain",
    [
        "notnetflix.com",  # suffix match is on whole labels only
        "netflix.com.evil.example",
        "ocsp.apple.com",  # Apple certificate checks are not viewing
        "apple.com",
        "com",
        "example.org",
        "",
    ],
)
def test_rejects_non_service_domains(domain):
    assert matcher_by_code().match(domain) is None


def test_most_specific_domain_wins():
    matcher = DomainMatcher({"apple.com": "apple", "tv.apple.com": "apple_tv"})
    assert matcher.match("x.tv.apple.com") == "apple_tv"
    assert matcher.match("www.apple.com") == "apple"


def test_seed_list_domains_are_unique_and_normalized():
    domains = [d for s in SERVICES for d in s.domains]
    assert len(domains) == len(set(domains))
    assert all(d == d.lower().strip(".") for d in domains)


def test_init_db_syncs_services_table(conn):
    rows = conn.execute(
        "SELECT service_code, service_name, domains FROM services"
    ).fetchall()
    assert {code for code, _, _ in rows} == {s.code for s in SERVICES}
    netflix = next(r for r in rows if r[0] == "netflix")
    assert "nflxvideo.net" in json.loads(netflix[2])

    ids_before = dict(conn.execute("SELECT service_code, id FROM services"))
    init_db(conn, SERVICES[:2] + (Service("netflix", "Netflix", ("x.example",)),))
    ids_after = dict(conn.execute("SELECT service_code, id FROM services"))
    assert set(ids_after) == {"netflix", "spotify"}
    assert ids_after["netflix"] == ids_before["netflix"]
