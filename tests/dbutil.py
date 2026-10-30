"""tests/dbutil.py -- read-only access to the dbt-built test database for every
tests/test_findings/*.py file, in this batch and every later fixture batch (T-09 onward).

    rows(query_id, window_days, workspace_ids=None) -> list[dict]
        reads findings.f_<query_id> WHERE window_days = ? (optionally AND workspace_id IN (...))
        from tests/db_audit_test.duckdb, read-only.

    usage_sum(where) -> float
        sums billing.usage.usage_quantity (every builder's parquet slice, unioned by name --
        the raw fixture source, not a dbt-built table) filtered by a raw SQL WHERE fragment. Not
        exercised by this batch's own tests (none of the three drill-down queries read
        billing.usage), but the signature must exist and be correct so later fixture batches can
        import it unchanged, to compute an expectation independently of any one dbt model's own
        aggregation logic -- straight from the builder's own parquet, per the "never hard-code a
        total" rule.

The database itself is built by `python tools/dbt_run.py build --target test` (tools/gate.py's
dbt_build_test step; DEC-45's tests/db_audit_test.duckdb file) against tests/fixtures/parquet
(written by tests/fixtures/build_fixtures.py) -- this module never builds it, only reads it.
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "tests" / "db_audit_test.duckdb"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"

_QUERY_ID_RE = re.compile(r"[a-z][a-z0-9_]*")


def _connect() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} does not exist yet -- run "
            "`python tests/fixtures/build_fixtures.py` then "
            "`python tools/dbt_run.py build --target test` first"
        )
    return duckdb.connect(str(DB_PATH), read_only=True)


def rows(query_id: str, window_days: int, workspace_ids: list[str] | None = None) -> list[dict]:
    """SELECT * FROM findings.f_<query_id> WHERE window_days = <window_days>, optionally scoped
    to workspace_ids, as a list of plain dicts (column name -> value).

    query_id is interpolated directly into the `findings."f_<query_id>"` identifier (DuckDB has
    no parameter placeholder for identifiers), so it is validated against the same charset every
    real query_id uses (lowercase, digits, underscore, starting with a letter) before that
    happens -- a bad value raises ValueError instead of being spliced into SQL unchecked."""
    if not _QUERY_ID_RE.fullmatch(query_id):
        raise ValueError(f"bad query_id {query_id!r}")
    sql = f'SELECT * FROM findings."f_{query_id}" WHERE window_days = ?'
    params: list = [window_days]
    if workspace_ids:
        placeholders = ", ".join("?" for _ in workspace_ids)
        sql += f" AND workspace_id IN ({placeholders})"
        params.extend(workspace_ids)
    con = _connect()
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def usage_sum(where: str) -> float:
    """SUM(usage_quantity) over the raw billing.usage fixture parquet (every builder's slice,
    unioned by name -- the same source dbt itself reads via read_parquet, not the dbt-built
    findings tables), filtered by the raw SQL WHERE fragment `where` (no leading "WHERE").
    Returns 0.0 when no row matches or the source has no parquet written yet."""
    pattern = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT SUM(usage_quantity) FROM read_parquet('{pattern}', union_by_name=true) WHERE {where}"
        result = con.execute(sql).fetchone()
        return float(result[0]) if result and result[0] is not None else 0.0
    finally:
        con.close()


# The pinned "today" audit_today() resolves to on the `test` dbt target (dbt/macros/audit_time.sql)
# -- every builder's D(n) is relative to this same instant, so a raw recomputation here sees the
# exact window a compiled finding model does.
TEST_TODAY = "2026-09-21"


def deduped_list_prices_sql(price_pattern: str) -> str:
    """The fixture's list prices with overlapping open-ended rows capped at the next row's start --
    written out here, not read from the dbt list_prices() macro, so a bug there can't hide itself."""
    return f"""
        SELECT sku_name, cloud, usage_unit, price_start_time, end_time AS price_end_time,
               CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
        FROM (
            SELECT *, CASE
                       WHEN price_end_time IS NULL THEN next_start
                       WHEN next_start IS NULL THEN price_end_time
                       WHEN price_end_time <= next_start THEN price_end_time
                       ELSE next_start
                   END AS end_time
            FROM (
                SELECT *, LEAD(price_start_time) OVER (
                           PARTITION BY sku_name, cloud, usage_unit
                           ORDER BY price_start_time, price_end_time NULLS LAST) AS next_start
                FROM read_parquet('{price_pattern}', union_by_name=true)
                WHERE currency_code = 'USD'
            ) ranked
        ) capped
        WHERE end_time IS NULL OR end_time > price_start_time
    """


def priced_usage_total(period_days: int, current: bool, only_null_workspace: bool = False) -> float | None:
    """Independently recomputes SUM(usage_quantity * effective_list_rate) across EVERY builder's
    billing.usage parquet slice (union_by_name=true, the same raw source usage_sum reads) joined to
    every builder's billing.list_prices slice (overlaps de-duplicated, as the app's list_prices()
    macro does), with the effective-list-price join every priced cost query uses (DEC-66.1), for
    the current period
    [TEST_TODAY-period_days, TEST_TODAY) or the previous period
    [TEST_TODAY-2*period_days, TEST_TODAY-period_days).

    Used by any test whose finding is scoped to the WHOLE account (not one builder's own dedicated
    id prefix or usage_unit) -- cost_chargeback_by_tag_value's share_of_total_pct denominator, for
    one -- so the test never hard-codes a whole-suite dollar figure that would silently go stale
    whenever another fixture's billing rows change. only_null_workspace=True instead scopes the sum
    to workspace_id IS NULL, the one "workspace" cost_chargeback_by_tag_value's own (untagged) rows
    are now computed within (DEC-15 account-level usage), since that subtraction is per workspace
    and every scenario tag_value's own fixture builder is one of several contributing NULL-workspace
    rows. Returns None if every row on that side was unpriced (no COALESCE to 0 -- callers compare
    against a query's own NULL-vs-real-zero handling)."""
    usage_pattern = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    price_pattern = (PARQUET_DIR / "billing__list_prices" / "*.parquet").as_posix()
    lo_days = period_days * (1 if current else 2)
    hi_days = 0 if current else period_days
    ws_clause = "AND u.workspace_id IS NULL" if only_null_workspace else ""
    con = duckdb.connect()
    try:
        sql = f"""
            WITH price AS ({deduped_list_prices_sql(price_pattern)})
            SELECT SUM(u.usage_quantity * p.list_rate)
            FROM read_parquet('{usage_pattern}', union_by_name=true) u
            LEFT JOIN price p
              ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
             AND u.usage_end_time >= p.price_start_time
             AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
            WHERE u.usage_date >= DATE '{TEST_TODAY}' - INTERVAL '{lo_days}' DAY
              AND u.usage_date <  DATE '{TEST_TODAY}' - INTERVAL '{hi_days}' DAY
              {ws_clause}
        """
        result = con.execute(sql).fetchone()
        return float(result[0]) if result and result[0] is not None else None
    finally:
        con.close()
