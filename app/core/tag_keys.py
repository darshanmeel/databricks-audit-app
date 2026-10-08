"""app/core/tag_keys.py -- P4-T's shared vocabulary (tasks/P4-T-SPEC.md, section 4 and appendix C).

SHARED by the TAG-IDX and TAG-ROLL lanes: written byte-identical in both from the spec; owned by
TAG-IDX. Do not edit it in TAG-ROLL -- report a needed change to the orchestrator instead.

Everything both lanes must spell the same way lives here: the key normalisation (the Python twin
of dbt/macros/norm_tag_key.sql), the sentinel values, the level names and the source codes with
their on-screen words. Stdlib only.
"""
from __future__ import annotations

import re

# ---- sentinel tag values (never a real tag value: real values never start with "__") ---------
UNTAGGED = "__untagged__"   # the thing exists but carries no value for this key
MIXED = "__mixed__"         # inferred level (workspace, billed entity): tagged, no dominant value
NONE = "__none__"           # there is no thing at this level (no workspace, no query, no compute)
EMPTY = "__empty__"         # API/URL spelling of a key-only tag (stored value "")
SENTINELS = frozenset({UNTAGGED, MIXED, NONE})

# ---- levels of the nested rollup, outermost first ---------------------------------------------
LEVELS = ("account", "workspace", "compute", "work")
LEVEL_LABELS = {
    "account": "Account",
    "workspace": "Workspace tag",
    "compute": "Compute tag",
    "work": "Query / job tag",
}

# ---- where a tag value came from ---------------------------------------------------------------
SOURCE_LABELS = {
    "billing_custom_tags": "tag on the bill (billing.usage custom_tags)",
    "workspace_inferred": "workspace tag, inferred from the bill",
    "billing:warehouse": "warehouse tag, as billed",
    "billing:cluster": "cluster tag, as billed (includes pool and policy tags)",
    "billing:budget_policy": "budget policy tag, as billed",
    "billing:serverless": "serverless tag, as billed",
    "billing:endpoint": "serving endpoint tag, as billed",
    "billing:app": "app tag, as billed",
    "billing:other": "tag on the bill",
    "warehouse_tags": "warehouse tag",
    "cluster_tags": "cluster tag (includes policy-enforced tags)",
    "pool_tags": "instance pool tag",
    "job_tags": "job tag",
    "pipeline_tags": "pipeline tag",
    "query_tags": "query tag",
    "attributed_query_tags": "query tag, on Databricks' per-query cost split",
    "uc_table_tags": "Unity Catalog table tag",
    "uc_schema_tags": "Unity Catalog schema tag",
    "uc_column_tags": "Unity Catalog column tag",
    "uc_volume_tags": "Unity Catalog volume tag",
    "ai_gateway_endpoint_tags": "AI gateway endpoint tag",
}

_NORM_RE = re.compile(r"[ _-]+")


def normalize_tag_key(raw: str | None) -> str:
    """The Python twin of dbt/macros/norm_tag_key.sql: lower-cased, every run of spaces, hyphens
    and underscores removed -- "Cost Center", "cost-center" and "cost_center" are one key. Returns
    "" for None or a key made only of separators (the API rejects "")."""
    if raw is None:
        return ""
    return _NORM_RE.sub("", str(raw).lower())


def api_value_to_model(value: str | None) -> str | None:
    """A tag_value query parameter as the models store it: EMPTY -> "" (a key-only tag), anything
    else unchanged (None means "any value of this key")."""
    return "" if value == EMPTY else value


def model_value_to_api(value: str | None) -> str | None:
    """A stored tag value as the API returns it: "" -> EMPTY, anything else unchanged."""
    return EMPTY if value == "" else value


def is_real_value(value: str | None) -> bool:
    """True for a real tag value (including the key-only ""), False for None and every sentinel."""
    return value is not None and value not in SENTINELS
