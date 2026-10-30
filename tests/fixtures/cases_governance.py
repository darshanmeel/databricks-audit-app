"""tests/fixtures/cases_governance.py -- B1's own new fixture rows:

  1. a column carrying TWO sensitivity tags, proving access_pii_propagation_untagged's
     sensitive_tags CTE collapses to one row per column (source_tags aggregated) instead of
     fanning the lineage event out once per tag.
  2. a source table reached through TWO different targets, proving
     access_table_lineage_blast_radius judges distinct_principals per SOURCE TABLE, not per
     source-target edge -- each edge alone stays under the WARN floor, but the table's own total
     reach crosses it.

Own workspace (7701, never shared with any other builder, DEC-15's own per-builder isolation) and
own id namespace (`b1_`) throughout. Self-contained -- writes its own access__column_lineage /
access__table_lineage / information_schema__column_tags rows directly rather than importing
governance.py's per-builder helpers, so this module has no load-order dependency on another
builder.

b1_pii_multitag_src.b1_col carries two matching tags (sensitive=true, phi=true -- neither collides
with any (tag_name, tag_value) tests/test_findings/test_governance_inventories.py's
test_access_tags_inventory already asserts an exact count for) and 3 lineage events into the
untagged b1_pii_multitag_tgt.b1_col2, from 2 distinct creators: without the CTE's own GROUP BY,
the raw join would produce 2 rows (one per tag) each double-counting the 3 events; with it, exactly
one row, source_tags = 'phi=true, sensitive=true' (sorted), event_count = 3, distinct_creators = 2.

b1_blast_fanout_src is read by 6 distinct principals via b1_blast_fanout_tgt_a and 6 DIFFERENT
distinct principals via b1_blast_fanout_tgt_b -- 6 < :warn_blast_principals=10 on either edge
alone (would read OK per-edge), but the table's own total reach is 12 distinct principals, at/above
:warn_blast_principals -- both edges must read WARN.
"""
from __future__ import annotations

from datetime import timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

WS = "7701"
ACCOUNT_ID = "b1_acct"
CATALOG = "b1_catalog"
SCHEMA = "b1_schema"

_COLUMN_LINEAGE_SQL = (
    "INSERT INTO access__column_lineage (account_id, metastore_id, workspace_id, "
    "source_table_catalog, source_table_schema, source_table_name, source_column_name, "
    "target_table_catalog, target_table_schema, target_table_name, target_column_name, "
    "created_by, event_time, event_date, direct_access) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_TAG_SQL = (
    "INSERT INTO information_schema__column_tags "
    "(catalog_name, schema_name, table_name, column_name, tag_name, tag_value) "
    "VALUES (?,?,?,?,?,?)"
)
_TABLE_LINEAGE_SQL = (
    "INSERT INTO access__table_lineage (account_id, metastore_id, workspace_id, entity_type, "
    "source_table_full_name, source_table_catalog, source_table_schema, source_table_name, "
    "source_type, target_table_full_name, target_table_catalog, target_table_schema, "
    "target_table_name, target_type, created_by, event_time, event_date, direct_access) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _column_lineage(con, created_by, minutes_ago):
    # Yesterday: these checks read full days and never today's partial one.
    event_time = AS_OF - timedelta(days=1, minutes=minutes_ago)
    con.execute(_COLUMN_LINEAGE_SQL, [
        ACCOUNT_ID, "b1-metastore", WS,
        CATALOG, SCHEMA, "b1_pii_multitag_src", "b1_col",
        CATALOG, SCHEMA, "b1_pii_multitag_tgt", "b1_col2",
        created_by, event_time, event_time.date(), True,
    ])


def _table_lineage(con, target_name, created_by, minutes_ago):
    event_time = AS_OF - timedelta(days=1, minutes=minutes_ago)
    source_full = f"{CATALOG}.{SCHEMA}.b1_blast_fanout_src"
    target_full = f"{CATALOG}.{SCHEMA}.{target_name}"
    con.execute(_TABLE_LINEAGE_SQL, [
        ACCOUNT_ID, "b1-metastore", WS, "NOTEBOOK",
        source_full, CATALOG, SCHEMA, "b1_blast_fanout_src", "TABLE",
        target_full, CATALOG, SCHEMA, target_name, None,
        created_by, event_time, event_time.date(), True,
    ])


def build(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_TAG_SQL, [CATALOG, SCHEMA, "b1_pii_multitag_src", "b1_col", "sensitive", "true"])
    con.execute(_TAG_SQL, [CATALOG, SCHEMA, "b1_pii_multitag_src", "b1_col", "phi", "true"])

    _column_lineage(con, "b1_alice@example.com", 60)
    _column_lineage(con, "b1_alice@example.com", 30)
    _column_lineage(con, "b1_bob@example.com", 10)

    for i in range(6):
        _table_lineage(con, "b1_blast_fanout_tgt_a", f"b1_blast_p{i}@example.com", 60 + i)
    for i in range(6, 12):
        _table_lineage(con, "b1_blast_fanout_tgt_b", f"b1_blast_p{i}@example.com", 60 + i)
