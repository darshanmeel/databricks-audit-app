"""tests/fixtures/classify.py -- P3-CLASSIFY, the fixtures for access_classification_coverage
(domain governance_access).

Writes rows to two sources this query reads:

  information_schema__tables       (also written by governance.py's `gv_` rows and ports.py's
                                     `pt_` rows -- dbt-duckdb unions every builder's parquet for
                                     the same source by name, DEC-15)
  data_classification__results     (also written by governance.py's `gv_` rows)

Every id this builder writes carries the prefix `cl_`, disjoint from every other builder's ids
(governance.py's `gv_`, ports.py's `pt_`), so this module's own assertions
(tests/test_findings/test_access_classification_coverage.py) filter to `cl_`-prefixed catalogs and
never depend on, or interfere with, another builder's rows in these two shared sources.

Scenarios (each catalog is one output row; :warn_coverage_pct default is 80):
  cl_cat_full     3 non-VIEW tables, all 3 classified (cl_full_t1 carries TWO classification rows,
                  under different class_tag/column_name, proving the query's own DISTINCT dedupes
                  a multiply-classified table down to one) -> tables_total=3, tables_classified=3,
                  covered_pct=100.0 -> OK.
  cl_cat_partial  4 non-VIEW tables, only 1 classified -> tables_total=4, tables_classified=1,
                  covered_pct=25.0 -> WARN (below the 80 default).
  cl_cat_zero     2 non-VIEW tables, none classified -> tables_total=2, tables_classified=0,
                  covered_pct=0.0 -> WARN. Proves a real, non-NULL zero is reported honestly (not
                  read as NOT_ASSESSED), since governance.py's own `gv_` rows already give the
                  account-wide data_classification.results table at least one row, so the global
                  "table is empty everywhere" branch never fires on the built fixture.

Exclusion proofs, all attached to catalogs/schemas the query's own WHERE clause drops before they
ever reach a GROUP BY, so none of them can appear in any catalog's tables_total or tables_classified:
  cl_full_view              table_type='VIEW' inside cl_cat_full, WITH its own classification row
                             -- proves a classified VIEW neither counts toward tables_total nor
                             inflates tables_classified past it (the query's own
                             classified_in_inventory INNER JOIN bound).
  cl_infoschema_excluded    catalog='cl_cat_full', schema='information_schema' -- proves the
                             schema exclusion (table_schema <> 'information_schema').
  cl_verify_system_excluded catalog='system' -- proves the catalog exclusion (table_catalog <>
                             'system'); this row's own catalog is 'system', not a `cl_` id, but it
                             is filtered out of the result entirely so it cannot collide with, or
                             be mistaken for, any other builder's use of that reserved name.

No `gv_` or `pt_` id is referenced anywhere in this module, and no other source table is touched.
"""
from __future__ import annotations

import duckdb

from base import AS_OF  # noqa: F401 (kept for parity with every other builder; not time-filtered)

IS_TABLES_COLUMNS = [
    "table_catalog", "table_schema", "table_name", "table_type", "is_insertable_into",
    "commit_action", "table_owner", "comment", "created", "created_by", "last_altered",
    "last_altered_by", "data_source_format", "storage_sub_directory", "storage_path",
]
DC_COLUMNS = [
    "catalog_name", "class_tag", "column_name", "confidence", "data_type",
    "first_detected_time", "frequency", "latest_detected_time", "schema_name", "table_name",
]


def _table_row(catalog: str, schema: str, name: str, table_type: str, **overrides) -> dict:
    row = {c: None for c in IS_TABLES_COLUMNS}
    row.update(
        table_catalog=catalog, table_schema=schema, table_name=name, table_type=table_type,
        is_insertable_into="YES" if table_type != "VIEW" else "NO",
        table_owner="cl_owner", created=AS_OF, created_by="cl_owner",
        last_altered=AS_OF, last_altered_by="cl_owner", data_source_format="DELTA",
    )
    unknown = set(overrides) - set(IS_TABLES_COLUMNS)
    if unknown:
        raise ValueError(f"unknown information_schema__tables column(s): {sorted(unknown)}")
    row.update(overrides)
    return row


def _dc_row(catalog: str, schema: str, table: str, class_tag: str, column: str) -> dict:
    return {
        "catalog_name": catalog, "schema_name": schema, "table_name": table,
        "class_tag": class_tag, "column_name": column, "confidence": "HIGH", "data_type": "STRING",
        "first_detected_time": "2026-08-01 00:00:00", "frequency": "0.90",
        "latest_detected_time": "2026-09-15 00:00:00",
    }


def _insert(con: duckdb.DuckDBPyConnection, table: str, columns: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f'INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})',
        [[r[c] for c in columns] for r in rows],
    )


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17): a single module-level build(con).
# ---------------------------------------------------------------------------------------------
def build(con: duckdb.DuckDBPyConnection) -> None:
    tables: list[dict] = []
    dc: list[dict] = []

    # cl_cat_full: 3 non-VIEW tables, all 3 classified -> 100% -> OK.
    tables += [
        _table_row("cl_cat_full", "cl_schema", "cl_full_t1", "MANAGED"),
        _table_row("cl_cat_full", "cl_schema", "cl_full_t2", "MANAGED"),
        _table_row("cl_cat_full", "cl_schema", "cl_full_t3", "EXTERNAL"),
    ]
    dc += [
        _dc_row("cl_cat_full", "cl_schema", "cl_full_t1", "PII", "col_a"),
        # second, differently-tagged row for the SAME table: proves the query's DISTINCT dedupes
        # a multiply-classified table to one hit, not two.
        _dc_row("cl_cat_full", "cl_schema", "cl_full_t1", "FINANCIAL", "col_b"),
        _dc_row("cl_cat_full", "cl_schema", "cl_full_t2", "PII", "col_a"),
        _dc_row("cl_cat_full", "cl_schema", "cl_full_t3", "PII", "col_a"),
    ]

    # cl_cat_partial: 4 non-VIEW tables, only 1 classified -> 25% -> WARN.
    tables += [
        _table_row("cl_cat_partial", "cl_schema", "cl_partial_t1", "MANAGED"),
        _table_row("cl_cat_partial", "cl_schema", "cl_partial_t2", "MANAGED"),
        _table_row("cl_cat_partial", "cl_schema", "cl_partial_t3", "MANAGED"),
        _table_row("cl_cat_partial", "cl_schema", "cl_partial_t4", "MANAGED"),
    ]
    dc.append(_dc_row("cl_cat_partial", "cl_schema", "cl_partial_t1", "PII", "col_a"))

    # cl_cat_zero: 2 non-VIEW tables, none classified -> 0% -> WARN (a real, honest zero).
    tables += [
        _table_row("cl_cat_zero", "cl_schema", "cl_zero_t1", "MANAGED"),
        _table_row("cl_cat_zero", "cl_schema", "cl_zero_t2", "MANAGED"),
    ]

    # Exclusion proofs (see module docstring): none of these may ever appear in a catalog's
    # tables_total or tables_classified.
    tables.append(_table_row("cl_cat_full", "cl_schema", "cl_full_view", "VIEW"))
    dc.append(_dc_row("cl_cat_full", "cl_schema", "cl_full_view", "PII", "col_a"))
    tables.append(_table_row("cl_cat_full", "information_schema", "cl_infoschema_excluded", "MANAGED"))
    tables.append(_table_row("system", "cl_verify_system", "cl_verify_system_excluded", "MANAGED"))

    _insert(con, "information_schema__tables", IS_TABLES_COLUMNS, tables)
    _insert(con, "data_classification__results", DC_COLUMNS, dc)
