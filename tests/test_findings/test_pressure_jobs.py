"""tests/test_findings/test_pressure_jobs.py -- T-65 (DEC-65) assertions for
findings.f_lakeflow_job_compute_pressure.

Reads the dbt-built table through tests/dbutil.py at windows 7 / 30 / 90 for workspace 9001 and
filters it to this builder's own jobs (tests/fixtures/pressure_jobs.py, prefix `pr_`) plus the
drill-down jobs `dd_tcu_J1`, `dd_tcu_J2`, `dd_tcu_J9` (tests/fixtures/drilldown.py), which the
same workspace makes test material for the new query too (DEC-65 rule 8).

Every expected number is derived, never remembered: the hardware profile of a job is recomputed
here from the RAW fixture parquet (task windows from job_task_run_timeline, node-minutes from
node_timeline, deduplicated per (run, cluster, instance, minute) in Python -- an independent path,
not the model's SQL), scenario facts (worker counts, node types, cluster ids) come from the
builder module's own constants, and every threshold is resolved the way dbt/macros/param.sql does
(config/thresholds.yml [query][name], then ['_all'][name], then the header default). The enum
verdicts themselves are the ones tasks/T-65-compute-pressure-verdicts.md lists per scenario.

The ceiling is re-derived the same independent way (review round 1): per (run, cluster), the most
worker instances running in one minute, that pair's own worker CPU p50, and the cluster's newest
SCD2 configuration -- a pair counts toward at_ceiling only when it was CPU-bound AND at a positive
ceiling, and the reference pair quoted by pressure_reason is the one that set at_ceiling, else a
CPU-bound one, else any, the most workers running at once first (review round 2). The DRIVER rule
is re-derived on the drivers of (run, cluster) pairs that had workers only (review round 2).
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT / "tests"
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
sys.path.insert(0, str(ROOT / "tools"))
import dbutil  # noqa: E402
import header_schema  # noqa: E402
import pressure_jobs as pj  # noqa: E402

QUERY_ID = "lakeflow_job_compute_pressure"
SQL_PATH = ROOT / "app" / "queries" / "app" / "jobs_pipelines" / f"{QUERY_ID}.sql"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WS = "9001"
AUDIT_TODAY = datetime(2026, 9, 21)            # dbt test target: audit_today()
AUDIT_NOW = datetime(2026, 9, 21, 12, 0, 0)    # dbt test target: audit_now()
DD_JOBS = ("dd_tcu_J1", "dd_tcu_J2", "dd_tcu_J9")
RANK = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}

COLUMNS = [
    "workspace_id", "job_id", "job_name", "runs_seen", "runs_with_telemetry", "task_keys_seen",
    "task_hours_total", "last_run_start", "clusters_seen", "max_clusters_per_run",
    "on_all_purpose", "latest_cluster_id", "cluster_source", "worker_node_type",
    "driver_node_type", "worker_memory_gb", "worker_cores", "workers_configured_max",
    "worker_nodes_seen_max", "at_ceiling", "single_node", "node_minutes", "worker_minutes",
    "worker_cpu_avg_pct", "worker_cpu_p50_pct", "worker_cpu_p90_pct", "worker_cpu_spread_pct",
    "worker_cpu_wait_avg_pct", "worker_mem_avg_pct", "worker_mem_p90_pct", "worker_mem_peak_pct",
    "worker_swap_p90_pct", "worker_swap_peak_pct", "driver_cpu_avg_pct", "driver_mem_p90_pct",
    "network_received_gb", "hottest_task_key", "hottest_task_mem_p90_pct", "pressure",
    "scaling_hint", "pressure_reason", "not_assessed_reason", "status",
]
PRESSURES = {"MEMORY", "SKEW", "CPU", "IO_WAIT", "DRIVER", "IDLE", "NONE"}
HINTS = {"SCALE_UP_MEMORY", "SCALE_OUT", "NONE", "FIX_SKEW", "DISTRIBUTE_WORK", "FIX_IO", "SCALE_DOWN"}
REASONS = {"task_not_executed", "no_cluster_recorded", "no_node_timeline_rows", "too_few_slices"}
ALL_PURPOSE_CLAUSE = " -- on an all-purpose cluster shared with other work"


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _rows(window_days: int) -> list[dict]:
    return dbutil.rows("lakeflow_job_compute_pressure", window_days, workspace_ids=["9001"])


def _own(window_days: int) -> list[dict]:
    """This file's rows in their physical (worst-first) order: pr_ jobs + the dd_tcu_ jobs."""
    return [r for r in _rows(window_days) if r["job_id"].startswith("pr_J_") or r["job_id"] in DD_JOBS]


def _by_job(window_days: int) -> dict[str, dict]:
    return {r["job_id"]: r for r in _own(window_days)}


def _param(name: str):
    """dbt/macros/param.sql's lookup order: thresholds.yml [qid][name] -> ['_all'][name] -> header."""
    doc = yaml.safe_load((ROOT / "config" / "thresholds.yml").read_text(encoding="utf-8")) or {}
    for scope in (doc.get(QUERY_ID) or {}, doc.get("_all") or {}):
        if isinstance(scope, dict) and name in scope:
            return scope[name]
    hdr = header_schema.parse_header(SQL_PATH.read_text(encoding="utf-8"))
    return {p["name"]: p["default"] for p in hdr["params"]}[name]


def _whole(x: float) -> int:
    """CAST(ROUND(x, 0) AS BIGINT): half away from zero (never Python's banker's round)."""
    return int(Decimal(repr(float(x))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _quantile_cont(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    pos = (len(v) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _close(got, want, tol=0.051) -> bool:
    if want is None or got is None:
        return got is None and want is None
    return abs(float(got) - float(want)) <= tol


def _glob(table: str) -> str:
    return (PARQUET_DIR / table / "*.parquet").as_posix()


def _raw_tasks(job_id: str, window_days: int) -> list[dict]:
    """Every task run of the job starting in the window, straight from the raw parquet."""
    days = min(window_days, 90)
    con = duckdb.connect()
    try:
        cur = con.execute(
            f"""
            SELECT job_run_id, run_id AS task_run_id, task_key,
                   MIN(period_start_time) AS s, MAX(period_end_time) AS e,
                   MAX(result_state) AS st, MAX(execution_duration_seconds) AS x,
                   MAX(compute_ids[1]) AS cluster_id
            FROM read_parquet('{_glob("lakeflow__job_task_run_timeline")}', union_by_name=true)
            WHERE workspace_id = ? AND job_id = ?
              AND period_start_time >= DATE '2026-09-21' - INTERVAL {days} DAY
            GROUP BY job_run_id, run_id, task_key
            """,
            [WS, job_id],
        )
        cols = [d[0] for d in cur.description]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()
    for t in out:
        t["executed"] = not (t["st"] in ("SKIPPED", "BLOCKED") or (t["st"] is not None and t["s"] == t["e"]))
        t["end"] = AUDIT_NOW if t["st"] is None else t["e"]
        t["task_s"] = t["x"] if t["x"] else (t["end"] - t["s"]).total_seconds()
    return out


def _raw_latest_cluster(cluster_id: str) -> dict | None:
    """The newest SCD2 system.compute.clusters row of one cluster, from the raw parquet (None
    when the cluster has no configuration row at all)."""
    con = duckdb.connect()
    try:
        cur = con.execute(
            f"""
            SELECT worker_count, max_autoscale_workers
            FROM read_parquet('{_glob("compute__clusters")}', union_by_name=true)
            WHERE workspace_id = ? AND cluster_id = ?
            ORDER BY change_time DESC LIMIT 1
            """,
            [WS, cluster_id],
        )
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return None if row is None else dict(zip(cols, row))
    finally:
        con.close()


def _configured(cluster_id: str):
    """COALESCE(max_autoscale_workers, worker_count) of the newest configuration, or None."""
    latest = _raw_latest_cluster(cluster_id)
    if latest is None:
        return None
    if latest["max_autoscale_workers"] is not None:
        return latest["max_autoscale_workers"]
    return latest["worker_count"]


def _raw_slices(job_id: str, window_days: int) -> tuple[list[dict], list[tuple]]:
    """(per-task slices, NOT deduplicated; deduplicated node-minutes) for one job."""
    days = min(window_days, 90)
    lower = AUDIT_TODAY - timedelta(days=days)
    per_task: list[dict] = []
    con = duckdb.connect()
    try:
        for t in _raw_tasks(job_id, window_days):
            if not t["executed"] or t["cluster_id"] is None:
                continue
            cur = con.execute(
                f"""
                SELECT cluster_id, instance_id, start_time, driver,
                       cpu_user_percent + cpu_system_percent AS cpu, cpu_wait_percent AS wait,
                       mem_used_percent AS mem, mem_swap_percent AS swap,
                       network_received_bytes AS rx
                FROM read_parquet('{_glob("compute__node_timeline")}', union_by_name=true)
                WHERE workspace_id = ? AND cluster_id = ? AND start_time < ? AND end_time > ?
                  AND start_time >= ?
                """,
                [WS, t["cluster_id"], t["end"], t["s"], lower],
            )
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["job_run_id"] = t["job_run_id"]
                d["task_key"] = t["task_key"]
                per_task.append(d)
    finally:
        con.close()
    keys = ("job_run_id", "cluster_id", "instance_id", "start_time", "driver", "cpu", "wait", "mem", "swap", "rx")
    dedup = sorted({tuple(d[k] for k in keys) for d in per_task}, key=str)
    return per_task, [dict(zip(keys, t)) for t in dedup]


def _profile(job_id: str, window_days: int) -> dict:
    """The job's pooled profile, recomputed from the raw parquet (see module docstring)."""
    per_task, dedup = _raw_slices(job_id, window_days)
    wk = [d for d in dedup if not d["driver"]]
    dr = [d for d in dedup if d["driver"]]
    pairs: dict = {}
    minutes: dict = {}
    pair_cpu: dict = {}
    for d in dedup:
        pair = (d["job_run_id"], d["cluster_id"])
        pairs.setdefault(pair, {}).setdefault((d["instance_id"], d["driver"]), []).append(d["cpu"])
        minute = d["start_time"].replace(second=0, microsecond=0)
        bucket = minutes.setdefault(pair, {}).setdefault(minute, set())
        pair_cpu.setdefault(pair, [])
        if not d["driver"]:
            bucket.add(d["instance_id"])
            pair_cpu[pair].append(d["cpu"])
    spreads, seen, distinct = [], {}, {}
    for pair, nodes in pairs.items():
        worker_avgs = [_avg(c) for (inst, drv), c in nodes.items() if not drv]
        distinct[pair] = len(worker_avgs)
        # the most workers running in one minute -- never a count of distinct instance ids
        seen[pair] = max(len(s) for s in minutes[pair].values())
        if worker_avgs:
            spreads.append(max(worker_avgs) - min(worker_avgs))
    heat: dict = {}
    for d in per_task:
        if not d["driver"]:
            heat.setdefault(d["task_key"], []).append(d["mem"])
    return {
        "per_task_minutes": len(per_task),
        "node_minutes": len(dedup),
        "worker_minutes": len(wk),
        "worker_cpu_avg_pct": _avg([d["cpu"] for d in wk]),
        "worker_cpu_p50_pct": _quantile_cont([d["cpu"] for d in wk], 0.5),
        "worker_cpu_p90_pct": _quantile_cont([d["cpu"] for d in wk], 0.9),
        "worker_cpu_wait_avg_pct": _avg([d["wait"] for d in wk]),
        "worker_mem_avg_pct": _avg([d["mem"] for d in wk]),
        "worker_mem_p90_pct": _quantile_cont([d["mem"] for d in wk], 0.9),
        "worker_mem_peak_pct": max((d["mem"] for d in wk), default=None),
        "worker_swap_p90_pct": _quantile_cont([d["swap"] for d in wk], 0.9),
        "worker_swap_peak_pct": max((d["swap"] for d in wk), default=None),
        "driver_cpu_avg_pct": _avg([d["cpu"] for d in dr]),
        "driver_mem_p90_pct": _quantile_cont([d["mem"] for d in dr], 0.9),
        "worker_cpu_spread_pct": max(spreads) if spreads else None,
        "network_received_gb": sum(d["rx"] for d in dedup) / 1e9 if dedup else None,
        "driver_minutes": len(dr),
        # the DRIVER rule's input: drivers of (run, cluster) pairs that had workers at some minute
        "driver_cpu_avg_with_workers_pct": _avg(
            [d["cpu"] for d in dr if seen[(d["job_run_id"], d["cluster_id"])] > 0]),
        "seen": seen,
        "distinct": distinct,
        "pair_cpu_p50": {k: _quantile_cont(v, 0.5) for k, v in pair_cpu.items()},
        "task_mem_p90": {k: _quantile_cont(v, 0.9) for k, v in heat.items()},
        "worker_mem_values": [d["mem"] for d in wk],
    }


def _expected_ceiling(job_id: str, window_days: int, p: dict) -> tuple:
    """(at_ceiling, worker_nodes_seen_max, workers_configured_max), re-derived from the raw
    parquet: a pair sets the ceiling only when it is CPU-bound and at a positive configured ceiling;
    a zero-worker (single-node) or unconfigured cluster has no ceiling at all. The reference pair
    is the one that set the ceiling, else a CPU-bound one, else any -- a light cluster at its fixed
    size never stands in for the CPU-bound cluster the verdict is about (review round 2)."""
    busy = _param("busy_cpu_pct")
    run_start: dict = {}
    for t in _raw_tasks(job_id, window_days):
        run_start[t["job_run_id"]] = min(run_start.get(t["job_run_id"], t["s"]), t["s"])
    facts = []
    for (run, cluster), seen in p["seen"].items():
        cfg = _configured(cluster)
        hit = None if not cfg else seen >= cfg
        cpu = p["pair_cpu_p50"][(run, cluster)]
        cpu_bound = 1 if (cpu is not None and cpu >= busy) else 0
        sets = 1 if (hit and cpu_bound) else 0
        facts.append((sets, cpu_bound, seen, run_start[run], cluster, hit, cfg))
    at_ceiling = None if all(f[5] is None for f in facts) else any(f[0] for f in facts)
    # ORDER BY sets_ceiling DESC, cpu_bound DESC, worker_nodes_seen DESC, run_start DESC, cluster_id
    facts.sort(key=lambda f: f[4])
    facts.sort(key=lambda f: (f[0], f[1], f[2], f[3]), reverse=True)
    ref = facts[0]
    return at_ceiling, ref[2], ref[6]


def _cpu_reason_ending(configured, at_ceiling) -> str:
    """The ending of a CPU pressure_reason, keyed on the QUOTED cluster's own configuration."""
    if configured is None:
        return " -- cluster config not found"
    if configured == 0:
        return " -- no worker ceiling configured"
    return " -- at ceiling" if at_ceiling else " -- headroom"


def _scenario_nodes(job_id: str) -> dict[str, set]:
    """cluster -> distinct worker instance suffixes the builder wrote for that job."""
    out: dict[str, set] = {}
    for run in pj.JOBS[job_id]["runs"]:
        for cluster, _offset, _minutes, nodes in run["telemetry"]:
            out.setdefault(cluster, set()).update(s for s, drv, *_ in nodes if not drv)
    return out


# ---------------------------------------------------------------------------------------------
# structure: columns, grain, enums, NULL-iff rules, ordering, windows
# ---------------------------------------------------------------------------------------------
def test_columns_in_contract_order():
    out = _rows(30)
    assert out, "no rows at window_days = 30"
    assert list(out[0].keys()) == ["window_days"] + COLUMNS


def test_grain_unique_and_rows_at_every_window():
    for w in (7, 30, 90):
        out = _rows(w)
        assert out, f"window {w}: the query must return rows at every window"
        assert all(r["window_days"] == w for r in out)
        keys = [(r["workspace_id"], r["job_id"]) for r in out]
        assert len(keys) == len(set(keys)), f"window {w}: duplicate (workspace_id, job_id)"
    assert dbutil.rows("lakeflow_job_compute_pressure", 0, workspace_ids=["9001"]) == []


def test_every_enum_value_appears():
    out = _own(30)
    assert {r["pressure"] for r in out if r["pressure"] is not None} == PRESSURES
    assert {r["scaling_hint"] for r in out if r["scaling_hint"] is not None} == HINTS
    assert {r["status"] for r in out} == set(RANK)
    assert {r["not_assessed_reason"] for r in out if r["not_assessed_reason"] is not None} == REASONS


def test_null_iff_rules():
    for w in (7, 30, 90):
        for r in _rows(w):
            assert r["status"] in RANK, r
            assert (r["not_assessed_reason"] is None) == (r["status"] != "NOT_ASSESSED"), r
            assert (r["scaling_hint"] is None) == (r["pressure"] is None), r
            assert (r["pressure_reason"] is None) == (r["pressure"] is None), r
            assert (r["pressure"] is None) == (r["status"] == "NOT_ASSESSED"), r


def test_worst_first_order():
    for w in (7, 30, 90):
        out = _rows(w)
        got = [(RANK[r["status"]], -(r["task_hours_total"] or 0), r["workspace_id"], r["job_id"]) for r in out]
        assert got == sorted(got), f"window {w}: rows are not in the model's worst-first order"
        assert out[0]["status"] == "CRITICAL"


def test_window_exclusions():
    ids = {w: set(_by_job(w)) for w in (7, 30, 90)}
    assert "pr_J_win40" in ids[90] and "pr_J_win40" not in ids[30] and "pr_J_win40" not in ids[7]
    assert "pr_J_win10" in ids[90] and "pr_J_win10" in ids[30] and "pr_J_win10" not in ids[7]
    recent = {j for j in pj.JOBS if j not in ("pr_J_win40", "pr_J_win10")}
    for w in (7, 30, 90):
        assert recent <= ids[w], f"window {w}: missing {recent - ids[w]}"
    for w in (30, 90):
        r = _by_job(w)["pr_J_win10"]
        assert (r["pressure"], r["scaling_hint"], r["status"]) == ("CPU", "SCALE_OUT", "WARN"), (w, r)
        assert r["at_ceiling"] is True
        assert r["workers_configured_max"] == pj.CLUSTERS["pr_c_win10"]["worker_count"] == r["worker_nodes_seen_max"]
    r90 = _by_job(90)["pr_J_win40"]
    assert r90["runs_seen"] == 1 and r90["last_run_start"] == pj.JOBS["pr_J_win40"]["runs"][0]["start"]


# ---------------------------------------------------------------------------------------------
# every assessed pr_ job: the profile columns against the raw-parquet recomputation
# ---------------------------------------------------------------------------------------------
def test_profile_columns_match_raw_parquet():
    for w in (7, 30, 90):
        for job_id, r in _by_job(w).items():
            if not (job_id.startswith("pr_J_") or job_id in DD_JOBS) or r["node_minutes"] is None:
                continue
            p = _profile(job_id, w)
            assert r["node_minutes"] == p["node_minutes"], (w, job_id)
            assert r["worker_minutes"] == p["worker_minutes"], (w, job_id)
            for col in ("worker_cpu_avg_pct", "worker_cpu_p50_pct", "worker_cpu_p90_pct",
                        "worker_cpu_wait_avg_pct", "worker_mem_avg_pct", "worker_mem_p90_pct",
                        "worker_mem_peak_pct", "worker_swap_p90_pct", "worker_swap_peak_pct",
                        "driver_cpu_avg_pct", "driver_mem_p90_pct", "worker_cpu_spread_pct"):
                assert _close(r[col], p[col]), (w, job_id, col, r[col], p[col])
            assert _close(r["network_received_gb"], p["network_received_gb"], 0.006), (w, job_id)
            at_ceiling, seen_max, configured_max = _expected_ceiling(job_id, w, p)
            assert r["at_ceiling"] is at_ceiling, (w, job_id, r["at_ceiling"], at_ceiling)
            assert r["worker_nodes_seen_max"] == seen_max, (w, job_id)
            assert r["workers_configured_max"] == configured_max, (w, job_id)
            if r["pressure"] == "CPU" and not r["single_node"]:
                assert r["pressure_reason"].endswith(_cpu_reason_ending(configured_max, at_ceiling)), (w, job_id)
            assert r["runs_with_telemetry"] == len({k[0] for k in p["seen"]}), (w, job_id)
            assert r["single_node"] is (p["worker_minutes"] == 0 and p["driver_minutes"] > 0), (w, job_id)


# ---------------------------------------------------------------------------------------------
# the scenarios, one by one (tasks/T-65 "Jobs scenarios")
# ---------------------------------------------------------------------------------------------
def test_short_mem_node_minute_dedupe_and_hottest_task():
    job = "pr_J_short_mem"
    r = _by_job(30)[job]
    p = _profile(job, 30)
    run = pj.JOBS[job]["runs"][0]
    (cluster, _o, minutes, nodes), = run["telemetry"]
    assert (r["pressure"], r["scaling_hint"], r["status"]) == ("MEMORY", "SCALE_UP_MEMORY", "CRITICAL")
    # dedupe: 4 nodes x 45 minutes, not the per-task sum over t_main (45) and t_side (30)
    per_task_sum = sum(len(nodes) * t[3] for t in run["tasks"])
    assert r["node_minutes"] == len(nodes) * minutes == p["node_minutes"] == 180
    assert p["per_task_minutes"] == per_task_sum == 300
    assert r["node_minutes"] != per_task_sum
    assert r["worker_minutes"] == (len(nodes) - 1) * minutes >= _param("min_slices")
    mem = {m for _s, drv, _c, _w, m, _sw in nodes if not drv}
    assert len(mem) == 1 and _close(r["worker_mem_p90_pct"], mem.pop())
    # tie on memory between t_main and t_side; the longer task wins
    assert p["task_mem_p90"]["t_main"] == p["task_mem_p90"]["t_side"]
    assert r["hottest_task_key"] == "t_main" and _close(r["hottest_task_mem_p90_pct"], p["task_mem_p90"]["t_main"])
    assert r["task_keys_seen"] == len({t[1] for t in run["tasks"]}) == 2
    assert _close(r["task_hours_total"], sum(t[3] for t in run["tasks"]) / 60.0, 0.006)
    # the one node type listed in node_types: worker_memory_gb / worker_cores come from it
    spec = pj.NODE_TYPES[pj.CLUSTERS[cluster]["worker_type"]]
    assert r["latest_cluster_id"] == cluster and r["worker_node_type"] == pj.CLUSTERS[cluster]["worker_type"]
    assert r["worker_memory_gb"] == round(spec["memory_mb"] / 1024.0, 1) == 32.0
    assert r["worker_cores"] == spec["core_count"] == 8
    assert r["pressure_reason"] == (
        f"worker memory p90 {_whole(p['worker_mem_p90_pct'])}% (threshold {_param('crit_mem_pct')}%), "
        f"swap p90 {_whole(p['worker_swap_p90_pct'])}% (threshold {_param('crit_swap_pct')}%); "
        f"hottest task t_main"
    )
    # a JOB cluster: on_all_purpose is FALSE (not NULL), and the name / driver type come through
    assert pj.CLUSTERS[cluster]["source"] == "JOB" and r["on_all_purpose"] is False
    assert r["job_name"] == pj.JOBS[job]["name"]
    assert r["driver_node_type"] == pj.NT_DRV
    # under 2 h: task_cluster_utilization cannot see this job at all -- the reason this query exists
    tcu = dbutil.rows("task_cluster_utilization", 30, workspace_ids=["9001"])
    assert not [t for t in tcu if t["job_id"] == job], "task_cluster_utilization must have no row for pr_J_short_mem"


def test_node_types_only_where_listed():
    for job_id, r in _by_job(30).items():
        if not job_id.startswith("pr_J_") or r["worker_node_type"] is None:
            continue
        if r["worker_node_type"] in pj.NODE_TYPES:
            assert r["worker_memory_gb"] is not None and r["worker_cores"] is not None, job_id
        else:
            assert r["worker_memory_gb"] is None and r["worker_cores"] is None, job_id


def test_swap_rule_is_thresholded():
    r = _by_job(30)["pr_J_swap"]
    p = _profile("pr_J_swap", 30)
    assert (r["pressure"], r["scaling_hint"], r["status"]) == ("MEMORY", "SCALE_UP_MEMORY", "CRITICAL")
    assert p["worker_mem_p90_pct"] < _param("crit_mem_pct")   # memory alone would not flag it
    assert p["worker_swap_p90_pct"] >= _param("crit_swap_pct")  # the swap rule does
    assert r["pressure_reason"].startswith(
        f"worker memory p90 {_whole(p['worker_mem_p90_pct'])}% (threshold {_param('crit_mem_pct')}%), "
        f"swap p90 {_whole(p['worker_swap_p90_pct'])}% (threshold {_param('crit_swap_pct')}%)"
    )


def test_swap_noise_calibration_case_agrees_with_task_cluster_utilization():
    job = "pr_J_swap_noise"
    r = _by_job(30)[job]
    p = _profile(job, 30)
    assert 0 < p["worker_swap_p90_pct"] < _param("crit_swap_pct")
    assert (r["pressure"], r["scaling_hint"], r["status"]) == ("NONE", "NONE", "OK")
    assert r["pressure_reason"] == (
        f"no threshold crossed: CPU p50 {_whole(p['worker_cpu_p50_pct'])}%, "
        f"memory p90 {_whole(p['worker_mem_p90_pct'])}%, swap p90 {_whole(p['worker_swap_p90_pct'])}%"
    )
    # the same task seen by the vendored query, which now judges swap the same way (p90 against
    # :crit_swap_pct, same default 10) instead of flagging ANY swap above zero -- the two agree.
    task_ids = {t[0] for run in pj.JOBS[job]["runs"] for t in run["tasks"]}
    tcu = [t for t in dbutil.rows("task_cluster_utilization", 30, workspace_ids=["9001"]) if t["job_id"] == job]
    assert {t["task_run_id"] for t in tcu} == task_ids
    for t in tcu:
        assert (t["bottleneck_hint"], t["status"]) == ("MIXED", "OK"), t


def test_cpu_at_ceiling_and_with_headroom():
    for job, cluster, want in (
        ("pr_J_cpu_ceiling", "pr_c_ceiling", ("CPU", "SCALE_OUT", "WARN", True, " -- at ceiling")),
        ("pr_J_cpu_headroom", "pr_c_headroom", ("CPU", "NONE", "OK", False, " -- headroom")),
    ):
        r = _by_job(30)[job]
        p = _profile(job, 30)
        cfg = pj.CLUSTERS[cluster]
        seen = len(_scenario_nodes(job)[cluster])
        assert (r["pressure"], r["scaling_hint"], r["status"], r["at_ceiling"]) == want[:4], (job, r)
        assert r["worker_nodes_seen_max"] == seen == 4, job
        # the latest SCD2 shape (pr_c_headroom's older row said max 4 -- it must not be read)
        assert r["workers_configured_max"] == cfg["max_as"], job
        assert p["worker_cpu_p50_pct"] >= _param("busy_cpu_pct")
        assert r["pressure_reason"] == (
            f"worker CPU p50 {_whole(p['worker_cpu_p50_pct'])}% (threshold {_param('busy_cpu_pct')}%), "
            f"up to {seen} of {cfg['max_as']} workers seen{want[4]}"
        ), job
    assert pj.CLUSTERS["pr_c_headroom"]["history"][0]["max_as"] == 4  # the shape a stale read would use


def test_pooled_runs_one_row_per_job():
    job = "pr_J_pooled"
    out = [r for r in _own(30) if r["job_id"] == job]
    assert len(out) == 1
    r = out[0]
    runs = pj.JOBS[job]["runs"]
    clusters = [run["tasks"][0][4] for run in runs]
    nodes = _scenario_nodes(job)
    assert r["runs_seen"] == len(runs) == 3
    assert r["clusters_seen"] == len(set(clusters)) == 3
    assert r["max_clusters_per_run"] == 1
    assert r["worker_nodes_seen_max"] == max(len(v) for v in nodes.values()) == 4
    ceiling_cluster = max(nodes, key=lambda c: len(nodes[c]))
    assert r["workers_configured_max"] == pj.CLUSTERS[ceiling_cluster]["max_as"] == len(nodes[ceiling_cluster])
    assert r["at_ceiling"] is True  # run 2 reached its ceiling; runs 1 and 3 did not
    assert [len(nodes[c]) >= pj.CLUSTERS[c]["max_as"] for c in clusters] == [False, True, False]
    assert (r["pressure"], r["scaling_hint"], r["status"]) == ("CPU", "SCALE_OUT", "WARN")
    latest = max(runs, key=lambda run: run["start"])["tasks"][0][4]
    assert r["latest_cluster_id"] == latest == "pr_c_pool_3"
    assert r["worker_node_type"] == pj.CLUSTERS[latest]["worker_type"]
    assert r["worker_node_type"] not in {pj.CLUSTERS[c]["worker_type"] for c in clusters if c != latest}
    assert r["last_run_start"] == max(run["start"] for run in runs)


def test_multi_cluster_run_pools_both_clusters():
    job = "pr_J_multi"
    r = _by_job(30)[job]
    p = _profile(job, 30)
    assert (r["pressure"], r["scaling_hint"], r["status"]) == ("MEMORY", "SCALE_UP_MEMORY", "CRITICAL")
    assert _close(r["worker_mem_p90_pct"], p["worker_mem_p90_pct"]) and p["worker_mem_p90_pct"] >= _param("crit_mem_pct")
    assert r["hottest_task_key"] == "heavy"
    assert p["task_mem_p90"]["heavy"] > p["task_mem_p90"]["light"]
    assert r["max_clusters_per_run"] == 2 and r["clusters_seen"] == 2


def test_all_purpose_cluster_is_carried():
    r = _by_job(30)["pr_J_all_purpose"]
    assert pj.CLUSTERS["pr_c_ap"]["source"] in ("UI", "API")
    assert (r["pressure"], r["scaling_hint"], r["status"]) == ("NONE", "NONE", "OK")
    assert r["on_all_purpose"] is True and r["cluster_source"] == pj.CLUSTERS["pr_c_ap"]["source"]
    assert r["pressure_reason"].endswith(ALL_PURPOSE_CLAUSE)
    for job_id, other in _by_job(30).items():
        if other["on_all_purpose"] is not True and other["pressure_reason"] is not None:
            assert not other["pressure_reason"].endswith(ALL_PURPOSE_CLAUSE), job_id


def test_serverless_and_noconf():
    s = _by_job(30)["pr_J_serverless"]
    assert (s["pressure"], s["scaling_hint"], s["status"], s["not_assessed_reason"]) == (
        None, None, "NOT_ASSESSED", "no_cluster_recorded")
    assert s["latest_cluster_id"] is None and s["node_minutes"] is None and s["on_all_purpose"] is None

    n = _by_job(30)["pr_J_noconf"]
    p = _profile("pr_J_noconf", 30)
    assert "pr_c_noconf" not in pj.CLUSTERS
    assert (n["pressure"], n["scaling_hint"], n["status"]) == ("CPU", "NONE", "OK")
    assert n["at_ceiling"] is None and n["workers_configured_max"] is None and n["on_all_purpose"] is None
    assert n["cluster_source"] is None and n["worker_node_type"] is None
    seen = len(_scenario_nodes("pr_J_noconf")["pr_c_noconf"])
    assert n["pressure_reason"] == (
        f"worker CPU p50 {_whole(p['worker_cpu_p50_pct'])}% (threshold {_param('busy_cpu_pct')}%), "
        f"up to {seen} workers seen -- cluster config not found"
    )
    assert n["pressure_reason"].endswith("cluster config not found")


def test_non_scaling_verdicts_and_not_assessed_reasons():
    by = _by_job(30)
    want = {
        "pr_J_skew": ("SKEW", "FIX_SKEW", "WARN"),
        "pr_J_io": ("IO_WAIT", "FIX_IO", "WARN"),
        "pr_J_driver": ("DRIVER", "DISTRIBUTE_WORK", "WARN"),
        "pr_J_idle": ("IDLE", "SCALE_DOWN", "WARN"),
    }
    for job, triple in want.items():
        r = by[job]
        assert (r["pressure"], r["scaling_hint"], r["status"]) == triple, (job, r)
    p = _profile("pr_J_skew", 30)
    assert by["pr_J_skew"]["pressure_reason"] == (
        f"hottest-coldest worker CPU gap {_whole(p['worker_cpu_spread_pct'])}% in the worst run "
        f"(threshold {_param('skew_gap_pct')}%)"
    )
    p = _profile("pr_J_io", 30)
    assert by["pr_J_io"]["pressure_reason"] == (
        f"worker CPU wait {_whole(p['worker_cpu_wait_avg_pct'])}% (threshold {_param('warn_io_wait_pct')}%)"
    )
    p = _profile("pr_J_driver", 30)
    assert by["pr_J_driver"]["pressure_reason"] == (
        f"driver CPU {_whole(p['driver_cpu_avg_pct'])}% while workers averaged {_whole(p['worker_cpu_avg_pct'])}%"
    )
    p = _profile("pr_J_idle", 30)
    assert by["pr_J_idle"]["pressure_reason"] == (
        f"workers averaged {_whole(p['worker_cpu_avg_pct'])}% CPU (threshold {_param('waiting_cpu_pct')}%)"
    )
    for job, reason in (("pr_J_skipped", "task_not_executed"), ("pr_J_nonodes", "no_node_timeline_rows"),
                        ("pr_J_brief", "too_few_slices")):
        r = by[job]
        assert (r["status"], r["not_assessed_reason"], r["pressure"]) == ("NOT_ASSESSED", reason, None), (job, r)
    brief = _profile("pr_J_brief", 30)
    assert 0 < brief["worker_minutes"] < _param("min_slices") == 60
    assert by["pr_J_brief"]["worker_minutes"] == brief["worker_minutes"]


# ---------------------------------------------------------------------------------------------
# review round 1: precedence proven on rows where TWO rules hold, the ceiling, the single-node
# path, the tie-break, the classic latest cluster, and the run <-> task chain
# ---------------------------------------------------------------------------------------------
def _rules(p: dict) -> dict:
    """Which of the heuristic's rules the raw profile satisfies (workers' numbers; every test
    job here has workers)."""
    return {
        "MEMORY": p["worker_mem_p90_pct"] >= _param("crit_mem_pct")
        or (p["worker_swap_p90_pct"] or 0) >= _param("crit_swap_pct"),
        "SKEW": (p["worker_cpu_spread_pct"] or 0) >= _param("skew_gap_pct"),
        "CPU": p["worker_cpu_p50_pct"] >= _param("busy_cpu_pct"),
        "IO_WAIT": (p["worker_cpu_wait_avg_pct"] or 0) >= _param("warn_io_wait_pct"),
        "DRIVER": p["worker_cpu_avg_pct"] < _param("waiting_cpu_pct")
        and (p["driver_cpu_avg_with_workers_pct"] or 0) >= _param("busy_cpu_pct"),
        "IDLE": p["worker_cpu_avg_pct"] < _param("waiting_cpu_pct"),
    }


def test_verdict_precedence_on_rows_where_two_rules_hold():
    """MEMORY > SKEW > CPU > IO_WAIT > DRIVER > IDLE: every ADJACENT pair is proven on a job whose
    raw profile satisfies BOTH rules -- MEMORY > SKEW on dd_tcu_J1, SKEW > CPU on pr_J_skew, CPU >
    IO_WAIT on pr_J_cpu_io, IO_WAIT > DRIVER on pr_J_io (review round 2: its driver runs at 95), DRIVER
    > IDLE on pr_J_driver -- plus MEMORY > CPU and IO_WAIT > IDLE."""
    by = _by_job(30)
    cases = (
        ("pr_J_short_mem", "MEMORY", "CPU"),
        ("pr_J_skew", "SKEW", "CPU"),
        ("pr_J_cpu_io", "CPU", "IO_WAIT"),
        ("pr_J_io", "IO_WAIT", "IDLE"),
        ("pr_J_io", "IO_WAIT", "DRIVER"),
        ("pr_J_driver", "DRIVER", "IDLE"),
    )
    for job, winner, loser in cases:
        rules = _rules(_profile(job, 30))
        assert rules[winner] and rules[loser], (job, rules)
        assert by[job]["pressure"] == winner, (job, by[job]["pressure"])
    j1 = _rules(_profile("dd_tcu_J1", 30))
    assert j1["MEMORY"] and j1["SKEW"] and by["dd_tcu_J1"]["pressure"] == "MEMORY"
    cpu_io = by["pr_J_cpu_io"]
    assert (cpu_io["scaling_hint"], cpu_io["status"], cpu_io["at_ceiling"]) == ("NONE", "OK", False)


def test_zero_worker_cluster_has_no_ceiling():
    """A single-node cluster (worker_count 0) never reads '0 of 0 workers -- at ceiling', alone
    or beside an autoscale cluster that still had headroom."""
    by = _by_job(30)
    sc = by["pr_J_single_cpu"]
    p = _profile("pr_J_single_cpu", 30)
    assert pj.CLUSTERS["pr_c_single_cpu"]["worker_count"] == 0
    assert sc["single_node"] is True and sc["worker_minutes"] == 0 and p["worker_minutes"] == 0
    assert p["driver_cpu_avg_pct"] >= _param("busy_cpu_pct")
    assert (sc["pressure"], sc["scaling_hint"], sc["status"], sc["at_ceiling"]) == ("CPU", "NONE", "OK", None)
    assert sc["pressure_reason"] == (
        f"driver CPU avg {_whole(p['driver_cpu_avg_pct'])}% (threshold {_param('busy_cpu_pct')}%) "
        f"on a single-node cluster"
    )

    mx = by["pr_J_mixed_sn"]
    p = _profile("pr_J_mixed_sn", 30)
    cfg = pj.CLUSTERS["pr_c_etl"]["max_as"]
    peak = max(v for (run, cl), v in p["seen"].items() if cl == "pr_c_etl")
    assert pj.CLUSTERS["pr_c_setup_sn"]["worker_count"] == 0 and peak < cfg
    assert mx["single_node"] is False
    assert (mx["pressure"], mx["scaling_hint"], mx["status"], mx["at_ceiling"]) == ("CPU", "NONE", "OK", False)
    assert (mx["worker_nodes_seen_max"], mx["workers_configured_max"]) == (peak, cfg) == (4, 10)
    assert mx["pressure_reason"] == (
        f"worker CPU p50 {_whole(p['worker_cpu_p50_pct'])}% (threshold {_param('busy_cpu_pct')}%), "
        f"up to {peak} of {cfg} workers seen -- headroom"
    )


def test_ceiling_counts_workers_running_at_once_not_instance_ids():
    job = "pr_J_churn"
    r = _by_job(30)[job]
    p = _profile(job, 30)
    cfg = pj.CLUSTERS["pr_c_churn"]["max_as"]
    (pair,) = p["seen"]
    assert p["distinct"][pair] == len(_scenario_nodes(job)["pr_c_churn"]) == 6 > cfg
    assert p["seen"][pair] == 3 < cfg                   # never more than 3 at once
    assert r["worker_nodes_seen_max"] == 3 and r["workers_configured_max"] == cfg
    assert (r["pressure"], r["scaling_hint"], r["status"], r["at_ceiling"]) == ("CPU", "NONE", "OK", False)
    assert r["pressure_reason"].endswith(f"up to 3 of {cfg} workers seen -- headroom")


def test_only_a_cpu_bound_cluster_can_be_at_ceiling():
    """A light fixed-size cluster that reached its size is not a reason to scale out a job whose
    CPU-bound cluster had headroom; the reason quotes the cluster that decided."""
    job = "pr_J_fixlight"
    r = _by_job(30)[job]
    p = _profile(job, 30)
    light = [k for k in p["seen"] if k[1] == "pr_c_fl_light"][0]
    heavy = [k for k in p["seen"] if k[1] == "pr_c_fl_heavy"][0]
    assert p["seen"][light] == pj.CLUSTERS["pr_c_fl_light"]["worker_count"]       # at its size...
    assert p["pair_cpu_p50"][light] < _param("busy_cpu_pct")                       # ...but not busy
    assert p["pair_cpu_p50"][heavy] >= _param("busy_cpu_pct")
    assert p["seen"][heavy] < pj.CLUSTERS["pr_c_fl_heavy"]["max_as"]
    # review round 2: the light cluster ran MORE workers at once, so a reference pair chosen by size
    # alone would quote "up to 6 of 6 workers seen -- headroom" for a cluster that was not busy
    assert p["seen"][light] > p["seen"][heavy]
    assert p["worker_cpu_p50_pct"] >= _param("busy_cpu_pct")
    assert (r["pressure"], r["scaling_hint"], r["status"], r["at_ceiling"]) == ("CPU", "NONE", "OK", False)
    assert (r["worker_nodes_seen_max"], r["workers_configured_max"]) == (
        p["seen"][heavy], pj.CLUSTERS["pr_c_fl_heavy"]["max_as"])
    assert r["pressure_reason"].endswith(
        f"up to {p['seen'][heavy]} of {pj.CLUSTERS['pr_c_fl_heavy']['max_as']} workers seen -- headroom")


def test_single_node_is_judged_and_worded_on_the_driver():
    by = _by_job(30)
    s = by["pr_J_single"]
    p = _profile("pr_J_single", 30)
    assert s["single_node"] is True and s["worker_minutes"] == 0 == p["worker_minutes"]
    assert p["driver_minutes"] >= _param("min_slices")
    assert (s["pressure"], s["scaling_hint"], s["status"]) == ("MEMORY", "SCALE_UP_MEMORY", "CRITICAL")
    assert _close(s["driver_mem_p90_pct"], p["driver_mem_p90_pct"]) and s["worker_mem_p90_pct"] is None
    assert s["hottest_task_mem_p90_pct"] is None      # no worker memory: no hottest-task clause
    assert s["hottest_task_key"] is None              # ...and no task named (review round 2)
    assert s["pressure_reason"] == (
        f"driver memory p90 {_whole(p['driver_mem_p90_pct'])}% (threshold {_param('crit_mem_pct')}%), "
        f"swap p90 0% (threshold {_param('crit_swap_pct')}%)"
    )

    sw = by["pr_J_single_swap"]
    (d,) = [n for n in pj.JOBS["pr_J_single_swap"]["runs"][0]["telemetry"][0][3] if n[1]]
    assert d[4] < _param("crit_mem_pct") and d[5] >= _param("crit_swap_pct")   # only the swap rule
    assert sw["single_node"] is True and sw["worker_swap_p90_pct"] is None
    assert (sw["pressure"], sw["scaling_hint"], sw["status"]) == ("MEMORY", "SCALE_UP_MEMORY", "CRITICAL")
    assert sw["pressure_reason"] == (
        f"driver memory p90 {_whole(d[4])}% (threshold {_param('crit_mem_pct')}%), "
        f"swap p90 {_whole(d[5])}% (threshold {_param('crit_swap_pct')}%)"
    )


def test_hottest_task_tie_goes_to_the_longer_task_not_the_first_key():
    r = _by_job(30)["pr_J_tie"]
    p = _profile("pr_J_tie", 30)
    tasks = {t[1]: t[3] for t in pj.JOBS["pr_J_tie"]["runs"][0]["tasks"]}
    assert p["task_mem_p90"]["a_short"] == p["task_mem_p90"]["z_long"]
    assert tasks["z_long"] > tasks["a_short"] and "a_short" < "z_long"
    assert r["hottest_task_key"] == "z_long"


def test_latest_cluster_is_the_classic_one_not_a_warehouse_id():
    r = _by_job(30)["pr_J_whtail"]
    tasks = {t[1]: t for t in pj.JOBS["pr_J_whtail"]["runs"][0]["tasks"]}
    assert tasks["sql"][2] > tasks["etl"][2] and tasks["sql"][4] == pj.WH_ID    # the SQL task starts later
    assert r["latest_cluster_id"] == "pr_c_whtail"
    assert r["worker_node_type"] == pj.CLUSTERS["pr_c_whtail"]["worker_type"] == pj.NT_MEM32
    assert r["worker_memory_gb"] == 32.0 and r["worker_cores"] == 8
    assert r["cluster_source"] == "JOB"
    # clusters_seen / max_clusters_per_run count every recorded compute id (a header caveat)
    assert r["clusters_seen"] == 2 and r["max_clusters_per_run"] == 2


def test_run_and_task_rows_are_chained():
    """Every pr_ task run belongs to a pr_ run that has its own job_run_timeline rows, and each
    task's observed window sits inside its run's (test_drilldown_chain covers only runs >= 2 h)."""
    con = duckdb.connect()
    try:
        runs = {
            (j, r): (s, e) for j, r, s, e in con.execute(
                f"""
                SELECT job_id, run_id, MIN(period_start_time), MAX(period_end_time)
                FROM read_parquet('{_glob("lakeflow__job_run_timeline")}', union_by_name=true)
                WHERE workspace_id = ? AND job_id LIKE 'pr_%'
                GROUP BY job_id, run_id
                """, [WS]).fetchall()
        }
        tasks = con.execute(
            f"""
            SELECT job_id, job_run_id, MIN(period_start_time), MAX(period_end_time)
            FROM read_parquet('{_glob("lakeflow__job_task_run_timeline")}', union_by_name=true)
            WHERE workspace_id = ? AND job_id LIKE 'pr_%'
            GROUP BY job_id, job_run_id, run_id
            """, [WS]).fetchall()
    finally:
        con.close()
    assert {(j, r) for j, r, _s, _e in tasks} == set(runs)
    want = {(j, run["run_id"]) for j, job in pj.JOBS.items() for run in job["runs"]}
    assert set(runs) == want
    for j, r, s, e in tasks:
        rs, re_ = runs[(j, r)]
        assert rs <= s and e <= re_, (j, r, s, e, rs, re_)


# ---------------------------------------------------------------------------------------------
# review round 2: the quoted cluster, the DRIVER rule on mixed jobs, the single-node reason branches
# ---------------------------------------------------------------------------------------------
def test_cpu_reason_ending_describes_the_quoted_cluster():
    """The ending of a CPU reason is keyed on the quoted cluster's own configuration: a CPU-bound
    cluster with no configuration row is 'cluster config not found' even when another cluster of
    the job has a known ceiling (at_ceiling FALSE), and a zero-worker latest shape is 'no worker
    ceiling configured' -- never 'headroom' for a ceiling nobody knows."""
    by = _by_job(30)
    nc = by["pr_J_nc_mix"]
    p = _profile("pr_J_nc_mix", 30)
    heavy = [k for k in p["seen"] if k[1] == "pr_c_ncm_heavy"][0]
    light = [k for k in p["seen"] if k[1] == "pr_c_ncm_light"][0]
    assert "pr_c_ncm_heavy" not in pj.CLUSTERS and _configured("pr_c_ncm_heavy") is None
    assert p["pair_cpu_p50"][heavy] >= _param("busy_cpu_pct") > p["pair_cpu_p50"][light]
    assert p["seen"][light] < _configured("pr_c_ncm_light")          # the light one had headroom
    assert (nc["pressure"], nc["scaling_hint"], nc["status"], nc["at_ceiling"]) == ("CPU", "NONE", "OK", False)
    assert (nc["worker_nodes_seen_max"], nc["workers_configured_max"]) == (p["seen"][heavy], None)
    assert nc["pressure_reason"] == (
        f"worker CPU p50 {_whole(p['worker_cpu_p50_pct'])}% (threshold {_param('busy_cpu_pct')}%), "
        f"up to {p['seen'][heavy]} workers seen -- cluster config not found"
    )

    rs = by["pr_J_resized"]
    p = _profile("pr_J_resized", 30)
    (pair,) = p["seen"]
    assert pj.CLUSTERS["pr_c_resized"]["history"][0]["worker_count"] == 2          # an older shape...
    assert _configured("pr_c_resized") == pj.CLUSTERS["pr_c_resized"]["worker_count"] == 0  # ...the latest
    assert rs["single_node"] is False and p["worker_minutes"] > 0
    assert (rs["pressure"], rs["scaling_hint"], rs["status"], rs["at_ceiling"]) == ("CPU", "NONE", "OK", None)
    assert (rs["worker_nodes_seen_max"], rs["workers_configured_max"]) == (p["seen"][pair], 0)
    assert rs["pressure_reason"] == (
        f"worker CPU p50 {_whole(p['worker_cpu_p50_pct'])}% (threshold {_param('busy_cpu_pct')}%), "
        f"up to {p['seen'][pair]} workers seen -- no worker ceiling configured"
    )


def test_driver_rule_ignores_a_single_node_clusters_own_node():
    """pr_J_mixed_idle: the job's POOLED driver CPU crosses :busy_cpu_pct only because its
    single-node setup cluster's only node was busy with its own work; the one driver of a cluster
    with workers idled at 20. So the job is IDLE (its workers waited), not DRIVER."""
    r = _by_job(30)["pr_J_mixed_idle"]
    p = _profile("pr_J_mixed_idle", 30)
    busy, waiting = _param("busy_cpu_pct"), _param("waiting_cpu_pct")
    assert pj.CLUSTERS["pr_c_mi_setup"]["worker_count"] == 0
    assert p["driver_cpu_avg_pct"] >= busy                    # pooled, the old rule would fire...
    assert p["driver_cpu_avg_with_workers_pct"] < busy        # ...the driver that had workers idled
    assert p["worker_cpu_avg_pct"] < waiting
    rules = _rules(p)
    assert rules["IDLE"] and not rules["DRIVER"], rules
    assert r["single_node"] is False
    assert _close(r["driver_cpu_avg_pct"], p["driver_cpu_avg_pct"])   # the column stays the pooled figure
    assert (r["pressure"], r["scaling_hint"], r["status"]) == ("IDLE", "SCALE_DOWN", "WARN")
    assert r["pressure_reason"] == f"workers averaged {_whole(p['worker_cpu_avg_pct'])}% CPU (threshold {waiting}%)"


def test_single_node_job_names_no_hottest_task():
    """Two tasks on one single-node cluster: the verdict is the driver's memory, pooled, and no
    worker memory exists to rank the tasks by -- so hottest_task_key is NULL rather than the task
    with the most hours (long_light), which did not use the memory."""
    r = _by_job(30)["pr_J_single_two"]
    p = _profile("pr_J_single_two", 30)
    tasks = {t[1]: t[3] for t in pj.JOBS["pr_J_single_two"]["runs"][0]["tasks"]}
    assert tasks["long_light"] > tasks["big_input"]            # the old tie-break's pick
    assert p["task_mem_p90"] == {}                             # no worker memory in any task window
    assert r["single_node"] is True and p["driver_mem_p90_pct"] >= _param("crit_mem_pct")
    assert (r["pressure"], r["scaling_hint"], r["status"]) == ("MEMORY", "SCALE_UP_MEMORY", "CRITICAL")
    assert r["hottest_task_key"] is None and r["hottest_task_mem_p90_pct"] is None
    assert r["pressure_reason"] == (
        f"driver memory p90 {_whole(p['driver_mem_p90_pct'])}% (threshold {_param('crit_mem_pct')}%), "
        f"swap p90 0% (threshold {_param('crit_swap_pct')}%)"
    )


def test_single_node_idle_and_none_reasons_name_the_driver():
    by = _by_job(30)
    idle = by["pr_J_single_idle"]
    p = _profile("pr_J_single_idle", 30)
    assert idle["single_node"] is True and p["driver_cpu_avg_pct"] < _param("waiting_cpu_pct")
    assert (idle["pressure"], idle["scaling_hint"], idle["status"]) == ("IDLE", "SCALE_DOWN", "WARN")
    assert idle["pressure_reason"] == (
        f"the driver averaged {_whole(p['driver_cpu_avg_pct'])}% CPU (threshold {_param('waiting_cpu_pct')}%)"
    )
    ok = by["pr_J_single_ok"]
    p = _profile("pr_J_single_ok", 30)
    assert ok["single_node"] is True
    assert (ok["pressure"], ok["scaling_hint"], ok["status"]) == ("NONE", "NONE", "OK")
    assert ok["pressure_reason"] == (
        f"no threshold crossed: driver CPU avg {_whole(p['driver_cpu_avg_pct'])}%, "
        f"driver memory p90 {_whole(p['driver_mem_p90_pct'])}%, swap p90 0%"
    )


# ---------------------------------------------------------------------------------------------
# the drill-down jobs (tests/fixtures/drilldown.py) through the new query
# ---------------------------------------------------------------------------------------------
def test_drilldown_jobs_through_the_new_query():
    by = _by_job(30)

    j1 = by["dd_tcu_J1"]
    p1 = _profile("dd_tcu_J1", 30)
    at_or_above = [m for m in p1["worker_mem_values"] if m >= _param("crit_mem_pct")]
    share = len(at_or_above) / len(p1["worker_mem_values"])
    assert 0.10 < share < 0.20, share          # "about 15%" of the deduplicated worker minutes
    assert j1["worker_minutes"] == p1["worker_minutes"] == len(p1["worker_mem_values"])
    assert _close(j1["worker_mem_p90_pct"], p1["worker_mem_p90_pct"]) and p1["worker_mem_p90_pct"] >= _param("crit_mem_pct")
    assert (j1["pressure"], j1["scaling_hint"], j1["status"]) == ("MEMORY", "SCALE_UP_MEMORY", "CRITICAL")
    assert j1["hottest_task_key"] == "mem" == max(p1["task_mem_p90"], key=p1["task_mem_p90"].get)
    raw_runs = {t["job_run_id"] for t in _raw_tasks("dd_tcu_J1", 30)}
    assert j1["runs_seen"] == len(raw_runs) == 2 and "dd_tcu_R_old" not in raw_runs
    # several of J1's run clusters sit at their own (fixed) size but none of those was CPU-bound,
    # so none of them makes the job at_ceiling (review round 1); the quoted cluster is the
    # CPU-bound one (review round 2)
    busy = _param("busy_cpu_pct")
    at_size_idle = [k for k, seen in p1["seen"].items()
                    if _configured(k[1]) and seen >= _configured(k[1]) and p1["pair_cpu_p50"][k] < busy]
    assert at_size_idle, "dd_tcu_J1 must keep a cluster at its size that was not CPU-bound"
    assert j1["at_ceiling"] is False
    quoted = {(p1["seen"][k], _configured(k[1])) for k in p1["seen"]
              if p1["pair_cpu_p50"][k] is not None and p1["pair_cpu_p50"][k] >= busy}
    assert (j1["worker_nodes_seen_max"], j1["workers_configured_max"]) in quoted, quoted

    j9 = by["dd_tcu_J9"]
    p9 = _profile("dd_tcu_J9", 30)
    assert j9["worker_minutes"] == p9["worker_minutes"] == _param("min_slices")
    assert p9["worker_cpu_p50_pct"] >= _param("busy_cpu_pct")
    (cluster,) = {t["cluster_id"] for t in _raw_tasks("dd_tcu_J9", 30)}
    latest = _raw_latest_cluster(cluster)
    configured = latest["max_autoscale_workers"] if latest["max_autoscale_workers"] is not None else latest["worker_count"]
    seen = max(p9["seen"].values())
    assert j9["workers_configured_max"] == configured == 4 and j9["worker_nodes_seen_max"] == seen == 2
    assert j9["at_ceiling"] is False and seen < configured
    assert (j9["pressure"], j9["scaling_hint"], j9["status"]) == ("CPU", "NONE", "OK")

    j2 = by["dd_tcu_J2"]
    assert (j2["pressure"], j2["scaling_hint"], j2["status"]) == ("NONE", "NONE", "OK")
