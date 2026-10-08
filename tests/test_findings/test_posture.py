"""tests/test_findings/test_posture.py -- P3-POSTURE.

Proves, against tests/fixtures/posture.py's own `ps_`-prefixed rows and only those rows, that the
two app-owned config-posture findings (app/queries/app/compute/) have the right grain, the right
snapshot (window_days=0 only, since neither query declares `:period_days`) shape, the SCD2
"latest row per key, delete_time IS NULL" dedupe, every reachable status band, the worst-flag-wins
status rule, the exact `reasons` text for each fired flag (including that a flag which does not
apply to a cluster's source, or has not fired, never appears in it), and the scope rule that
auto-termination is judged only on all-purpose (UI/API) clusters. Expectations are read directly
off tests/fixtures/posture.py's own `build()` (imported as `ps`) and its module docstring, never a
private constant re-derived independently -- there is no cost rollup or window arithmetic here to
independently re-derive, unlike the windowed compute queries.

Both `classic_clusters_config_current` / `sql_warehouse_config_current` also read
system.compute.clusters / system.compute.warehouses (T-15), so this file's rows sit alongside
theirs in the raw fixture tables; every assertion below filters to this builder's own `ps_`-
prefixed ids by dict lookup, so it can never be affected by (or accidentally assert on) another
builder's rows -- the same convention tests/test_findings/test_compute_snapshots.py documents for
its own `cp_`-prefixed rows sharing the same two tables with `lakeflow.py` / `drilldown.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import posture as ps  # noqa: E402

STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

CLUSTER_ID = "compute_cluster_config_posture"
WAREHOUSE_ID = "compute_warehouse_config_posture"


def _raw_row_count(schema_table, where):
    import duckdb

    parquet_dir = TESTS_DIR / "fixtures" / "parquet"
    pattern = (parquet_dir / schema_table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT COUNT(*) FROM read_parquet('{pattern}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def _cluster_by_id():
    return {r["cluster_id"]: r for r in dbutil.rows(CLUSTER_ID, 0)}


def _warehouse_by_id():
    return {r["warehouse_id"]: r for r in dbutil.rows(WAREHOUSE_ID, 0)}


# -------------------------------------------------------------------------------------------
# Snapshot shape: neither query declares :period_days, so window_days=0 is the only non-empty
# read (the generator emits a single "0 AS window_days" block for a snapshot-type query, DEC-24).
# -------------------------------------------------------------------------------------------
def test_snapshot_ids_live_only_at_window_zero():
    for qid in (CLUSTER_ID, WAREHOUSE_ID):
        assert dbutil.rows(qid, 0) != []
        for w in (7, 30, 90):
            assert dbutil.rows(qid, w) == [], f"{qid} at window {w}"


def test_status_enum():
    for qid in (CLUSTER_ID, WAREHOUSE_ID):
        for r in dbutil.rows(qid, 0):
            assert r["status"] in STATUS_VALUES, (qid, r)


# -------------------------------------------------------------------------------------------
# compute_cluster_config_posture -- grain [cluster_id].
# -------------------------------------------------------------------------------------------
def test_cluster_posture_critical_isolation_bypass():
    by_id = _cluster_by_id()
    row = by_id["ps_cl_critical"]
    assert row["status"] == "CRITICAL"
    assert row["flag_no_isolation"] is True
    assert row["flag_eol_runtime"] is False
    assert row["flag_no_policy"] is False
    assert row["flag_auto_term_risk"] is False
    assert row["reasons"] == "access mode NONE bypasses Unity Catalog (no user isolation)"
    # mask_user() is off by default, so owned_by passes through raw.
    assert row["owned_by"] == ps.OWNER_EMAIL


def test_cluster_posture_warn_multi_flag():
    by_id = _cluster_by_id()
    row = by_id["ps_cl_warn_multi"]
    assert row["status"] == "WARN"
    assert row["flag_no_isolation"] is False
    assert row["flag_eol_runtime"] is True
    assert row["flag_no_policy"] is True
    assert row["flag_auto_term_risk"] is True
    assert row["reasons"] == (
        "runtime 11.3.x-scala2.12 is past Databricks end-of-support; "
        "no cluster policy attached; auto-termination is off"
    )
    # owned_by is a service-principal GUID -> passed through unchanged (RLIKE branch).
    assert row["owned_by"] == ps.SP_GUID


def test_cluster_posture_warn_autoterm_only():
    by_id = _cluster_by_id()
    row = by_id["ps_cl_warn_autoterm"]
    assert row["status"] == "WARN"
    assert row["flag_auto_term_risk"] is True
    assert row["flag_eol_runtime"] is False
    assert row["flag_no_policy"] is False
    assert row["flag_no_isolation"] is False
    assert row["reasons"] == "auto-termination 500 min, above the 120 min limit"


def test_cluster_posture_ok():
    by_id = _cluster_by_id()
    row = by_id["ps_cl_ok"]
    assert row["status"] == "OK"
    assert row["flag_no_isolation"] is False
    assert row["flag_eol_runtime"] is False
    assert row["flag_no_policy"] is False
    assert row["flag_auto_term_risk"] is False
    assert row["reasons"] in (None, "")
    # owned_by IS NULL -> passed through unchanged (NULL branch).
    assert row["owned_by"] is None


def test_cluster_posture_job_cluster_skips_auto_term_flag():
    """A JOB (ephemeral) cluster with policy_id NULL AND auto_termination_minutes NULL: the
    no-policy flag applies to every cluster source and must fire; the auto-termination flag is
    all-purpose-only (cluster_source IN ('UI','API')) and must NOT fire even though the value
    itself would trip it on an all-purpose cluster."""
    by_id = _cluster_by_id()
    row = by_id["ps_cl_job_nopolicy"]
    assert row["is_all_purpose"] is False
    assert row["status"] == "WARN"
    assert row["flag_no_policy"] is True
    assert row["flag_auto_term_risk"] is False
    assert row["reasons"] == "no cluster policy attached"
    assert "auto-termination" not in row["reasons"]


def test_cluster_posture_pipeline_cluster_eol_only():
    """A PIPELINE cluster on an end-of-support runtime, with a policy attached and
    auto_termination_minutes NULL: eol_runtime applies to every cluster source and fires;
    no-policy does not (policy_id is set); auto-termination does not (not all-purpose)."""
    by_id = _cluster_by_id()
    row = by_id["ps_cl_pipeline_eol"]
    assert row["is_all_purpose"] is False
    assert row["status"] == "WARN"
    assert row["flag_eol_runtime"] is True
    assert row["flag_no_policy"] is False
    assert row["flag_auto_term_risk"] is False
    assert row["reasons"] == "runtime 9.1.x-scala2.12 is past Databricks end-of-support"


def test_cluster_posture_not_assessed_mode_unknown():
    """data_security_mode NULL must not fall through to OK, and must not fall through to a
    silently-assumed-safe False either: `data_security_mode = 'NONE' OR data_security_mode LIKE
    'LEGACY_%'` is itself NULL (SQL three-valued logic) when data_security_mode is NULL, so
    flag_no_isolation reads NULL/None -- the query header's own caveat calls this out verbatim
    ("NULL is never read as OK: it is left out of flag_no_isolation (unknown, not assumed safe or
    assumed unsafe)"), the same DEC-57/58 honesty rule applied to a flag column instead of just
    `status`. A cluster whose isolation could not even be checked is not a pass either way --
    status reads NOT_ASSESSED with a plain-words reason."""
    by_id = _cluster_by_id()
    row = by_id["ps_cl_mode_unknown"]
    assert row["status"] == "NOT_ASSESSED"
    assert row["flag_no_isolation"] is None
    assert row["flag_eol_runtime"] is False
    assert row["flag_no_policy"] is False
    assert row["flag_auto_term_risk"] is False
    assert row["reasons"] == "access mode not recorded, so Unity Catalog isolation could not be checked"


def test_cluster_posture_critical_legacy_single_user_standard():
    """LEGACY_SINGLE_USER_STANDARD is a real Clusters API value not in an enumerated LEGACY_*
    list -- proves flag_no_isolation matches on the LEGACY_ prefix, not named values."""
    by_id = _cluster_by_id()
    row = by_id["ps_cl_legacy_std"]
    assert row["status"] == "CRITICAL"
    assert row["flag_no_isolation"] is True
    assert row["reasons"] == (
        "access mode LEGACY_SINGLE_USER_STANDARD bypasses Unity Catalog (no user isolation)"
    )


def test_cluster_posture_eol_old_runtime():
    """A runtime well below the 14.3 LTS line (7.3.x) must be flagged -- the old five-value LTS
    list missed everything not named on it."""
    by_id = _cluster_by_id()
    row = by_id["ps_cl_eol_old"]
    assert row["status"] == "WARN"
    assert row["flag_eol_runtime"] is True
    assert row["reasons"] == "runtime 7.3.x-scala2.12 is past Databricks end-of-support"


def test_cluster_posture_eol_nonlts_runtime():
    """A non-LTS line (15.2.x, between LTS releases) must also be flagged -- the old rule only
    caught the five named LTS releases, not the non-LTS lines between them."""
    by_id = _cluster_by_id()
    row = by_id["ps_cl_eol_nonlts"]
    assert row["status"] == "WARN"
    assert row["flag_eol_runtime"] is True
    assert row["reasons"] == "runtime 15.2.x-scala2.12 is past Databricks end-of-support"


def test_cluster_posture_deleted_excluded():
    assert _raw_row_count("compute__clusters", "cluster_id = 'ps_cl_deleted'") == 2
    assert (
        _raw_row_count(
            "compute__clusters", "cluster_id = 'ps_cl_deleted' AND delete_time IS NOT NULL"
        )
        == 1
    )
    assert "ps_cl_deleted" not in _cluster_by_id()


def test_cluster_posture_scd2_reads_latest_row():
    assert _raw_row_count("compute__clusters", "cluster_id = 'ps_cl_scd2'") == 2
    by_id = _cluster_by_id()
    assert sum(1 for r in dbutil.rows(CLUSTER_ID, 0) if r["cluster_id"] == "ps_cl_scd2") == 1
    row = by_id["ps_cl_scd2"]
    # the OLD row (auto_termination=20, no flag) must not win -- the NEW row turned it off.
    assert row["auto_termination_minutes"] is None
    assert row["status"] == "WARN"
    assert row["flag_auto_term_risk"] is True
    assert row["reasons"] == "auto-termination is off"


def test_cluster_posture_order_by_worst_first_then_id():
    out = dbutil.rows(CLUSTER_ID, 0)
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    ranks = [rank[r["status"]] for r in out]
    assert ranks == sorted(ranks), "not worst-first"
    # within a status band, cluster_id is ascending (the whole returned sequence, any builder).
    same_band = {}
    for r in out:
        same_band.setdefault(r["status"], []).append(r["cluster_id"])
    for ids in same_band.values():
        assert ids == sorted(ids), ids


# -------------------------------------------------------------------------------------------
# compute_warehouse_config_posture -- grain [warehouse_id].
# -------------------------------------------------------------------------------------------
def test_warehouse_posture_ok():
    by_id = _warehouse_by_id()
    row = by_id["ps_wh_ok"]
    assert row["status"] == "OK"
    assert row["flag_autostop_risk"] is False
    assert row["flag_classic_type"] is False
    assert row["flag_preview_channel"] is False
    assert row["reasons"] in (None, "")
    assert row["created_by"] is None


def test_warehouse_posture_warn_multi_flag():
    by_id = _warehouse_by_id()
    row = by_id["ps_wh_warn_multi"]
    assert row["status"] == "WARN"
    assert row["flag_autostop_risk"] is True
    assert row["flag_classic_type"] is True
    assert row["flag_preview_channel"] is True
    assert row["reasons"] == (
        "auto-stop is off; CLASSIC type; Pro/Serverless offers faster start-up and Unity "
        "Catalog-native governance; on the Preview channel (pre-release features, confirm this "
        "is intentional)"
    )
    assert row["created_by"] == ps.SP_GUID


def test_warehouse_posture_warn_autostop_only():
    by_id = _warehouse_by_id()
    row = by_id["ps_wh_warn_autostop"]
    assert row["status"] == "WARN"
    assert row["flag_autostop_risk"] is True
    assert row["flag_classic_type"] is False
    assert row["flag_preview_channel"] is False
    assert row["reasons"] == "auto-stop 120 min, above the 60 min limit"
    # mask_user() is off by default, so created_by passes through raw.
    assert row["created_by"] == ps.CREATOR_EMAIL


def test_warehouse_posture_warn_classic_only():
    by_id = _warehouse_by_id()
    row = by_id["ps_wh_warn_classic"]
    assert row["status"] == "WARN"
    assert row["flag_classic_type"] is True
    assert row["flag_autostop_risk"] is False
    assert row["flag_preview_channel"] is False
    assert row["reasons"] == (
        "CLASSIC type; Pro/Serverless offers faster start-up and Unity Catalog-native governance"
    )


def test_warehouse_posture_warn_preview_only():
    by_id = _warehouse_by_id()
    row = by_id["ps_wh_warn_preview"]
    assert row["status"] == "WARN"
    assert row["flag_preview_channel"] is True
    assert row["flag_autostop_risk"] is False
    assert row["flag_classic_type"] is False
    assert row["reasons"] == "on the Preview channel (pre-release features, confirm this is intentional)"


def test_warehouse_posture_warn_preview_short_spelling():
    """warehouse_channel stored as the bare 'PREVIEW' (not 'CHANNEL_NAME_PREVIEW') must still
    fire -- the stored spelling is unconfirmed on a live account, so both are matched."""
    by_id = _warehouse_by_id()
    row = by_id["ps_wh_warn_preview_short"]
    assert row["status"] == "WARN"
    assert row["flag_preview_channel"] is True
    assert row["flag_autostop_risk"] is False
    assert row["flag_classic_type"] is False
    assert row["reasons"] == "on the Preview channel (pre-release features, confirm this is intentional)"


def test_warehouse_posture_deleted_excluded():
    assert _raw_row_count("compute__warehouses", "warehouse_id = 'ps_wh_deleted'") == 2
    assert (
        _raw_row_count(
            "compute__warehouses", "warehouse_id = 'ps_wh_deleted' AND delete_time IS NOT NULL"
        )
        == 1
    )
    assert "ps_wh_deleted" not in _warehouse_by_id()


def test_warehouse_posture_scd2_reads_latest_row():
    assert _raw_row_count("compute__warehouses", "warehouse_id = 'ps_wh_scd2'") == 2
    by_id = _warehouse_by_id()
    assert sum(1 for r in dbutil.rows(WAREHOUSE_ID, 0) if r["warehouse_id"] == "ps_wh_scd2") == 1
    row = by_id["ps_wh_scd2"]
    assert row["auto_stop_minutes"] is None
    assert row["status"] == "WARN"
    assert row["flag_autostop_risk"] is True
    assert row["reasons"] == "auto-stop is off"


def test_warehouse_posture_order_by_worst_first_then_id():
    out = dbutil.rows(WAREHOUSE_ID, 0)
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    ranks = [rank[r["status"]] for r in out]
    assert ranks == sorted(ranks), "not worst-first"
    same_band = {}
    for r in out:
        same_band.setdefault(r["status"], []).append(r["warehouse_id"])
    for ids in same_band.values():
        assert ids == sorted(ids), ids
