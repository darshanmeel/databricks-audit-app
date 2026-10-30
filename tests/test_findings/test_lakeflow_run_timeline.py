"""tests/test_findings/test_lakeflow_run_timeline.py -- T-17.

Proves, against tests/fixtures/lakeflow.py's own rows (and, for the three inventory ids, the whole
built fixture, since their grains have no job_id/pipeline_id to scope by), that each of the 7 "C2
run-timeline" findings.f_<query_id> tables (PLAN.md 7.2 batch C2) has the right grain, the right
status enum where the body has one, worst-first ordering matching the model's own `order_by` meta
(dbt/models/findings/jobs_pipelines/_findings__jobs_pipelines.yml), a reachable WARN/CRITICAL/
NOT_ASSESSED row by id where the body can emit that band, correct window_days behaviour at 7/30/90
(and `[] `at 0, every one of these 7 ids being windowed -- none is a snapshot-type id), and the
AS_OF-day exclusion -- including the one place it deliberately does NOT happen
(`lakeflow_termination_type_probe`, which has no upper bound at all) next to the five places it
does (the shared `period_end_time < date_trunc('DAY', ...)` bound) and the one place it uses a
different column for the same idea (`lakeflow_workload_mix_hours`'s `period_start_time <
date_trunc('DAY', ...)`). Per tests/test_findings/README.md's checklist and this task's own
Inputs/Steps.

All seven bodies read only system.lakeflow.job_run_timeline (no join), so every expectation here is
computed either from a specific, individually-known-by-construction fixture row (named in
tests/fixtures/lakeflow.py's SEC D/E/F/G comments, e.g. `lf_job_fr_a` = 20 FAILED/CLUSTER_ERROR
rows) or, for the three inventory ids whose grain merges rows from DIFFERENT fixture scenarios that
happen to share a termination_code/termination_type/run_day (so a hand-summed total would be
exactly the "aggregate row count derived by summing unrelated per-scenario totals by hand" the
README forbids), by an independent re-derivation straight from
tests/fixtures/parquet/lakeflow__job_run_timeline/*.parquet using each id's own WHERE/GROUP BY --
never trusting the dbt model's own arithmetic, never importing another builder's private constants.
This mirrors tests/test_findings/test_compute_activity.py's `_cost_rollup` precedent (re-derive the
model's own predicate against the raw parquet) rather than a hard-coded number.

The termination_type_probe vs workload_mix_hours divergence (the most interesting assertion in this
batch, per this task's instructions) is proven in both directions by
`test_probe_vs_workload_mix_hours_asof_divergence` below, off the SAME underlying AS_OF-day fixture
rows (`lf_job_probe_asof`, `lf_job_asof_general`, `lf_job_swf_asof`): present in the probe (no upper
bound), absent from workload_mix_hours (`period_start_time < date_trunc('DAY', now)`), with a raw-
parquet anchor proving the excluded side's rows genuinely exist -- an absent row and a never-built
row look identical to a test that only ever checks non-membership.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import lakeflow as lf  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_run_timeline.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
JRT_GLOB = (PARQUET_DIR / "lakeflow__job_run_timeline" / "*.parquet").as_posix()

WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

# The 4 ids with a status column (is_finding: true in the generated yml).
STATUS_IDS = [
    "lakeflow_failed_runs",
    "lakeflow_job_queue_time",
    "lakeflow_never_started_runs",
    "lakeflow_phase_cold_start",
]
# The 3 inventory-only ids (is_finding: false) -- no status column is generated for these at all.
INVENTORY_IDS = [
    "lakeflow_termination_taxonomy",
    "lakeflow_termination_type_probe",
    "lakeflow_workload_mix_hours",
]
ALL_IDS = STATUS_IDS + INVENTORY_IDS


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _raw_row_exists(where: str) -> bool:
    """True if at least one row matching `where` exists anywhere in the raw
    lakeflow__job_run_timeline fixture parquet (bypassing every window/model filter) -- proves a
    negative-control row genuinely exists rather than having been silently deleted (companion to
    every "must be absent from the model" assertion below)."""
    con = duckdb.connect()
    try:
        sql = f"SELECT COUNT(*) FROM read_parquet('{JRT_GLOB}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0] > 0
    finally:
        con.close()


def _assert_sorted_desc(values):
    """order_by is a bare `<col> DESC` with no NULLs possible in that column (COUNT/SUM(CASE...)
    always returns a number) -- checked directly against the physical row order dbutil.rows()
    returns, never re-sorted by the test (README: "checked directly ... not re-sorted")."""
    assert values == sorted(values, reverse=True), values


def _assert_desc_then_nulls_last(values):
    """order_by is a bare `<col> DESC` where the column CAN be NULL (a fully-NULL
    PERCENTILE/PERCENTILE_APPROX group -> NOT_ASSESSED). DuckDB's default null ordering sorts NULL
    last regardless of ASC/DESC (verified directly against this build: `SELECT * FROM (VALUES
    (1),(NULL),(5),(3)) t(x) ORDER BY x DESC` returns 5, 3, 1, NULL) -- so the non-null prefix must
    be sorted descending and no non-null value may follow a null one."""
    seen_null = False
    non_null = []
    for v in values:
        if v is None:
            seen_null = True
        else:
            assert not seen_null, f"non-null value {v!r} followed a null value -- NULLS LAST violated"
            non_null.append(v)
    assert non_null == sorted(non_null, reverse=True), non_null


def _assert_group_asc_metric_desc(rows, group_col, metric_col):
    """order_by is `<group_col>, <metric_col> DESC` (lakeflow_termination_taxonomy,
    lakeflow_termination_type_probe, lakeflow_workload_mix_hours's own meta.order_by) -- groups by
    group_col appear in non-decreasing order and never repeat once left, and within a group
    metric_col is non-increasing (ties allowed, since the body names no tertiary sort)."""
    seen_groups = []
    last_group = object()
    last_metric = None
    for r in rows:
        g, m = r[group_col], r[metric_col]
        if g != last_group:
            assert g not in seen_groups, f"group {g!r} reappeared out of order: {seen_groups}"
            seen_groups.append(g)
            last_group = g
            last_metric = None
        else:
            assert last_metric is None or m <= last_metric, (
                f"{metric_col} not DESC within group {g!r}: {last_metric} then {m}"
            )
        last_metric = m


def _run_day(r):
    """dbutil.rows() returns lakeflow_workload_mix_hours.run_day as a naive datetime.datetime
    (the built column is TIMESTAMP, confirmed via `typeof(run_day)` against
    tests/db_audit_test.duckdb) -- normalise to a date for comparison against Python `date` values."""
    v = r["run_day"]
    return v.date() if isinstance(v, datetime) else v


# =====================================================================================================
# Independent re-derivations straight from the raw fixture parquet, mirroring each inventory id's own
# WHERE/GROUP BY exactly (never the dbt model's own arithmetic, never a hand-summed total) -- the
# tests/test_findings/test_compute_activity.py `_cost_rollup` precedent applied to these three ids,
# whose grains merge rows across DIFFERENT fixture scenarios (e.g. termination_code CLUSTER_ERROR is
# emitted by both the SEC D failed-run group and a SEC F never-started group), so a hand-added total
# would be exactly the forbidden "aggregate ... summing unrelated per-scenario totals by hand".
# =====================================================================================================
def _raw_taxonomy(window_days: int) -> dict:
    lower = lf.D0 - timedelta(days=window_days)
    con = duckdb.connect()
    try:
        sql = f"""
            SELECT workspace_id, job_id, termination_code,
                   COUNT(*) AS run_rows, COUNT(DISTINCT run_id) AS distinct_runs
            FROM read_parquet('{JRT_GLOB}', union_by_name=true)
            WHERE period_start_time >= TIMESTAMP '{lower.isoformat()} 00:00:00'
              AND period_end_time < TIMESTAMP '{lf.D0.isoformat()} 00:00:00'
              AND result_state IS NOT NULL
              AND termination_code IS NOT NULL
            GROUP BY workspace_id, job_id, termination_code
        """
        return {(r[0], r[1], r[2]): (r[3], r[4]) for r in con.execute(sql).fetchall()}
    finally:
        con.close()


def _raw_probe(window_days: int) -> dict:
    lower = lf.D0 - timedelta(days=window_days)
    con = duckdb.connect()
    try:
        # Deliberately NO period_end_time upper bound -- this is the entire point of the probe.
        sql = f"""
            SELECT workspace_id, job_id, termination_type, COUNT(*) AS run_rows
            FROM read_parquet('{JRT_GLOB}', union_by_name=true)
            WHERE period_start_time >= TIMESTAMP '{lower.isoformat()} 00:00:00'
              AND result_state IS NOT NULL
            GROUP BY workspace_id, job_id, termination_type
        """
        return {(r[0], r[1], r[2]): r[3] for r in con.execute(sql).fetchall()}
    finally:
        con.close()


def _raw_mix(window_days: int) -> dict:
    lower = lf.D0 - timedelta(days=window_days)
    con = duckdb.connect()
    try:
        # Bounds on period_start_time (not period_end_time) with NO result_state filter -- this id
        # keeps non-end rows too, unlike every other id in this batch.
        sql = f"""
            SELECT workspace_id, job_id, run_type, trigger_type,
                   CAST(date_trunc('DAY', period_start_time) AS DATE) AS run_day,
                   COUNT(DISTINCT run_id) AS distinct_runs,
                   SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) AS completed_run_rows,
                   SUM(execution_duration_seconds) AS execution_s_total
            FROM read_parquet('{JRT_GLOB}', union_by_name=true)
            WHERE period_start_time >= TIMESTAMP '{lower.isoformat()} 00:00:00'
              AND period_start_time < TIMESTAMP '{lf.D0.isoformat()} 00:00:00'
            GROUP BY workspace_id, job_id, run_type, trigger_type, date_trunc('DAY', period_start_time)
        """
        return {(r[0], r[1], r[2], r[3], r[4]): (r[5], r[6], r[7]) for r in con.execute(sql).fetchall()}
    finally:
        con.close()


# =====================================================================================================
# Generic checks over all 7 ids.
# =====================================================================================================
def test_grain_uniqueness_all_ids():
    grains = _grains()
    for qid in ALL_IDS:
        cols = grains[qid]
        for w in WINDOWS:
            out = dbutil.rows(qid, w)
            assert out, (qid, w)  # a vacuously-true uniqueness check on an empty result proves nothing
            keys = [tuple(r[c] for c in cols) for r in out]
            assert len(keys) == len(set(keys)), f"{qid} w={w}: duplicate grain rows"


def test_window_days_zero_is_empty_for_all_ids():
    """All 7 ids are windowed (meta.windowed: true) -- none is a snapshot-type id, so there is no
    0-window id to test the opposite direction for in this batch."""
    for qid in ALL_IDS:
        assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


def test_status_enum_for_status_ids():
    for qid in STATUS_IDS:
        for w in WINDOWS:
            out = dbutil.rows(qid, w)
            assert out, (qid, w)
            for r in out:
                assert r["status"] in STATUS_VALUES, f"{qid} w={w}: bad status {r['status']!r}"


def test_inventory_ids_have_no_status_column():
    for qid in INVENTORY_IDS:
        out = dbutil.rows(qid, 30)
        assert out, qid
        for r in out:
            assert "status" not in r, f"{qid}: unexpected status column on an inventory id"


# =====================================================================================================
# lakeflow_failed_runs -- warn_failed_run_rows=5, crit_failed_run_rows=20; grain [workspace_id,
# run_type, trigger_type, result_state, termination_code]; order_by failed_run_rows DESC.
# lf_job_fr_a (CLUSTER_ERROR) = 20 FAILED rows CRITICAL, lf_job_fr_b (STORAGE_ACCESS_ERROR) = 12
# WARN, lf_job_fr_c (WORKSPACE_RUN_LIMIT_EXCEEDED) = 4 OK (tests/fixtures/lakeflow.py SEC D). Each
# is its own (job, termination_code) FAILED-run group, so this grain (which has no job_id) never
# blends them, and result_state=FAILED keeps them disjoint from any SKIPPED/SUCCEEDED group sharing
# the same termination_code -- safe to assert exact counts.
# =====================================================================================================
def test_lakeflow_failed_runs():
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_failed_runs", w)
        by_code = {
            r["termination_code"]: r for r in out
            if r["workspace_id"] == "1111" and r["result_state"] == "FAILED"
            and r["termination_code"] in
            ("CLUSTER_ERROR", "STORAGE_ACCESS_ERROR", "WORKSPACE_RUN_LIMIT_EXCEEDED")
        }
        crit = by_code["CLUSTER_ERROR"]
        assert crit["status"] == "CRITICAL"
        assert crit["failed_run_rows"] == 20
        assert crit["run_rows"] == 20
        assert crit["distinct_runs"] == 20  # every run in the group is FAILED, no retries

        warn = by_code["STORAGE_ACCESS_ERROR"]
        assert warn["status"] == "WARN"
        assert warn["failed_run_rows"] == 12

        ok = by_code["WORKSPACE_RUN_LIMIT_EXCEEDED"]
        assert ok["status"] == "OK"
        assert ok["failed_run_rows"] == 4

        # worst-first: order_by is `failed_run_rows DESC` (model meta) -- the whole table's
        # physical row order must already satisfy it.
        _assert_sorted_desc([r["failed_run_rows"] for r in out])

    # AS_OF-day exclusion (general period_end_time < date_trunc bound, shared by 5 of these 7
    # ids): lf_job_asof_general (termination_code=ASOF_EXCLUDED) is a FAILED end row whose
    # period_end_time lands on the AS_OF day -- must never appear, at any window.
    assert _raw_row_exists("job_id = 'lf_job_asof_general'")  # anchor: the row genuinely exists
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_failed_runs", w)
        assert not any(r["termination_code"] == "ASOF_EXCLUDED" for r in out), w


# =====================================================================================================
# lakeflow_job_queue_time -- warn_queue_p95_s=60, crit_queue_p95_s=300; grain [workspace_id, job_id];
# order_by queue_s_p95 DESC. Shares the lf_job_phase_* 5-job set with lakeflow_phase_cold_start (SEC
# E): lf_job_phase_crit (queue=350s) CRITICAL, lf_job_phase_warn (150s) WARN, lf_job_phase_ok (10s)
# OK, lf_job_phase_null (all 5 duration columns NULL) NOT_ASSESSED, lf_job_phase_zero (T-75B review
# fix: run_duration_seconds=300 > 0 but queue=0 on all 3 runs) OK with a REAL 0.0 p95, not NULL.
# =====================================================================================================
def test_lakeflow_job_queue_time():
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_job_queue_time", w)
        by_job = {r["job_id"]: r for r in out if r["job_id"].startswith("lf_job_phase_")}

        crit = by_job["lf_job_phase_crit"]
        assert crit["status"] == "CRITICAL"
        assert crit["queue_s_p95"] == 350.0
        assert crit["distinct_runs"] == 3
        assert crit["runs_queue_null"] == 0

        warn = by_job["lf_job_phase_warn"]
        assert warn["status"] == "WARN"
        assert warn["queue_s_p95"] == 150.0

        ok = by_job["lf_job_phase_ok"]
        assert ok["status"] == "OK"
        assert ok["queue_s_p95"] == 10.0

        # NOT_ASSESSED: the whole queue_duration_seconds column is NULL for this job's runs (the
        # "not populated before late Nov 2025" degrade path) -- runs_queue_null == distinct_runs,
        # not read as zero queue time.
        null_row = by_job["lf_job_phase_null"]
        assert null_row["status"] == "NOT_ASSESSED"
        assert null_row["queue_s_p95"] is None
        assert null_row["distinct_runs"] == 3
        assert null_row["runs_queue_null"] == 3

        # T-75B review fix: run_duration_seconds=300 (> 0, a real legacy single-task run) but
        # queue_duration_seconds=0 on all 3 runs -- a GENUINE 0 must be kept as a real 0 (OK,
        # runs_queue_null=0), not folded to NULL by a blind NULLIF(queue_duration_seconds, 0).
        zero = by_job["lf_job_phase_zero"]
        assert zero["status"] == "OK"
        assert zero["queue_s_p95"] == 0.0
        assert zero["distinct_runs"] == 3
        assert zero["runs_queue_null"] == 0

        # worst-first: order_by is `queue_s_p95 DESC`, NULLs (NOT_ASSESSED) last.
        _assert_desc_then_nulls_last([r["queue_s_p95"] for r in out])

    # Multi-period end-row-only proof (SEC M): lf_run_multiperiod has 3 physical period rows (2
    # NULL-state + 1 end row), but this id filters to result_state IS NOT NULL before aggregating,
    # so it must reduce to exactly 1 distinct run with the END row's own queue_duration_seconds=0,
    # never NULL and never double-counted.
    assert _raw_row_exists("run_id = 'lf_run_multiperiod'")
    raw_period_rows = duckdb.connect().execute(
        f"SELECT COUNT(*) FROM read_parquet('{JRT_GLOB}', union_by_name=true) "
        "WHERE run_id = 'lf_run_multiperiod'"
    ).fetchone()[0]
    assert raw_period_rows == 3  # 2 non-end + 1 end row, confirmed physically present
    mp = [r for r in dbutil.rows("lakeflow_job_queue_time", 30) if r["job_id"] == "lf_job_multiperiod"]
    assert len(mp) == 1
    assert mp[0]["distinct_runs"] == 1
    # queue=3 (not 0): a literal 0 now folds to NULL/not-reported (T-75B), so this proof uses a
    # small non-zero value instead -- see _build_multiperiod's own comment.
    assert mp[0]["queue_s_p95"] == 3.0
    assert mp[0]["status"] == "OK"

    # AS_OF-day exclusion: lf_job_asof_general's end row lands on the AS_OF day.
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_job_queue_time", w)
        assert not any(r["job_id"] == "lf_job_asof_general" for r in out), w


# =====================================================================================================
# lakeflow_phase_cold_start -- warn_setup_p95_s=60, crit_setup_p95_s=300; grain [workspace_id,
# job_id]; order_by setup_s_p95 DESC. Same lf_job_phase_* 5-job set as lakeflow_job_queue_time (SEC
# E), same thresholds, uses exact PERCENTILE (DuckDB: quantile_cont) rather than PERCENTILE_APPROX.
# setup_s_p95/rows_setup_null/status now come from job_task_run_timeline (task-level, SEC E's own
# jtrt() rows added alongside each jrt() row), not job_run_timeline -- these five single-task jobs
# prove the new source still bands the same way. SEC E2's lf_job_phase_multitask (own assertions
# below) proves the actual fix: a multi-task job whose run-level phase columns are all 0
# (Databricks' documented multi-task sentinel) still gets a real setup_s_p95/status from its tasks.
# =====================================================================================================
def test_lakeflow_phase_cold_start():
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_phase_cold_start", w)
        by_job = {r["job_id"]: r for r in out if r["job_id"].startswith("lf_job_phase_")}

        crit = by_job["lf_job_phase_crit"]
        assert crit["status"] == "CRITICAL"
        assert crit["setup_s_p95"] == 350.0
        assert crit["queue_s_p95"] == 350.0  # same seconds value fixture-wide (SEC E)
        assert crit["runs"] == 3
        assert crit["rows_setup_null"] == 0

        warn = by_job["lf_job_phase_warn"]
        assert warn["status"] == "WARN"
        assert warn["setup_s_p95"] == 150.0

        ok = by_job["lf_job_phase_ok"]
        assert ok["status"] == "OK"
        assert ok["setup_s_p95"] == 10.0

        null_row = by_job["lf_job_phase_null"]
        assert null_row["status"] == "NOT_ASSESSED"
        assert null_row["setup_s_p95"] is None
        assert null_row["runs"] == 3
        assert null_row["rows_setup_null"] == 3

        # T-75B review fix: run_duration_seconds=300 (> 0) but setup_duration_seconds=0 on all 3
        # runs -- a GENUINE 0 must be kept as a real 0 (OK, rows_setup_null=0), not folded to NULL.
        zero = by_job["lf_job_phase_zero"]
        assert zero["status"] == "OK"
        assert zero["setup_s_p95"] == 0.0
        assert zero["runs"] == 3
        assert zero["rows_setup_null"] == 0

        # worst-first: order_by is `setup_s_p95 DESC`, NULLs (NOT_ASSESSED) last.
        _assert_desc_then_nulls_last([r["setup_s_p95"] for r in out])

        # SEC E2 -- the actual multi-task fix: lf_job_phase_multitask's run-level row folds every
        # phase column to Databricks' documented 0-sentinel, so queue_s_p95 (still sourced from
        # job_run_timeline, still legacy-single-task-only -- see the query's own header) stays
        # NULL, exactly the permanent gap this query cannot close. But its 6 task rows (2 tasks x
        # 3 runs) each carry a real setup_duration_seconds=320 on job_task_run_timeline -> a
        # genuine cold-start read instead of the NOT_ASSESSED this job would have read forever
        # under the old run-level-only query.
        multitask = by_job["lf_job_phase_multitask"]
        assert multitask["setup_s_p95"] == 320.0
        assert multitask["status"] == "CRITICAL"
        assert multitask["rows_setup_null"] == 0
        assert multitask["runs"] == 3
        assert multitask["queue_s_p95"] is None

        # SEC E3 -- a failed start's 1,800 s setup is left out: only the successful retries count.
        failstart = by_job["lf_job_phase_failstart"]
        assert failstart["setup_s_p95"] == 20.0
        assert failstart["status"] == "OK"

    # Multi-period end-row-only proof, same fixture row as lakeflow_job_queue_time's own check.
    mp = [r for r in dbutil.rows("lakeflow_phase_cold_start", 30) if r["job_id"] == "lf_job_multiperiod"]
    assert len(mp) == 1
    assert mp[0]["runs"] == 1
    # setup=5 (not 0): a literal 0 now folds to NULL/not-reported (T-75B), so this proof uses a
    # small non-zero value instead -- see _build_multiperiod's own comment.
    assert mp[0]["setup_s_p95"] == 5.0
    assert mp[0]["status"] == "OK"

    # AS_OF-day exclusion.
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_phase_cold_start", w)
        assert not any(r["job_id"] == "lf_job_asof_general" for r in out), w


# =====================================================================================================
# lakeflow_never_started_runs -- warn_never_started=3, crit_never_started=10; grain [workspace_id,
# job_id, termination_code]; order_by never_started_runs DESC (no NOT_ASSESSED branch exists in this
# body -- only OK/WARN/CRITICAL are reachable). lf_job_never_crit=10 CRITICAL, _warn=5 WARN, _ok=1 OK
# (SEC F). This is the one status id in this batch whose fixture does not place a dedicated
# never-started row on the AS_OF day (tests/fixtures/lakeflow.py builds only a day-D(6) family for
# this id) -- the general period_end_time exclusion is proven directly on the other 4 general-bound
# ids instead (failed_runs, job_queue_time, phase_cold_start, termination_taxonomy), matching
# tests/test_findings/README.md's own precedent of naming a fixture gap rather than papering over it.
# =====================================================================================================
def test_lakeflow_never_started_runs():
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_never_started_runs", w)
        by_job = {r["job_id"]: r for r in out}

        crit = by_job["lf_job_never_crit"]
        assert crit["status"] == "CRITICAL"
        assert crit["never_started_runs"] == 10
        assert crit["termination_code"] == "MAX_JOB_QUEUE_SIZE_EXCEEDED"

        warn = by_job["lf_job_never_warn"]
        assert warn["status"] == "WARN"
        assert warn["never_started_runs"] == 5
        assert warn["termination_code"] == "WORKSPACE_RUN_LIMIT_EXCEEDED"

        ok = by_job["lf_job_never_ok"]
        assert ok["status"] == "OK"
        assert ok["never_started_runs"] == 1
        assert ok["termination_code"] == "CLUSTER_ERROR"

        assert not any(r["status"] == "NOT_ASSESSED" for r in out), (w, "no NOT_ASSESSED branch exists")

        # worst-first: order_by is `never_started_runs DESC`.
        _assert_sorted_desc([r["never_started_runs"] for r in out])


# =====================================================================================================
# lakeflow_termination_taxonomy -- inventory (no bands, no status column); grain [workspace_id, job_id,
# termination_code]; order_by workspace_id, run_rows DESC. This id's grain merges rows from
# different SEC D/F scenarios sharing the same termination_code (e.g. CLUSTER_ERROR = SEC D's
# lf_job_fr_a's 20 FAILED rows + SEC F's lf_job_never_ok's 1 SKIPPED row), so expectations are
# re-derived independently from the raw parquet (`_raw_taxonomy`) rather than hand-summed.
# =====================================================================================================
def test_lakeflow_termination_taxonomy():
    for w in WINDOWS:
        expected = _raw_taxonomy(w)
        out = dbutil.rows("lakeflow_termination_taxonomy", w)
        got = {(r["workspace_id"], r["job_id"], r["termination_code"]): (r["run_rows"], r["distinct_runs"]) for r in out}
        assert got == expected, (w, got, expected)

        # worst-first: order_by is `workspace_id, run_rows DESC`.
        _assert_group_asc_metric_desc(out, "workspace_id", "run_rows")

    # AS_OF-day exclusion: lf_job_probe_asof (PROBE_CODE) and lf_job_asof_general (ASOF_EXCLUDED)
    # both have an end row whose period_end_time lands on the AS_OF day.
    assert _raw_row_exists("termination_code = 'PROBE_CODE'")
    assert _raw_row_exists("termination_code = 'ASOF_EXCLUDED'")
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_termination_taxonomy", w)
        codes = {r["termination_code"] for r in out}
        assert "PROBE_CODE" not in codes, w
        assert "ASOF_EXCLUDED" not in codes, w


# =====================================================================================================
# lakeflow_termination_type_probe -- inventory (no bands, no status column); grain [workspace_id, job_id,
# termination_type]; order_by workspace_id, run_rows DESC. NO current-day upper bound at all (the
# entire point of the probe) -- re-derived independently from the raw parquet (`_raw_probe`) for the
# same "merges scenarios" reason as termination_taxonomy above.
# =====================================================================================================
def test_lakeflow_termination_type_probe():
    for w in WINDOWS:
        expected = _raw_probe(w)
        out = dbutil.rows("lakeflow_termination_type_probe", w)
        got = {(r["workspace_id"], r["job_id"], r["termination_type"]): r["run_rows"] for r in out}
        assert got == expected, (w, got, expected)

        # worst-first: order_by is `workspace_id, run_rows DESC`.
        _assert_group_asc_metric_desc(out, "workspace_id", "run_rows")

    # The AS_OF-day survivor, named directly: lf_job_probe_asof's own termination_type
    # (PROBE_ASOF_SURVIVES) is unique to that one row, so its run_rows == 1 is
    # individually-known-by-construction and safe to assert exactly, at every window (no upper
    # bound means the window's lower edge is the only thing that could ever exclude it, and it sits
    # well inside all three).
    assert _raw_row_exists("termination_type = 'PROBE_ASOF_SURVIVES'")
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_termination_type_probe", w)
        survivors = [r for r in out if r["termination_type"] == "PROBE_ASOF_SURVIVES"]
        assert len(survivors) == 1, (w, survivors)
        assert survivors[0]["run_rows"] == 1
        assert survivors[0]["workspace_id"] == "1111"

    # At least one termination_type NULL row exists (pre-population history) and survives at every
    # window -- proven both against the raw parquet and the built table.
    assert _raw_row_exists("termination_type IS NULL AND result_state IS NOT NULL")
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_termination_type_probe", w)
        assert any(r["termination_type"] is None for r in out), w


# =====================================================================================================
# lakeflow_workload_mix_hours -- inventory (no bands, no status column); grain [workspace_id, job_id,
# run_type, trigger_type, run_day]; order_by workspace_id, run_day DESC. Bounds on
# `period_start_time < date_trunc('DAY', now)` (NOT period_end_time -- the one C2 id that diverges
# from the shared bound in the OTHER direction from the probe) and carries NO `result_state IS NOT
# NULL` filter, so it is the one id in this batch that keeps non-end rows too. Re-derived
# independently (`_raw_mix`) for the same "merges scenarios" reason as the other two inventory ids.
# =====================================================================================================
def test_lakeflow_workload_mix_hours():
    for w in WINDOWS:
        expected = _raw_mix(w)
        out = dbutil.rows("lakeflow_workload_mix_hours", w)
        got = {
            (r["workspace_id"], r["job_id"], r["run_type"], r["trigger_type"], _run_day(r)):
                (r["distinct_runs"], r["completed_run_rows"], r["execution_s_total"])
            for r in out
        }
        assert got == expected, (w, got, expected)

        # worst-first: order_by is `workspace_id, run_day DESC`.
        _assert_group_asc_metric_desc(out, "workspace_id", "run_day")

    # SEC M multi-period proof, the OTHER direction from lakeflow_job_queue_time's: because this id
    # does NOT filter to end rows, lf_run_multiperiod's 3 physical period rows split into TWO
    # groups by trigger_type (the 2 NULL-state rows default to PERIODIC, the 1 end row is
    # SCHEDULE), yet COUNT(DISTINCT run_id) still nets each group to 1, and completed_run_rows
    # correctly isolates the end row (1 in the SCHEDULE group, 0 in the PERIODIC group) rather than
    # over- or under-counting the run.
    mp_day = (lf.D0 - timedelta(days=10))
    mp_rows = {
        (r["run_type"], r["trigger_type"]): r
        for r in dbutil.rows("lakeflow_workload_mix_hours", 30)
        if r["workspace_id"] == "1111" and r["job_id"] == "lf_job_multiperiod" and _run_day(r) == mp_day
    }
    periodic = mp_rows[("JOB_RUN", "PERIODIC")]
    assert periodic["distinct_runs"] == 1
    assert periodic["completed_run_rows"] == 0
    assert periodic["execution_s_total"] is None

    schedule = mp_rows[("JOB_RUN", "SCHEDULE")]
    assert schedule["distinct_runs"] == 1
    assert schedule["completed_run_rows"] == 1
    assert schedule["execution_s_total"] == 0.0  # the end row's own execution_duration_seconds=0


# =====================================================================================================
# The divergence: lakeflow_termination_type_probe (no upper bound) vs lakeflow_workload_mix_hours
# (`period_start_time < date_trunc('DAY', now)`) over the SAME AS_OF-day fixture rows -- present in
# one, absent from the other, proven in both directions with a raw-parquet anchor on the excluded
# side so an absent row and a never-built row cannot look identical.
# =====================================================================================================
def test_probe_vs_workload_mix_hours_asof_divergence():
    asof_day = lf.D0  # 2026-09-21, AS_OF's own calendar day

    # Anchor: the AS_OF-day rows genuinely exist in the raw fixture, independent of any model.
    for job_id in ("lf_job_probe_asof", "lf_job_asof_general"):
        assert _raw_row_exists(f"job_id = '{job_id}'"), job_id
    con = duckdb.connect()
    try:
        raw = con.execute(
            f"SELECT job_id, CAST(period_start_time AS DATE) FROM read_parquet('{JRT_GLOB}', "
            "union_by_name=true) WHERE job_id IN ('lf_job_probe_asof', 'lf_job_asof_general')"
        ).fetchall()
    finally:
        con.close()
    for job_id, day in raw:
        assert day == asof_day, (job_id, day)  # both rows really do land on the AS_OF day

    for w in WINDOWS:
        # (1) lakeflow_termination_type_probe has NO upper bound -- the AS_OF-day row survives.
        probe_out = dbutil.rows("lakeflow_termination_type_probe", w)
        probe_types = {r["termination_type"] for r in probe_out}
        assert "PROBE_ASOF_SURVIVES" in probe_types, (w, "probe must include the AS_OF-day row")

        # (2) lakeflow_workload_mix_hours bounds on period_start_time < date_trunc('DAY', now) --
        # the SAME calendar day is entirely absent, for every run_type/trigger_type combination
        # (not just the specific probe/asof_general jobs -- lf_job_swf_asof from another builder
        # section lands on the same day and must be excluded too).
        mix_out = dbutil.rows("lakeflow_workload_mix_hours", w)
        mix_days = {_run_day(r) for r in mix_out if r["workspace_id"] == "1111"}
        assert asof_day not in mix_days, (w, "workload_mix_hours must exclude the AS_OF day entirely")


# =====================================================================================================
# lakeflow_failed_cluster_starts -- grain [workspace_id, job_id, termination_code]. SEC E3's
# lf_job_phase_failstart: 3 task runs FAILED with CLOUD_FAILURE before any execution, 1,800 s setup
# each; its successful retries and every other lf_ job's tasks are not failed starts.
# =====================================================================================================
def test_lakeflow_failed_cluster_starts():
    for w in WINDOWS:
        out = dbutil.rows("lakeflow_failed_cluster_starts", w)
        keys = [(r["workspace_id"], r["job_id"], r["termination_code"]) for r in out]
        assert len(keys) == len(set(keys)), w
        mine = [r for r in out if r["job_id"] == "lf_job_phase_failstart"]
        assert len(mine) == 1, w
        assert mine[0]["termination_code"] == "CLOUD_FAILURE"
        assert mine[0]["failed_starts"] == 3
        assert mine[0]["setup_s_total"] == 5400
        assert not any(r["job_id"] == "lf_job_phase_multitask" for r in out)
