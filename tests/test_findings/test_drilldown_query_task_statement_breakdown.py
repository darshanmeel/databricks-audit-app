"""tests/test_findings/test_drilldown_query_task_statement_breakdown.py

Ported from the earlier subaudit suite's tests/
test_task_statement_breakdown.py's main()/check() calls (54 assertions), reading the dbt-built
findings.f_query_task_statement_breakdown table through tests/dbutil.py.

See tests/fixtures/drilldown.py's module docstring for why this file filters the workspace-scoped
rows down to this scenario's own dd_qts_ ids before applying the original assertions (DEC-15).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbutil

WINDOW_DAYS = 30
WORKSPACE = "9001"


def test_query_task_statement_breakdown():
    raw = dbutil.rows("query_task_statement_breakdown", WINDOW_DAYS, workspace_ids=[WORKSPACE])
    out = [r for r in raw if r["job_run_id"] and r["job_run_id"].startswith("dd_qts_")]

    by_run: dict[str, list[dict]] = {}
    for r in out:
        by_run.setdefault(r["job_run_id"], []).append(r)

    # 1. scoping
    assert "dd_qts_R2" not in by_run, "R2 (1 h) must be excluded by :warn_run_hours"
    # The prefix filter above (`out = [...]`) already drops any row whose job_run_id is not one
    # of this scenario's own dd_qts_ ids, which would make a `None not in by_run` half of this
    # check vacuously true no matter what the model does -- so the NULL-job_run_id half is
    # checked against `raw` instead (still scoped to this batch's own workspace 9001, DEC-15),
    # which is what actually proves the model never emits a row with a NULL job_run_id (the
    # dd_qts_s-x1 unknown-run statement must be dropped entirely, never surface as a row with
    # job_run_id = NULL).
    assert "dd_qts_R_unknown" not in by_run and None not in {r["job_run_id"] for r in raw}, (
        "statements of a run the timeline does not know must be dropped"
    )
    assert "dd_qts_R1" in by_run, "multi-task run with run_duration_seconds = 0 must still be examined (wall-clock fallback)"

    # 2. grain: one row per (run, task-or-bucket, shape)
    keys = [(r["job_run_id"], r["task_run_id"], r["stmt_task_run_id"], r["shape_fingerprint"]) for r in out]
    assert len(keys) == len(set(keys)), "fan-out: duplicate rows"

    r1 = by_run["dd_qts_R1"]
    t: dict[str, list[dict]] = {}
    for r in r1:
        t.setdefault(r["task_run_id"], []).append(r)

    # 3. classic-cluster task -> exactly one NOT_ASSESSED row with the classic reason
    t2 = t["dd_qts_T2"]
    assert len(t2) == 1 and t2[0]["status"] == "NOT_ASSESSED", "classic task T2 must be one NOT_ASSESSED row"
    assert t2[0]["not_assessed_reason"] == "classic_cluster_not_in_query_history", t2[0]["not_assessed_reason"]
    assert (
        t2[0]["task_compute_kind"] == "CLASSIC_CLUSTER"
        and t2[0]["task_cluster_id"] == "dd_qts_c1"
        and t2[0]["task_compute_type_raw"] == "CLASSIC"
    ), "raw compute columns"
    assert t2[0]["shape_fingerprint"] is None and t2[0]["stmt_rank_in_task"] is None and t2[0]["total_ms"] is None, (
        "NOT_ASSESSED row carries no statement columns"
    )
    assert t2[0]["task_statement_count"] == 0 and t2[0]["sql_may_be_incomplete"] is False, (
        "task_statement_count 0, settled"
    )

    # 4. warehouse task with no captured SQL; skipped task never ran
    t3 = t["dd_qts_T3"]
    assert len(t3) == 1 and t3[0]["status"] == "NOT_ASSESSED" and t3[0]["not_assessed_reason"] == "no_statements_captured", (
        t3[0]["not_assessed_reason"]
    )
    assert t3[0]["task_compute_kind"] == "SQL_WAREHOUSE", t3[0]["task_compute_kind"]
    t8 = t["dd_qts_T8"]
    assert len(t8) == 1 and t8[0]["status"] == "NOT_ASSESSED" and t8[0]["not_assessed_reason"] == "task_not_executed", (
        f"skipped task: {t8[0]['not_assessed_reason']}"
    )
    assert t8[0]["task_state"] == "SKIPPED", "skipped state"

    # 5. serverless task with statements (doc join shape: job_run_id NULL): ranks, shares, bands
    t1b = sorted(t["dd_qts_T1b"], key=lambda r: r["stmt_rank_in_task"])
    assert [r["stmt_rank_in_task"] for r in t1b] == [1, 2, 3, 4, 5], (
        f"T1b must have 5 shapes ranked 1..5, got {[(r['sample_statement_id'], r['total_ms']) for r in t1b]}"
    )
    assert (
        t1b[0]["sample_statement_id"] == "dd_qts_s-b1"
        and t1b[0]["status"] == "CRITICAL"
        and abs(t1b[0]["share_of_task_pct"] - 80.0) < 0.2
    ), "shape B (80 %) CRITICAL"
    assert (
        t1b[1]["statement_count"] == 3 and t1b[1]["status"] == "WARN" and abs(t1b[1]["share_of_task_pct"] - 50.0) < 0.2
    ), "shape A (50 %) WARN"
    loop = t1b[2]
    assert (
        loop["statement_count"] == 3 and loop["literal_variants_in_shape"] == 3 and loop["sample_statement_id"] == "dd_qts_s-l0"
    ), f"numeric loop must be ONE shape of 3 statements: {loop['statement_count']}/{loop['literal_variants_in_shape']}"
    assert loop["status"] == "OK" and loop["total_ms"] == 6 * 60 * 1000, "loop shape totals"
    assert t1b[3]["sample_statement_id"] == "dd_qts_s-c1" and t1b[3]["status"] == "OK", "shape C OK"
    assert t1b[4]["sample_statement_id"] == "dd_qts_s-d1" and t1b[4]["statements_not_finished"] == 1, (
        "failed shape kept and counted"
    )
    assert all(
        r["task_compute_kind"] == "SERVERLESS_OR_UNRECORDED" and r["job_run_id"] == "dd_qts_R1" and r["job_id"] == "dd_qts_J1"
        for r in t1b
    ), "task keys resolved from the task side"
    assert all(r["task_statement_count"] == 9 for r in t1b), f"task_statement_count = 9 in T1b, got {t1b[0]['task_statement_count']}"
    expected_task_share = 100.0 * (2.4 * 3600 + 3 * 1800 + 3 * 120 + 300 + 60) / 10800
    assert all(abs(r["task_sql_share_pct"] - expected_task_share) < 0.2 for r in t1b), (
        f"task_sql_share_pct {t1b[0]['task_sql_share_pct']} vs {expected_task_share}"
    )
    assert all(r["not_assessed_reason"] is None and r["text_redacted"] is False for r in t1b), (
        "assessed rows carry no reason"
    )
    assert t1b[0]["total_task_ms"] == 4 * t1b[0]["total_ms"], "total_task_ms carried"

    # 6. de-valued text + both fingerprints
    a = t1b[1]
    assert (
        "secret@example.com" not in a["statement_text_devalued"]
        and "'" not in a["statement_text_devalued"]
        and "?" in a["statement_text_devalued"]
    ), a["statement_text_devalued"]
    b = t1b[0]
    assert b["statement_text_devalued"] == "SELECT * FROM big WHERE k = ?", b["statement_text_devalued"]
    assert b["statement_fingerprint"] == hashlib.sha256(b["statement_text_devalued"].encode()).hexdigest(), (
        "statement_fingerprint = repo recipe"
    )
    assert b["shape_fingerprint"] == hashlib.sha256(b["statement_text_devalued"].encode()).hexdigest(), (
        "no numbers -> shape = statement fingerprint"
    )
    assert loop["shape_fingerprint"] == hashlib.sha256(
        "DELETE FROM t WHERE batch_id = ? AND id IN (?)".encode()
    ).hexdigest(), "shape masks numbers and collapses ?-lists"
    assert (
        "17" in loop["statement_text_devalued"]
        or "18" in loop["statement_text_devalued"]
        or "19" in loop["statement_text_devalued"]
    ), "devalued text keeps the string-only recipe"

    # 7. failed first attempt is its own task run
    t1 = t["dd_qts_T1"]
    assert len(t1) == 1 and t1[0]["sample_statement_id"] == "dd_qts_s-t1" and t1[0]["task_state"] == "FAILED", (
        "retry grain = task run"
    )

    # 8. unattributed statements: two buckets (NULL task id, ghost task id), measured against the run
    un = sorted([r for r in r1 if r["task_run_id"] is None], key=lambda r: r["sample_statement_id"])
    assert len(un) == 2, f"two UNATTRIBUTED rows expected, got {len(un)}"
    ghost, nul = un[0], un[1]
    assert (
        ghost["stmt_task_run_id"] == "dd_qts_T-ghost"
        and ghost["stmt_rank_in_task"] == 1
        and abs(ghost["share_of_task_pct"] - 10.0) < 0.2
    ), "ghost bucket"
    assert (
        nul["stmt_task_run_id"] is None
        and nul["stmt_rank_in_task"] == 1
        and abs(nul["share_of_task_pct"] - 20.0) < 0.2
        and nul["status"] == "OK"
    ), "null bucket"
    assert all(
        r["task_compute_kind"] == "UNATTRIBUTED" and r["task_key"] is None and r["job_id"] == "dd_qts_J1" and r["task_statement_count"] is None
        for r in un
    ), "unattributed row keys"

    # 9. per-run coverage counts on every row of the run
    assert all(
        r["run_tasks_seen"] == 5 and r["run_tasks_not_assessed"] == 2 and r["run_unattributed_statements"] == 2 for r in r1
    ), f"run counts: {r1[0]['run_tasks_seen']}/{r1[0]['run_tasks_not_assessed']}/{r1[0]['run_unattributed_statements']}"
    assert all(r["job_name"] == "etl_daily_v2" for r in r1), "SCD2: latest job name"
    assert all(abs(r["run_hours"] - 5.0) < 0.01 and r["in_flight"] is False for r in r1), (
        "R1 run_hours 5 from wall clock, finished"
    )

    # 10. in-flight submit run on an unknown cluster
    r3 = by_run["dd_qts_R3"]
    assert len(r3) == 1 and r3[0]["in_flight"] is True and r3[0]["job_name"] is None and r3[0]["task_state"] == "RUNNING", (
        "R3 in flight"
    )
    assert 2.9 < r3[0]["run_hours"] < 3.1 and r3[0]["task_compute_kind"] == "UNRESOLVED", f"R3 {r3[0]['run_hours']}"
    assert (
        r3[0]["status"] == "NOT_ASSESSED"
        and r3[0]["not_assessed_reason"] == "statements_may_still_be_landing"
        and r3[0]["sql_may_be_incomplete"] is True
    ), r3[0]["not_assessed_reason"]

    # 11. :top_n caps shapes per task; legacy run duration honoured
    r4 = sorted(by_run["dd_qts_R4"], key=lambda r: r["stmt_rank_in_task"])
    assert len(r4) == 10 and [r["stmt_rank_in_task"] for r in r4] == list(range(1, 11)), "top_n = 10 shapes for T6"
    assert r4[0]["total_ms"] == 15 * 60 * 1000 and all(r["task_statement_count"] == 15 for r in r4), (
        "worst-first within task; count is of all 15"
    )
    assert abs(r4[0]["run_hours"] - 6.0) < 0.01, "legacy run_duration_seconds used"

    # 12. <REDACTED>: per-statement rows, still banded
    r5 = sorted(by_run["dd_qts_R5"], key=lambda r: r["stmt_rank_in_task"])
    assert len(r5) == 2 and all(r["text_redacted"] is True and r["shape_fingerprint"].startswith("statement:") for r in r5), (
        "redacted -> one row per statement"
    )
    assert r5[0]["sample_statement_id"] == "dd_qts_s-q1" and r5[0]["status"] == "CRITICAL" and r5[1]["status"] == "OK", (
        "redacted rows banded per statement"
    )
    assert all(r["statement_count"] == 1 for r in r5), "no grouping when redacted"

    # 13. task that ended 20 minutes ago: a small shape is NOT_ASSESSED (landing), a big one still CRITICAL
    r6 = sorted(by_run["dd_qts_R6"], key=lambda r: r["stmt_rank_in_task"])
    assert len(r6) == 2 and all(r["sql_may_be_incomplete"] is True for r in r6), "settle window flagged"
    assert r6[0]["status"] == "CRITICAL" and r6[0]["not_assessed_reason"] is None, "over the bar still flags while landing"
    assert r6[1]["status"] == "NOT_ASSESSED" and r6[1]["not_assessed_reason"] == "statements_may_still_be_landing", (
        "small shape not banded OK while landing"
    )

    # 14. ordering + enum
    order = [r["status"] for r in out]
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    assert order == sorted(order, key=rank.get) and out[0]["status"] == "CRITICAL", f"status order {order}"
    assert set(order) <= set(rank), "status enum"
    assert all(r["status"] != "NOT_ASSESSED" or r["not_assessed_reason"] is not None for r in out), (
        "every NOT_ASSESSED row has a reason"
    )
