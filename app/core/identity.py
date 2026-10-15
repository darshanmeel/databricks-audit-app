"""app/core/identity.py -- T-66B: DEC-66.3's identity display format, computed in Python.

Masking is optional (off by default): masking_enabled() reads config/settings.yml's
privacy.mask_user_identities; app/core/data.py's dim readers check it before calling
format_identity below.

DEC-55 (2026-09-22) lets four project-owned dims carry a person's identity UNMASKED at the DuckDB
level, because their Databricks system-table source does not mask them either (unlike
identity_metadata.run_as, which every vendored finding query partial-masks in-SQL, DEC-48):
dim_job.run_as / run_as_user_name / creator_user_name, dim_cluster.owned_by,
dim_warehouse.created_by, dim_pipeline.created_by / run_as. DEC-66.3 closes that gap for anything
this app actually serves to a browser: every one of those columns is masked to DEC-66.3's format
before it leaves app/core/data.py's read_dim_job/read_dim_cluster/read_dim_warehouse/
read_dim_pipeline (T-66B) -- no unmasked email reaches GET /api/dims, and therefore none reaches
the Jobs tab's job-focus panel (app/web/components/tab_jobs.jsx), which only ever displays what
that endpoint gives it.

format_identity(value, real_id=None) below reproduces, byte-for-byte, the exact CASE T-66A put in
the vendored SQL library (git log --grep T-66A; e.g. app/queries/vendored/cost/
cost_chargeback_by_identity.sql, app/queries/vendored/performance/query_costly_statements.sql):

    CASE
      WHEN x IS NULL OR x = '__REDACTED__' THEN x
      WHEN x RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
        THEN x                                                       -- service-principal GUID
      ELSE concat(COALESCE(<real_id_col>, substr(sha2(lower(trim(x)), 256), 1, 8)), ' ',
                  substr(x, 1, 2), '***')
    END

Spark's trim() (and DuckDB's) strips only ASCII space, never tab/newline/other whitespace, so the
Python side below strips only ' ' too (`text.strip(' ')`, not the bare `text.strip()`, which would
also eat \t/\n/\r and friends and hash the same identity to two different codes depending on which
side computed it) -- this is DEC-66.3's own "byte-identical to the SQL" requirement, not a style
choice.

`real_id` is the row's own real Databricks user id (e.g. system.lakeflow.jobs.creator_id, or
dim_job.run_as itself for run_as_user_name -- see app/core/data._mask_dim_job, T-66B review fix)
when the caller has one in hand -- COALESCE semantics: real_id is used whenever it is not None (an
empty string is a value like any other, never treated as "missing", exactly like SQL's COALESCE
only firing on NULL). Most of the four dims this module masks still have NO real per-row id
available for most of their identity columns: system.lakeflow.jobs.creator_id exists at the source
and could supply one for dim_job's creator_user_name, but dbt/models/dims/dim_job.sql (outside this
item's Lane B scope, and tests/fixtures/lakeflow.py's job_row() hard-codes creator_id NULL besides)
does not select it yet -- see tasks/DECISIONS.md T-66B not_done. Those call sites take the hash-
fallback branch today; the real_id parameter exists so the algorithm is already complete for the
day that column is added, without a second implementation to keep in sync with T-66A's SQL one.
"""
from __future__ import annotations

import hashlib
import re

from app.core import config as app_config

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# tools/export_direct_results.py's own _ALREADY_MASKED_RE -- a value already in this shape (e.g.
# read back out of a dim the export already masked) is returned unchanged, never re-hashed.
_MASKED_RE = re.compile(r"^\S+ .{0,2}\*\*\*$")

_REDACTED = "__REDACTED__"


def masking_enabled() -> bool:
    """config/settings.yml's `privacy.mask_user_identities`; off by default."""
    return bool(app_config.load_settings().get("privacy", {}).get("mask_user_identities", False))


def format_identity(value, real_id=None):
    """Mask one identity string to DEC-66.3's display format: "<id> <first 2 chars>***".

    `value` is the raw identity (a person's email, a service-principal identifier, or None/NaN).
    `real_id` is that row's own real Databricks user id when the caller has one; None means "none
    available" (COALESCE semantics -- see the module docstring).

    Returns:
      - None unchanged, for value is None (or NaN, so a caller can pass a pandas cell straight
        through with no pre-check -- DuckDB VARCHAR NULLs surface as None, but a defensive NaN
        check costs nothing and matches app/api/service._json_scalar's own leniency elsewhere);
      - '__REDACTED__' unchanged, for that literal (FedRamp workspaces' own "identity
        unavailable" sentinel -- never hashed into a fake person, T-66A's own must-fix);
      - the bare identity unchanged, when it is a 36-char service-principal GUID or already in
        this function's own masked shape (idempotent -- masking an already-masked value again
        would hash the mask itself into a different, wrong code);
      - otherwise "<id> <first 2 raw chars>***", <id> = real_id if given, else the first 8
        lowercase hex characters of sha256(lower(trim(value))).
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN != NaN, cheaper than importing math/pd
        return None
    if value == _REDACTED:
        return value
    text = value if isinstance(value, str) else str(value)
    if _GUID_RE.match(text) or _MASKED_RE.match(text):
        return text
    if real_id is not None:
        id_part = str(real_id)
    else:
        id_part = hashlib.sha256(text.strip(" ").lower().encode("utf-8")).hexdigest()[:8]
    return f"{id_part} {text[:2]}***"
