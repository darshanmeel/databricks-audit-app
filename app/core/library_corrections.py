"""app/core/library_corrections.py

Loader and validator for config/library_corrections.yml -- the library-corrections register
(DEC-66.2, T-75A): OUR OWN written-down account of every known defect in a query we vendor from
the reference library (app/queries/vendored/**). DEC-66.2's actual rule (review round 1 must_fix
1 -- this docstring previously stated the OPPOSITE rule and has been corrected): every vendored
query is OUR OWN copy, and when one is wrong we fix the SQL IN PLACE, in our copy -- there is no
"never edit it" prohibition, and config/vendored.lock records our edited copies, it does not pin
against editing them. What this register exists for is disclosure: every known defect is listed
here with what it does to the verdict or numbers, and either how our copy was fixed (status
"fixed", with the fix in `fix`) or where the fix is planned (status "not_fixed", with the plan in
`fix`) -- so a defect is never silently absorbed into a finding a reader trusts at face value. This
module is the one place that loads and validates that register; app/api/service.py attaches its
entries to the API responses (a "library_corrections" field on GET /api/findings rows, GET
/api/finding/{id}, and the full register on GET /api/coverage), and app/web renders a short
"Library issue" / "Corrected" line whichever entry point uses it, with the full entry one click
away (finding_detail.jsx / findings_table.jsx / the Coverage tab's browsable list).

Repo-owned: read from ROOT/config, never from AUDIT_CONFIG_DIR. AUDIT_CONFIG_DIR (DEC-27) is a
per-install, user-editable settings directory (settings.yml/thresholds.yml/tag_aliases.yml);
config/library_corrections.yml is version-controlled, developer-authored data ABOUT the vendored
library itself, the same "ships with the repo, not the install" status config/vendored.lock
already has -- tests/conftest.py's AUDIT_CONFIG_DIR override therefore has no effect on this
module, by design.

    load_corrections() -> list[Correction]         every entry, validated (raises
                                                     LibraryCorrectionsError on any problem -- an
                                                     unknown or non-vendored query_id, a duplicate
                                                     id, a bad status, a missing field -- fail
                                                     loudly, never a silent skip, the same
                                                     discipline app/core/registry.py's RegistryError
                                                     uses for the query registry itself). []
                                                     (never an error) when the file does not exist
                                                     at all -- unlike settings.yml/thresholds.yml
                                                     this file is never auto-seeded, since it is
                                                     version-controlled developer data, not a
                                                     per-install runtime setting.
    by_query_id() -> dict[str, list[Correction]]    load_corrections() grouped by query_id -- what
                                                     the API attaches per finding row. A query_id
                                                     with no entries is simply absent; callers use
                                                     .get(query_id, []).

docs/LIBRARY_CORRECTIONS.md (tools/build_library_corrections.py) is generated straight from this
module's load_corrections(); tests/test_library_corrections.py fails when the two drift.

Stdlib + pyyaml, then app/core/registry (query_id + origin validation only -- this module never
touches the vendored .sql bodies themselves).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core import registry

ROOT = Path(__file__).resolve().parent.parent.parent
_PATH = ROOT / "config" / "library_corrections.yml"

STATUS_VALUES = {"fixed", "not_fixed"}
_REQUIRED_FIELDS = ("id", "query_id", "problem", "effect", "status", "fix")
_OPTIONAL_FIELDS = ("item",)


class LibraryCorrectionsError(ValueError):
    """config/library_corrections.yml failed validation: an unknown or non-vendored query_id, a
    duplicate correction id, a bad status, a missing/blank required field, or an unknown field.
    Raised at load time -- never swallowed. A register entry that silently fails to load is worse
    than none: the whole point of T-75A is that a listed defect is actually shown to users."""


@dataclass(frozen=True)
class Correction:
    id: str
    query_id: str
    problem: str          # plain-words description of what is wrong in the vendored SQL
    effect: str            # effect on the verdict/numbers a reader of that finding sees
    status: str             # "fixed" | "not_fixed"
    fix: str                  # the fix (status == "fixed") or where it is planned (status == "not_fixed")
    item: str | None          # a task id this is/will be fixed under (e.g. "T-73"), or None
    #                            when status == "not_fixed" and nothing is planned yet.


def _validate(raw: object) -> list[Correction]:
    if not isinstance(raw, dict) or "entries" not in raw:
        raise LibraryCorrectionsError(
            "library_corrections.yml must be a mapping with a top-level 'entries' list"
        )
    entries = raw["entries"]
    if not isinstance(entries, list):
        raise LibraryCorrectionsError("library_corrections.yml['entries'] must be a list")

    origin_by_id: dict[str, str] = {s.query_id: s.origin for s in registry.load_registry()}

    seen_ids: set[str] = set()
    out: list[Correction] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise LibraryCorrectionsError(f"entries[{i}] must be a mapping")

        missing = [f for f in _REQUIRED_FIELDS if f not in entry]
        if missing:
            raise LibraryCorrectionsError(f"entries[{i}] is missing required field(s): {missing}")
        unknown = set(entry) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_FIELDS)
        if unknown:
            raise LibraryCorrectionsError(f"entries[{i}] has unknown field(s): {sorted(unknown)}")

        cid = entry["id"]
        if not isinstance(cid, str) or not cid.strip():
            raise LibraryCorrectionsError(f"entries[{i}]['id'] must be a non-empty string")
        if cid in seen_ids:
            raise LibraryCorrectionsError(f"duplicate correction id: {cid!r}")
        seen_ids.add(cid)

        qid = entry["query_id"]
        if not isinstance(qid, str) or not qid.strip():
            raise LibraryCorrectionsError(f"{cid}: 'query_id' must be a non-empty string")
        if qid not in origin_by_id:
            raise LibraryCorrectionsError(
                f"{cid}: query_id {qid!r} is not in app.core.registry (unknown query id)"
            )
        if origin_by_id[qid] != "vendored":
            raise LibraryCorrectionsError(
                f"{cid}: query_id {qid!r} has origin {origin_by_id[qid]!r}, not 'vendored' -- "
                "the library-corrections register is only for OUR COPY of the reference library "
                "(DEC-66.2); an app-owned query's bug is fixed directly, with no register entry"
            )

        status = entry["status"]
        if status not in STATUS_VALUES:
            raise LibraryCorrectionsError(
                f"{cid}: status must be one of {sorted(STATUS_VALUES)}, got {status!r}"
            )

        for field_name in ("problem", "effect", "fix"):
            value = entry[field_name]
            if not isinstance(value, str) or not value.strip():
                raise LibraryCorrectionsError(f"{cid}: {field_name!r} must be a non-empty string")

        item = entry.get("item")
        if item is not None and (not isinstance(item, str) or not item.strip()):
            raise LibraryCorrectionsError(f"{cid}: 'item' must be a non-empty string or absent")

        out.append(
            Correction(
                id=cid, query_id=qid, problem=entry["problem"], effect=entry["effect"],
                status=status, fix=entry["fix"], item=item,
            )
        )
    return out


# Cached on the file's (mtime_ns, size) -- the same cheap-stat discipline app/core/registry.py's
# _cache_key uses. GET /api/findings calls by_query_id() once per request (across ~100 rows), so
# re-parsing + re-validating the YAML (which itself calls registry.load_registry(), already
# separately cached) on every row would be wasteful.
_CACHE: tuple[tuple, list[Correction]] | None = None


def _cache_key() -> tuple:
    try:
        st = _PATH.stat()
        return (str(_PATH), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(_PATH), None, None)


def load_corrections() -> list[Correction]:
    """Every entry in config/library_corrections.yml, validated (LibraryCorrectionsError on any
    problem). [] when the file does not exist -- a fresh checkout, or a test that intentionally
    points _PATH elsewhere -- never auto-seeded (unlike settings.yml/thresholds.yml), since this
    is version-controlled, developer-authored data, not a per-install runtime setting."""
    global _CACHE
    key = _cache_key()
    if _CACHE is not None and _CACHE[0] == key:
        return list(_CACHE[1])
    if not _PATH.exists():
        _CACHE = (key, [])
        return []
    raw = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
    corrections = _validate(raw)
    _CACHE = (key, corrections)
    return list(corrections)


def by_query_id() -> dict[str, list[Correction]]:
    """load_corrections() grouped by query_id -- what the API attaches per finding row. A
    query_id with no entries is simply absent (never an empty-list placeholder); callers use
    .get(query_id, [])."""
    out: dict[str, list[Correction]] = {}
    for c in load_corrections():
        out.setdefault(c.query_id, []).append(c)
    return out
