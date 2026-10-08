"""app/core/registry.py

Turns the vendored (and, later, app-owned) query headers + manifest.json entries into typed
QuerySpec objects. This is the single place that parses a manifest entry and its .sql file into
a structured record -- the generator (T-06), the store (T-25) and the app (T-26+) all import this
module instead of re-deriving any of it.

    load_registry() -> list[QuerySpec]   one entry per vendored + app-owned query
    by_id(query_id) -> QuerySpec         lookup, raises KeyError if not found

Every `sources` pair is validated at load time against the 47-source contract (SOURCE_CONTRACT
below, copied verbatim from tasks/T-03-registry-and-fixture-ddl.md "Inputs" -- see DECISIONS.md
DEC-07/DEC-08 for the two derived counts this module's callers may want to reproduce). Every
`next` token is validated at load time against the combined vendored+app id set. A validation
failure raises RegistryError -- fail loudly, never a silent skip.

app/core is imported by the Streamlit app itself, so this module deliberately does NOT import
anything from tools/ (scripts-only, not a runtime dependency of the app); the small amount of
header-parsing logic it needs (just enough to recover `caveats` and `not_assessed_reasons`, the
two header fields manifest.json does not carry) is reimplemented locally rather than imported
across that boundary.

Stdlib only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
VENDORED_DIR = ROOT / "app" / "queries" / "vendored"
APP_DIR = ROOT / "app" / "queries" / "app"

# The 48-source contract: schema -> the set of table names a query's `reads` entry may name.
# Verbatim from the task's Inputs section (also reproduced in lineage/sources.yml with per-table
# query counts), plus one app-owned addition: storage.table_metrics_history backs 3 app-owned
# storage checks, not a vendored query, so it is added here rather than in the vendored library's
# own sources.yml. Total: 6 + 1 + 3 + 7 + 1 + 19 + 6 + 1 + 2 + 2 = 48.
SOURCE_CONTRACT: dict[str, set[str]] = {
    "access": {
        "audit", "column_lineage", "inbound_network", "outbound_network",
        "table_lineage", "workspaces_latest",
    },
    "ai_gateway": {"usage"},
    "billing": {"attributed_usage", "list_prices", "usage"},
    "compute": {
        "clusters", "instance_events", "instance_pools", "node_timeline",
        "node_types", "warehouse_events", "warehouses",
    },
    "data_classification": {"results"},
    "information_schema": {
        "abac_policy_definitions", "catalog_privileges", "column_masks", "column_tags", "connection_privileges",
        "credential_privileges", "external_location_privileges", "row_filters",
        "schema_privileges", "schema_share_usage", "schema_tags",
        "share_recipient_privileges", "shares", "table_privileges", "table_share_usage",
        "table_tags", "tables", "views", "volume_tags", "volumes",
    },
    "lakeflow": {
        "job_run_timeline", "job_task_run_timeline", "job_tasks", "jobs",
        "pipeline_update_timeline", "pipelines",
    },
    "query": {"history"},
    "serving": {"endpoint_usage", "served_entities"},
    "storage": {"predictive_optimization_operations_history", "table_metrics_history"},
}


class RegistryError(ValueError):
    """A structural problem in the vendored/app query set (a `reads` pair outside the 47-source
    contract, an unresolved `next` token). Raised at load time -- never swallowed."""


# The 14 canonical header fields plus the 2 optional ones (runnable, empty_if), in schema order.
# Mirrors tools/header_schema.py's FIELDS/OPTIONAL_FIELDS by value (not by import -- see the
# module docstring). Used only to re-locate the `caveats:` field's text in a .sql file's header.
_HEADER_FIELD_NAMES = (
    "query_id", "title", "domain", "reads", "requires", "params", "confidence",
    "confidence_note", "read_this", "healthy", "investigate_if", "actions", "next", "caveats",
    "runnable", "empty_if", "not_assessed_reasons",
)
_FIELD_RE = re.compile(r"^--\s*(" + "|".join(_HEADER_FIELD_NAMES) + r")\s*:\s?(.*)$")
# P4-03: "code: words; code2: words2" -- same shape as tools/header_schema.py's own _REASON_RE,
# reimplemented locally for the same reason _parse_caveats is (this module does not import tools/).
_REASON_RE = re.compile(r"^([a-z][a-z0-9_]*)\s*:\s*(.+)$", re.I)


def _raw_header_fields(text: str) -> dict[str, str]:
    """Re-parse the leading `--`-comment header block of a .sql file's text into {field: value},
    handling a continuation line (a `--` line that is not itself a known field start gets appended
    to the field currently in progress) exactly the way tools/header_schema.py's `_raw_fields`
    does. Shared by `_parse_caveats` and `_parse_not_assessed_reasons` -- the two header fields
    manifest.json does not carry, so both are re-read straight from the .sql file."""
    fields: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if not line.startswith("--"):
            break
        m = _FIELD_RE.match(line)
        if m:
            current = m.group(1)
            fields[current] = m.group(2).strip()
        else:
            cont = line[2:].strip()
            if current is not None and cont:
                fields[current] = (fields[current] + " " + cont).strip()
    return fields


def _parse_caveats(text: str) -> str:
    """The `caveats:` field's value (manifest.json lacks it)."""
    return _raw_header_fields(text).get("caveats", "")


def _parse_not_assessed_reasons(text: str) -> dict:
    """P4-03: the `not_assessed_reasons:` field's value (manifest.json lacks it, same reason
    caveats does) -- {} for the vast majority of queries, which carry no such field because they
    carry no `not_assessed_reason` column at all. A malformed entry is dropped rather than raised
    here (tools/lint_headers.py -> header_schema.parse_not_assessed_reasons is where a typo in an
    app-owned query's header fails loudly at commit time; this runtime path stays lenient so one
    bad entry never turns into a 500 for every reader)."""
    value = _raw_header_fields(text).get("not_assessed_reasons", "")
    out: dict[str, str] = {}
    if not value or value.strip().lower().startswith("n/a"):
        return out
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _REASON_RE.match(chunk)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _strip_header(text: str) -> str:
    """The query body: the .sql file's text with the leading `--`-comment header block removed
    (stop at the first non-`--` line) and any trailing `;` stripped -- the same stripping rule
    the public repo's own runner (tools/run_audit.py) and the subaudit's harness.strip_header
    both use (Inputs)."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and lines[i].startswith("--"):
        i += 1
    body = "\n".join(lines[i:]).strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    return body


_SOURCE_RE = re.compile(r"system\.([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)")


def _parse_sources(query_id: str, reads: list[str]) -> list[tuple[str, str]]:
    """Every `system.<schema>.<table>` pair named in `reads` (a `reads` entry starting with
    "none" yields no pairs). A pair that is not a member of the 47-source contract is a registry
    error -- fail loudly, never a silent skip (Inputs)."""
    pairs: list[tuple[str, str]] = []
    for entry in reads:
        if entry.strip().lower().startswith("none"):
            continue
        m = _SOURCE_RE.fullmatch(entry.strip())
        if not m:
            raise RegistryError(
                f"{query_id}: reads entry {entry!r} is neither a 'system.<schema>.<table>' "
                "pair nor a 'none - ...' sentinel"
            )
        schema, table = m.group(1), m.group(2)
        if schema not in SOURCE_CONTRACT or table not in SOURCE_CONTRACT[schema]:
            raise RegistryError(
                f"{query_id}: reads pair ('{schema}', '{table}') is not a member of the "
                "47-source contract"
            )
        pairs.append((schema, table))
    return pairs


# DEC-08: is_finding is true only when `healthy` does not start with "n/a -" AND the body emits
# an "... AS status" column (case-sensitive match on the exact literal every finding query uses;
# verified to reproduce 103 vendored / 42 inventory-by-header / 60 findings / 1 counterexample
# -- cost_premium_serverless_photon, real `healthy:` text but no status band -- against the
# vendored tree as of 2026-09-22).
_STATUS_COL_RE = re.compile(r"AS\s+status\b", re.IGNORECASE)


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    title: str
    domain: str
    tier: str
    stars: bool
    runnable: bool
    empty_if: list
    reads: list
    requires: str
    params: list
    confidence: str
    confidence_note: str
    read_this: str
    healthy: str
    investigate_if: str
    actions: list
    next: list
    sql_url: str
    learn_url: str
    origin: str            # "vendored" | "app"
    path: str               # repo-relative .sql path, e.g. app/queries/vendored/cost/cost_by_job.sql
    sources: list            # list[tuple[schema, table]], every system.<schema>.<table> in `reads`
    executable: bool         # runnable AND reads[0] is not a "none - ..." sentinel
    is_finding: bool         # DEC-08
    windowed: bool           # True iff a `period_days` param drives the query (T-06 loops windows)
    body: str                # header-stripped query text, trailing ';' removed
    caveats: str              # parsed straight from the .sql file (manifest.json lacks it)
    not_assessed_reasons: dict   # P4-03: {code: words} for this query's own not_assessed_reason
                                  # column, parsed straight from the .sql file (manifest.json lacks
                                  # it, same reason caveats does); {} for a query with no such column


def _load_domain_dir(base_dir: Path, origin: str) -> list[QuerySpec]:
    manifest_path = base_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest:
        return []
    specs: list[QuerySpec] = []
    for domain, entries in manifest.items():
        for entry in entries:
            qid = entry["query_id"]
            sql_path = base_dir / domain / f"{qid}.sql"
            text = sql_path.read_text(encoding="utf-8")
            body = _strip_header(text)
            caveats = _parse_caveats(text)
            not_assessed_reasons = _parse_not_assessed_reasons(text)
            reads = entry["reads"]
            sources = _parse_sources(qid, reads)
            executable = bool(entry["runnable"]) and bool(reads) and not (
                reads[0].strip().lower().startswith("none")
            )
            is_finding = (
                not entry["healthy"].strip().lower().startswith("n/a -")
                and bool(_STATUS_COL_RE.search(body))
            )
            windowed = any(p["name"] == "period_days" for p in entry["params"])
            specs.append(
                QuerySpec(
                    query_id=qid,
                    title=entry["title"],
                    domain=entry["domain"],
                    tier=entry["tier"],
                    stars=entry["stars"],
                    runnable=entry["runnable"],
                    empty_if=entry["empty_if"],
                    reads=reads,
                    requires=entry["requires"],
                    params=entry["params"],
                    confidence=entry["confidence"],
                    confidence_note=entry["confidence_note"],
                    read_this=entry["read_this"],
                    healthy=entry["healthy"],
                    investigate_if=entry["investigate_if"],
                    actions=entry["actions"],
                    next=entry["next"],
                    sql_url=entry["sql_url"],
                    learn_url=entry["learn_url"],
                    origin=origin,
                    path=sql_path.relative_to(ROOT).as_posix(),
                    sources=sources,
                    executable=executable,
                    is_finding=is_finding,
                    windowed=windowed,
                    body=body,
                    caveats=caveats,
                    not_assessed_reasons=not_assessed_reasons,
                )
            )
    return specs


def _validate_next(specs: list[QuerySpec]) -> None:
    ids = {s.query_id for s in specs}
    for s in specs:
        for n in s.next:
            target = n["query_id"]
            if target not in ids:
                raise RegistryError(
                    f"{s.query_id}: next-entry target '{target}' does not resolve in the "
                    "combined vendored+app registry"
                )


# Parsing the two manifests plus 116 .sql headers takes ~0.25 s, and the Streamlit pages call
# by_id() once per rendered query, so an uncached registry turned a Domains render into seconds of
# repeated disk I/O. The result is cached against the two manifest files' (mtime_ns, size): a
# regenerated manifest invalidates it, and QuerySpec is frozen, so callers cannot corrupt the
# cache. _CACHE_KEY is deliberately cheap -- two os.stat calls per load_registry().
_CACHE: tuple[tuple, list[QuerySpec]] | None = None


def _cache_key() -> tuple:
    key = []
    for base in (VENDORED_DIR, APP_DIR):
        mp = base / "manifest.json"
        try:
            st = mp.stat()
            key.append((str(mp), st.st_mtime_ns, st.st_size))
        except OSError:
            key.append((str(mp), None, None))
    return tuple(key)


def load_registry() -> list[QuerySpec]:
    """Every vendored query, plus every app-owned query if app/queries/app/manifest.json exists
    and is non-empty. Cached on the manifests' mtime/size (see _CACHE above); the returned list is
    a fresh copy each call, so a caller that sorts or filters in place cannot affect the cache."""
    global _CACHE
    key = _cache_key()
    if _CACHE is not None and _CACHE[0] == key:
        return list(_CACHE[1])
    specs = _load_domain_dir(VENDORED_DIR, "vendored")
    specs += _load_domain_dir(APP_DIR, "app")
    _validate_next(specs)
    _CACHE = (key, specs)
    return list(specs)


def by_id(query_id: str) -> QuerySpec:
    for s in load_registry():
        if s.query_id == query_id:
            return s
    raise KeyError(query_id)
