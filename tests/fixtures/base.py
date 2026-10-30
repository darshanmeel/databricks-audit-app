"""tests/fixtures/base.py

Shared framework every fixture builder module under tests/fixtures/ (this one, drilldown.py, and
every later batch's billing.py / compute.py / lakeflow.py / ...) is built on:

  AS_OF            the fixed "now" every builder's synthetic rows are placed relative to.
  Builder          the shape build_fixtures.py expects from a module it auto-discovers: a single
                   module-level build(con) function (DEC-17 - there is no class to subclass).
  write_parquet()  copies the current contents of an in-memory table to the parquet folder
                   dbt-duckdb's sources union by name at read time.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

import duckdb

# Fixed as-of instant every builder's fixture data is built relative to. Matches the pinned
# test-target literals in dbt/macros/audit_time.sql exactly: audit_today() = DATE '2026-09-21',
# audit_now() = TIMESTAMP '2026-09-21 12:00:00' (T-05). A fixture built against any other "now"
# (e.g. datetime.now()) will not line up with those pinned SQL literals - never use datetime.now()
# in a builder.
AS_OF = datetime(2026, 9, 21, 12, 0, 0)

ROOT = Path(__file__).resolve().parent.parent.parent
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"


class Builder(Protocol):
    """The shape build_fixtures.py auto-discovers (DEC-17): a module-level function named
    `build`, matching the subaudit's own `build(con)` signature so a port is close to
    copy-paste. This Protocol documents the shape; it is never subclassed."""

    def __call__(self, con: duckdb.DuckDBPyConnection) -> None: ...


def write_parquet(con: duckdb.DuckDBPyConnection, schema: str, table: str, builder_name: str) -> Path:
    """COPY the current contents of the in-memory table "<schema>__<table>" to
    tests/fixtures/parquet/<schema>__<table>/<builder_name>.parquet.

    Every builder's parquet for the same source lands in the same folder under a different file
    name (its own builder_name) and dbt-duckdb's `external_location` (T-05) unions them by name at
    read time - so two builders can each own a slice of the same source table without either one
    needing to know about the other.
    """
    out_dir = PARQUET_DIR / f"{schema}__{table}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{builder_name}.parquet"
    path_literal = out_path.as_posix().replace("'", "''")
    con.execute(f"COPY (SELECT * FROM \"{schema}__{table}\") TO '{path_literal}' (FORMAT PARQUET)")
    return out_path
