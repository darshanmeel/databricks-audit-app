"""tests/test_findings/test_lakeflow_job_run_changes.py -- T11.

Proves, against tests/fixtures/cases_jobs.py's `b5_job_changed` (workspace 7705, DEC-15), that
findings.f_lakeflow_job_run_changes (grain [workspace_id, job_id, attribute]) has the right grain,
a reachable row for a job whose latest run failed, one row per attribute with baseline_run_id/
latest_run_id, and changed=true only where the two runs' own compute shape genuinely differs --
never fabricated for the two attributes (upstream_tables, input_bytes) this fixture has no lineage
data for.

Every expectation below is computed by hand from tests/fixtures/cases_jobs.py's own docstring and
build() -- never read back from the model under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402

QID = "lakeflow_job_run_changes"
GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_job_run_changes.yml"
WS = "7705"
WINDOWS = (30, 90)


def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _rows(window_days: int) -> dict[str, dict]:
    out = dbutil.rows(QID, window_days, workspace_ids=[WS])
    return {r["attribute"]: r for r in out if r["job_id"] == "b5_job_changed"}


def test_grain():
    grains = _grains()
    assert grains[QID] == ["workspace_id", "job_id", "attribute"]


def test_window_days_zero_is_empty():
    assert dbutil.rows(QID, 0) == []


def test_grain_uniqueness_and_run_ids():
    for w in WINDOWS:
        out = dbutil.rows(QID, w, workspace_ids=[WS])
        keys = [(r["workspace_id"], r["job_id"], r["attribute"]) for r in out]
        assert len(keys) == len(set(keys)), "duplicate grain rows"
        for r in out:
            if r["job_id"] != "b5_job_changed":
                continue
            assert r["baseline_run_id"] == "b5_run_changed_base"
            assert r["latest_run_id"] == "b5_run_changed_latest"


# =====================================================================================================
# b5_job_changed: baseline run (SUCCEEDED, D(5), cluster b5_cluster_old: dbr 13.3.x, m5.xlarge, 2
# fixed workers) vs latest run (FAILED, D(1), cluster b5_cluster_new: dbr 14.3.x, m5.2xlarge, 4
# fixed workers). The latest run's own failure makes this job a candidate on its own (no p90
# needed); the baseline run is the only SUCCEEDED run before it.
# =====================================================================================================
def test_compute_shape_changed():
    for w in WINDOWS:
        rows = _rows(w)
        assert set(rows) == {
            "job_definition", "runtime_version", "worker_node_type", "worker_count",
            "upstream_tables", "input_bytes", "queue_s", "setup_s", "run_s",
        }

        rt = rows["runtime_version"]
        assert rt["baseline_value"] == "13.3.x-scala2.12"
        assert rt["latest_value"] == "14.3.x-scala2.12"
        assert rt["changed"] is True

        wnt = rows["worker_node_type"]
        assert wnt["baseline_value"] == "m5.xlarge"
        assert wnt["latest_value"] == "m5.2xlarge"
        assert wnt["changed"] is True

        wc = rows["worker_count"]
        assert wc["baseline_value"] == "2"
        assert wc["latest_value"] == "4"
        assert wc["changed"] is True

        # the one job version was in effect for both runs (a single, old job row) -> unchanged.
        jd = rows["job_definition"]
        assert jd["baseline_value"] == jd["latest_value"]
        assert jd["changed"] is False


# =====================================================================================================
# No system.access.table_lineage or system.storage.table_metrics_history rows exist for this job ->
# upstream_tables/input_bytes both read NULL on both sides, never a fabricated "no change".
# =====================================================================================================
def test_no_lineage_reads_null_not_fabricated():
    for w in WINDOWS:
        rows = _rows(w)
        for attr in ("upstream_tables", "input_bytes"):
            r = rows[attr]
            assert r["baseline_value"] is None, (attr, r)
            assert r["latest_value"] is None, (attr, r)
            assert r["changed"] is False, (attr, r)


# =====================================================================================================
# queue_s/setup_s/run_s: latest_value is the latest run's own reading; baseline_value is the median
# of the job's last 5 SUCCEEDED runs before the latest one (here, just the one baseline run) --
# queue 30s -> 300s, setup 60s -> 600s, run 1200s -> 6000s (run_duration_seconds itself, not the
# 10-minute period row it sits in), all changed.
# =====================================================================================================
def test_duration_attributes():
    for w in WINDOWS:
        rows = _rows(w)
        expected = {"queue_s": (30.0, 300.0), "setup_s": (60.0, 600.0), "run_s": (1200.0, 6000.0)}
        for attr, (base, latest) in expected.items():
            r = rows[attr]
            assert float(r["baseline_value"]) == base, (attr, r)
            assert float(r["latest_value"]) == latest, (attr, r)
            assert r["changed"] is True, (attr, r)


def test_changed_rows_sort_first():
    for w in WINDOWS:
        out = [r for r in dbutil.rows(QID, w, workspace_ids=[WS]) if r["job_id"] == "b5_job_changed"]
        changed_flags = [bool(r["changed"]) for r in out]
        first_false = next((i for i, c in enumerate(changed_flags) if not c), len(changed_flags))
        assert all(changed_flags[:first_false]), "a changed=false row sorts before a changed=true one"


# =====================================================================================================
# b5_job_never_succeeded: two FAILED runs, no SUCCEEDED run anywhere in the window -> baseline_pick
# finds nothing, so the candidate gets the single no_success_in_window marker row (every column
# NULL but latest_run_id) instead of a baseline/latest comparison.
# =====================================================================================================
def test_no_success_in_window_marker_row():
    for w in WINDOWS:
        out = [r for r in dbutil.rows(QID, w, workspace_ids=[WS]) if r["job_id"] == "b5_job_never_succeeded"]
        assert len(out) == 1, out
        r = out[0]
        assert r["baseline_kind"] == "no_success_in_window"
        assert r["baseline_run_id"] is None
        assert r["latest_run_id"] == "b5_run_never_succeeded_2"
        assert r["attribute"] is None
        assert r["baseline_value"] is None
        assert r["latest_value"] is None
        assert r["changed"] is None
