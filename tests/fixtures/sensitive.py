"""tests/fixtures/sensitive.py -- P3-SENSITIVE, the ONE builder for access_sensitive_table_reads.

Writes rows to the sources that query reads: system.access.table_lineage,
system.information_schema.table_tags, system.information_schema.column_tags,
system.information_schema.tables, system.data_classification.results (DEC-17 auto-discovers
tests/fixtures/<name>.py modules by file name, sorted).

Every id this builder writes (catalog, schema, table, reader/owner email local-parts) carries the
prefix `sn_`, disjoint from every other builder's ids (governance.py already writes rows to four of
these five sources for its own F1/F2 ids, under its own `gv_` prefix and `gv_catalog`/`gv_schema` --
this builder never reuses those names, so its rows never collide with governance.py's join keys).

IMPORTANT (shared-source effect on the account-wide NOT_ASSESSED sentinel): the query's own
sentinel row (status='NOT_ASSESSED', not_assessed_reason='no_sensitivity_signal') fires only when
the WHOLE fixture DB -- every builder's rows pooled, not just this one's -- has zero rows across
table_tags/column_tags/data_classification matching the sensitivity regex. governance.py's own PII
scenario rows (tag_name/tag_value 'pii', 'confidential', 'restricted' on its `gv_pii_*` tables,
system.information_schema.column_tags) already guarantee that count is never zero in this shared
suite, so that branch cannot be exercised here by construction, not by omission -- it was verified
separately by a standalone DuckDB smoke test against an EMPTY fixture DB (every one of the 47
sources created from ddl.py with no builder's rows loaded), which returns exactly the one expected
sentinel row. See tests/test_findings/test_sensitive_table_reads.py for the note on this id's own
test and the not_done write-up for this task.

Five reusable time anchors, relative to AS_OF = 2026-09-21 12:00:00 (identical deltas to
tests/fixtures/query_history.py's and governance.py's, already proven against the same pinned
audit_today() / audit_now() literals):

    D7     AS_OF - 3d    inside the 7d window (and 30d, 90d)
    D30    AS_OF - 16d   inside the 30d window, outside the 7d window
    D90    AS_OF - 68d   inside the 90d window, outside the 30d window
    DTODAY 2026-09-21 08:00:00   on AS_OF's own calendar day
    DOLD   AS_OF - 143d  older than the 90d window -- excluded everywhere

access.table_lineage filters on event_date with BOTH a lower bound (>= current_date() -
:period_days) AND an upper bound (< current_date()) -- DTODAY is EXCLUDED at every window (today
itself never lands in any window, same as governance.py's own table_lineage/column_lineage rows);
DOLD is excluded everywhere.

Scenario groups (warn_reads=20; every table below lives at sn_catalog.sn_schema.<name> unless
noted):

  sn_owner_tbl    (table_tags: tag_name='pii')
    OK   owner reads their OWN table 25x (above warn_reads) -- is_owner short-circuits to OK
         regardless of volume: reader=owner='sn_owner_person@example.com'

  sn_warn_tbl     (column_tags: tag_value='confidential', on one column)
    WARN a non-owner ('sn_warn_reader@example.com') reads it 21x (> warn_reads=20), spread 7 reads
         each over 3 distinct calendar days (D7, AS_OF-2d, AS_OF-1d, i minutes apart) so
         read_count/distinct_read_days/first_read_time/last_read_time are each independently
         proven, not just read_count; owner is a DIFFERENT person ('sn_warn_owner@example.com')

  sn_okunder_tbl  (data_classification.results: class_tag NOT NULL)
    OK   a non-owner ('sn_okunder_reader@example.com') reads it EXACTLY 20x -- the boundary itself
         (read_count > :warn_reads is false at 20, proving '>' not '>='); owner is a different
         person ('sn_okunder_owner@example.com')

  sn_guid_tbl     (table_tags: tag_name='sensitive')
    WARN reader is a 36-char hex GUID (DEC-66.3 mask branch 2, passthrough) reading 21x as a
         non-owner; owner is a plain email, different from the GUID

  sn_null_tbl     (table_tags: tag_value='restricted')
    WARN reader (created_by) is NULL (DEC-66.3 mask branch 1, passthrough) reading 21x; NULL never
         equals a non-NULL owner, so is_owner is false and this still flags WARN with reader NULL
         in the output

  sn_noowner_tbl  (table_tags: tag_name='pii')
    WARN reader is a plain email reading 21x; this table carries NO information_schema.tables row
         at all, so table_owner is NULL in the join (owner unknown) -- is_owner is false (the safer
         direction) and table_owner displays NULL

  sn_multi_tbl    (BOTH table_tags tag_name='pii' AND column_tags tag_value='sensitive', on one
                   column) -- one reader, 3 reads (well under warn_reads) -- proves
         sensitivity_basis is the comma-joined SET of every source that matched ({'table_tag',
         'column_tag'}), read order-independent since array_join(collect_set(...))'s own internal
         order is not guaranteed (same caveat cost_daily_spikes' top_skus already documents)

  sn_viaview_tbl  (table_tags: tag_name='pii')
    excluded  a non-owner ('sn_viaview_reader@example.com') reads it 21x, but every lineage row
              carries direct_access=False (indirect/view-expansion) -- table_reads' own filter
              drops them all, so this table never appears at any window despite the volume; owner
              is 'sn_viaview_owner@example.com'

  sn_notsensitive_tbl (no tag row anywhere, no classification row)
    excluded  read 5x by 'sn_notsensitive_reader@example.com' -- never appears: this table never
              enters the sensitive_tables CTE at all (proves the join, not just the WHERE, gates
              this id's rows)

  sn_sys_tbl (catalog='system', table_tags tag_name='pii' -- a tag row on it exists so this table
              WOULD otherwise qualify as sensitive)
    excluded  read 5x -- table_reads' own `source_table_catalog <> 'system'` filter drops the read
              event regardless of the tag, so this table still never appears

  sn_win_tbl (table_tags: tag_name='pii')
    window   sn_win_reader_{t7,t30,t90,ttoday,told90}@example.com, one read each at its own anchor
             (own reader per anchor keeps the (table, reader) grain unambiguous per DEC-15) --
             ttoday and told90 rows exist in the raw table but must NEVER appear in ANY window's
             result (see caveats above); t7/t30/t90 rows must each appear once their own window
             includes that anchor.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from base import AS_OF  # noqa: F401 (kept for parity with every other builder's import line)

SN_CATALOG = "sn_catalog"
SN_SCHEMA = "sn_schema"

D7 = AS_OF - timedelta(days=3)
D30 = AS_OF - timedelta(days=16)
D90 = AS_OF - timedelta(days=68)
DTODAY = datetime(2026, 9, 21, 8, 0, 0)
DOLD = AS_OF - timedelta(days=143)

WINDOW_ANCHORS = [("t7", D7), ("t30", D30), ("t90", D90), ("ttoday", DTODAY), ("told90", DOLD)]

GUID_EXAMPLE = "aaaa1111-bbbb-2222-cccc-3333dddd4444"

_seq = {"n": 0}


def _next_id() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------------------------
# access__table_lineage
# ---------------------------------------------------------------------------------------------
TABLE_LINEAGE_COLUMNS = [
    "account_id", "metastore_id", "workspace_id", "entity_type", "entity_id", "entity_run_id",
    "source_table_full_name", "source_table_catalog", "source_table_schema", "source_table_name",
    "source_path", "source_type", "target_table_full_name", "target_table_catalog",
    "target_table_schema", "target_table_name", "target_path", "target_type", "created_by",
    "event_time", "event_date", "record_id", "event_id", "statement_id", "entity_metadata",
    "direct_access",
]

_LINEAGE_DEFAULTS = {
    "account_id": "sn-account", "metastore_id": "sn-metastore", "workspace_id": "1111",
    "entity_type": "NOTEBOOK", "entity_id": None, "entity_run_id": None,
    "target_table_full_name": None, "target_table_catalog": None, "target_table_schema": None,
    "target_table_name": None, "source_path": None, "target_path": None, "source_type": "TABLE",
    "target_type": None, "record_id": None, "statement_id": None, "entity_metadata": None,
    "direct_access": True,
}


def _read_row(table_name: str, created_by: str | None, event_time: datetime,
              catalog: str = SN_CATALOG, schema: str = SN_SCHEMA,
              direct_access: bool = True) -> dict:
    r = {c: None for c in TABLE_LINEAGE_COLUMNS}
    r.update(_LINEAGE_DEFAULTS)
    r["source_table_catalog"] = catalog
    r["source_table_schema"] = schema
    r["source_table_name"] = table_name
    r["source_table_full_name"] = f"{catalog}.{schema}.{table_name}"
    r["created_by"] = created_by
    r["event_time"] = event_time
    r["event_date"] = event_time.date()
    r["event_id"] = f"sn_evt_{_next_id()}"
    r["direct_access"] = direct_access
    return r


# ---------------------------------------------------------------------------------------------
# information_schema.table_tags / column_tags
# ---------------------------------------------------------------------------------------------
IS_TABLE_TAGS_COLUMNS = ["catalog_name", "schema_name", "table_name", "tag_name", "tag_value"]
IS_COLUMN_TAGS_COLUMNS = ["catalog_name", "schema_name", "table_name", "column_name", "tag_name", "tag_value"]


def _table_tag_row(table_name: str, tag_name: str, tag_value: str,
                    catalog: str = SN_CATALOG, schema: str = SN_SCHEMA) -> dict:
    return {"catalog_name": catalog, "schema_name": schema, "table_name": table_name,
            "tag_name": tag_name, "tag_value": tag_value}


def _column_tag_row(table_name: str, column_name: str, tag_name: str, tag_value: str,
                     catalog: str = SN_CATALOG, schema: str = SN_SCHEMA) -> dict:
    return {"catalog_name": catalog, "schema_name": schema, "table_name": table_name,
            "column_name": column_name, "tag_name": tag_name, "tag_value": tag_value}


# ---------------------------------------------------------------------------------------------
# information_schema.tables (owners)
# ---------------------------------------------------------------------------------------------
IS_TABLES_COLUMNS = [
    "table_catalog", "table_schema", "table_name", "table_type", "is_insertable_into",
    "commit_action", "table_owner", "comment", "created", "created_by", "last_altered",
    "last_altered_by", "data_source_format", "storage_sub_directory", "storage_path",
]


def _tables_row(table_name: str, owner: str, catalog: str = SN_CATALOG, schema: str = SN_SCHEMA) -> dict:
    r = {c: None for c in IS_TABLES_COLUMNS}
    r.update({
        "table_catalog": catalog, "table_schema": schema, "table_name": table_name,
        "table_type": "MANAGED", "is_insertable_into": "YES", "table_owner": owner,
        "created": AS_OF - timedelta(days=90), "created_by": owner,
        "last_altered": AS_OF - timedelta(days=1), "last_altered_by": owner,
        "data_source_format": "DELTA",
    })
    return r


# ---------------------------------------------------------------------------------------------
# data_classification.results (MISSING_FROM_DUMP, all-VARCHAR -- see governance.py's own note)
# ---------------------------------------------------------------------------------------------
DC_COLUMNS = [
    "catalog_name", "class_tag", "column_name", "confidence", "data_type",
    "first_detected_time", "frequency", "latest_detected_time", "schema_name", "table_name",
]


def _dc_row(table_name: str, column_name: str, catalog: str = SN_CATALOG, schema: str = SN_SCHEMA) -> dict:
    return {
        "catalog_name": catalog, "schema_name": schema, "table_name": table_name,
        "column_name": column_name, "class_tag": "PII", "confidence": "HIGH", "data_type": "STRING",
        "frequency": "0.85", "first_detected_time": "2026-08-01 00:00:00",
        "latest_detected_time": "2026-09-15 00:00:00",
    }


# ---------------------------------------------------------------------------------------------
# Scenario assembly
# ---------------------------------------------------------------------------------------------
def _scenario_rows() -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    lineage_rows: list[dict] = []
    table_tag_rows: list[dict] = []
    column_tag_rows: list[dict] = []
    tables_rows: list[dict] = []
    dc_rows: list[dict] = []

    # sn_owner_tbl -- OK: owner reads their own table 25x (above warn_reads, still OK).
    table_tag_rows.append(_table_tag_row("sn_owner_tbl", "pii", "true"))
    tables_rows.append(_tables_row("sn_owner_tbl", "sn_owner_person@example.com"))
    for i in range(25):
        lineage_rows.append(_read_row("sn_owner_tbl", "sn_owner_person@example.com", D7 + timedelta(minutes=i)))

    # sn_warn_tbl -- WARN: non-owner reads 21x (> warn_reads=20), spread over 3 distinct calendar
    # days (7 reads each at D7=AS_OF-3d, AS_OF-2d, AS_OF-1d, i minutes apart) so read_count,
    # distinct_read_days, first_read_time and last_read_time are each proven independently rather
    # than all collapsing onto one calendar day.
    column_tag_rows.append(_column_tag_row("sn_warn_tbl", "sn_col_a", "owner", "confidential"))
    tables_rows.append(_tables_row("sn_warn_tbl", "sn_warn_owner@example.com"))
    for day_anchor in (D7, AS_OF - timedelta(days=2), AS_OF - timedelta(days=1)):
        for i in range(7):
            lineage_rows.append(_read_row("sn_warn_tbl", "sn_warn_reader@example.com", day_anchor + timedelta(minutes=i)))

    # sn_okunder_tbl -- OK: non-owner reads EXACTLY 20x, the boundary (> is false at 20).
    dc_rows.append(_dc_row("sn_okunder_tbl", "sn_col_b"))
    tables_rows.append(_tables_row("sn_okunder_tbl", "sn_okunder_owner@example.com"))
    for i in range(20):
        lineage_rows.append(_read_row("sn_okunder_tbl", "sn_okunder_reader@example.com", D7 + timedelta(minutes=i)))

    # sn_guid_tbl -- WARN: reader is a GUID (mask branch 2, passthrough), 21x, non-owner.
    table_tag_rows.append(_table_tag_row("sn_guid_tbl", "sensitive", "true"))
    tables_rows.append(_tables_row("sn_guid_tbl", "sn_guid_owner@example.com"))
    for i in range(21):
        lineage_rows.append(_read_row("sn_guid_tbl", GUID_EXAMPLE, D7 + timedelta(minutes=i)))

    # sn_null_tbl -- WARN: reader (created_by) is NULL (mask branch 1, passthrough), 21x.
    table_tag_rows.append(_table_tag_row("sn_null_tbl", "owner", "restricted"))
    tables_rows.append(_tables_row("sn_null_tbl", "sn_null_owner@example.com"))
    for i in range(21):
        lineage_rows.append(_read_row("sn_null_tbl", None, D7 + timedelta(minutes=i)))

    # sn_noowner_tbl -- WARN: no information_schema.tables row at all -> owner unknown -> non-owner.
    table_tag_rows.append(_table_tag_row("sn_noowner_tbl", "pii", "true"))
    for i in range(21):
        lineage_rows.append(_read_row("sn_noowner_tbl", "sn_noowner_reader@example.com", D7 + timedelta(minutes=i)))

    # sn_multi_tbl -- both table_tag AND column_tag match -> sensitivity_basis carries both.
    table_tag_rows.append(_table_tag_row("sn_multi_tbl", "pii", "true"))
    column_tag_rows.append(_column_tag_row("sn_multi_tbl", "sn_col_c", "owner", "sensitive"))
    tables_rows.append(_tables_row("sn_multi_tbl", "sn_multi_owner@example.com"))
    for i in range(3):
        lineage_rows.append(_read_row("sn_multi_tbl", "sn_multi_reader@example.com", D7 + timedelta(minutes=i)))

    # sn_viaview_tbl -- excluded at every window: 21 reads by a non-owner, but every one of them
    # has direct_access=False (an indirect/view-expansion lineage row, recorded against the raw
    # table only because some view over it was queried) -- table_reads' own direct_access filter
    # drops all of them, so this table never appears here despite the read volume.
    table_tag_rows.append(_table_tag_row("sn_viaview_tbl", "pii", "true"))
    tables_rows.append(_tables_row("sn_viaview_tbl", "sn_viaview_owner@example.com"))
    for i in range(21):
        lineage_rows.append(_read_row("sn_viaview_tbl", "sn_viaview_reader@example.com", D7 + timedelta(minutes=i), direct_access=False))

    # sn_notsensitive_tbl -- excluded: read activity, but no tag/classification row anywhere.
    for i in range(5):
        lineage_rows.append(_read_row("sn_notsensitive_tbl", "sn_notsensitive_reader@example.com", D7 + timedelta(minutes=i)))

    # sn_sys_tbl -- excluded: tagged sensitive, but catalog='system' -> table_reads' own filter drops it.
    table_tag_rows.append(_table_tag_row("sn_sys_tbl", "pii", "true", catalog="system"))
    for i in range(5):
        lineage_rows.append(_read_row("sn_sys_tbl", "sn_sys_reader@example.com", D7 + timedelta(minutes=i), catalog="system"))

    # sn_win_tbl -- window boundary: one read per anchor, own reader per anchor.
    table_tag_rows.append(_table_tag_row("sn_win_tbl", "pii", "true"))
    for suffix, ts in WINDOW_ANCHORS:
        lineage_rows.append(_read_row("sn_win_tbl", f"sn_win_reader_{suffix}@example.com", ts))

    return lineage_rows, table_tag_rows, column_tag_rows, tables_rows, dc_rows


# ---------------------------------------------------------------------------------------------
# Generic named-column insert helper.
# ---------------------------------------------------------------------------------------------
def _insert(con, table: str, columns: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f'INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})',
        [[r[c] for c in columns] for r in rows],
    )


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17).
# ---------------------------------------------------------------------------------------------
def build(con) -> None:
    lineage_rows, table_tag_rows, column_tag_rows, tables_rows, dc_rows = _scenario_rows()

    seen = set()
    for r in lineage_rows:
        eid = r["event_id"]
        if eid in seen:
            raise ValueError(f"duplicate event_id in sensitive.py access__table_lineage rows: {eid!r}")
        seen.add(eid)

    _insert(con, "access__table_lineage", TABLE_LINEAGE_COLUMNS, lineage_rows)
    _insert(con, "information_schema__table_tags", IS_TABLE_TAGS_COLUMNS, table_tag_rows)
    _insert(con, "information_schema__column_tags", IS_COLUMN_TAGS_COLUMNS, column_tag_rows)
    _insert(con, "information_schema__tables", IS_TABLES_COLUMNS, tables_rows)
    _insert(con, "data_classification__results", DC_COLUMNS, dc_rows)
