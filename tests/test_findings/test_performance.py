"""tests/test_findings/test_performance.py -- T-19.

Proves, against tests/fixtures/query_history.py's own rows and only those rows, that each of the
12 built findings.f_<query_id> tables (batch E, PLAN.md 7.2) has the right grain, the right
window_days behaviour at 7/30/90 (and [] at 0), a reachable WARN and CRITICAL where the query has
thresholds, and that audit_self_cost correctly isolates the audit's own marked statements. Per
tests/test_findings/README.md's checklist and this task's own Inputs/Steps.

Expectations are computed from the builder's own row-building functions (imported directly,
never a hard-coded total) or from a specific, individually-known-by-construction row -- the same
style tests/test_findings/test_drilldown_*.py uses. `query_task_statement_breakdown` (this
domain's 13th id) is T-07's own file, out of scope here.

Worst-first ordering notes (README: "the model's own order_by ... worst band first"): several of
these 12 bodies pick an order_by column that is NOT a strict global proxy for status rank once
rows from different partitions / different triggering signals are combined -- e.g.
query_costly_statements orders by absolute execution_duration_ms but status is a WITHIN-WAREHOUSE
share, so a huge statement on a lightly-loaded warehouse can outrank (in raw duration) a small,
CRITICAL statement on a busy warehouse without outranking it in status; query_shuffle_write_
amplification's status can be triggered by either the shuffle-volume signal (which order_by
tracks) or the independent small-file signal (which it does not). Per-id worst-first checks below
are therefore scoped to a known subset of rows where the order_by IS guaranteed monotonic with
status (documented at each call site), rather than asserted over the id's entire output.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import dbutil  # noqa: E402
import ddl  # noqa: E402
import jinja_stub  # noqa: E402
import query_history as qh  # noqa: E402
from app.core.registry import by_id  # noqa: E402
from tools.generate_models import render_model  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "performance.yml"
WS = "1111"
WINDOWS = (7, 30, 90)
STATUS_RANK = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}

# audit_today()/audit_now() are pinned to this exact instant on the `test` dbt target
# (dbt/macros/audit_time.sql), matching tests/fixtures/base.py's AS_OF.
TODAY = datetime(2026, 9, 21)  # current_date() -- midnight

ALL_IDS = [
    "audit_self_cost", "query_cache_coldstart", "query_costly_statements",
    "query_costly_statements_grouped", "query_failed_queries_daily", "query_local_spillage",
    "query_per_query_estimate_lane", "query_provenance_by_source", "query_pruning_effectiveness",
    "query_queuing_waits", "query_shuffle_write_amplification", "query_workload_mix_hours",
]


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _devalue(text):
    """Same recipe every masking id uses: strip emails, then single-quoted literals -> '?'."""
    import re
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"'[^']*'", "?", text)
    return text


def _fingerprint(text):
    return hashlib.sha256(_devalue(text).encode()).hexdigest()


# -------------------------------------------------------------------------------------------
# Generic checks over all 12 ids.
# -------------------------------------------------------------------------------------------
def test_grain_uniqueness_all_ids():
    grains = _grains()
    for qid in ALL_IDS:
        cols = grains[qid]
        out = dbutil.rows(qid, 30, workspace_ids=None)
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"


def test_window_days_zero_is_empty_for_all_ids():
    for qid in ALL_IDS:
        assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


def test_status_enum_for_ids_with_status():
    grains = _grains()
    status_ids = [
        "query_cache_coldstart", "query_costly_statements", "query_costly_statements_grouped",
        "query_failed_queries_daily", "query_local_spillage", "query_pruning_effectiveness",
        "query_queuing_waits", "query_shuffle_write_amplification",
    ]
    for qid in status_ids:
        for w in WINDOWS:
            out = dbutil.rows(qid, w)
            for r in out:
                assert r["status"] in STATUS_RANK, f"{qid} w={w}: bad status {r['status']!r}"
    assert set(status_ids) <= set(grains)  # sanity: every status id is also a grain id


# -------------------------------------------------------------------------------------------
# audit_self_cost -- no status (is_finding=false); no upper bound on start_time.
# -------------------------------------------------------------------------------------------
def test_audit_self_cost():
    all_rows = {r["statement_id"]: r for r in qh._audit_self_cost_rows()}
    marker_rows = {sid: r for sid, r in all_rows.items() if "databricks_audit" in r["statement_text"]}
    assert "qh_asc_nomarker" not in marker_rows  # sanity: the non-marker row really lacks it
    # T-75B: qh_asc_wildcard_decoy's text is "databricksXaudit", which matches the OLD unescaped
    # '%databricks_audit%' ILIKE pattern (LIKE's '_' wildcard matches the 'X'), but must NOT match
    # the fixed, escaped pattern -- Python's plain substring check already agrees it lacks the
    # literal marker, so it is correctly excluded from `marker_rows`/`expected_count` below.
    assert "qh_asc_wildcard_decoy" not in marker_rows

    for window_days in WINDOWS:
        lower = TODAY - timedelta(days=window_days)
        included = [r for r in marker_rows.values() if r["start_time"] >= lower]
        expected_count = len(included)
        expected_total_secs = sum(r["total_duration_ms"] for r in included) / 1000.0
        expected_task_secs = sum(r["total_task_duration_ms"] for r in included) / 1000.0
        expected_principals = len({r["executed_by"] for r in included})

        out = [r for r in dbutil.rows("audit_self_cost", window_days, workspace_ids=[WS])
               if r["statement_type"] == "SELECT"]
        assert len(out) == 1, f"window={window_days}: expected one (workspace,type) group, got {out}"
        row = out[0]
        assert row["query_count"] == expected_count, (window_days, row, expected_count)
        assert abs(row["total_duration_secs"] - expected_total_secs) < 1e-6
        assert abs(row["total_task_secs"] - expected_task_secs) < 1e-6
        assert row["distinct_principals"] == expected_principals
        # the decoy's own executed_by ("frank") and duration must never leak into these totals.
        assert "frank" not in {r["executed_by"] for r in included}

    # the "no upper bound" behaviour: qh_asc_marker_today lands on AS_OF's own calendar day, and
    # is INCLUDED at every window (unlike every other id in this module, where "today" is always
    # excluded) because audit_self_cost's filter has no start_time upper bound.
    today_row = all_rows["qh_asc_marker_today"]
    for window_days in WINDOWS:
        out = [r for r in dbutil.rows("audit_self_cost", window_days, workspace_ids=[WS])
               if r["statement_type"] == "SELECT"][0]
        assert out["last_query_time"] == today_row["start_time"], (
            window_days, out["last_query_time"], today_row["start_time"]
        )

    # qh_asc_marker_old90 (older than even the 90d lower bound) never contributes at any window --
    # already implied by the exact expected_count/expected_total_secs match above (it is excluded
    # from `included` at every window_days in WINDOWS, so any leak would break those assertions).


# -------------------------------------------------------------------------------------------
# query_cache_coldstart -- status: warn=50, crit=20 (avg read_io_cache_percent).
# -------------------------------------------------------------------------------------------
def test_query_cache_coldstart():
    out = dbutil.rows("query_cache_coldstart", 30, workspace_ids=[WS])
    by_wh = {r["warehouse_id"]: r for r in out if r["warehouse_id"] in
             {"qh_cc_wh_ok", "qh_cc_wh_warn", "qh_cc_wh_crit", "qh_cc_wh_null"}}
    assert by_wh["qh_cc_wh_ok"]["status"] == "OK"
    assert by_wh["qh_cc_wh_warn"]["status"] == "WARN"
    assert by_wh["qh_cc_wh_crit"]["status"] == "CRITICAL"
    assert by_wh["qh_cc_wh_null"]["status"] == "NOT_ASSESSED"
    assert by_wh["qh_cc_wh_null"]["read_io_cache_percent_avg"] is None

    # worst-first: order_by is read_io_cache_percent_avg ASC (lower avg = worse = first). Scoped
    # to the three non-NULL-avg rows -- DuckDB's NULL-last ASC ordering would otherwise put the
    # NOT_ASSESSED row after OK, which is not the CRITICAL>WARN>NOT_ASSESSED>OK rank order.
    seq = [r["status"] for r in out
           if r["warehouse_id"] in ("qh_cc_wh_crit", "qh_cc_wh_warn", "qh_cc_wh_ok")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq

    d7, d30, d90 = date(2026, 9, 18), date(2026, 9, 5), date(2026, 7, 15)
    for window_days, expect_days in [(7, {d7}), (30, {d7, d30}), (90, {d7, d30, d90})]:
        out_w = dbutil.rows("query_cache_coldstart", window_days, workspace_ids=[WS])
        days_present = {r["day"] for r in out_w if r["warehouse_id"] == "qh_cc_win"}
        assert days_present == expect_days, (window_days, days_present, expect_days)


# -------------------------------------------------------------------------------------------
# query_costly_statements -- row-level; status: warn=0.1, crit=0.25 (within-warehouse share).
# -------------------------------------------------------------------------------------------
def test_query_costly_statements():
    out = dbutil.rows("query_costly_statements", 30, workspace_ids=None)
    by_sid = {r["statement_id"]: r for r in out}

    assert by_sid["qh_cs_wh1_a"]["status"] == "CRITICAL"
    assert by_sid["qh_cs_wh1_b"]["status"] == "WARN"
    assert by_sid["qh_cs_wh2_e"]["status"] == "CRITICAL"
    assert by_sid["qh_cs_wh3_small"]["status"] == "OK"
    assert by_sid["qh_cs_wh3_big"]["status"] == "CRITICAL"
    assert by_sid["qh_cs_serverless1"]["status"] == "CRITICAL"
    assert by_sid["qh_cs_serverless1"]["warehouse_id"] is None

    # executed_by: mask_user() is off by default, so the raw value passes through.
    assert by_sid["qh_cs_wh1_a"]["executed_by"] is None  # NULL passthrough
    assert by_sid["qh_cs_wh1_b"]["executed_by"] == "dana@example.com"
    assert by_sid["qh_cs_wh2_e"]["executed_by"] == "11112222-3333-4444-5555-666677778888"  # GUID passthrough
    assert by_sid["qh_cs_wh3_small"]["executed_by"] == "root_operator"
    assert by_sid["qh_cs_wh3_big"]["executed_by"] == "__REDACTED__"  # literal passthrough

    # worst-first, scoped to warehouse qh_cs_wh1 alone: within one warehouse, DESC duration ==
    # DESC share, so order_by (execution_duration_ms DESC) is guaranteed worst-first here.
    seq = [r["status"] for r in out if r["warehouse_id"] == "qh_cs_wh1"]
    assert seq == ["CRITICAL", "WARN"], seq

    for window_days, expect in [
        (7, {"qh_cs_win_t7"}),
        (30, {"qh_cs_win_t7", "qh_cs_win_t30"}),
        (90, {"qh_cs_win_t7", "qh_cs_win_t30", "qh_cs_win_t90"}),
    ]:
        out_w = dbutil.rows("query_costly_statements", window_days, workspace_ids=None)
        present = {r["statement_id"] for r in out_w if r["statement_id"].startswith("qh_cs_win_")}
        assert present == expect, (window_days, present, expect)


# -------------------------------------------------------------------------------------------
# query_costly_statements_grouped -- status: warn=1hr(3.6e6ms), crit=5hr(1.8e7ms).
# -------------------------------------------------------------------------------------------
def test_query_costly_statements_grouped():
    hot_fp = _fingerprint("SELECT * FROM hot_table WHERE k = 'aaa'")
    assert hot_fp == _fingerprint("SELECT * FROM hot_table WHERE k = 'bbb'")  # same shape
    warn_fp = _fingerprint("SELECT * FROM warm_table")
    ok_fp = _fingerprint("SELECT * FROM cold_table")

    out = dbutil.rows("query_costly_statements_grouped", 30, workspace_ids=None)
    by_fp = {r["statement_fingerprint"]: r for r in out}

    assert by_fp[hot_fp]["status"] == "CRITICAL"
    assert by_fp[hot_fp]["runs"] == 2
    assert by_fp[hot_fp]["total_exec_ms"] == 20_000_000
    assert by_fp[warn_fp]["status"] == "WARN"
    assert by_fp[warn_fp]["runs"] == 1
    assert by_fp[ok_fp]["status"] == "OK"

    # worst-first over these three groups: order_by is total_exec_ms DESC, which IS monotonic
    # with status here (no independent second signal, unlike shuffle_write_amplification).
    seq = [r["status"] for r in out if r["statement_fingerprint"] in (hot_fp, warn_fp, ok_fp)]
    assert seq == ["CRITICAL", "WARN", "OK"], seq

    for window_days, anchors in [
        (7, ["qh_csg_win_t7"]),
        (30, ["qh_csg_win_t7", "qh_csg_win_t30"]),
        (90, ["qh_csg_win_t7", "qh_csg_win_t30", "qh_csg_win_t90"]),
    ]:
        expect_fps = {_fingerprint(f"SELECT 1 AS {sid}") for sid in anchors}
        out_w = dbutil.rows("query_costly_statements_grouped", window_days, workspace_ids=None)
        win_fps = {r["statement_fingerprint"] for r in out_w
                   if r["sample_statement_text"].startswith("SELECT 1 AS qh_csg_win_")}
        assert win_fps == expect_fps, (window_days, win_fps, expect_fps)


# -------------------------------------------------------------------------------------------
# query_failed_queries_daily -- status: warn=5, crit=20 (count).
# -------------------------------------------------------------------------------------------
def test_query_failed_queries_daily():
    out = dbutil.rows("query_failed_queries_daily", 30, workspace_ids=[WS])
    by_wh = {r["warehouse_id"]: r for r in out
             if r["warehouse_id"] in ("qh_fq_wh_crit", "qh_fq_wh_warn", "qh_fq_wh_ok")}
    assert by_wh["qh_fq_wh_crit"]["status"] == "CRITICAL"
    assert by_wh["qh_fq_wh_crit"]["query_count"] == 20
    assert by_wh["qh_fq_wh_crit"]["error_message_sample"] == "user ? query failed: reason = ?"
    assert by_wh["qh_fq_wh_warn"]["status"] == "WARN"
    assert by_wh["qh_fq_wh_warn"]["query_count"] == 6
    assert by_wh["qh_fq_wh_ok"]["status"] == "OK"
    assert by_wh["qh_fq_wh_ok"]["query_count"] == 2

    seq = [r["status"] for r in out
           if r["warehouse_id"] in ("qh_fq_wh_crit", "qh_fq_wh_warn", "qh_fq_wh_ok")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq  # order_by: query_count DESC

    d7, d30, d90 = date(2026, 9, 18), date(2026, 9, 5), date(2026, 7, 15)
    for window_days, expect_days in [(7, {d7}), (30, {d7, d30}), (90, {d7, d30, d90})]:
        out_w = dbutil.rows("query_failed_queries_daily", window_days, workspace_ids=[WS])
        days_present = {r["day"] for r in out_w if r["warehouse_id"] == "qh_fq_win"}
        assert days_present == expect_days, (window_days, days_present, expect_days)


# -------------------------------------------------------------------------------------------
# query_local_spillage -- status: warn=1GB (1e9 B), crit=10GB (1e10 B).
# -------------------------------------------------------------------------------------------
def test_query_local_spillage():
    out = dbutil.rows("query_local_spillage", 30, workspace_ids=[WS])
    by_wh = {r["warehouse_id"]: r for r in out
             if r["warehouse_id"] in ("qh_ls_wh_ok", "qh_ls_wh_warn", "qh_ls_wh_crit")}
    assert by_wh["qh_ls_wh_ok"]["status"] == "OK"
    assert by_wh["qh_ls_wh_warn"]["status"] == "WARN"
    assert by_wh["qh_ls_wh_crit"]["status"] == "CRITICAL"
    assert by_wh["qh_ls_wh_crit"]["spilled_local_bytes_sum"] == 15_000_000_000

    # executed_by: mask_user() is off by default, so the raw value passes through.
    assert by_wh["qh_ls_wh_warn"]["executed_by"] == "dana@example.com"

    seq = [r["status"] for r in out
           if r["warehouse_id"] in ("qh_ls_wh_crit", "qh_ls_wh_warn", "qh_ls_wh_ok")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq  # order_by: spilled_local_bytes_sum DESC

    d7, d30, d90 = date(2026, 9, 18), date(2026, 9, 5), date(2026, 7, 15)
    for window_days, expect_days in [(7, {d7}), (30, {d7, d30}), (90, {d7, d30, d90})]:
        out_w = dbutil.rows("query_local_spillage", window_days, workspace_ids=[WS])
        days_present = {r["day"] for r in out_w if r["warehouse_id"] == "qh_ls_win"}
        assert days_present == expect_days, (window_days, days_present, expect_days)


# -------------------------------------------------------------------------------------------
# query_per_query_estimate_lane -- row-level; no status; filter: FINISHED, not cached,
# execution_duration_ms>0, compute.warehouse_id IS NOT NULL.
# -------------------------------------------------------------------------------------------
def test_query_per_query_estimate_lane():
    out = dbutil.rows("query_per_query_estimate_lane", 30, workspace_ids=None)
    by_sid = {r["statement_id"]: r for r in out}

    assert "qh_pel_ok1" in by_sid and "qh_pel_ok2" in by_sid
    for excluded in ("qh_pel_excl_wh", "qh_pel_excl_status", "qh_pel_excl_cached", "qh_pel_excl_zero"):
        assert excluded not in by_sid, f"{excluded} should be excluded by the row-level filter"

    assert by_sid["qh_pel_ok1"]["execution_duration_ms"] == 1000
    assert by_sid["qh_pel_ok1"]["waiting_for_compute_duration_ms"] == 100
    assert by_sid["qh_pel_ok1"]["total_task_duration_ms"] == 4000
    assert by_sid["qh_pel_ok1"]["read_bytes"] == 10_000
    assert by_sid["qh_pel_ok1"]["total_duration_ms"] == 1200

    for window_days, expect in [
        (7, {"qh_pel_win_t7"}),
        (30, {"qh_pel_win_t7", "qh_pel_win_t30"}),
        (90, {"qh_pel_win_t7", "qh_pel_win_t30", "qh_pel_win_t90"}),
    ]:
        out_w = dbutil.rows("query_per_query_estimate_lane", window_days, workspace_ids=None)
        present = {r["statement_id"] for r in out_w if r["statement_id"].startswith("qh_pel_win_")}
        assert present == expect, (window_days, present, expect)


# -------------------------------------------------------------------------------------------
# query_provenance_by_source -- no status; one row per source_kind CASE branch.
# -------------------------------------------------------------------------------------------
def test_query_provenance_by_source():
    out = dbutil.rows("query_provenance_by_source", 30, workspace_ids=[WS])
    by_job = {r["job_id"]: r for r in out if r["job_id"] == "qh_prov_job_1"}
    assert by_job["qh_prov_job_1"]["source_kind"] == "job"

    dash_row = [r for r in out if r["dashboard_id"] == "qh_prov_dash_1"][0]
    assert dash_row["source_kind"] == "dashboard"
    nb_row = [r for r in out if r["notebook_id"] == "qh_prov_nb_1"][0]
    assert nb_row["source_kind"] == "notebook"

    # source_kind is derived from query_source alone, not from an output column for every
    # branch (legacy_dashboard/alert/genie/sql_editor/other have no dedicated output column in
    # this id's SELECT list) -- distinguish those rows via workspace/compute/executed_by, which
    # are identical across all 8 branch rows, leaving source_kind as the only real differentiator;
    # collect the set of source_kind values seen among this scenario's known 8 branch rows by
    # excluding the ones already pinned above via job_id/dashboard_id/notebook_id and the window
    # family (job_id startswith qh_prov_win_).
    branch_rows = [r for r in out
                   if r["executed_by"] == "frank@example.com"
                   and not (r["job_id"] or "").startswith("qh_prov_win_")]
    kinds = {r["source_kind"] for r in branch_rows}
    assert kinds == {"job", "dashboard", "legacy_dashboard", "notebook", "alert", "genie",
                      "sql_editor", "other"}, kinds

    # This id's query has no statement_id filter, so it aggregates the WHOLE query__history
    # table within window/workspace -- including rows other ids' fixtures wrote for WS=1111
    # within D7 (qh_cs_wh3_big and qh_ls_crit, both executed_by='__REDACTED__'; qh_cs_wh3_small,
    # executed_by='root_operator', a non-email non-GUID identity). mask_user() is off by default,
    # so both pass through raw, same as every other identity here.
    ebs = {r["executed_by"] for r in out}
    assert "__REDACTED__" in ebs
    assert "root_operator" in ebs

    for window_days, anchors in [
        (7, {"qh_prov_win_t7"}),
        (30, {"qh_prov_win_t7", "qh_prov_win_t30"}),
        (90, {"qh_prov_win_t7", "qh_prov_win_t30", "qh_prov_win_t90"}),
    ]:
        out_w = dbutil.rows("query_provenance_by_source", window_days, workspace_ids=[WS])
        present = {r["job_id"] for r in out_w if (r["job_id"] or "").startswith("qh_prov_win_")}
        assert present == anchors, (window_days, present, anchors)


# -------------------------------------------------------------------------------------------
# query_pruning_effectiveness -- status: warn=0.5, crit=0.2 (ratio); WHERE excludes pruned=0
# AND read=0 entirely (never even NOT_ASSESSED).
# -------------------------------------------------------------------------------------------
def test_query_pruning_effectiveness():
    out = dbutil.rows("query_pruning_effectiveness", 30, workspace_ids=[WS])
    # qh_pe_wh_crit has two flagged days (days_flagged=2 clears :min_recurring_flagged_days), so
    # both stay CRITICAL instead of being capped to WARN as a one-off.
    crit_rows = [r for r in out if r["warehouse_id"] == "qh_pe_wh_crit"]
    assert len(crit_rows) == 2 and all(r["status"] == "CRITICAL" for r in crit_rows)
    by_wh = {r["warehouse_id"]: r for r in out
             if r["warehouse_id"] in ("qh_pe_wh_warn", "qh_pe_wh_ok", "qh_pe_wh_zero")}
    assert by_wh["qh_pe_wh_warn"]["status"] == "WARN"
    assert by_wh["qh_pe_wh_ok"]["status"] == "OK"
    assert "qh_pe_wh_zero" not in by_wh, "all-zero pruned/read row must never appear, not even NOT_ASSESSED"
    for window_days in WINDOWS:
        out_w = dbutil.rows("query_pruning_effectiveness", window_days, workspace_ids=[WS])
        assert not any(r["warehouse_id"] == "qh_pe_wh_zero" for r in out_w), window_days

    # worst-first: order_by is ratio ASC -- both CRITICAL (ratio 0.1) rows sort before WARN (0.3)
    # before OK (0.8).
    seq = [r["status"] for r in out
           if r["warehouse_id"] in ("qh_pe_wh_crit", "qh_pe_wh_warn", "qh_pe_wh_ok")]
    assert seq == ["CRITICAL", "CRITICAL", "WARN", "OK"], seq

    d7, d30, d90 = date(2026, 9, 18), date(2026, 9, 5), date(2026, 7, 15)
    for window_days, expect_days in [(7, {d7}), (30, {d7, d30}), (90, {d7, d30, d90})]:
        out_w = dbutil.rows("query_pruning_effectiveness", window_days, workspace_ids=[WS])
        days_present = {r["day"] for r in out_w if r["warehouse_id"] == "qh_pe_win"}
        assert days_present == expect_days, (window_days, days_present, expect_days)


# -------------------------------------------------------------------------------------------
# query_queuing_waits -- status: warn=60s (60000ms), crit=600s (600000ms) combined.
# -------------------------------------------------------------------------------------------
def test_query_queuing_waits():
    out = dbutil.rows("query_queuing_waits", 30, workspace_ids=[WS])
    by_wh = {r["warehouse_id"]: r for r in out
             if r["warehouse_id"] in ("qh_qw_wh_ok", "qh_qw_wh_warn", "qh_qw_wh_crit")}
    assert by_wh["qh_qw_wh_ok"]["status"] == "OK"
    assert by_wh["qh_qw_wh_warn"]["status"] == "WARN"
    assert by_wh["qh_qw_wh_crit"]["status"] == "CRITICAL"
    assert (by_wh["qh_qw_wh_crit"]["waiting_at_capacity_ms_sum"]
            + by_wh["qh_qw_wh_crit"]["waiting_for_compute_ms_sum"]) == 700_000
    # one statement per warehouse here, so avg_wait_ms equals that single statement's own wait.
    assert by_wh["qh_qw_wh_ok"]["avg_wait_ms"] == 15_000
    assert by_wh["qh_qw_wh_warn"]["avg_wait_ms"] == 70_000
    assert by_wh["qh_qw_wh_crit"]["avg_wait_ms"] == 700_000

    seq = [r["status"] for r in out
           if r["warehouse_id"] in ("qh_qw_wh_crit", "qh_qw_wh_warn", "qh_qw_wh_ok")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq

    # T-75B: a group where BOTH wait buckets are NULL on every row (SERVERLESS_COMPUTE that never
    # reports either wait) must read NOT_ASSESSED, not silently fall through NULL + SUM = NULL to
    # a false OK.
    unreported = next(r for r in out if r["warehouse_id"] == "qh_qw_wh_unreported")
    assert unreported["status"] == "NOT_ASSESSED"
    assert unreported["waiting_at_capacity_ms_sum"] is None
    assert unreported["waiting_for_compute_ms_sum"] is None
    assert unreported["avg_wait_ms"] is None

    # T-75B review fix: only ONE bucket is NULL, but the OTHER bucket alone (900s) clears
    # crit_queue_secs=600 by itself -- must read CRITICAL, not NOT_ASSESSED (CRITICAL/WARN is
    # banded on COALESCE(SUM,0)+COALESCE(SUM,0) BEFORE the either-NULL NOT_ASSESSED check).
    partial_crit = next(r for r in out if r["warehouse_id"] == "qh_qw_wh_partial_crit")
    assert partial_crit["status"] == "CRITICAL"
    assert partial_crit["waiting_at_capacity_ms_sum"] is None
    assert partial_crit["waiting_for_compute_ms_sum"] == 900_000
    assert partial_crit["avg_wait_ms"] == 900_000

    d7, d30, d90 = date(2026, 9, 18), date(2026, 9, 5), date(2026, 7, 15)
    for window_days, expect_days in [(7, {d7}), (30, {d7, d30}), (90, {d7, d30, d90})]:
        out_w = dbutil.rows("query_queuing_waits", window_days, workspace_ids=[WS])
        days_present = {r["day"] for r in out_w if r["warehouse_id"] == "qh_qw_win"}
        assert days_present == expect_days, (window_days, days_present, expect_days)


# -------------------------------------------------------------------------------------------
# Synthetic, in-memory: a day of many tiny waits must not sum into a false CRITICAL -- status is
# judged on the average wait of statements that waited, not the day's raw total.
# -------------------------------------------------------------------------------------------
def test_query_queuing_waits_many_tiny_waits_reads_ok():
    con = duckdb.connect()
    try:
        for _name, create_sql in ddl.DDL.items():
            con.execute(create_sql)

        rows = [
            qh._row(statement_id=f"qh_qw_tiny_{i}", start_time=qh.D7,
                     compute=qh._compute(warehouse_id="qh_qw_wh_tiny"),
                     waiting_at_capacity_duration_ms=1_000, waiting_for_compute_duration_ms=0)
            for i in range(1_000)
        ]
        cols_sql = ", ".join(f'"{c}"' for c in qh.QH_COLUMNS)
        placeholders = ", ".join("?" for _ in qh.QH_COLUMNS)
        con.executemany(
            f"INSERT INTO query__history ({cols_sql}) VALUES ({placeholders})",
            [[r[c] for c in qh.QH_COLUMNS] for r in rows],
        )

        spec = by_id("query_queuing_waits")
        text = render_model(spec, target="duckdb")
        sql = jinja_stub.render(text, windows=(30,), source=jinja_stub.memory_source)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
        r = [x for x in out if x["warehouse_id"] == "qh_qw_wh_tiny"][0]

        assert r["waiting_at_capacity_ms_sum"] == 1_000_000  # 1,000 statements x 1 s each
        assert r["avg_wait_ms"] == 1_000                     # but each one only waited 1 s
        assert r["status"] == "OK"
    finally:
        con.close()


# -------------------------------------------------------------------------------------------
# query_shuffle_write_amplification -- status: warn_shuffle=50GB, crit_shuffle=200GB,
# warn_file=32MB, crit_file=8MB. Two independent trigger signals (shuffle volume, small files).
# -------------------------------------------------------------------------------------------
def test_query_shuffle_write_amplification():
    out = dbutil.rows("query_shuffle_write_amplification", 30, workspace_ids=[WS])
    by_wh = {r["warehouse_id"]: r for r in out if r["warehouse_id"] in (
        "qh_sw_wh_shufflecrit", "qh_sw_wh_shufflewarn", "qh_sw_wh_ok",
        "qh_sw_wh_smallfilecrit", "qh_sw_wh_smallfilewarn",
    )}
    assert by_wh["qh_sw_wh_shufflecrit"]["status"] == "CRITICAL"
    assert by_wh["qh_sw_wh_shufflewarn"]["status"] == "WARN"
    assert by_wh["qh_sw_wh_ok"]["status"] == "OK"
    assert by_wh["qh_sw_wh_smallfilecrit"]["status"] == "CRITICAL"  # triggered by file size, not shuffle (0)
    assert by_wh["qh_sw_wh_smallfilewarn"]["status"] == "WARN"

    # worst-first, scoped to the shuffle-triggered trio only (order_by is shuffle_read_bytes_sum
    # DESC, which the independent small-file signal is not monotonic with -- see module docstring).
    seq = [r["status"] for r in out
           if r["warehouse_id"] in ("qh_sw_wh_shufflecrit", "qh_sw_wh_shufflewarn", "qh_sw_wh_ok")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq

    for window_days in WINDOWS:
        out_w = dbutil.rows("query_shuffle_write_amplification", window_days, workspace_ids=[WS])
        assert any(r["warehouse_id"] == "qh_sw_wh_smallfilecrit" for r in out_w)

    d7, d30, d90 = date(2026, 9, 18), date(2026, 9, 5), date(2026, 7, 15)
    for window_days, expect_days in [(7, {d7}), (30, {d7, d30}), (90, {d7, d30, d90})]:
        out_w = dbutil.rows("query_shuffle_write_amplification", window_days, workspace_ids=[WS])
        days_present = {r["day"] for r in out_w if r["warehouse_id"] == "qh_sw_win"}
        assert days_present == expect_days, (window_days, days_present, expect_days)


# -------------------------------------------------------------------------------------------
# query_workload_mix_hours -- no status; grain includes hour(start_time).
# -------------------------------------------------------------------------------------------
def test_query_workload_mix_hours():
    out = dbutil.rows("query_workload_mix_hours", 30, workspace_ids=[WS])
    hour_rows = {r["hour_of_day"]: r for r in out if r["warehouse_id"] == "qh_wm_hourwh"}
    assert set(hour_rows) == {3, 20}, hour_rows
    assert hour_rows[3]["produced_rows_sum"] == 50
    assert hour_rows[20]["produced_rows_sum"] == 75

    failed_row = [r for r in out if r["warehouse_id"] == "qh_wm_failedwh"][0]
    assert failed_row["query_count"] == 1
    assert failed_row["finished_count"] == 0

    d7, d30, d90 = date(2026, 9, 18), date(2026, 9, 5), date(2026, 7, 15)
    for window_days, expect_days in [(7, {d7}), (30, {d7, d30}), (90, {d7, d30, d90})]:
        out_w = dbutil.rows("query_workload_mix_hours", window_days, workspace_ids=[WS])
        days_present = {r["day"] for r in out_w if r["warehouse_id"] == "qh_wm_win"}
        assert days_present == expect_days, (window_days, days_present, expect_days)
