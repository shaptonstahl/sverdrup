"""Streaming-service seed list and DNS domain matching.

A service matches a queried domain when one of its domains equals the
queried domain or is a parent of it (``nflxvideo.net`` matches
``ipv4-c001.nflxvideo.net``). Matching is on whole DNS labels, so
``notnetflix.com`` does not match ``netflix.com``. When domains from two
services overlap, the longest (most specific) one wins.

This list is the source of truth; ``sverdrup.db.init_db`` mirrors it into the
``services`` table. Keep it in sync with the domain mappings in
ARCHITECTURE.md.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    """A streaming service and the DNS domains that identify it."""

    code: str
    name: str
    domains: tuple[str, ...]


SERVICES: tuple[Service, ...] = (
    Service(
        "netflix",
        "Netflix",
        (
            "netflix.com",
            "netflix.net",
            "nflxvideo.net",
            "nflximg.net",
            "nflxext.com",
            "nflxso.net",
        ),
    ),
    Service(
        "spotify",
        "Spotify",
        ("spotify.com", "scdn.co", "pscdn.co", "spotifycdn.com", "spotifycdn.net"),
    ),
    Service(
        "disney_plus",
        "Disney+",
        ("disneyplus.com", "disney-plus.net", "bamgrid.com", "dssott.com"),
    ),
    Service(
        "youtube",
        "YouTube",
        ("youtube.com", "youtu.be", "googlevideo.com", "ytimg.com"),
    ),
    # Only the TV app's own hosts: generic apple.com traffic such as
    # ocsp.apple.com certificate checks must not count as viewing.
    Service("apple_tv_plus", "Apple TV+", ("tv.apple.com",)),
    Service("hbo_max", "HBO Max", ("hbomax.com", "max.com")),
    Service("paramount_plus", "Paramount+", ("paramountplus.com", "cbsaavideo.com")),
    # amazon.com is ambiguous (shopping, Alexa devices, video) and will
    # overcount Prime Video use. Accepted for now; see ARCHITECTURE.md.
    Service(
        "prime_video",
        "Amazon Prime Video",
        ("primevideo.com", "aiv-cdn.net", "aiv-delivery.net", "amazon.com"),
    ),
)


def normalize_domain(domain: str) -> str:
    """Lowercase a domain and strip surrounding whitespace and the root dot."""
    return domain.strip().lower().rstrip(".")


class DomainMatcher:
    """Map queried domains to a service key using whole-label suffix matching."""

    def __init__(self, domain_to_key: dict[str, object]) -> None:
        self._lookup = {normalize_domain(d): key for d, key in domain_to_key.items()}

    def match(self, domain: str) -> object | None:
        """Return the key for the most specific matching domain, or None."""
        labels = normalize_domain(domain).split(".")
        for i in range(len(labels)):
            key = self._lookup.get(".".join(labels[i:]))
            if key is not None:
                return key
        return None


def matcher_by_code(services: tuple[Service, ...] = SERVICES) -> DomainMatcher:
    """Build a matcher that returns service codes."""
    return DomainMatcher({d: s.code for s in services for d in s.domains})
