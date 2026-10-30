"""app/core/finding_columns.py

Which of a check's own columns is its headline dollar figure, and what KIND of dollar it is (the
screen rule: spend, change and possible waste are never added together) -- and which is its
headline "N of M <noun>" entity count, for /api/findings' bulk `money`/`affected` fields
(contract B). Column-name maps only, no query_id list to maintain by hand: a check whose table
carries none of these column shapes simply has no money/affected figure.
"""
from __future__ import annotations

from app.core import pricing as app_pricing

# column name -> kind. Any OTHER money-shaped column (app.core.pricing's own list-price regex)
# reads "spend" -- the money screen rule's "the rest".
MONEY_COLUMN_KIND: dict[str, str] = {
    "est_wasted_usd_list": "waste",
    "total_est_wasted_usd_list": "waste",
    "est_change_usd_list": "change",
    "change_usd_list": "change",
    "est_saving_usd_list": "saving",
    "gap_usd_list": "gap",
}

# id column -> (noun, the dims.dim_<x> table it counts every object of that kind from).
ENTITY_COLUMNS: dict[str, tuple[str, str]] = {
    "job_id": ("jobs", "dim_job"),
    "pipeline_id": ("pipelines", "dim_pipeline"),
    "warehouse_id": ("warehouses", "dim_warehouse"),
    "cluster_id": ("clusters", "dim_cluster"),
    # No dim: an endpoint check lists its OK rows too, so its own endpoints are the base.
    "endpoint_id": ("serving endpoints", None),
    "instance_pool_id": ("pools", None),
}
# job/pipeline is the more specific "thing flagged" on a table that also happens to carry a
# warehouse_id (e.g. a job's own compute).
ENTITY_PRIORITY = ("job_id", "pipeline_id", "warehouse_id", "cluster_id", "endpoint_id", "instance_pool_id")


# child column -> parent id: one row per child that repeats its parent's whole spend, so the $ is
# added once per parent (a job's tasks each carry the job's spend).
PARENT_MONEY: dict[str, str] = {"task_key": "job_id"}


def money_parent(columns) -> str | None:
    """The parent id whose spend every row repeats, or None when rows add up as they are."""
    cols = set(columns or [])
    for child, parent in PARENT_MONEY.items():
        if child in cols and parent in cols:
            return parent
    return None


# A table with no entity id counts its own rows; these column shapes say what one row is.
ROW_NOUNS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"securable", "privilege_type"}), "grants"),
    (frozenset({"run_by", "run_as"}), "run-as pairs"),
    (frozenset({"actor", "action_name"}), "actor-action pairs"),
    (frozenset({"principal", "source_ip_address"}), "sign-in patterns"),
    (frozenset({"volume_name"}), "volumes"),
    (frozenset({"source_column_name", "target_column_name"}), "column copies"),
    (frozenset({"table_schema", "table_name"}), "tables"),
    (frozenset({"catalog_name", "schema_name", "table_name"}), "tables"),
)


def row_noun(columns) -> str:
    """What one row of a table with no entity id is, or "rows"."""
    cols = set(columns or [])
    for needed, noun in ROW_NOUNS:
        if needed <= cols:
            return noun
    return "rows"


def money_column(columns) -> tuple[str, str] | None:
    """(column, kind) for this table's headline dollar figure, or None. A column MONEY_COLUMN_KIND
    names explicitly wins; else the first money-shaped column reads "spend"."""
    cols = list(columns or [])
    for name in cols:
        if name in MONEY_COLUMN_KIND:
            return name, MONEY_COLUMN_KIND[name]
    for name in cols:
        if app_pricing._LIST_COL_RE.match(name):
            return name, "spend"
    return None


def entity_column(columns) -> tuple[str, str, str | None] | None:
    """(column, noun, dim_table) for this table's headline "N of M <noun>" count, or None -- a
    table with none of ENTITY_COLUMNS falls back to the check's own row count instead
    (finding_window_counts)."""
    cols = set(columns or [])
    for name in ENTITY_PRIORITY:
        if name in cols:
            noun, dim = ENTITY_COLUMNS[name]
            return name, noun, dim
    return None
