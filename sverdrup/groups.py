"""Profile groups: named sets of NextDNS profiles, defined in the environment.

A group either lists its profiles (``SVERDRUP_GROUP_<NAME>_PROFILES``) or
takes every known profile except the ones it lists
(``SVERDRUP_GROUP_<NAME>_EXCLUDE``), so an exclusion group keeps covering
profiles that appear later. An entry is a profile ID or a NextDNS profile
name; names match without regard to case. Groups are resolved against the
profiles the database knows on each processor run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_GROUP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# The dashboard and API use "all" for every profile, so no group may take it.
ALL = "all"


class GroupConfigError(Exception):
    """Group configuration is invalid; ``problems`` lists every issue."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Group:
    """A named group: only ``entries``, or every profile except them."""

    name: str
    exclude: bool
    entries: tuple[str, ...]

    @property
    def rule(self) -> str:
        return "exclude" if self.exclude else "include"

    def resolve(
        self, profiles: Mapping[str, str | None]
    ) -> tuple[list[str], list[str]]:
        """Return (member profile IDs, entries that match no profile), sorted.

        ``profiles`` maps each known profile ID to its NextDNS name, or None.
        """
        matched: set[str] = set()
        unmatched: list[str] = []
        for entry in self.entries:
            hits = {
                pid
                for pid, name in profiles.items()
                if entry == pid
                or (name is not None and entry.casefold() == name.casefold())
            }
            if not hits:
                unmatched.append(entry)
            matched |= hits
        members = set(profiles) - matched if self.exclude else matched
        return sorted(members), sorted(unmatched)


def parse_groups(environ: Mapping[str, str]) -> tuple[Group, ...]:
    """Read ``SVERDRUP_GROUPS`` and each group's definition, in listed order.

    Raises GroupConfigError naming every bad variable.
    """
    raw = environ.get("SVERDRUP_GROUPS", "")
    if not raw.strip():
        return ()
    problems: list[str] = []
    groups: list[Group] = []
    seen: set[str] = set()
    for name in (item.strip() for item in raw.split(",")):
        if not name:
            problems.append("SVERDRUP_GROUPS has an empty name; remove the extra comma")
            continue
        if not _GROUP_RE.match(name):
            problems.append(
                f"SVERDRUP_GROUPS: group name {name!r} must start with a letter"
                " and use only letters, digits and underscores"
            )
            continue
        if name.casefold() == ALL:
            problems.append(
                f"SVERDRUP_GROUPS: {name!r} is reserved for every profile; pick"
                " another group name"
            )
            continue
        if name.casefold() in seen:
            problems.append(f"SVERDRUP_GROUPS lists group {name!r} more than once")
            continue
        seen.add(name.casefold())
        prefix = f"SVERDRUP_GROUP_{name.upper()}_"
        include = environ.get(prefix + "PROFILES", "")
        exclude = environ.get(prefix + "EXCLUDE", "")
        if include.strip() and exclude.strip():
            problems.append(
                f"{prefix}PROFILES and {prefix}EXCLUDE are both set; a group"
                " either lists its profiles or excludes some from all of them"
            )
            continue
        if not include.strip() and not exclude.strip():
            problems.append(
                f"group {name!r} has no definition; set {prefix}PROFILES to the"
                f" profiles in it, or {prefix}EXCLUDE to the profiles left out"
            )
            continue
        var = prefix + ("EXCLUDE" if exclude.strip() else "PROFILES")
        entries = [item.strip() for item in (exclude or include).split(",")]
        if any(not entry for entry in entries):
            problems.append(f"{var} has an empty entry; remove the extra comma")
            continue
        groups.append(Group(name, bool(exclude.strip()), tuple(entries)))
    if problems:
        raise GroupConfigError(problems)
    return tuple(groups)
