"""tests/fixtures/pressure_jobs.py -- T-65 (DEC-65), the jobs half of the compute-pressure
fixtures: the scenarios `lakeflow_job_compute_pressure` is tested on.

Writes, in workspace 9001 only (DEC-65 rule 8 amends DEC-15: this is the second builder in the
drill-down workspace, so the drilldown.py `dd_tcu_` scenarios double as test material for the new
query and the two heuristics are proven side by side on the same fixture):

  lakeflow__job_run_timeline, lakeflow__job_task_run_timeline, lakeflow__jobs,
  compute__clusters, compute__node_timeline, compute__node_types

Every id this builder writes carries the prefix `pr_` (jobs, runs, task runs, clusters, instance
ids, node types, job names). It never references a `dd_`, `cp_`, `qh_` or `lf_` id, writes no
access__workspaces_latest and no billing__usage row, and names only workspace_id, job_id, name and
change_time on its lakeflow__jobs rows (health_rules / creator_user_name / run_as_user_name stay
NULL, as drilldown.py's do), so every 9001-wide or whole-table assertion of another test file keeps
its meaning (tasks/T-65 "Fixture constraints" 1-9). Every run and every task run has several
period rows (start .. mid, mid .. end - 5 min, end row with result_state), exactly the shape
drilldown.py writes, and every task run's run has a job_run_timeline row set, so the drill-down
chain contract (test_drilldown_chain.py) holds: the one run of 2 h or more here
(pr_R_swap_noise) has in-window task rows and is seen by lakeflow_long_running_runs,
task_cluster_utilization and query_task_statement_breakdown alike.

The grain of the query is PER JOB, so every scenario is its own job. Every node_timeline row is one
node-minute with non-NULL cpu_user / cpu_system / mem_used (the CPU is split 75 / 25 between user
and system, so the sum is exact). Scenario numbers live in the module-level dicts below and the
test (tests/test_findings/test_pressure_jobs.py) derives its expectations from them and from the
raw parquet, never from memory.

Scenarios (one job each; within the last 3 days of AS_OF unless stated, so present at 7/30/90):
  pr_J_short_mem    ONE 45-minute run on job cluster pr_c_short (3 workers + driver), tasks
                    t_main (45 min) and t_side (30 min) in parallel on that cluster, worker mem 92,
                    swap 0, worker CPU 85 (so the CPU rule holds too and MEMORY is proven to win
                    over CPU) -> MEMORY / SCALE_UP_MEMORY / CRITICAL; node_minutes 4 x 45 = 180 (not
                    the 300 a per-task sum gives); hottest_task_key t_main (tie on memory, longer
                    task wins); worker node type pr_node_mem32 is the one node_types row (32768 MB,
                    8 cores). Under 2 h, so task_cluster_utilization has no row for it.
  pr_J_swap         one 60-minute run, 2 workers + driver, worker mem 60, swap 20 -> MEMORY via
                    the swap rule.
  pr_J_swap_noise   one 2.5-hour run, 2 workers + driver, CPU 50, mem 60, swap 0.5 -> NONE / NONE
                    / OK here; task_cluster_utilization now judges swap the same way (p90 against
                    :crit_swap_pct, same default) and agrees: MIXED / OK -- the calibration case.
  pr_J_cpu_ceiling  autoscale pr_c_ceiling (min 2, max 4, worker_count NULL), w1-w2 for the whole
                    hour and w3-w4 for its second half (4 distinct workers seen), CPU 85 flat ->
                    CPU / SCALE_OUT / WARN, at_ceiling TRUE.
  pr_J_cpu_headroom autoscale pr_c_headroom, latest SCD2 row max 8 (an older row said max 4), 4
                    workers seen, CPU 85 -> CPU / NONE / OK, at_ceiling FALSE.
  pr_J_pooled       3 runs, each on its own job cluster pr_c_pool_1..3 (2, 4, 3 workers seen;
                    max_autoscale_workers 6, 4, 6); pr_c_pool_3 (the latest run's) has worker node
                    type pr_node_alt, the others pr_node_std; CPU 85 -> one row, CPU / SCALE_OUT /
                    WARN, at_ceiling TRUE (run 2 hit its ceiling).
  pr_J_multi        one 60-minute run, tasks heavy (pr_c_heavy, 2 workers, mem 92) and light
                    (pr_c_light, 2 workers, mem 30) in parallel -> MEMORY, hottest_task_key heavy,
                    max_clusters_per_run 2.
  pr_J_all_purpose  one run on pr_c_ap (cluster_source UI), CPU 50, mem 50 -> NONE / OK,
                    on_all_purpose TRUE.
  pr_J_serverless   compute_ids NULL -> NOT_ASSESSED / no_cluster_recorded.
  pr_J_noconf       pr_c_noconf has node_timeline rows but NO clusters row, CPU 85 -> CPU / NONE /
                    OK, at_ceiling NULL, on_all_purpose NULL.
  pr_J_win40        one run 40 days before AS_OF -> present at window 90 only.
  pr_J_win10        one run 10 days before AS_OF, fixed 2-worker pr_c_win10, CPU 85 -> CPU /
                    SCALE_OUT / WARN at 30 and 90, absent at 7.
  Added so every enum value of the query appears at least once (the scenarios above never reach
  them): pr_J_skew (SKEW / FIX_SKEW), pr_J_io (IO_WAIT / FIX_IO), pr_J_driver (DRIVER /
  DISTRIBUTE_WORK), pr_J_idle (IDLE / SCALE_DOWN), pr_J_skipped (task_not_executed), pr_J_nonodes
  (no_node_timeline_rows), pr_J_brief (15 minutes on 2 workers = 30 worker-minutes ->
  too_few_slices). Each of these also holds a SECOND rule, so the verdict precedence is proven, not
  assumed: pr_J_skew's 3 workers at 90 / 90 / 10 give CPU p50 90 as well as an 80-point gap (SKEW
  over CPU); pr_J_io's workers average 10% CPU under a 35% wait and its driver runs at 95, so the
  idle AND the driver rules hold as well (IO_WAIT over DRIVER and over IDLE); pr_J_cpu_io runs
  85% CPU with 25% wait (CPU over IO_WAIT; autoscale max 4 with 2 seen, so CPU / NONE / OK);
  pr_J_driver's driver at 95 over idle workers (DRIVER over IDLE).

  Review round 1 (the ceiling, the single-node path and the tie-breaks):
  pr_J_tie          tasks a_short (20 min) and z_long (40 min) on one cluster at equal memory ->
                    hottest_task_key z_long: the longer task wins although it sorts later.
  pr_J_single       single-node pr_c_single (worker_count 0), driver-only telemetry for 70 minutes,
                    driver mem 90 -> single_node TRUE, MEMORY / SCALE_UP_MEMORY on the driver's
                    numbers, worker_minutes 0, reason names the driver's memory.
  pr_J_single_swap  single-node, driver mem 50 and swap 20 -> MEMORY via the DRIVER's swap p90.
  pr_J_single_cpu   single-node, driver CPU 90 -> CPU / NONE / OK, at_ceiling NULL: a zero-worker
                    cluster has no worker ceiling, so it never reads "0 of 0 workers -- at ceiling".
  pr_J_mixed_sn     one run: task setup on single-node pr_c_setup_sn (driver-only, CPU 90) then
                    task etl on autoscale pr_c_etl (max 10) with 4 workers at 85 -> CPU / NONE / OK,
                    at_ceiling FALSE: the single-node side cluster no longer counts as "at ceiling".
  pr_J_churn        autoscale pr_c_churn (max 4): w1-w2 for 100 minutes and w3..w6 for 25 minutes
                    each, back to back -> 6 distinct worker ids but never more than 3 at once ->
                    worker_nodes_seen_max 3, at_ceiling FALSE, CPU / NONE / OK.
  pr_J_fixlight     one run: light on fixed pr_c_fl_light (6 of 6 workers, CPU 30, 10 min)
                    and heavy on autoscale pr_c_fl_heavy (4 of max 10 workers, CPU 90, 60 min)
                    -> pooled CPU p50 90, CPU / NONE / OK: the light cluster reached its size but
                    was not CPU-bound, so it does not make the job at_ceiling; and although it ran
                    MORE workers at once (6 > 4), the reason quotes the CPU-bound heavy cluster's
                    4 of 10 (review round 2: the reference pair prefers a CPU-bound cluster).
  pr_J_whtail       task etl on classic pr_c_whtail (worker type pr_node_mem32), then a later task
                    sql whose compute_ids carry a SQL warehouse id (pr_wh_jobsql, no telemetry, no
                    clusters row) -> latest_cluster_id stays pr_c_whtail and worker_memory_gb /
                    worker_cores are read; clusters_seen counts both compute ids (a caveat).

  Review round 2 (the reference pair, the DRIVER rule on mixed jobs, single-node reasons):
  pr_J_nc_mix       one run: light on autoscale pr_c_ncm_light (max 4, 2 workers, CPU 30) and
                    heavy on pr_c_ncm_heavy (NO clusters row, 4 workers, CPU 90) -> CPU / NONE /
                    OK, at_ceiling FALSE (the light cluster has a known ceiling); the reason quotes
                    the CPU-bound heavy cluster, "up to 4 workers seen -- cluster config not
                    found", never "-- headroom" for a cluster whose ceiling is unknown.
  pr_J_resized      pr_c_resized: an older SCD2 row says worker_count 2, the latest says 0; 2
                    workers at CPU 85 for 60 min -> CPU / NONE / OK, at_ceiling NULL,
                    workers_configured_max 0, reason ends "-- no worker ceiling configured".
  pr_J_mixed_idle   one run: setup on single-node pr_c_mi_setup (driver CPU 95, 100 min) and etl
                    on fixed pr_c_mi_etl (2 workers at CPU 5, its driver at 20, 40 min) in
                    parallel -> the pooled driver CPU is 74, but the only driver of a cluster with
                    workers ran at 20, so IDLE / SCALE_DOWN / WARN, not DRIVER: the single-node
                    cluster's only node was doing its own work.
  pr_J_single_two   single-node pr_c_single_two: task big_input (30 min, driver mem 95), then
                    long_light (60 min, driver mem 40) -> MEMORY / SCALE_UP_MEMORY / CRITICAL on the
                    driver's memory, hottest_task_key NULL (no worker memory to rank tasks by; it
                    must not name long_light, the task with more hours).
  pr_J_single_idle  single-node, driver CPU 10, mem 40, 70 min -> IDLE / SCALE_DOWN / WARN, "the
                    driver averaged 10% CPU (threshold 20%)".
  pr_J_single_ok    single-node, driver CPU 50, mem 50, 70 min -> NONE / NONE / OK, "no threshold
                    crossed: driver CPU avg 50%, driver memory p90 50%, swap p90 0%".
"""
from __future__ import annotations

from datetime import timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py callers)

WS = "9001"
H = timedelta(hours=1)
M = timedelta(minutes=1)
DAY = timedelta(days=1)

ACCOUNT_ID = "pr_acct"
T0 = AS_OF - 2 * DAY                 # anchor of every "recent" scenario: inside the 7-day window
RECEIVED_BYTES = 5_000_000           # network_received_bytes per node-minute
SENT_BYTES = 1_000                   # network_sent_bytes per node-minute

NT_MEM32 = "pr_node_mem32"           # the one node type this builder lists in node_types
NT_STD = "pr_node_std"               # not listed in node_types -> worker_memory_gb NULL
NT_ALT = "pr_node_alt"               # pr_c_pool_3's worker type, distinct from the other pools
NT_DRV = "pr_node_drv"               # every driver
WH_ID = "pr_wh_jobsql"               # a SQL warehouse id in a SQL task's compute_ids (pr_J_whtail)
NODE_TYPES = {NT_MEM32: {"core_count": 8.0, "memory_mb": 32768}}

DRIVER_CPU = 20.0                    # every driver unless a scenario says otherwise
DRIVER_MEM = 40.0

# ---------------------------------------------------------------------------------------------
# Cluster configurations (system.compute.clusters, SCD2). `history` rows are older shapes that the
# query must NOT read. pr_c_noconf is deliberately absent.
# ---------------------------------------------------------------------------------------------
CLUSTERS = {
    "pr_c_short":      dict(source="JOB", worker_type=NT_MEM32, worker_count=3),
    "pr_c_swap":       dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_swap_noise": dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_ceiling":    dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=4),
    "pr_c_headroom":   dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=8,
                            history=[dict(min_as=2, max_as=4)]),
    "pr_c_pool_1":     dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=6),
    "pr_c_pool_2":     dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=4),
    "pr_c_pool_3":     dict(source="JOB", worker_type=NT_ALT, min_as=2, max_as=6),
    "pr_c_heavy":      dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_light":      dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_ap":         dict(source="UI", worker_type=NT_STD, worker_count=2),
    "pr_c_win40":      dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_win10":      dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_skew":       dict(source="JOB", worker_type=NT_STD, worker_count=3),
    "pr_c_io":         dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_cpu_io":     dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=4),
    "pr_c_tie":        dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_single":     dict(source="JOB", worker_type=NT_STD, worker_count=0),
    "pr_c_single_swap": dict(source="JOB", worker_type=NT_STD, worker_count=0),
    "pr_c_single_cpu": dict(source="JOB", worker_type=NT_STD, worker_count=0),
    "pr_c_setup_sn":   dict(source="JOB", worker_type=NT_STD, worker_count=0),
    "pr_c_etl":        dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=10),
    "pr_c_churn":      dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=4),
    "pr_c_fl_light":   dict(source="JOB", worker_type=NT_STD, worker_count=6),
    "pr_c_fl_heavy":   dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=10),
    "pr_c_whtail":     dict(source="JOB", worker_type=NT_MEM32, worker_count=2),
    "pr_c_driver":     dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_idle":       dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_skipped":    dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_nonodes":    dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_brief":      dict(source="JOB", worker_type=NT_STD, worker_count=2),
    # review round 2 (pr_c_ncm_heavy is deliberately absent: no configuration row)
    "pr_c_ncm_light":  dict(source="JOB", worker_type=NT_STD, min_as=2, max_as=4),
    "pr_c_resized":    dict(source="JOB", worker_type=NT_STD, worker_count=0,
                            history=[dict(worker_count=2)]),
    "pr_c_mi_setup":   dict(source="JOB", worker_type=NT_STD, worker_count=0),
    "pr_c_mi_etl":     dict(source="JOB", worker_type=NT_STD, worker_count=2),
    "pr_c_single_two": dict(source="JOB", worker_type=NT_STD, worker_count=0),
    "pr_c_single_idle": dict(source="JOB", worker_type=NT_STD, worker_count=0),
    "pr_c_single_ok":  dict(source="JOB", worker_type=NT_STD, worker_count=0),
}


def workers(n, cpu, mem, swap=0.0, wait=0.0, first=1):
    """Worker node specs (suffix, driver, cpu, cpu_wait, mem, swap), numbered from `first`."""
    return [(f"w{i}", False, float(cpu), float(wait), float(mem), float(swap)) for i in range(first, first + n)]


def driver(cpu=DRIVER_CPU, mem=DRIVER_MEM, swap=0.0):
    return [("d", True, float(cpu), 0.0, float(mem), float(swap))]


def instance_id(cluster: str, suffix: str) -> str:
    """pr_c_short + w1 -> pr_i_short_w1 (every instance id is pr_-prefixed too)."""
    return "pr_i_" + cluster[len("pr_c_"):] + "_" + suffix


# ---------------------------------------------------------------------------------------------
# Jobs. One job per scenario. A task: (task_run_id, task_key, offset minutes from the run start,
# minutes, cluster or None, result_state); a SKIPPED task is written as the doc's single
# zero-length row. Telemetry: (cluster, offset minutes from the run start, minutes, node specs),
# one node_timeline row per node per minute.
# ---------------------------------------------------------------------------------------------
def _job(name, runs):
    return {"name": name, "runs": runs}


def _r(run_id, start, minutes, tasks, telemetry):
    return {"run_id": run_id, "start": start, "minutes": minutes, "tasks": tasks, "telemetry": telemetry}


JOBS = {
    "pr_J_short_mem": _job("pr_short_mem", [
        _r("pr_R_short_mem", T0, 45,
           [("pr_T_short_main", "t_main", 0, 45, "pr_c_short", "SUCCEEDED"),
            ("pr_T_short_side", "t_side", 0, 30, "pr_c_short", "SUCCEEDED")],
           [("pr_c_short", 0, 45, driver() + workers(3, cpu=85, mem=92))]),
    ]),
    "pr_J_swap": _job("pr_swap", [
        _r("pr_R_swap", T0, 60,
           [("pr_T_swap", "main", 0, 60, "pr_c_swap", "SUCCEEDED")],
           [("pr_c_swap", 0, 60, driver() + workers(2, cpu=50, mem=60, swap=20))]),
    ]),
    "pr_J_swap_noise": _job("pr_swap_noise", [
        _r("pr_R_swap_noise", T0, 150,
           [("pr_T_swap_noise", "main", 0, 150, "pr_c_swap_noise", "SUCCEEDED")],
           [("pr_c_swap_noise", 0, 150, driver() + workers(2, cpu=50, mem=60, swap=0.5))]),
    ]),
    "pr_J_cpu_ceiling": _job("pr_cpu_ceiling", [
        _r("pr_R_cpu_ceiling", T0, 60,
           [("pr_T_cpu_ceiling", "main", 0, 60, "pr_c_ceiling", "SUCCEEDED")],
           [("pr_c_ceiling", 0, 60, driver() + workers(2, cpu=85, mem=50)),
            ("pr_c_ceiling", 30, 30, workers(2, cpu=85, mem=50, first=3))]),   # autoscaled 2 -> 4
    ]),
    "pr_J_cpu_headroom": _job("pr_cpu_headroom", [
        _r("pr_R_cpu_headroom", T0, 60,
           [("pr_T_cpu_headroom", "main", 0, 60, "pr_c_headroom", "SUCCEEDED")],
           [("pr_c_headroom", 0, 60, driver() + workers(4, cpu=85, mem=50))]),
    ]),
    "pr_J_pooled": _job("pr_pooled", [
        _r("pr_R_pool_1", AS_OF - 60 * H, 60,
           [("pr_T_pool_1", "main", 0, 60, "pr_c_pool_1", "SUCCEEDED")],
           [("pr_c_pool_1", 0, 60, driver() + workers(2, cpu=85, mem=50))]),
        _r("pr_R_pool_2", AS_OF - 40 * H, 60,
           [("pr_T_pool_2", "main", 0, 60, "pr_c_pool_2", "SUCCEEDED")],
           [("pr_c_pool_2", 0, 60, driver() + workers(4, cpu=85, mem=50))]),
        _r("pr_R_pool_3", AS_OF - 20 * H, 60,
           [("pr_T_pool_3", "main", 0, 60, "pr_c_pool_3", "SUCCEEDED")],
           [("pr_c_pool_3", 0, 60, driver() + workers(3, cpu=85, mem=50))]),
    ]),
    "pr_J_multi": _job("pr_multi", [
        _r("pr_R_multi", T0, 60,
           [("pr_T_multi_heavy", "heavy", 0, 60, "pr_c_heavy", "SUCCEEDED"),
            ("pr_T_multi_light", "light", 0, 60, "pr_c_light", "SUCCEEDED")],
           [("pr_c_heavy", 0, 60, driver() + workers(2, cpu=50, mem=92)),
            ("pr_c_light", 0, 60, driver() + workers(2, cpu=50, mem=30))]),
    ]),
    "pr_J_all_purpose": _job("pr_all_purpose", [
        _r("pr_R_all_purpose", T0, 60,
           [("pr_T_all_purpose", "main", 0, 60, "pr_c_ap", "SUCCEEDED")],
           [("pr_c_ap", 0, 60, driver() + workers(2, cpu=50, mem=50))]),
    ]),
    "pr_J_serverless": _job("pr_serverless", [
        _r("pr_R_serverless", T0, 60,
           [("pr_T_serverless", "main", 0, 60, None, "SUCCEEDED")],
           []),
    ]),
    "pr_J_noconf": _job("pr_noconf", [
        _r("pr_R_noconf", T0, 60,
           [("pr_T_noconf", "main", 0, 60, "pr_c_noconf", "SUCCEEDED")],
           [("pr_c_noconf", 0, 60, driver() + workers(2, cpu=85, mem=50))]),
    ]),
    "pr_J_win40": _job("pr_win40", [
        _r("pr_R_win40", AS_OF - 40 * DAY, 60,
           [("pr_T_win40", "main", 0, 60, "pr_c_win40", "SUCCEEDED")],
           [("pr_c_win40", 0, 60, driver() + workers(2, cpu=50, mem=50))]),
    ]),
    "pr_J_win10": _job("pr_win10", [
        _r("pr_R_win10", AS_OF - 10 * DAY, 60,
           [("pr_T_win10", "main", 0, 60, "pr_c_win10", "SUCCEEDED")],
           [("pr_c_win10", 0, 60, driver() + workers(2, cpu=85, mem=50))]),
    ]),
    # ---- enum coverage (see module docstring) ----
    "pr_J_skew": _job("pr_skew", [
        _r("pr_R_skew", T0, 60,
           [("pr_T_skew", "main", 0, 60, "pr_c_skew", "SUCCEEDED")],
           [("pr_c_skew", 0, 60, driver() + workers(2, cpu=90, mem=50) + workers(1, cpu=10, mem=50, first=3))]),
    ]),
    "pr_J_io": _job("pr_io", [
        _r("pr_R_io", T0, 60,
           [("pr_T_io", "main", 0, 60, "pr_c_io", "SUCCEEDED")],
           [("pr_c_io", 0, 60, driver(cpu=95) + workers(2, cpu=10, mem=40, wait=35))]),
    ]),
    "pr_J_cpu_io": _job("pr_cpu_io", [
        _r("pr_R_cpu_io", T0, 60,
           [("pr_T_cpu_io", "main", 0, 60, "pr_c_cpu_io", "SUCCEEDED")],
           [("pr_c_cpu_io", 0, 60, driver() + workers(2, cpu=85, mem=40, wait=25))]),
    ]),
    "pr_J_driver": _job("pr_driver", [
        _r("pr_R_driver", T0, 60,
           [("pr_T_driver", "main", 0, 60, "pr_c_driver", "SUCCEEDED")],
           [("pr_c_driver", 0, 60, driver(cpu=95, mem=70) + workers(2, cpu=5, mem=30))]),
    ]),
    "pr_J_idle": _job("pr_idle", [
        _r("pr_R_idle", T0, 60,
           [("pr_T_idle", "main", 0, 60, "pr_c_idle", "SUCCEEDED")],
           [("pr_c_idle", 0, 60, driver(cpu=10) + workers(2, cpu=5, mem=30))]),
    ]),
    "pr_J_skipped": _job("pr_skipped", [
        _r("pr_R_skipped", T0, 20,
           [("pr_T_skipped", "main", 10, 0, "pr_c_skipped", "SKIPPED")],
           []),
    ]),
    "pr_J_nonodes": _job("pr_nonodes", [
        _r("pr_R_nonodes", T0, 60,
           [("pr_T_nonodes", "main", 0, 60, "pr_c_nonodes", "SUCCEEDED")],
           []),
    ]),
    "pr_J_brief": _job("pr_brief", [
        _r("pr_R_brief", T0, 15,
           [("pr_T_brief", "main", 0, 15, "pr_c_brief", "SUCCEEDED")],
           [("pr_c_brief", 0, 15, driver() + workers(2, cpu=50, mem=50))]),
    ]),
    # ---- review round 1 (see module docstring) ----
    "pr_J_tie": _job("pr_tie", [
        _r("pr_R_tie", T0, 40,
           [("pr_T_tie_short", "a_short", 0, 20, "pr_c_tie", "SUCCEEDED"),
            ("pr_T_tie_long", "z_long", 0, 40, "pr_c_tie", "SUCCEEDED")],
           [("pr_c_tie", 0, 40, driver() + workers(2, cpu=50, mem=50))]),
    ]),
    "pr_J_single": _job("pr_single", [
        _r("pr_R_single", T0, 70,
           [("pr_T_single", "main", 0, 70, "pr_c_single", "SUCCEEDED")],
           [("pr_c_single", 0, 70, driver(cpu=30, mem=90))]),
    ]),
    "pr_J_single_swap": _job("pr_single_swap", [
        _r("pr_R_single_swap", T0, 70,
           [("pr_T_single_swap", "main", 0, 70, "pr_c_single_swap", "SUCCEEDED")],
           [("pr_c_single_swap", 0, 70, driver(cpu=30, mem=50, swap=20))]),
    ]),
    "pr_J_single_cpu": _job("pr_single_cpu", [
        _r("pr_R_single_cpu", T0, 70,
           [("pr_T_single_cpu", "main", 0, 70, "pr_c_single_cpu", "SUCCEEDED")],
           [("pr_c_single_cpu", 0, 70, driver(cpu=90, mem=50))]),
    ]),
    "pr_J_mixed_sn": _job("pr_mixed_sn", [
        _r("pr_R_mixed_sn", T0, 80,
           [("pr_T_mixed_setup", "setup", 0, 20, "pr_c_setup_sn", "SUCCEEDED"),
            ("pr_T_mixed_etl", "etl", 20, 60, "pr_c_etl", "SUCCEEDED")],
           [("pr_c_setup_sn", 0, 20, driver(cpu=90, mem=40)),
            ("pr_c_etl", 20, 60, driver() + workers(4, cpu=85, mem=50))]),
    ]),
    "pr_J_churn": _job("pr_churn", [
        _r("pr_R_churn", T0, 100,
           [("pr_T_churn", "main", 0, 100, "pr_c_churn", "SUCCEEDED")],
           [("pr_c_churn", 0, 100, driver() + workers(2, cpu=85, mem=50)),
            ("pr_c_churn", 0, 25, workers(1, cpu=85, mem=50, first=3)),     # replaced every 25 min:
            ("pr_c_churn", 25, 25, workers(1, cpu=85, mem=50, first=4)),    # 6 worker ids in all,
            ("pr_c_churn", 50, 25, workers(1, cpu=85, mem=50, first=5)),    # never more than 3
            ("pr_c_churn", 75, 25, workers(1, cpu=85, mem=50, first=6))]),  # at once
    ]),
    "pr_J_fixlight": _job("pr_fixlight", [
        _r("pr_R_fixlight", T0, 60,
           [("pr_T_fixlight_light", "light", 0, 10, "pr_c_fl_light", "SUCCEEDED"),
            ("pr_T_fixlight_heavy", "heavy", 0, 60, "pr_c_fl_heavy", "SUCCEEDED")],
           [("pr_c_fl_light", 0, 10, driver() + workers(6, cpu=30, mem=50)),
            ("pr_c_fl_heavy", 0, 60, driver() + workers(4, cpu=90, mem=50))]),
    ]),
    "pr_J_whtail": _job("pr_whtail", [
        _r("pr_R_whtail", T0, 70,
           [("pr_T_whtail_etl", "etl", 0, 60, "pr_c_whtail", "SUCCEEDED"),
            ("pr_T_whtail_sql", "sql", 60, 10, WH_ID, "SUCCEEDED")],
           [("pr_c_whtail", 0, 60, driver() + workers(2, cpu=50, mem=50))]),
    ]),
    # ---- review round 2 (see module docstring) ----
    "pr_J_nc_mix": _job("pr_nc_mix", [
        _r("pr_R_nc_mix", T0, 60,
           [("pr_T_nc_mix_light", "light", 0, 60, "pr_c_ncm_light", "SUCCEEDED"),
            ("pr_T_nc_mix_heavy", "heavy", 0, 60, "pr_c_ncm_heavy", "SUCCEEDED")],
           [("pr_c_ncm_light", 0, 60, driver() + workers(2, cpu=30, mem=50)),
            ("pr_c_ncm_heavy", 0, 60, driver() + workers(4, cpu=90, mem=50))]),
    ]),
    "pr_J_resized": _job("pr_resized", [
        _r("pr_R_resized", T0, 60,
           [("pr_T_resized", "main", 0, 60, "pr_c_resized", "SUCCEEDED")],
           [("pr_c_resized", 0, 60, driver() + workers(2, cpu=85, mem=50))]),
    ]),
    "pr_J_mixed_idle": _job("pr_mixed_idle", [
        _r("pr_R_mixed_idle", T0, 100,
           [("pr_T_mixed_idle_setup", "setup", 0, 100, "pr_c_mi_setup", "SUCCEEDED"),
            ("pr_T_mixed_idle_etl", "etl", 0, 40, "pr_c_mi_etl", "SUCCEEDED")],
           [("pr_c_mi_setup", 0, 100, driver(cpu=95, mem=40)),
            ("pr_c_mi_etl", 0, 40, driver(cpu=20) + workers(2, cpu=5, mem=30))]),
    ]),
    "pr_J_single_two": _job("pr_single_two", [
        _r("pr_R_single_two", T0, 90,
           [("pr_T_single_two_big", "big_input", 0, 30, "pr_c_single_two", "SUCCEEDED"),
            ("pr_T_single_two_long", "long_light", 30, 60, "pr_c_single_two", "SUCCEEDED")],
           [("pr_c_single_two", 0, 30, driver(cpu=30, mem=95)),
            ("pr_c_single_two", 30, 60, driver(cpu=30, mem=40))]),
    ]),
    "pr_J_single_idle": _job("pr_single_idle", [
        _r("pr_R_single_idle", T0, 70,
           [("pr_T_single_idle", "main", 0, 70, "pr_c_single_idle", "SUCCEEDED")],
           [("pr_c_single_idle", 0, 70, driver(cpu=10, mem=40))]),
    ]),
    "pr_J_single_ok": _job("pr_single_ok", [
        _r("pr_R_single_ok", T0, 70,
           [("pr_T_single_ok", "main", 0, 70, "pr_c_single_ok", "SUCCEEDED")],
           [("pr_c_single_ok", 0, 70, driver(cpu=50, mem=50))]),
    ]),
}


# ---------------------------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------------------------
_JRT_SQL = (
    "INSERT INTO lakeflow__job_run_timeline "
    "(workspace_id, job_id, run_id, period_start_time, period_end_time, result_state, "
    "run_duration_seconds) VALUES (?,?,?,?,?,?,?)"
)
_JTRT_SQL = (
    "INSERT INTO lakeflow__job_task_run_timeline "
    "(workspace_id, job_id, run_id, job_run_id, task_key, period_start_time, period_end_time, "
    "result_state, compute_ids, execution_duration_seconds) VALUES (?,?,?,?,?,?,?,?,?,?)"
)
_JOBS_SQL = "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time) VALUES (?,?,?,?)"
_CL_SQL = (
    "INSERT INTO compute__clusters "
    "(workspace_id, cluster_id, cluster_name, cluster_source, driver_node_type, worker_node_type, "
    "worker_count, min_autoscale_workers, max_autoscale_workers, dbr_version, change_time) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
)
_NT_SQL = (
    "INSERT INTO compute__node_timeline "
    "(workspace_id, cluster_id, instance_id, start_time, end_time, driver, cpu_user_percent, "
    "cpu_system_percent, cpu_wait_percent, mem_used_percent, mem_swap_percent, "
    "network_sent_bytes, network_received_bytes, node_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_NTYPE_SQL = (
    "INSERT INTO compute__node_types (account_id, node_type, core_count, memory_mb, gpu_count) "
    "VALUES (?,?,?,?,?)"
)


def _period_rows(start, minutes):
    """The drill-down shape: start .. mid, mid .. end - 5 min, and the end row (flag True)."""
    end = start + minutes * M
    mid = start + (minutes * M) / 2
    return [(start, mid, False), (mid, end - 5 * M, False), (end - 5 * M, end, True)]


def _write_run(con, job_id, run):
    rows = []
    for a, b, is_end in _period_rows(run["start"], run["minutes"]):
        # multi-task semantics (doc): result_state and a 0 run_duration_seconds on the end row only
        rows.append((WS, job_id, run["run_id"], a, b, "SUCCEEDED" if is_end else None, 0 if is_end else None))
    con.executemany(_JRT_SQL, rows)


def _write_task(con, job_id, run, task):
    task_run_id, key, offset, minutes, cluster, state = task
    start = run["start"] + offset * M
    cids = None if cluster is None else [cluster]
    if state == "SKIPPED":
        # the doc's never-run task: one zero-length row
        rows = [(WS, job_id, task_run_id, run["run_id"], key, start, start, state, cids, None)]
    else:
        rows = [
            (WS, job_id, task_run_id, run["run_id"], key, a, b,
             state if is_end else None, cids, minutes * 60 if is_end else None)
            for a, b, is_end in _period_rows(start, minutes)
        ]
    con.executemany(_JTRT_SQL, rows)


def _write_telemetry(con, run, telemetry):
    cluster, offset, minutes, nodes = telemetry
    worker_type = CLUSTERS.get(cluster, {}).get("worker_type", NT_STD)
    rows = []
    for i in range(minutes):
        a = run["start"] + (offset + i) * M
        for suffix, is_driver, cpu, wait, mem, swap in nodes:
            rows.append((
                WS, cluster, instance_id(cluster, suffix), a, a + M, is_driver,
                cpu * 0.75, cpu * 0.25, wait, mem, swap, SENT_BYTES, RECEIVED_BYTES,
                NT_DRV if is_driver else worker_type,
            ))
    con.executemany(_NT_SQL, rows)


def _write_cluster(con, cluster_id, cfg):
    """Older SCD2 shapes (`history`) at AS_OF - 90 days, the latest shape at AS_OF - 30 days."""
    shapes = [(h, AS_OF - 90 * DAY) for h in cfg.get("history", [])]
    shapes.append((cfg, AS_OF - 30 * DAY))
    for shape, change_time in shapes:
        con.execute(_CL_SQL, [
            WS, cluster_id, cluster_id.replace("pr_c_", "pr_cluster_"), cfg["source"], NT_DRV,
            cfg["worker_type"], shape.get("worker_count"), shape.get("min_as"), shape.get("max_as"),
            "15.4.x-scala2.12", change_time,
        ])


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17).
# ---------------------------------------------------------------------------------------------
def build(con: duckdb.DuckDBPyConnection) -> None:
    """Insert every scenario into the (already created, ddl.py-shaped) source tables."""
    for job_id, job in JOBS.items():
        con.execute(_JOBS_SQL, [WS, job_id, job["name"], AS_OF - 60 * DAY])
        for run in job["runs"]:
            _write_run(con, job_id, run)
            for task in run["tasks"]:
                _write_task(con, job_id, run, task)
            for telemetry in run["telemetry"]:
                _write_telemetry(con, run, telemetry)
    for cluster_id, cfg in CLUSTERS.items():
        _write_cluster(con, cluster_id, cfg)
    for node_type, spec in NODE_TYPES.items():
        con.execute(_NTYPE_SQL, [ACCOUNT_ID, node_type, spec["core_count"], spec["memory_mb"], 0])
