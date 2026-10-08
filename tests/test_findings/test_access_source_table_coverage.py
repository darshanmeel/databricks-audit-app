"""tests/test_findings/test_access_source_table_coverage.py

Proves findings.f_access_source_table_coverage (grain [schema_name, table_name], window_days =
30) has exactly one row per one of the 45 inventoried system tables, and that its status-
derivation logic holds for every row: only the 5 tables that fill every day (billing.usage,
access.audit, query.history, compute.node_timeline, lakeflow.job_run_timeline) run the gap/lag
state machine (CRITICAL iff total_row_count = 0; WARN iff rows_in_window = 0 OR
days_with_data_in_window < 29 OR lag_days > 2, else OK); the other 40 tables - change-time tables
that only update sometimes, and tables that are sparse or legitimately empty on many accounts -
always read OK regardless of row count, gap or lag.

This query's grain is the SYSTEM TABLE itself, account-wide, with no scenario-id column - unlike
every other query's own fixture rows, several OTHER builders (tests/fixtures/governance.py,
tests/fixtures/sensitive.py) also write to some of the same tables tests/fixtures/compute_
coverage.py uses for this query's scenarios (see that module's own docstring). So instead of
hardcoding per-table row counts, most assertions below are self-consistency invariants checked
across all 45 rows - true no matter what any other builder's fixture contains, today or in the
future - plus two tables (column_lineage, table_privileges) where compute_coverage.py's own rows
are enough on their own to guarantee the outcome regardless of bleed-through.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "access_source_table_coverage.yml"
QID = "access_source_table_coverage"

# The only 5 tables that run the gap/lag state machine - see the query's own caveats.
FILLS_EVERY_DAY = {
    ("billing", "usage"), ("access", "audit"), ("query", "history"),
    ("compute", "node_timeline"), ("lakeflow", "job_run_timeline"),
}


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _rows():
    return dbutil.rows(QID, 30)


def _by_table(schema_name: str, table_name: str) -> dict:
    matches = [r for r in _rows() if r["schema_name"] == schema_name and r["table_name"] == table_name]
    assert len(matches) == 1, f"expected exactly one row for {schema_name}.{table_name}"
    return matches[0]


def test_grain_uniqueness_and_count():
    grains = _grains()
    cols = grains[QID]
    out = _rows()
    assert len(out) == 45  # 47 system tables minus data_classification.results and
    # storage.predictive_optimization_operations_history (both excluded -- see the query's own
    # header caveats)
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_status_enum():
    for r in _rows():
        assert r["status"] in {"OK", "WARN", "CRITICAL"}
        assert r["catalog_name"] == "system"


def test_excluded_tables_absent():
    names = {(r["schema_name"], r["table_name"]) for r in _rows()}
    assert ("data_classification", "results") not in names
    assert ("storage", "predictive_optimization_operations_history") not in names


def test_critical_iff_fills_every_day_table_is_empty():
    """CRITICAL is possible only for the 5 fills-every-day tables, and only when they have zero
    rows anywhere - checked across every one of the 45 rows, so this holds regardless of which (if
    any) fills-every-day table is genuinely empty in the shared fixture."""
    for r in _rows():
        key = (r["schema_name"], r["table_name"])
        if key in FILLS_EVERY_DAY and r["total_row_count"] == 0:
            assert r["status"] == "CRITICAL", key
        else:
            assert r["status"] != "CRITICAL", key


def test_no_time_column_never_warns():
    """A table with no time column has only two possible outcomes (CRITICAL or OK) - there is no
    window/gap/lag concept for it to WARN on."""
    for r in _rows():
        if not r["has_time_column"]:
            assert r["status"] != "WARN", (r["schema_name"], r["table_name"])
            assert r["rows_in_window"] == r["total_row_count"]
            assert r["days_with_data_in_window"] is None
            assert r["lag_days"] is None


def test_warn_matches_the_gap_and_lag_rule_for_fills_every_day_tables_only():
    """For each of the 5 fills-every-day tables with at least one row ever, WARN iff
    rows_in_window = 0, or the window has a gap (days_with_data_in_window < 29), or the newest row
    is stale (lag_days > 2) - re-derived from each row's own columns, so this is true for whatever
    real data the shared fixture holds, not a fixed scenario. Every other table - change-time
    tables that only update sometimes, and tables that are sparse or legitimately empty on many
    accounts - always reads OK, whatever its own row count, gap or lag looks like."""
    for r in _rows():
        key = (r["schema_name"], r["table_name"])
        if key in FILLS_EVERY_DAY and r["total_row_count"] > 0:
            should_warn = (
                r["rows_in_window"] == 0
                or r["days_with_data_in_window"] < 29
                or r["lag_days"] > 2
            )
            assert r["status"] == ("WARN" if should_warn else "OK"), key
        elif key not in FILLS_EVERY_DAY:
            assert r["status"] == "OK", key


def test_table_lineage_and_inbound_and_outbound_have_at_least_their_own_rows():
    """This module's own rows for these three tables never disappear even though another
    builder's rows can change the table's overall status (see module docstring)."""
    assert _by_table("access", "table_lineage")["total_row_count"] >= 1
    assert _by_table("access", "inbound_network")["total_row_count"] >= 2
    assert _by_table("access", "outbound_network")["total_row_count"] >= 25


def test_ok_gap_free_fresh_with_time_column():
    """access.column_lineage is not one of the 5 fills-every-day tables, so it always reads OK
    regardless of gap or lag; this module's own row/day for D(0)..D(29) also happens to leave it
    gap-free and fresh, so both readings agree here."""
    r = _by_table("access", "column_lineage")
    assert r["has_time_column"] is True
    assert r["total_row_count"] >= 30
    assert r["days_with_data_in_window"] == 30
    assert r["lag_days"] == 0
    assert r["status"] == "OK"


def test_ok_without_time_column():
    """information_schema.table_privileges: this module's own three rows already guarantee
    total_row_count > 0, so OK is guaranteed regardless of what else is in the shared fixture."""
    r = _by_table("information_schema", "table_privileges")
    assert r["has_time_column"] is False
    assert r["total_row_count"] >= 3
    assert r["status"] == "OK"
