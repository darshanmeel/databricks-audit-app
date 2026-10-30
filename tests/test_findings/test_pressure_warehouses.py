"""tests/test_findings/test_pressure_warehouses.py -- T-65 (DEC-65), the warehouse half.

Proves findings.f_query_warehouse_pressure against tests/fixtures/pressure_warehouses.py's own
`pr_` rows (workspace 1111, DEC-15: every assertion filters on `pr_wh_` warehouse ids, never on
the workspace alone -- query_history.py's `qh_` warehouses share 1111 and land in the same table as
NOT_ASSESSED rows). Per tests/test_findings/README.md:

  - grain [window_days, workspace_id, warehouse_id] unique at 7 / 30 / 90, and nothing at 0;
  - the output columns in the order the task contract fixes;
  - every scenario's pressure / scaling_hint / status / at_ceiling and its pressure_reason text;
  - every value of the pressure, scaling_hint, status and not_assessed_reason enums at least once;
  - not_assessed_reason is NULL iff status is not NOT_ASSESSED, and scaling_hint (and
    pressure_reason) is NULL iff pressure is NULL;
  - worst-first physical order (status rank, then spill_time_pct DESC, capacity_wait_pct DESC,
    warehouse_id), read as returned, never re-sorted;
  - the window: pr_wh_win40 only at 90, pr_wh_win10 at 30 and 90 but not 7;
  - no row for the serverless statements (warehouse_id NULL), which do exist in the raw parquet;
  - spill_time_pct / capacity_wait_pct / cold_start_wait_pct / query_count / the worst day's
    spill and the GB / seconds columns cross-checked against an independent SUM over the raw
    query__history parquet (every builder's slice, unioned by name) -- never a hard-coded total on
    its own;
  - every threshold in an expectation (the reason text, "below the warn band") resolved the way
    dbt/macros/param.sql does (config/thresholds.yml [query][name], then ['_all'][name], then the
    header default), never spelled as a literal;
  - review round 1: the memory WARN band (share and volume), a mixed-band row whose status is the
    worse band, and both bounds of the warehouse_events window plus today's exclusion
    (pr_wh_edge);
  - review round 2: a warehouse with no autoscale range is at its ceiling from its configuration
    (pr_wh_fixed and every 1..1 warehouse), the mixed band in the other direction (memory CRITICAL,
    capacity WARN: pr_wh_memcrit_capwarn), shares a hair below their thresholds judged unrounded
    and never written as the threshold (pr_wh_nearwarn), a reason that quotes the threshold of the
    band it reached, and the configuration columns (warehouse_type, auto_stop_minutes) read from
    the builder's own config table;
  - review round 2, orchestrator decisions: a statement spills only once it reaches
    :min_stmt_spill_mb (pr_wh_trivial_spill: CRITICAL on any spilled byte, NONE with the floor),
    and a MEMORY_AND_CAPACITY warehouse below its ceiling reads SCALE_UP_THEN_WARM (pr_wh_mixed).
"""
from __future__ import annotations

import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
sys.path.insert(0, str(ROOT / "tools"))
import dbutil  # noqa: E402
import header_schema  # noqa: E402
import pressure_warehouses as pw  # noqa: E402

QUERY_ID = "query_warehouse_pressure"
SQL_PATH = ROOT / "app" / "queries" / "app" / "performance" / f"{QUERY_ID}.sql"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WINDOWS = (7, 30, 90)
STATUS_RANK = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}

# The contract's output columns, in order (the generator prepends window_days).
COLUMNS = [
    "window_days",
    "workspace_id", "warehouse_id", "compute_type", "warehouse_type", "warehouse_size",
    "min_clusters", "max_clusters", "auto_stop_minutes", "warehouse_deleted", "query_count",
    "finished_count", "spilling_query_count", "spill_query_pct", "spill_time_pct",
    "spilled_local_gb_sum", "spilled_local_gb_max_day", "read_gb_sum", "shuffle_read_gb_sum",
    "queued_at_capacity_count", "capacity_wait_pct", "waiting_at_capacity_s_sum",
    "waited_for_compute_count", "cold_start_wait_pct", "waiting_for_compute_s_sum",
    "execution_s_sum", "total_duration_s_sum", "max_cluster_count_seen", "scaling_events",
    "at_ceiling", "pressure", "scaling_hint", "pressure_reason", "not_assessed_reason", "status",
]

# warehouse_id -> (pressure, scaling_hint, status, at_ceiling), per the builder's docstring.
EXPECTED = {
    "pr_wh_mem": ("MEMORY", "SCALE_UP", "CRITICAL", False),
    "pr_wh_bigspill": ("MEMORY", "SCALE_UP", "CRITICAL", True),
    "pr_wh_cap_ceiling": ("CAPACITY", "SCALE_OUT", "CRITICAL", True),
    "pr_wh_cap_lag": ("CAPACITY", "KEEP_WARM", "WARN", False),
    "pr_wh_both": ("MEMORY_AND_CAPACITY", "SCALE_UP_THEN_OUT", "CRITICAL", True),
    "pr_wh_cold": ("NONE", "NONE", "OK", True),
    "pr_wh_ok": ("NONE", "NONE", "OK", None),
    "pr_wh_few": (None, None, "NOT_ASSESSED", True),
    "pr_wh_noconf": ("CAPACITY", "SCALE_OUT", "WARN", None),
    "pr_wh_deleted": ("NONE", "NONE", "OK", True),
    "pr_wh_win40": ("NONE", "NONE", "OK", True),
    "pr_wh_win10": ("CAPACITY", "SCALE_OUT", "WARN", True),
    "pr_wh_memwarn": ("MEMORY", "SCALE_UP", "WARN", True),
    "pr_wh_volwarn": ("MEMORY", "SCALE_UP", "WARN", True),
    "pr_wh_mixed": ("MEMORY_AND_CAPACITY", "SCALE_UP_THEN_WARM", "CRITICAL", False),
    "pr_wh_edge": ("CAPACITY", "SCALE_OUT", "WARN", True),
    # review round 2
    "pr_wh_fixed": ("CAPACITY", "SCALE_OUT", "WARN", True),
    "pr_wh_memcrit_capwarn": ("MEMORY_AND_CAPACITY", "SCALE_UP_THEN_OUT", "CRITICAL", None),
    "pr_wh_nearwarn": ("NONE", "NONE", "OK", None),
    # orchestrator decision: the :min_stmt_spill_mb floor
    "pr_wh_trivial_spill": ("NONE", "NONE", "OK", True),
}
# review round 2: a warehouse with no autoscale range (min_clusters >= max_clusters) reads
# at_ceiling TRUE from its configuration alone -- every 1..1 warehouse of the builder.
FIXED_SIZE = {wh for wh, _size, lo, hi, _stop in pw._CONFIG if lo >= hi} | {"pr_wh_deleted"}
# pr_wh_edge's 20-day-old event (cluster_count 3 = max_clusters) is outside window 7 only.
EXPECTED_AT = {7: {"pr_wh_edge": ("CAPACITY", "KEEP_WARM", "WARN", False)}}
PRESENT = {
    7: set(EXPECTED) - {"pr_wh_win40", "pr_wh_win10"},
    30: set(EXPECTED) - {"pr_wh_win40"},
    90: set(EXPECTED),
}


def _param(name: str):
    """dbt/macros/param.sql's lookup order: thresholds.yml [qid][name] -> ['_all'][name] -> header."""
    doc = yaml.safe_load((ROOT / "config" / "thresholds.yml").read_text(encoding="utf-8")) or {}
    for scope in (doc.get(QUERY_ID) or {}, doc.get("_all") or {}):
        if isinstance(scope, dict) and name in scope:
            return scope[name]
    hdr = header_schema.parse_header(SQL_PATH.read_text(encoding="utf-8"))
    return {p["name"]: p["default"] for p in hdr["params"]}[name]


def _mem_clause(pct: int, gb: str, band: str) -> str:
    """The memory clause, quoting the thresholds of the band the row reached (review round 2)."""
    lvl = "crit" if band == "CRITICAL" else "warn"
    return (f"{pct}% of execution time was in statements that spilled to local disk "
            f"(threshold {_param(lvl + '_spill_time_pct')}%); worst day spilled {gb} GB "
            f"(threshold {_param(lvl + '_spill_gb')} GB)")


def _cap_clause(pct: int, clusters: str, band: str) -> str:
    lvl = "crit" if band == "CRITICAL" else "warn"
    return (f"{pct}% of statement time was spent queued at capacity "
            f"(threshold {_param(lvl + '_queue_time_pct')}%); {clusters}")


def _no_range(lo: int, hi: int) -> str:
    return f"no autoscale range (min_clusters {lo}, max_clusters {hi})"


def _none_share(raw_pct: float, warn) -> str:
    """A NONE row's share: whole percent, or "just under <warn>" when it rounds up to it."""
    whole = int(_half_up(raw_pct))
    return f"just under {warn}" if whole >= warn else str(whole)


def _rows(window_days: int) -> list[dict]:
    """Every row of the finding in workspace 1111 at one window, in the model's own order."""
    return dbutil.rows("query_warehouse_pressure", window_days, workspace_ids=["1111"])


def _pr(rows: list[dict]) -> list[dict]:
    return [r for r in rows if (r["warehouse_id"] or "").startswith("pr_wh_")]


def _raw(where: str, params: list, window_days: int) -> dict:
    """Independent sums over the raw query__history parquet (not the dbt-built table), with the
    query's own window: start_time in [2026-09-21 - window_days, 2026-09-21), NULL read as 0.
    spill_ms / spilling_n apply the query's :min_stmt_spill_mb floor (MB = 10^6 bytes);
    any_spill_ms counts a statement's time on any spilled byte (the rule the floor replaced)."""
    glob = (PARQUET_DIR / "query__history" / "*.parquet").as_posix()
    floor = float(_param("min_stmt_spill_mb")) * 1e6
    con = duckdb.connect()
    try:
        sql = f"""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN COALESCE(spilled_local_bytes, 0) > 0
                             AND COALESCE(spilled_local_bytes, 0) >= {floor}
                            THEN COALESCE(execution_duration_ms, 0) ELSE 0 END) AS spill_ms,
                   SUM(CASE WHEN COALESCE(spilled_local_bytes, 0) > 0
                             AND COALESCE(spilled_local_bytes, 0) >= {floor}
                            THEN 1 ELSE 0 END) AS spilling_n,
                   SUM(CASE WHEN COALESCE(spilled_local_bytes, 0) > 0
                            THEN COALESCE(execution_duration_ms, 0) ELSE 0 END) AS any_spill_ms,
                   SUM(COALESCE(spilled_local_bytes, 0)) AS spill_bytes,
                   SUM(COALESCE(execution_duration_ms, 0)) AS exec_ms,
                   SUM(COALESCE(waiting_at_capacity_duration_ms, 0)) AS cap_ms,
                   SUM(COALESCE(waiting_for_compute_duration_ms, 0)) AS cold_ms,
                   SUM(COALESCE(total_duration_ms, 0)) AS total_ms,
                   SUM(COALESCE(read_bytes, 0)) AS read_bytes,
                   SUM(COALESCE(shuffle_read_bytes, 0)) AS shuffle_bytes
            FROM read_parquet('{glob}', union_by_name=true)
            WHERE {where}
              AND start_time >= DATE '2026-09-21' - INTERVAL {int(window_days)} DAY
              AND start_time < DATE '2026-09-21'
        """
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))
    finally:
        con.close()


def _raw_worst_day_gb(warehouse_id: str, window_days: int) -> float:
    glob = (PARQUET_DIR / "query__history" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"""
            SELECT MAX(day_bytes) FROM (
              SELECT CAST(start_time AS DATE) AS d, SUM(COALESCE(spilled_local_bytes, 0)) AS day_bytes
              FROM read_parquet('{glob}', union_by_name=true)
              WHERE compute.warehouse_id = ?
                AND start_time >= DATE '2026-09-21' - INTERVAL {int(window_days)} DAY
                AND start_time < DATE '2026-09-21'
              GROUP BY 1
            )
        """
        return round(con.execute(sql, [warehouse_id]).fetchone()[0] / 1e9, 2)
    finally:
        con.close()


def _pct(num, den) -> float | None:
    return None if not den else round(num * 100.0 / den, 1)


def _half_up(x, places: int = 0) -> float:
    """ROUND(x, places) as both engines do it: half away from zero, never banker's rounding."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


# -------------------------------------------------------------------------------------------
# grain, columns, enums, NULL-iff rules -- every window
# -------------------------------------------------------------------------------------------
def test_grain_columns_and_window_zero():
    for w in WINDOWS:
        out = _rows(w)
        assert out, f"no rows at window {w}"
        assert all(r["window_days"] == w for r in out)
        keys = [(r["window_days"], r["workspace_id"], r["warehouse_id"]) for r in out]
        assert len(keys) == len(set(keys)), f"duplicate grain at window {w}"
        assert list(out[0].keys()) == COLUMNS, list(out[0].keys())
    assert _rows(0) == []


def test_enums_and_null_iff_rules():
    for w in WINDOWS:
        for r in _rows(w):
            assert r["status"] in STATUS_RANK, r
            assert r["pressure"] in {None, "MEMORY", "CAPACITY", "MEMORY_AND_CAPACITY", "NONE"}, r
            assert r["scaling_hint"] in {
                None, "SCALE_UP", "SCALE_OUT", "KEEP_WARM", "SCALE_UP_THEN_OUT",
                "SCALE_UP_THEN_WARM", "NONE"}, r
            # the lever follows the verdict and at_ceiling: below the ceiling (FALSE) the step
            # after size is a warm cluster; at it, or unseen (NULL), it is more clusters
            below = r["at_ceiling"] is False
            assert r["scaling_hint"] == {
                None: None, "NONE": "NONE", "MEMORY": "SCALE_UP",
                "CAPACITY": "KEEP_WARM" if below else "SCALE_OUT",
                "MEMORY_AND_CAPACITY": "SCALE_UP_THEN_WARM" if below else "SCALE_UP_THEN_OUT",
            }[r["pressure"]], r
            assert r["not_assessed_reason"] in {None, "too_few_statements"}, r
            # NOT_ASSESSED is a reason, never a blank -- and only then
            assert (r["not_assessed_reason"] is None) == (r["status"] != "NOT_ASSESSED"), r
            assert (r["scaling_hint"] is None) == (r["pressure"] is None), r
            assert (r["pressure_reason"] is None) == (r["pressure"] is None), r
            assert (r["status"] == "NOT_ASSESSED") == (r["pressure"] is None), r
            assert r["warehouse_id"] is not None, "a statement with no warehouse_id became a row"

    pr30 = _pr(_rows(30))
    assert {r["pressure"] for r in pr30} == {
        None, "MEMORY", "CAPACITY", "MEMORY_AND_CAPACITY", "NONE"}
    assert {r["scaling_hint"] for r in pr30} == {
        None, "SCALE_UP", "SCALE_OUT", "KEEP_WARM", "SCALE_UP_THEN_OUT", "SCALE_UP_THEN_WARM",
        "NONE"}
    assert {r["status"] for r in pr30} == set(STATUS_RANK)
    assert {r["not_assessed_reason"] for r in pr30} == {None, "too_few_statements"}
    assert {r["at_ceiling"] for r in pr30} == {True, False, None}


# -------------------------------------------------------------------------------------------
# every scenario, at every window it is present in; the window exclusions
# -------------------------------------------------------------------------------------------
def test_scenarios_by_window():
    for w in WINDOWS:
        by_wh = {r["warehouse_id"]: r for r in _pr(_rows(w))}
        assert set(by_wh) == PRESENT[w], (w, sorted(by_wh))
        for wh in PRESENT[w]:
            r = by_wh[wh]
            got = (r["pressure"], r["scaling_hint"], r["status"], r["at_ceiling"])
            assert got == EXPECTED_AT.get(w, {}).get(wh, EXPECTED[wh]), (w, wh, got)
            assert r["compute_type"] == "WAREHOUSE", (w, wh)
    assert "pr_wh_win40" in {r["warehouse_id"] for r in _rows(90)}
    assert "pr_wh_win40" not in {r["warehouse_id"] for r in _rows(30)}
    assert "pr_wh_win40" not in {r["warehouse_id"] for r in _rows(7)}
    assert "pr_wh_win10" not in {r["warehouse_id"] for r in _rows(7)}


def test_scenario_values_at_30():
    by_wh = {r["warehouse_id"]: r for r in _pr(_rows(30))}

    mem = by_wh["pr_wh_mem"]
    assert mem["query_count"] == 40 and mem["spilling_query_count"] == 4
    assert mem["spill_time_pct"] == 87.0 and mem["spill_query_pct"] == 10.0
    assert mem["spilled_local_gb_max_day"] == 2.0 and mem["spilled_local_gb_sum"] == 2.0
    # SCD2: the latest config row (X_SMALL, 1..2), not the older SMALL 1..4 row
    assert (mem["warehouse_size"], mem["min_clusters"], mem["max_clusters"]) == ("X_SMALL", 1, 2)
    assert mem["max_cluster_count_seen"] == 1 and mem["warehouse_deleted"] is False
    assert mem["pressure_reason"] == _mem_clause(87, "2.0", "CRITICAL")
    assert mem["spill_time_pct"] >= _param("crit_spill_time_pct")
    # NULL waits read as 0 (the fixture writes this scenario's zeros as NULL)
    assert mem["capacity_wait_pct"] == 0.0 and mem["queued_at_capacity_count"] == 0

    big = by_wh["pr_wh_bigspill"]
    assert big["spill_time_pct"] == 2.5
    assert big["spill_time_pct"] < _param("warn_spill_time_pct"), "the share alone is below the warn band"
    assert big["spilled_local_gb_max_day"] == 15.0
    assert big["spilled_local_gb_max_day"] >= _param("crit_spill_gb"), "the rare-but-huge case flags on volume"
    assert big["pressure_reason"] == _mem_clause(3, "15.0", "CRITICAL")

    ceil = by_wh["pr_wh_cap_ceiling"]
    assert ceil["query_count"] == 50 and ceil["queued_at_capacity_count"] == 25
    assert ceil["capacity_wait_pct"] == 30.0
    assert (ceil["max_cluster_count_seen"], ceil["min_clusters"], ceil["max_clusters"]) == (1, 1, 1)
    assert ceil["capacity_wait_pct"] >= _param("crit_queue_time_pct")
    assert ceil["pressure_reason"] == _cap_clause(30, _no_range(1, 1), "CRITICAL")

    lag = by_wh["pr_wh_cap_lag"]
    assert lag["capacity_wait_pct"] == 8.0
    assert (lag["max_cluster_count_seen"], lag["max_clusters"]) == (2, 4)
    assert lag["scaling_events"] == 2
    assert _param("warn_queue_time_pct") <= lag["capacity_wait_pct"] < _param("crit_queue_time_pct")
    assert lag["pressure_reason"] == _cap_clause(8, "up to 2 of 4 clusters seen", "WARN")

    both = by_wh["pr_wh_both"]
    assert both["spill_time_pct"] == 30.0 and both["capacity_wait_pct"] == 25.0
    assert both["pressure_reason"] == (
        _mem_clause(30, "1.2", "CRITICAL") + "; " + _cap_clause(25, "up to 2 of 2 clusters seen", "CRITICAL"))

    cold = by_wh["pr_wh_cold"]
    assert cold["cold_start_wait_pct"] == 40.0, "cold start is carried as context..."
    assert cold["pressure"] == "NONE", "...and is never a verdict"
    assert cold["waited_for_compute_count"] == 12
    assert cold["query_count"] == 30 and cold["finished_count"] == 28
    assert cold["pressure_reason"] == (
        "no threshold crossed: 0% of execution time spilled, 0% of statement time queued")

    ok = by_wh["pr_wh_ok"]
    assert ok["spill_time_pct"] == 2.0 and ok["spilled_local_gb_max_day"] == 0.1
    assert ok["pressure_reason"] == (
        "no threshold crossed: 2% of execution time spilled, 0% of statement time queued")

    few = by_wh["pr_wh_few"]
    assert few["query_count"] == 5 and few["spilling_query_count"] == 5
    assert few["not_assessed_reason"] == "too_few_statements"
    assert few["pressure_reason"] is None

    noconf = by_wh["pr_wh_noconf"]
    assert noconf["capacity_wait_pct"] == 10.0
    for col in ("warehouse_type", "warehouse_size", "min_clusters", "max_clusters",
                "auto_stop_minutes", "warehouse_deleted", "max_cluster_count_seen"):
        assert noconf[col] is None, col
    assert "clusters seen unknown" in noconf["pressure_reason"]
    assert noconf["pressure_reason"] == _cap_clause(10, "clusters seen unknown", "WARN")

    deleted = by_wh["pr_wh_deleted"]
    assert deleted["warehouse_deleted"] is True, "a deleted warehouse keeps its row"
    assert deleted["query_count"] == 30

    win10 = by_wh["pr_wh_win10"]
    assert win10["capacity_wait_pct"] == 10.0 and win10["query_count"] == 25

    # review round 2: the configuration columns come from the builder's own config table, the
    # latest SCD2 row (pr_wh_cold is the one with a different auto-stop)
    for wh, size, lo, hi, stop in pw._CONFIG:
        if wh not in by_wh:
            continue
        r = by_wh[wh]
        assert (r["warehouse_type"], r["warehouse_size"], r["min_clusters"], r["max_clusters"],
                r["auto_stop_minutes"]) == (pw.WH_TYPE, size, lo, hi, stop), wh
    assert (cold["warehouse_type"], cold["auto_stop_minutes"]) == ("PRO", 5) and pw.WH_TYPE == "PRO"
    assert (mem["warehouse_type"], mem["auto_stop_minutes"]) == (pw.WH_TYPE, 10)
    assert (deleted["warehouse_type"], deleted["auto_stop_minutes"]) == (pw.WH_TYPE, 10)


def test_memory_warn_band_and_mixed_bands():
    """The memory WARN band by share (pr_wh_memwarn) and by volume (pr_wh_volwarn), and the two
    mixed-band rows: memory WARN with concurrency CRITICAL (pr_wh_mixed) and memory CRITICAL with
    concurrency WARN (pr_wh_memcrit_capwarn). The status is the worse of the two bands in both
    directions, MEMORY_AND_CAPACITY needs only WARN on each, and each clause quotes the threshold
    of its own band."""
    by_wh = {r["warehouse_id"]: r for r in _pr(_rows(30))}
    warn_pct, crit_pct = _param("warn_spill_time_pct"), _param("crit_spill_time_pct")
    warn_gb, crit_gb = _param("warn_spill_gb"), _param("crit_spill_gb")

    mw = by_wh["pr_wh_memwarn"]
    assert warn_pct <= mw["spill_time_pct"] < crit_pct and mw["spill_time_pct"] == 15.0
    assert mw["spilled_local_gb_max_day"] < warn_gb
    assert mw["pressure_reason"] == _mem_clause(15, "0.3", "WARN")

    vw = by_wh["pr_wh_volwarn"]
    assert vw["spill_time_pct"] < warn_pct and vw["spill_time_pct"] == 2.6
    assert warn_gb <= vw["spilled_local_gb_max_day"] < crit_gb and vw["spilled_local_gb_max_day"] == 3.0
    assert vw["pressure_reason"] == _mem_clause(3, "3.0", "WARN")

    mx = by_wh["pr_wh_mixed"]
    assert warn_pct <= mx["spill_time_pct"] < crit_pct and mx["spilled_local_gb_max_day"] < warn_gb
    assert mx["capacity_wait_pct"] >= _param("crit_queue_time_pct") and mx["capacity_wait_pct"] == 33.3
    assert (mx["max_cluster_count_seen"], mx["max_clusters"], mx["at_ceiling"]) == (1, 3, False)
    # below its ceiling, so the step after size is a warm cluster, not more clusters
    assert (mx["pressure"], mx["scaling_hint"], mx["status"]) == (
        "MEMORY_AND_CAPACITY", "SCALE_UP_THEN_WARM", "CRITICAL")
    assert mx["pressure_reason"] == (
        _mem_clause(15, "0.3", "WARN") + "; " + _cap_clause(33, "up to 1 of 3 clusters seen", "CRITICAL"))

    mc = by_wh["pr_wh_memcrit_capwarn"]
    assert mc["spill_time_pct"] >= crit_pct and mc["spill_time_pct"] == 30.0
    assert mc["spilled_local_gb_max_day"] < warn_gb
    assert _param("warn_queue_time_pct") <= mc["capacity_wait_pct"] < _param("crit_queue_time_pct")
    assert mc["capacity_wait_pct"] == 16.7
    assert (mc["pressure"], mc["scaling_hint"], mc["status"]) == (
        "MEMORY_AND_CAPACITY", "SCALE_UP_THEN_OUT", "CRITICAL")
    assert mc["pressure_reason"] == (
        _mem_clause(30, "0.6", "CRITICAL") + "; " + _cap_clause(17, "clusters seen unknown", "WARN"))
    assert mc["at_ceiling"] is None, "unseen keeps SCALE_UP_THEN_OUT, as CAPACITY keeps SCALE_OUT"


def test_a_statement_spills_only_once_it_reaches_the_floor():
    """pr_wh_trivial_spill: ten long statements each spill a few MB, under :min_stmt_spill_mb.
    Counting any spilled byte would put their whole execution time in spill_time_pct and read
    CRITICAL; with the floor they are not spilling statements (NONE / OK), while the GB columns
    still add every spilled byte."""
    floor_bytes = float(_param("min_stmt_spill_mb")) * 1e6
    assert 0 < pw.TRIVIAL_SPILL_BYTES < floor_bytes
    for w in WINDOWS:
        raw = _raw("compute.warehouse_id = ?", ["pr_wh_trivial_spill"], w)
        # the rule the floor replaced would have flagged it CRITICAL
        assert raw["any_spill_ms"] * 100.0 / raw["exec_ms"] >= _param("crit_spill_time_pct"), w
        assert raw["spill_ms"] == 0 and raw["spilling_n"] == 0, w
        r = {x["warehouse_id"]: x for x in _pr(_rows(w))}["pr_wh_trivial_spill"]
        assert (r["pressure"], r["scaling_hint"], r["status"]) == ("NONE", "NONE", "OK"), w
        assert r["spilling_query_count"] == 0 and r["spill_time_pct"] == 0.0, w
        assert r["spilled_local_gb_sum"] == _half_up(raw["spill_bytes"] / 1e9, 2) == 0.05, w
        assert r["spilled_local_gb_max_day"] == _raw_worst_day_gb("pr_wh_trivial_spill", w), w
        assert r["pressure_reason"] == (
            "no threshold crossed: 0% of execution time spilled, 0% of statement time queued")


def test_window_edges_today_and_the_events_bounds():
    """pr_wh_edge: today's statement is outside every window, and the warehouse_events window
    has BOTH bounds -- the 20-day-old SCALED_UP 3 is outside window 7 only, today's SCALED_UP 3 is
    outside all three. Dropping either bound flips the window-7 row to 3 of 3 / SCALE_OUT."""
    raw_all = _raw("compute.warehouse_id = ?", ["pr_wh_edge"], 90)
    glob = (PARQUET_DIR / "query__history" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        today_n = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{glob}', union_by_name=true) "
            "WHERE statement_id = ? AND CAST(start_time AS DATE) = DATE '2026-09-21'",
            [pw.TODAY_STMT_ID]).fetchone()[0]
    finally:
        con.close()
    assert today_n == 1, "the fixture must carry one statement on the build day itself"
    for w in WINDOWS:
        e = {r["warehouse_id"]: r for r in _pr(_rows(w))}["pr_wh_edge"]
        assert e["query_count"] == raw_all["n"] == 20, (w, e["query_count"])
        assert e["max_clusters"] == 3
        if w == 7:
            assert (e["max_cluster_count_seen"], e["at_ceiling"], e["scaling_hint"]) == (1, False, "KEEP_WARM")
            assert e["pressure_reason"] == _cap_clause(17, "up to 1 of 3 clusters seen", "WARN")
        else:
            assert (e["max_cluster_count_seen"], e["at_ceiling"], e["scaling_hint"]) == (3, True, "SCALE_OUT")
            assert e["pressure_reason"] == _cap_clause(17, "up to 3 of 3 clusters seen", "WARN")


def test_fixed_size_warehouse_is_at_its_ceiling():
    """A warehouse with no autoscale range (min_clusters >= max_clusters) cannot add a cluster,
    so at_ceiling is TRUE from its configuration and a queueing one reads SCALE_OUT, never
    KEEP_WARM ("raise min_clusters" is impossible at max_clusters 1) -- even when its only event in
    the window is a stop that shows 0 clusters (pr_wh_fixed)."""
    lo, hi = next((c[2], c[3]) for c in pw._CONFIG if c[0] == "pr_wh_fixed")
    assert lo >= hi
    glob = (PARQUET_DIR / "compute__warehouse_events" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        events = con.execute(
            f"SELECT event_type, cluster_count FROM read_parquet('{glob}', union_by_name=true) "
            "WHERE warehouse_id = ?", ["pr_wh_fixed"]).fetchall()
    finally:
        con.close()
    assert events == [("STOPPED", 0)], events
    for w in WINDOWS:
        by_wh = {r["warehouse_id"]: r for r in _pr(_rows(w))}
        f = by_wh["pr_wh_fixed"]
        assert f["max_cluster_count_seen"] == 0 and f["at_ceiling"] is True, (w, f)
        assert (f["pressure"], f["scaling_hint"], f["status"]) == ("CAPACITY", "SCALE_OUT", "WARN"), w
        assert _param("warn_queue_time_pct") <= f["capacity_wait_pct"] < _param("crit_queue_time_pct")
        assert f["pressure_reason"] == _cap_clause(17, _no_range(lo, hi), "WARN"), w
        # every warehouse of the builder without an autoscale range reads at_ceiling TRUE
        for wh in FIXED_SIZE & set(by_wh):
            assert by_wh[wh]["at_ceiling"] is True, (w, wh)
            assert by_wh[wh]["min_clusters"] >= by_wh[wh]["max_clusters"], (w, wh)


def test_near_threshold_shares_are_judged_unrounded_and_never_printed_as_the_threshold():
    """pr_wh_nearwarn: both true shares sit a hair below their WARN thresholds but DISPLAY as the
    threshold (10.0 / 5.0). The bands are judged on the unrounded shares (NONE / OK), and the NONE
    reason writes them as "just under" the threshold rather than as the threshold itself."""
    warn_spill, warn_queue = _param("warn_spill_time_pct"), _param("warn_queue_time_pct")
    for w in WINDOWS:
        raw = _raw("compute.warehouse_id = ?", ["pr_wh_nearwarn"], w)
        spill_raw = raw["spill_ms"] * 100.0 / raw["exec_ms"]
        cap_raw = raw["cap_ms"] * 100.0 / raw["total_ms"]
        assert spill_raw < warn_spill and cap_raw < warn_queue, (spill_raw, cap_raw)
        r = {x["warehouse_id"]: x for x in _pr(_rows(w))}["pr_wh_nearwarn"]
        assert r["spill_time_pct"] == _half_up(spill_raw, 1) == warn_spill      # displays AS the band
        assert r["capacity_wait_pct"] == _half_up(cap_raw, 1) == warn_queue
        assert (r["pressure"], r["scaling_hint"], r["status"]) == ("NONE", "NONE", "OK"), w
        assert r["pressure_reason"] == (
            f"no threshold crossed: {_none_share(spill_raw, warn_spill)}% of execution time spilled, "
            f"{_none_share(cap_raw, warn_queue)}% of statement time queued")
        assert r["pressure_reason"] == (
            f"no threshold crossed: just under {warn_spill}% of execution time spilled, "
            f"just under {warn_queue}% of statement time queued")


# -------------------------------------------------------------------------------------------
# worst-first physical order
# -------------------------------------------------------------------------------------------
def test_worst_first_order():
    for w in WINDOWS:
        out = _rows(w)
        ranks = [STATUS_RANK[r["status"]] for r in out]
        assert ranks == sorted(ranks), (w, [(r["warehouse_id"], r["status"]) for r in out])
        pr = _pr(out)
        for a, b in zip(pr, pr[1:]):
            if a["status"] != b["status"]:
                continue
            ka = (-a["spill_time_pct"], -a["capacity_wait_pct"], a["warehouse_id"])
            kb = (-b["spill_time_pct"], -b["capacity_wait_pct"], b["warehouse_id"])
            assert ka <= kb, (w, a["warehouse_id"], b["warehouse_id"])
    order30 = [r["warehouse_id"] for r in _pr(_rows(30))]
    assert order30[:6] == ["pr_wh_mem", "pr_wh_both", "pr_wh_memcrit_capwarn", "pr_wh_mixed",
                           "pr_wh_bigspill", "pr_wh_cap_ceiling"], order30


# -------------------------------------------------------------------------------------------
# independent re-derivation from the raw parquet
# -------------------------------------------------------------------------------------------
def test_shares_match_raw_parquet():
    for w in WINDOWS:
        by_wh = {r["warehouse_id"]: r for r in _pr(_rows(w))}
        for wh in ("pr_wh_mem", "pr_wh_both", "pr_wh_cap_ceiling", "pr_wh_cap_lag", "pr_wh_cold",
                   "pr_wh_nearwarn"):
            raw = _raw("compute.warehouse_id = ?", [wh], w)
            r = by_wh[wh]
            assert r["query_count"] == raw["n"], (w, wh)
            assert r["spill_time_pct"] == _pct(raw["spill_ms"], raw["exec_ms"]), (w, wh)
            assert r["capacity_wait_pct"] == _pct(raw["cap_ms"], raw["total_ms"]), (w, wh)
            assert r["cold_start_wait_pct"] == _pct(raw["cold_ms"], raw["total_ms"]), (w, wh)
            # units: GB = bytes / 1e9 to 2 dp, seconds = ms / 1000 to 0 dp
            assert r["read_gb_sum"] == _half_up(raw["read_bytes"] / 1e9, 2), (w, wh)
            assert r["shuffle_read_gb_sum"] == _half_up(raw["shuffle_bytes"] / 1e9, 2), (w, wh)
            assert r["waiting_at_capacity_s_sum"] == _half_up(raw["cap_ms"] / 1000), (w, wh)
            assert r["waiting_for_compute_s_sum"] == _half_up(raw["cold_ms"] / 1000), (w, wh)
            assert r["execution_s_sum"] == _half_up(raw["exec_ms"] / 1000), (w, wh)
            assert r["total_duration_s_sum"] == _half_up(raw["total_ms"] / 1000), (w, wh)
        assert by_wh["pr_wh_bigspill"]["spilled_local_gb_max_day"] == _raw_worst_day_gb(
            "pr_wh_bigspill", w) == 15.0
        assert by_wh["pr_wh_mem"]["spilled_local_gb_max_day"] == _raw_worst_day_gb(
            "pr_wh_mem", w) == 2.0

    # the literal expectations of the task contract, re-derived from the parquet at 30
    raw = _raw("compute.warehouse_id = ?", ["pr_wh_mem"], 30)
    assert _pct(raw["spill_ms"], raw["exec_ms"]) == 87.0
    raw = _raw("compute.warehouse_id = ?", ["pr_wh_both"], 30)
    assert _pct(raw["spill_ms"], raw["exec_ms"]) == 30.0
    assert _pct(raw["cap_ms"], raw["total_ms"]) == 25.0
    raw = _raw("compute.warehouse_id = ?", ["pr_wh_cap_ceiling"], 30)
    assert _pct(raw["cap_ms"], raw["total_ms"]) == 30.0
    raw = _raw("compute.warehouse_id = ?", ["pr_wh_win40"], 90)
    assert raw["n"] == 25
    assert {r["warehouse_id"]: r for r in _pr(_rows(90))}["pr_wh_win40"]["query_count"] == raw["n"]


def test_serverless_statements_have_no_row():
    serverless = f"{pw.SERVERLESS_PREFIX}%"
    for w in WINDOWS:
        raw = _raw("statement_id LIKE ? AND compute.warehouse_id IS NULL "
                   "AND compute.type = 'SERVERLESS_COMPUTE'", [serverless], w)
        assert raw["n"] == pw.SERVERLESS_COUNT, (w, raw["n"])  # they exist in the source...
        out = _rows(w)
        assert all(r["warehouse_id"] is not None for r in out)  # ...and never become a row
        # every in-window pr_ statement WITH a warehouse is counted, and nothing else is
        pr_with_wh = _raw("statement_id LIKE 'pr_st_%' AND compute.warehouse_id IS NOT NULL", [], w)
        assert sum(r["query_count"] for r in _pr(out)) == pr_with_wh["n"], w
