"""Shared parser for the v2 SQL header schema.

Every query file in queries/<domain>/*.sql opens with a comment header. This module
parses that header into a dict. It is the single source of truth for the header contract:
build_manifest.py and lint_headers.py both import it. See CONTRIBUTING.md for the schema.

Stdlib only — no third-party dependencies.
"""
from __future__ import annotations

import re
from pathlib import Path

# The 14 canonical header fields, in schema order. `tier` rides on the `domain:` line.
FIELDS = [
    "query_id",
    "title",
    "domain",
    "reads",
    "requires",
    "params",
    "confidence",
    "confidence_note",
    "read_this",
    "healthy",
    "investigate_if",
    "actions",
    "next",
    "caveats",
]

# Optional header fields (absent => a default). `runnable: false` marks a COPY-PASTE-ONLY query
# (e.g. an ANALYZE TABLE template against a placeholder table) that must NOT flow into the
# manifest-driven runner. Everything without the flag is runnable (a plain SELECT).
# `not_assessed_reasons` (P4-03) is code -> plain words for every value a query's own
# `not_assessed_reason` column can carry -- the one place those words are written, so app/api and
# app/web never show the raw code (e.g. "no_cluster_recorded") to a reader.
OPTIONAL_FIELDS = ["runnable", "empty_if", "not_assessed_reasons"]

# Controlled vocabulary for the optional `empty_if:` field: the machine-checkable COVERAGE reasons a
# query can return zero rows, so a finding / dashboard can attribute an empty result to a real gap
# (e.g. "usage tracking is off") instead of reading it as "nothing there". Documented in COVERAGE.md.
EMPTY_IF_VOCAB = {
    "schema_not_enabled",      # the system schema is opt-in and may not be enabled on the metastore
    "usage_tracking_off",      # serving / AI-gateway usage tracking not turned on for the endpoint
    "preview_unavailable",     # Public Preview / Beta table may not exist in this region yet
    "po_not_enabled",          # Predictive Optimization not enabled / no UC managed tables
    "compute_scope_gap",       # covers only some compute (classic-only, or SQL-warehouse/serverless-only)
    "no_serverless",           # the feature needs serverless compute (scans / query capture)
    "abac_only",               # only manual masks/filters shown; ABAC policy-derived ones are not captured
    "submit_run_skipped",      # one-time SUBMIT_RUN / WORKFLOW_RUN skip the jobs dimension tables
    "verbose_audit_required",  # fine-grained audit events need verbose audit logging
    "account_admin_only",      # read requires account admin (e.g. system.ai_gateway.usage)
    "privilege_scoped",        # reads are privilege-scoped; a non-admin sees fewer / zero rows
    "retention_window",        # activity older than the table's retention is purged
    "no_activity",             # genuinely no matching activity in the window
    "lineage_inference_only",  # lineage misses unsupported paths (spark-submit, RDD, JDBC, UDF, path-only)
    "ingestion_lag",           # recent activity not yet materialized
}

CONFIDENCE_ENUM = {"confirmed", "needs_confirmation"}
TIER_ENUM = {"lite", "standard", "deep"}
DOMAINS = {
    "cost",
    "performance",
    "compute",
    "jobs_pipelines",
    "serving_ai",
    "storage",
    "governance_access",
}

# First-audit picks (the ★ queries). This set is the source of truth; it flows into
# app/queries/vendored/manifest.json's own `stars` field, which is where the app and its docs
# read it from -- kept here so the manifest is deterministic. cost_actual_vs_list_by_sku is no
# longer a star: T-69A demoted it (`stars: false` in that manifest).
STARS = {
    "cost_by_job",
    "cost_chargeback_by_tag",
    "cost_dollarized_by_sku_day",
    "cost_premium_serverless_photon",
    "query_costly_statements",
    "compute_warehouse_idle_gaps",
    "lakeflow_failed_jobs_wasted_dbus",
    "lakeflow_jobs_on_all_purpose",
    "access_runas_escalation",
}

REPO = "https://github.com/darshanmeel/crosshire-audit-databricks-admin/blob/master/queries"
LEARN = "/learn/tech/databricks/audit"

_FIELD_RE = re.compile(r"^--\s*(" + "|".join(FIELDS + OPTIONAL_FIELDS) + r")\s*:\s?(.*)$")
_DOMAIN_RE = re.compile(r"^\s*(\S+)\s+tier\s*:\s*(\S+)\s*$")
_PARAM_RE = re.compile(r":(\w+)\s*\(default\s+([^)]+)\)\s*(.*)")
# not_assessed_reasons: one "code: plain words" entry per not_assessed_reason value, ";"-separated
# (the same split style as `params:`) -- code is the exact string the query's own CASE emits.
_REASON_RE = re.compile(r"^([a-z][a-z0-9_]*)\s*:\s*(.+)$", re.I)
# A next-token is a query_id optionally followed by a parenthetical condition.
# The condition may itself contain commas, so we split the field only on TOP-LEVEL commas.
_NEXT_SPLIT_RE = re.compile(r",(?![^(]*\))")
_NEXT_RE = re.compile(r"^([a-z0-9_]+)\s*(?:\(\s*(?:if\s+)?(.*?)\s*\))?$", re.I)
_ACTION_SPLIT_RE = re.compile(r"\s*\d+\)\s*")


class HeaderError(ValueError):
    pass


def _raw_fields(text: str) -> dict[str, str]:
    """Extract raw (string) field values from the leading comment block."""
    fields: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if not line.startswith("--"):
            # First non-comment line ends the header block.
            break
        m = _FIELD_RE.match(line)
        if m:
            current = m.group(1)
            fields[current] = m.group(2).strip()
        else:
            # Continuation line: append to the field in progress.
            cont = line[2:].strip()
            if current is not None and cont:
                fields[current] = (fields[current] + " " + cont).strip()
    return fields


def parse_params(value: str) -> list[dict]:
    out = []
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _PARAM_RE.search(chunk)
        if not m:
            continue
        name, default, meaning = m.group(1), m.group(2).strip(), m.group(3).strip()
        val: object = default
        try:
            val = int(default)
        except ValueError:
            try:
                val = float(default)
            except ValueError:
                val = default
        out.append({"name": name, "default": val, "meaning": meaning})
    return out


def parse_next(value: str) -> list[dict]:
    out = []
    if not value or value.strip().lower().startswith("n/a"):
        return out
    # Split on TOP-LEVEL commas only (_NEXT_SPLIT_RE): a next-token's "(if ...)" condition may
    # itself contain commas, and a plain str.split(",") would shred it and silently drop the
    # target. A non-empty token that still fails to parse is a malformed next-entry (typo,
    # unbalanced paren) and is raised loudly rather than dropped — a broken cross-link must fail
    # the linter, not vanish into an empty list that quietly passes.
    for token in _NEXT_SPLIT_RE.split(value):
        token = token.strip()
        if not token:
            continue
        m = _NEXT_RE.match(token)
        if not m:
            raise HeaderError(f"malformed next-entry token {token!r}")
        out.append({"query_id": m.group(1), "if": (m.group(2) or "").strip()})
    return out


def parse_actions(value: str) -> list[str]:
    if not value or value.strip().lower().startswith("n/a"):
        return []
    parts = [p.strip() for p in _ACTION_SPLIT_RE.split(value) if p.strip()]
    return parts


def parse_reads(value: str) -> list[str]:
    return [r.strip() for r in value.split(",") if r.strip()]


def parse_not_assessed_reasons(value: str) -> dict[str, str]:
    """"code: words; code2: words2" -> {code: words}. Empty/absent/"n/a" -> {} (most queries carry
    no `not_assessed_reason` column at all). A non-empty chunk that does not match "code: words" is
    a typo in the header, not a silently-dropped entry -- raised loudly, the same discipline
    parse_next uses for a malformed next-entry."""
    out: dict[str, str] = {}
    if not value or value.strip().lower().startswith("n/a"):
        return out
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _REASON_RE.match(chunk)
        if not m:
            raise HeaderError(f"malformed not_assessed_reasons entry {chunk!r}")
        out[m.group(1)] = m.group(2).strip()
    return out


def parse_header(text: str, *, path: str = "<memory>") -> dict:
    """Parse a full SQL file's text into a structured header dict.

    Raises HeaderError on a structurally-broken header (missing required field or
    malformed domain line). Value-level validation lives in lint_headers.py.
    """
    raw = _raw_fields(text)
    for f in FIELDS:
        if f not in raw:
            raise HeaderError(f"{path}: missing required header field '{f}'")

    dm = _DOMAIN_RE.match(raw["domain"])
    if not dm:
        raise HeaderError(
            f"{path}: 'domain:' line must read '<domain>   tier: <tier>', got {raw['domain']!r}"
        )
    domain, tier = dm.group(1), dm.group(2)

    try:
        nxt = parse_next(raw["next"])
    except HeaderError as e:
        raise HeaderError(f"{path}: {e}") from None

    # Optional: `runnable: false` (default true) excludes a copy-paste-only query from the runner.
    runnable = not raw.get("runnable", "true").strip().lower().startswith("false")
    # Optional: `empty_if: <token>, ...` — machine-checkable coverage reasons the query can be empty.
    empty_if = [t.strip().lower() for t in raw.get("empty_if", "").split(",") if t.strip()]
    # Optional: `not_assessed_reasons: code: words; ...` (P4-03) — plain words for this query's own
    # not_assessed_reason codes, if it carries that column at all.
    try:
        not_assessed_reasons = parse_not_assessed_reasons(raw.get("not_assessed_reasons", ""))
    except HeaderError as e:
        raise HeaderError(f"{path}: {e}") from None

    return {
        "query_id": raw["query_id"].strip(),
        "title": raw["title"].strip(),
        "domain": domain,
        "tier": tier,
        "stars": raw["query_id"].strip() in STARS,
        "runnable": runnable,
        "empty_if": empty_if,
        "not_assessed_reasons": not_assessed_reasons,
        "reads": parse_reads(raw["reads"]),
        "requires": raw["requires"].strip(),
        "params": parse_params(raw["params"]),
        "confidence": raw["confidence"].strip(),
        "confidence_note": raw["confidence_note"].strip(),
        "read_this": raw["read_this"].strip(),
        "healthy": raw["healthy"].strip(),
        "investigate_if": raw["investigate_if"].strip(),
        "actions": parse_actions(raw["actions"]),
        "next": nxt,
        "caveats": raw["caveats"].strip(),
        "_raw": raw,
    }


def iter_query_files(queries_dir: Path):
    for p in sorted(queries_dir.rglob("*.sql")):
        yield p


def load_all(queries_dir: Path) -> dict[str, dict]:
    """query_id -> parsed header, for every .sql under queries_dir."""
    out = {}
    for p in iter_query_files(queries_dir):
        hdr = parse_header(p.read_text(encoding="utf-8"), path=str(p))
        out[hdr["query_id"]] = hdr
    return out
