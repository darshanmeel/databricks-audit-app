"""app/core/databricks_docs.py -- loader + validator for config/databricks_docs.yml: the lookup
table from a system-table or "concept" key to its official Databricks documentation URL, per
cloud. Never imports tools/; its one cross-module dependency is app.core.registry (query_id
validation only)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.core import registry

ROOT = Path(__file__).resolve().parent.parent.parent
_PATH = ROOT / "config" / "databricks_docs.yml"

CLOUDS = ("aws", "azure", "gcp")
_HOSTS = ("https://docs.databricks.com/", "https://learn.microsoft.com/")
_MAX_TEXT_LEN = 120

_TABLE_KEY_RE = re.compile(r"^system\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_CONCEPT_KEY_RE = re.compile(r"^concept\.[a-z][a-z0-9_]*$")

# The app's own 7 registry domains (tools/header_schema.py DOMAINS, duplicated here by value, not
# by import -- app/core never imports tools/, the same discipline app/core/registry.py's own
# _HEADER_FIELD_NAMES comment documents for that module).
KNOWN_AREAS = frozenset({
    "cost", "performance", "compute", "jobs_pipelines", "serving_ai", "storage", "governance_access",
})

_TABLE_REQUIRED = {"key", "title", "aws", "azure", "gcp", "anchor", "verified"}
_TABLE_OPTIONAL = {"what", "enable", "note", "guide_sections"}
_CONCEPT_REQUIRED = {"key", "title", "aws", "azure", "gcp", "anchor", "verified"}
_CONCEPT_OPTIONAL = {"what", "enable", "note", "areas", "queries", "guide_sections"}


class DocsError(ValueError):
    """config/databricks_docs.yml failed validation. Raised at load time, never swallowed."""


@dataclass(frozen=True)
class DocsEntry:
    key: str
    kind: str                       # "table" | "concept"
    title: str | None
    aws: str | None
    azure: str | None
    gcp: str | None
    anchor: str | None
    verified: object                # True | False | list[str] (a subset of CLOUDS)
    what: str | None = None
    enable: str | None = None
    note: str | None = None
    guide_sections: tuple = field(default_factory=tuple)
    areas: tuple = field(default_factory=tuple)
    queries: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class DocsMap:
    version: int
    default_cloud: str
    default_enable: str
    entries: dict            # key -> DocsEntry, insertion order: every table, then every concept

    def tables(self) -> list[DocsEntry]:
        return [e for e in self.entries.values() if e.kind == "table"]

    def concepts(self) -> list[DocsEntry]:
        return [e for e in self.entries.values() if e.kind == "concept"]


def _cloud_verified(entry: DocsEntry, cloud: str) -> bool:
    v = entry.verified
    if v is True:
        return True
    if v is False:
        return False
    return cloud in v  # a list -- only these clouds were opened


def _full_url(url: str | None, anchor: str | None) -> str | None:
    if not url:
        return None
    return f"{url}#{anchor}" if anchor else url


def entry_urls(entry: DocsEntry) -> dict:
    """Resolved for all three clouds at once: cloud C's own URL when C is verified; otherwise the
    AWS URL (marked aws_fallback=True) when AWS is verified; otherwise None."""
    aws_ok = _cloud_verified(entry, "aws") and bool(entry.aws)
    aws_url = _full_url(entry.aws, entry.anchor) if aws_ok else None
    out: dict = {}
    for cloud in CLOUDS:
        own = getattr(entry, cloud)
        if _cloud_verified(entry, cloud) and own:
            out[cloud] = {"url": _full_url(own, entry.anchor), "aws_fallback": False}
        elif aws_url:
            out[cloud] = {"url": aws_url, "aws_fallback": True}
        else:
            out[cloud] = None
    return out


def resolve_cloud(meta: dict | None, default_cloud: str = "aws") -> tuple[str, str]:
    """meta.metastore.cloud, lower-cased; anything not aws/azure/gcp counts as unknown. Returns
    (cloud, source) -- source is "snapshot" when the manifest named a real cloud, else "default"."""
    metastore = (meta or {}).get("metastore") or {}
    cloud = str(metastore.get("cloud") or "").strip().lower()
    if cloud in CLOUDS:
        return cloud, "snapshot"
    return default_cloud, "default"


def _check_url(key: str, cloud: str, url) -> None:
    if url is None:
        return
    if not isinstance(url, str) or not url.startswith(_HOSTS):
        raise DocsError(
            f"{key}: {cloud} url must be https on docs.databricks.com or learn.microsoft.com, "
            f"got {url!r}"
        )


def _check_len(key: str, field_name: str, value) -> None:
    if value is not None and len(value) > _MAX_TEXT_LEN:
        raise DocsError(f"{key}: {field_name!r} is {len(value)} characters, over the {_MAX_TEXT_LEN} limit")


def _check_verified(key: str, verified) -> None:
    if verified is True or verified is False:
        return
    if isinstance(verified, list) and all(isinstance(c, str) and c in CLOUDS for c in verified):
        return
    raise DocsError(f"{key}: 'verified' must be true, false, or a list drawn from {CLOUDS}, got {verified!r}")


def _parse_entry(raw: object, kind: str, valid_slugs: frozenset) -> DocsEntry:
    if not isinstance(raw, dict):
        raise DocsError(f"a {kind} entry must be a mapping, got {raw!r}")
    required = _TABLE_REQUIRED if kind == "table" else _CONCEPT_REQUIRED
    optional = _TABLE_OPTIONAL if kind == "table" else _CONCEPT_OPTIONAL
    label = raw.get("key", "?")

    unknown = set(raw) - required - optional
    if unknown:
        raise DocsError(f"{label}: unknown field(s) {sorted(unknown)}")
    missing = required - set(raw)
    if missing:
        raise DocsError(f"{label}: missing required field(s) {sorted(missing)}")

    key = raw["key"]
    if not isinstance(key, str):
        raise DocsError(f"a {kind} key must be a string, got {key!r}")
    key_re = _TABLE_KEY_RE if kind == "table" else _CONCEPT_KEY_RE
    if not key_re.match(key):
        shape = "system.<schema>.<table>" if kind == "table" else "concept.<slug>"
        raise DocsError(f"{key!r} is not shaped like {shape!r}")

    _check_verified(key, raw["verified"])
    for cloud in CLOUDS:
        _check_url(key, cloud, raw.get(cloud))
    _check_len(key, "what", raw.get("what"))
    _check_len(key, "enable", raw.get("enable"))

    guide_sections = tuple(raw.get("guide_sections") or [])
    for slug in guide_sections:
        if slug not in valid_slugs:
            raise DocsError(f"{key}: guide_sections names unknown slug {slug!r}")

    areas = tuple(raw.get("areas") or [])
    for area in areas:
        if area not in KNOWN_AREAS:
            raise DocsError(f"{key}: areas names unknown area {area!r}")

    return DocsEntry(
        key=key, kind=kind, title=raw.get("title"),
        aws=raw.get("aws"), azure=raw.get("azure"), gcp=raw.get("gcp"),
        anchor=raw.get("anchor"), verified=raw["verified"],
        what=raw.get("what"), enable=raw.get("enable"), note=raw.get("note"),
        guide_sections=guide_sections, areas=areas, queries=tuple(raw.get("queries") or []),
    )


_TOP_REQUIRED = {"version", "default_cloud", "default_enable", "system_tables", "concepts"}


def parse_docs(raw: object, *, valid_slugs: frozenset, valid_query_ids: frozenset) -> DocsMap:
    """Validates an already-yaml.safe_load()-ed mapping into a DocsMap; separate from load_docs()
    so a test can hand this a deliberately-broken structure without touching the real file."""
    if not isinstance(raw, dict):
        raise DocsError("databricks_docs.yml must be a mapping")
    unknown = set(raw) - _TOP_REQUIRED
    if unknown:
        raise DocsError(f"unknown top-level field(s) {sorted(unknown)}")
    missing = _TOP_REQUIRED - set(raw)
    if missing:
        raise DocsError(f"missing top-level field(s) {sorted(missing)}")
    if raw["default_cloud"] not in CLOUDS:
        raise DocsError(f"default_cloud must be one of {CLOUDS}, got {raw['default_cloud']!r}")

    entries: dict[str, DocsEntry] = {}

    def _add(raw_entry, kind: str) -> None:
        entry = _parse_entry(raw_entry, kind, valid_slugs)
        if entry.key in entries:
            raise DocsError(f"duplicate key: {entry.key!r}")
        entries[entry.key] = entry

    if not isinstance(raw["system_tables"], list):
        raise DocsError("'system_tables' must be a list")
    for t in raw["system_tables"]:
        _add(t, "table")
    if not isinstance(raw["concepts"], list):
        raise DocsError("'concepts' must be a list")
    for c in raw["concepts"]:
        _add(c, "concept")

    for entry in entries.values():
        for qid in entry.queries:
            if qid not in valid_query_ids:
                raise DocsError(f"{entry.key}: queries names unknown query_id {qid!r}")

    return DocsMap(
        version=raw["version"], default_cloud=raw["default_cloud"],
        default_enable=raw["default_enable"], entries=entries,
    )


_CACHE: tuple[tuple, DocsMap] | None = None


def _cache_key() -> tuple:
    try:
        st = _PATH.stat()
        return (str(_PATH), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(_PATH), None, None)


def load_docs(*, valid_slugs: frozenset | None = None, valid_query_ids: frozenset | None = None) -> DocsMap:
    """config/databricks_docs.yml, parsed and validated. Cached on the file's own (mtime_ns, size)
    plus the two validation sets, so a test's own small sets never collide with the real cache."""
    global _CACHE
    slugs = frozenset(valid_slugs) if valid_slugs is not None else frozenset()
    ids = (
        frozenset(valid_query_ids)
        if valid_query_ids is not None
        else frozenset(s.query_id for s in registry.load_registry())
    )
    key = (_cache_key(), slugs, ids)
    if _CACHE is not None and _CACHE[0] == key:
        return _CACHE[1]
    raw = yaml.safe_load(_PATH.read_text(encoding="utf-8"))
    parsed = parse_docs(raw, valid_slugs=slugs, valid_query_ids=ids)
    _CACHE = (key, parsed)
    return parsed
