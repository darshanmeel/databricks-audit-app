"""tests/test_findings/test_lakeflow_pipelines.py -- T-16.

Proves, against tests/fixtures/lakeflow.py's own rows and only those rows, that each of the 5 C1
"pipeline" findings.f_<query_id> tables (batch C, PLAN.md 7.2) has the right grain, the right
status band on a named row, that the four windowed ids' AS_OF-day rows are excluded from every
window, and that net_pipeline_dbus / net_dbus equal tests/dbutil.usage_sum(...) computed straight
from the builder's own billing__usage parquet -- never a hard-coded total. Per
tests/test_findings/README.md's checklist.

lakeflow_pipelines_inventory_tier is the one snapshot-type id in this batch (single
`0 AS window_days` block, plan 5.3): tested at window_days=0 with grain uniqueness only, no status
assertion (the query has no status column) and no usage_sum assertion (it reads only
system.lakeflow.pipelines).

The other 4 C1 ids share this fixture's `lf_` id space with T-17's 7 C2 ids and T-18's 9 C3 ids
(all written by the same lakeflow.py builder, per the task file's "this builder must carry enough
shape to serve all 21 lakeflow_* queries" requirement) -- every assertion below therefore filters
the rows dbutil.rows() returns down to this file's own scenario ids (the `lf_pl_*` / `lf_job_swf_*`
prefixes named in lakeflow.py's SEC A/B/C) before asserting, per DEC-15 ("assertions filter on the
builder's own ids, never on workspace alone").
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_pipelines.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WS1 = "1111"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

# audit_today()/audit_now() are pinned to this exact instant on the `test` dbt target
# (dbt/macros/audit_time.sql), matching tests/fixtures/base.py's AS_OF.
AS_OF = datetime(2026, 9, 21, 12, 0, 0)
D0 = AS_OF.date()

ALL_IDS = [
    "lakeflow_pipeline_cost",
    "lakeflow_pipeline_idle_tail_duration",
    "lakeflow_pipeline_update_failures_retries",
    "lakeflow_pipelines_inventory_tier",
    "lakeflow_succeeded_with_failed_tasks",
]
WINDOWED_IDS = [
    "lakeflow_pipeline_cost",
    "lakeflow_pipeline_idle_tail_duration",
    "lakeflow_pipeline_update_failures_retries",
    "lakeflow_succeeded_with_failed_tasks",
]


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _assert_grain_unique(rows: list[dict], key_fields: list[str]) -> None:
    assert rows, f"no rows to check grain uniqueness on ({key_fields}) -- an empty result proves nothing"
    keys = [tuple(r[f] for f in key_fields) for r in rows]
    assert len(keys) == len(set(keys)), f"duplicate grain keys: {[k for k in keys if keys.count(k) > 1]}"


def _assert_status_enum(rows: list[dict]) -> None:
    assert rows, "no rows to check the status enum on -- an empty result proves nothing"
    bad = {r["status"] for r in rows} - STATUS_VALUES
    assert not bad, f"status values outside the enum: {bad}"


def _raw_row_exists(schema_table: str, where: str) -> bool:
    """True if at least one row matching `where` exists anywhere in the raw
    tests/fixtures/parquet/<schema_table>/*.parquet fixture (bypassing every window/model filter)
    -- proves a negative-control row genuinely exists rather than having been silently deleted or
    renamed (companion to every "must be absent from the model" assertion below; mirrors
    test_lakeflow_run_timeline.py's own _raw_row_exists, generalised here to the three different
    source tables this file's four AS_OF-day negative controls live in)."""
    glob = (PARQUET_DIR / schema_table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT COUNT(*) FROM read_parquet('{glob}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0] > 0
    finally:
        con.close()


def _dbu_where(pipeline_id: str, window_days: int, *, extra: str = "") -> str:
    lower = D0 - timedelta(days=window_days)
    frag = (
        f"usage_metadata.dlt_pipeline_id = '{pipeline_id}' AND upper(usage_unit) = 'DBU' "
        f"AND usage_date >= DATE '{lower.isoformat()}' AND usage_date < DATE '{D0.isoformat()}'"
    )
    return frag + (f" AND {extra}" if extra else "")


# -------------------------------------------------------------------------------------------
# Generic checks over all 5 ids.
# -------------------------------------------------------------------------------------------
def test_grain_uniqueness_all_ids():
    grains = _grains()
    for qid in ALL_IDS:
        cols = grains[qid]
        window_days = 0 if qid == "lakeflow_pipelines_inventory_tier" else 30
        out = dbutil.rows(qid, window_days, workspace_ids=None)
        _assert_grain_unique(out, cols)


def test_window_days_zero_is_empty_for_windowed_ids():
    for qid in WINDOWED_IDS:
        assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty (windowed id)"


def test_status_enum_for_windowed_ids():
    for qid in WINDOWED_IDS:
        for w in WINDOWS:
            _assert_status_enum(dbutil.rows(qid, w))


# -------------------------------------------------------------------------------------------
# lakeflow_pipeline_cost -- warn_pipeline_dbus=500, crit_pipeline_dbus=2000. Grain
# [workspace_id, pipeline_id]. order_by: net_pipeline_dbus DESC.
# -------------------------------------------------------------------------------------------
def test_lakeflow_pipeline_cost():
    out = dbutil.rows("lakeflow_pipeline_cost", 30, workspace_ids=[WS1])
    by_pid = {r["pipeline_id"]: r for r in out
              if r["pipeline_id"] in ("lf_pl_cost_crit", "lf_pl_cost_warn", "lf_pl_cost_ok")}

    crit = by_pid["lf_pl_cost_crit"]
    assert crit["status"] == "CRITICAL"
    expected_crit = dbutil.usage_sum(_dbu_where("lf_pl_cost_crit", 30))
    assert expected_crit == 2500.0
    assert crit["net_pipeline_dbus"] == expected_crit
    expected_maint = dbutil.usage_sum(_dbu_where("lf_pl_cost_crit", 30, extra="usage_metadata.dlt_maintenance_id IS NOT NULL"))
    assert expected_maint == 400.0
    assert crit["net_maintenance_dbus"] == expected_maint

    warn = by_pid["lf_pl_cost_warn"]
    assert warn["status"] == "WARN"
    assert warn["net_pipeline_dbus"] == dbutil.usage_sum(_dbu_where("lf_pl_cost_warn", 30)) == 800.0

    ok = by_pid["lf_pl_cost_ok"]
    assert ok["status"] == "OK"
    assert ok["net_pipeline_dbus"] == dbutil.usage_sum(_dbu_where("lf_pl_cost_ok", 30)) == 100.0

    # AS_OF-day exclusion: lf_pl_asof_excl's only billing.usage row lands on D0 (today) and is
    # excluded by usage_date < current_date(); the pipeline never crosses the `net_pipeline_dbus >
    # 0` floor and so never appears in this id's output at all. Anchored first: without the anchor,
    # deleting lf_pl_asof_excl's fixture row entirely would make the "not in" checks below pass
    # just as well, silently losing this id's own AS_OF-day-exclusion proof.
    assert _raw_row_exists("billing__usage", "usage_metadata.dlt_pipeline_id = 'lf_pl_asof_excl'")
    assert "lf_pl_asof_excl" not in {r["pipeline_id"] for r in out}
    for w in WINDOWS:
        assert "lf_pl_asof_excl" not in {r["pipeline_id"] for r in dbutil.rows("lakeflow_pipeline_cost", w, workspace_ids=[WS1])}

    # worst-first: order_by is CASE status (CRITICAL/WARN/NOT_ASSESSED/OK), then net_pipeline_dbus
    # DESC (P4-FIXES28 added the status key; net_pipeline_dbus DESC alone is no longer the whole
    # story). Checked over the WHOLE physical row order dbutil.rows() returned for this workspace
    # (never re-sorted here, never filtered to a named subset first) -- a filtered 3-element
    # subsequence can coincidentally look sorted purely from DuckDB's own hash-aggregate bucket
    # order even with no ORDER BY at all.
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    ranks = [rank[r["status"]] for r in out]
    assert ranks == sorted(ranks), [(r["pipeline_id"], r["status"]) for r in out]
    for a, b in zip(out, out[1:]):
        if a["status"] == b["status"]:
            assert a["net_pipeline_dbus"] >= b["net_pipeline_dbus"], (a["pipeline_id"], b["pipeline_id"])

    assert dbutil.rows("lakeflow_pipeline_cost", 0) == []


# -------------------------------------------------------------------------------------------
# lakeflow_pipeline_idle_tail_duration -- warn_idle_dbus=100, crit_idle_dbus=500. NOT_ASSESSED
# when settings.continuous=true. Grain [workspace_id, pipeline_id]. order_by: net_dbus DESC.
# -------------------------------------------------------------------------------------------
def test_lakeflow_pipeline_idle_tail_duration():
    out = dbutil.rows("lakeflow_pipeline_idle_tail_duration", 30, workspace_ids=[WS1])
    by_pid = {r["pipeline_id"]: r for r in out if r["pipeline_id"] in (
        "lf_pl_idle_continuous", "lf_pl_idle_crit", "lf_pl_idle_warn", "lf_pl_idle_ok",
    )}

    cont = by_pid["lf_pl_idle_continuous"]
    assert cont["status"] == "NOT_ASSESSED"
    assert cont["setting_continuous"] is True

    crit = by_pid["lf_pl_idle_crit"]
    assert crit["status"] == "CRITICAL"
    assert crit["setting_continuous"] is False
    expected_crit = dbutil.usage_sum(_dbu_where("lf_pl_idle_crit", 30))
    assert expected_crit == 600.0
    assert crit["net_dbus"] == expected_crit

    warn = by_pid["lf_pl_idle_warn"]
    assert warn["status"] == "WARN"
    assert warn["net_dbus"] == dbutil.usage_sum(_dbu_where("lf_pl_idle_warn", 30)) == 150.0

    ok = by_pid["lf_pl_idle_ok"]
    assert ok["status"] == "OK"
    assert ok["net_dbus"] == dbutil.usage_sum(_dbu_where("lf_pl_idle_ok", 30)) == 20.0

    # AS_OF-day exclusion: lf_pl_asof_excl's only pipeline_update_timeline row has period_end_time
    # on D0 (today), excluded by period_end_time < date_trunc('DAY', current_timestamp()); the
    # pipeline never appears as a row in this id's output (it is driven FROM the update timeline).
    # Anchored first, same reasoning as test_lakeflow_pipeline_cost above.
    assert _raw_row_exists("lakeflow__pipeline_update_timeline", "pipeline_id = 'lf_pl_asof_excl'")
    assert "lf_pl_asof_excl" not in {r["pipeline_id"] for r in out}
    for w in WINDOWS:
        assert "lf_pl_asof_excl" not in {r["pipeline_id"] for r in dbutil.rows("lakeflow_pipeline_idle_tail_duration", w, workspace_ids=[WS1])}

    # worst-first: checked over the WHOLE physical row order dbutil.rows() returned, not a filtered
    # subset -- see test_lakeflow_pipeline_cost's comment on why a filtered subsequence can
    # coincidentally look sorted even without the model's ORDER BY. Verified in-memory: WITH
    # ORDER BY this workspace's full net_dbus sequence is sorted descending; WITHOUT it, it is not.
    seq = [r["net_dbus"] for r in out]
    assert seq == sorted(seq, reverse=True), seq

    assert dbutil.rows("lakeflow_pipeline_idle_tail_duration", 0) == []


# -------------------------------------------------------------------------------------------
# lakeflow_pipeline_update_failures_retries -- warn_failed_updates=3, crit_failed_updates=10.
# Grain [workspace_id, pipeline_id, update_type, trigger_type, result_state]. order_by:
# failed_update_rows DESC.
# -------------------------------------------------------------------------------------------
def test_lakeflow_pipeline_update_failures_retries():
    out = dbutil.rows("lakeflow_pipeline_update_failures_retries", 30, workspace_ids=[WS1])

    def find(pipeline_id, update_type, trigger_type, result_state):
        matches = [
            r for r in out
            if r["pipeline_id"] == pipeline_id and r["update_type"] == update_type
            and r["trigger_type"] == trigger_type and r["result_state"] == result_state
        ]
        assert len(matches) == 1, (pipeline_id, update_type, trigger_type, result_state, matches)
        return matches[0]

    crit = find("lf_pl_upd_crit", "REFRESH", "SCHEDULE", "FAILED")
    assert crit["status"] == "CRITICAL"
    assert crit["failed_update_rows"] == 12
    assert crit["updates"] == 12
    assert crit["retry_triggered_rows"] == 0

    warn = find("lf_pl_upd_warn", "FULL_REFRESH", "SCHEDULE", "FAILED")
    assert warn["status"] == "WARN"
    assert warn["failed_update_rows"] == 5

    retry = find("lf_pl_upd_retry", "REFRESH", "RETRY_ON_FAILURE", "FAILED")
    assert retry["status"] == "OK"
    assert retry["failed_update_rows"] == 2
    assert retry["retry_triggered_rows"] == 2, "every row in this group has trigger_type=RETRY_ON_FAILURE"

    # AS_OF-day exclusion: lf_pl_asof_excl's only (FAILED) update row lands on D0 -> excluded.
    # Anchored first, same reasoning as test_lakeflow_pipeline_cost above.
    assert _raw_row_exists("lakeflow__pipeline_update_timeline", "pipeline_id = 'lf_pl_asof_excl' AND result_state = 'FAILED'")
    assert not any(r["pipeline_id"] == "lf_pl_asof_excl" for r in out)

    # worst-first: checked over the WHOLE physical row order dbutil.rows() returned, not a filtered
    # subset -- see test_lakeflow_pipeline_cost's comment on why a filtered subsequence (whether by
    # pipeline_id alone or by the full identifying tuple) can coincidentally look sorted even
    # without the model's ORDER BY. Verified in-memory: WITH ORDER BY this workspace's full
    # failed_update_rows sequence is sorted descending; WITHOUT it, it is not.
    seq = [r["failed_update_rows"] for r in out]
    assert seq == sorted(seq, reverse=True), seq

    assert dbutil.rows("lakeflow_pipeline_update_failures_retries", 0) == []


# -------------------------------------------------------------------------------------------
# lakeflow_pipelines_inventory_tier -- snapshot type, single "0 AS window_days" block (no
# :period_days). Grain [workspace_id, pipeline_id] -- one row per pipeline, so a tag filter
# reaches it by the pipeline's own tag. No status column, no usage_sum assertion.
# -------------------------------------------------------------------------------------------
def _pipelines_inventory_from_parquet() -> dict[str, dict]:
    """Re-derive lakeflow_pipelines_inventory_tier's own latest-row-per-(workspace_id,pipeline_id)
    + delete_time IS NULL logic directly against the raw lakeflow__pipelines parquet -- never
    trusting the built model's own arithmetic, never hard-coding a pipeline count (same
    "re-derive against the raw parquet" precedent test_lakeflow_run_timeline.py's docstring names
    for this batch's inventory ids). QUALIFY runs inside the CTE with no WHERE before it (the exact
    shape the real query and every other id in this batch use) -- delete_time IS NULL is applied
    only in the outer SELECT, never as a QUALIFY-partition-column WHERE filter. Returns
    {pipeline_id: row} for workspace WS1 only."""
    glob = (PARQUET_DIR / "lakeflow__pipelines" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"""
            WITH latest AS (
                SELECT workspace_id, pipeline_id, name AS pipeline_name, pipeline_type, delete_time,
                       settings.serverless AS setting_serverless,
                       settings.development AS setting_development,
                       settings.continuous AS setting_continuous,
                       settings.photon AS setting_photon,
                       settings.edition AS setting_edition,
                       settings.channel AS setting_channel
                FROM read_parquet('{glob}', union_by_name=true)
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC
                ) = 1
            )
            SELECT workspace_id, pipeline_id, pipeline_name, pipeline_type,
                   setting_serverless, setting_development, setting_continuous, setting_photon,
                   setting_edition, setting_channel
            FROM latest
            WHERE delete_time IS NULL AND workspace_id = '{WS1}'
        """
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return {r[1]: dict(zip(cols, r)) for r in cur.fetchall()}
    finally:
        con.close()


def test_lakeflow_pipelines_inventory_tier():
    out = dbutil.rows("lakeflow_pipelines_inventory_tier", 0, workspace_ids=[WS1])
    assert all(r["window_days"] == 0 for r in out), "snapshot id: window_days must always be 0"
    assert "status" not in out[0], "lakeflow_pipelines_inventory_tier has no status column"

    # Converse of the four windowed ids' `rows(id, 0) == []`: this is the ONE snapshot id in the
    # batch, so it must be empty at every real window and carry all its rows at window_days=0.
    for w in WINDOWS:
        assert dbutil.rows("lakeflow_pipelines_inventory_tier", w) == [], w

    grains = _grains()
    _assert_grain_unique(out, grains["lakeflow_pipelines_inventory_tier"])

    # >= 2 distinct (pipeline_type, setting_*) combinations, per the fixture facts ("pipeline
    # tiers (>= 2 distinct pipeline_type/settings combinations)") -- now read off individual
    # pipeline rows instead of a pre-grouped count.
    combos = {
        (r["pipeline_type"], r["setting_serverless"], r["setting_development"],
         r["setting_continuous"], r["setting_edition"])
        for r in out
    }
    assert len(combos) >= 2, combos

    # Full re-derivation against the raw parquet, one row per pipeline_id.
    derived_by_id = _pipelines_inventory_from_parquet()
    out_by_id = {r["pipeline_id"]: r for r in out}
    assert set(out_by_id) == set(derived_by_id), (set(out_by_id), set(derived_by_id))
    for pid, exp in derived_by_id.items():
        row = out_by_id[pid]
        for col in ("pipeline_name", "pipeline_type", "setting_serverless", "setting_development",
                    "setting_continuous", "setting_photon", "setting_edition", "setting_channel"):
            assert row[col] == exp[col], (pid, col)

    # lf_pl_deleted (delete_time set) must not appear -- excluded by the WHERE delete_time IS NULL
    # filter in both the model and this re-derivation.
    assert "lf_pl_deleted" not in out_by_id, out_by_id
    assert _raw_row_exists("lakeflow__pipelines", "pipeline_id = 'lf_pl_deleted'")


# -------------------------------------------------------------------------------------------
# lakeflow_succeeded_with_failed_tasks -- warn_succeeded_with_failed=3,
# crit_succeeded_with_failed=10. Grain [workspace_id, job_id]. order_by:
# succeeded_runs_with_failed_task DESC.
# -------------------------------------------------------------------------------------------
def test_lakeflow_succeeded_with_failed_tasks():
    out = dbutil.rows("lakeflow_succeeded_with_failed_tasks", 30, workspace_ids=[WS1])
    by_job = {r["job_id"]: r for r in out
              if r["job_id"] in ("lf_job_swf_crit", "lf_job_swf_warn", "lf_job_swf_ok")}

    crit = by_job["lf_job_swf_crit"]
    assert crit["status"] == "CRITICAL"
    assert crit["succeeded_runs"] == 12
    assert crit["succeeded_runs_with_failed_task"] == 12

    warn = by_job["lf_job_swf_warn"]
    assert warn["status"] == "WARN"
    assert warn["succeeded_runs_with_failed_task"] == 5

    ok = by_job["lf_job_swf_ok"]
    assert ok["status"] == "OK"
    assert ok["succeeded_runs"] == 3
    assert ok["succeeded_runs_with_failed_task"] == 1, "exactly one of lf_job_swf_ok's 3 succeeded runs has a failed task"

    # AS_OF-day exclusion: lf_job_swf_asof's one SUCCEEDED run has period_end_time on D0 (today),
    # excluded from job_end by period_end_time < date_trunc('DAY', current_timestamp()) -- the job
    # never appears in this id's output even though it also carries a FAILED task. Anchored first,
    # same reasoning as test_lakeflow_pipeline_cost above.
    assert _raw_row_exists("lakeflow__job_run_timeline", "job_id = 'lf_job_swf_asof'")
    assert "lf_job_swf_asof" not in {r["job_id"] for r in out}
    for w in WINDOWS:
        assert "lf_job_swf_asof" not in {r["job_id"] for r in dbutil.rows("lakeflow_succeeded_with_failed_tasks", w, workspace_ids=[WS1])}

    # worst-first: checked over the WHOLE physical row order dbutil.rows() returned, not a filtered
    # subset -- see test_lakeflow_pipeline_cost's comment on why a filtered subsequence can
    # coincidentally look sorted even without the model's ORDER BY. Verified in-memory: WITH
    # ORDER BY this workspace's full succeeded_runs_with_failed_task sequence is sorted descending;
    # WITHOUT it, it is not.
    seq = [r["succeeded_runs_with_failed_task"] for r in out]
    assert seq == sorted(seq, reverse=True), seq

    assert dbutil.rows("lakeflow_succeeded_with_failed_tasks", 0) == []
