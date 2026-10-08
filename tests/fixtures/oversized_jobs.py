"""tests/fixtures/oversized_jobs.py -- the scenarios lakeflow_job_oversized is tested on.

Writes, in workspace 9101 only (a fresh workspace, not shared with any other builder):
  lakeflow__jobs, lakeflow__job_task_run_timeline, compute__clusters, compute__node_timeline,
  compute__node_types, billing__usage, billing__list_prices

Every id here carries the `ovj_` prefix. Each scenario is its own job with a fixed-CPU/fixed-memory
telemetry profile (a constant value on every node-minute), so percentile_approx's p90 is exactly
that constant regardless of sample count -- a handful of minutes per run is enough to judge, no
large synthetic telemetry volume needed. Each task run is written as ONE row (period_start_time !=
period_end_time, result_state set), which already satisfies "not task_not_executed" without the
multi-row period slicing other builders use for realism.

Scenarios (thresholds at their header defaults: oversized_cpu_pct 30, oversized_mem_pct 40,
half = 15 / 20, warn_saving_usd 25, crit_saving_usd 200):
  ovj_crit  3 runs, autoscale cluster (max 4 workers, 2 seen), CPU 10% / mem 15% -- both under
            half the thresholds -- and $1000 of job compute -> oversized TRUE, est_saving_usd_list
            = 1000 x 0.5 = 500 -> CRITICAL; workers seen (2) < configured (4) -> suggested_action
            'fewer workers / lower autoscale max'.
  ovj_warn  3 runs, fixed cluster (4 of 4 workers), CPU 25% / mem 35% -- under the full thresholds
            but NOT under half -- and $400 of job compute -> est_saving_usd_list = 400 x 0.25 = 100
            -> WARN; workers seen (4) = configured (4) -> suggested_action 'one node size down'.
  ovj_ok    3 runs, CPU 60% / mem 60% (above both thresholds) -> oversized FALSE -> est_saving_usd
            0 -> OK, regardless of its own $300 of job compute.
  ovj_na    1 run only (below :min_runs default 3), with real telemetry and a real cluster ->
            NOT_ASSESSED / too_few_runs.
  ovj_spike 3 runs, CPU 10% / mem 15% except one worker minute per run at mem 70% -> p90 stays 15
            but the memory peak is 70 (>= 40) -> oversized FALSE -> OK.
  ovj_swap  3 runs, CPU 10% / mem 15% with one worker minute per run at 2% swap -> oversized FALSE
            -> OK.
  Neither of the last two has usage rows: a FALSE verdict prices at 0 before cost is read.
"""
from __future__ import annotations

from datetime import timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

WS = "9101"
ACCOUNT_ID = "ovj_acct"
H = timedelta(hours=1)
M = timedelta(minutes=1)
DAY = timedelta(days=1)
T0 = AS_OF - 2 * DAY               # inside every window (7/30/90)

NODE_TYPE = "ovj_node_std"


def _job_sql():
    return "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time) VALUES (?,?,?,?)"


_TASK_SQL = (
    "INSERT INTO lakeflow__job_task_run_timeline "
    "(workspace_id, job_id, run_id, period_start_time, period_end_time, task_key, compute_ids, "
    "result_state, job_run_id) VALUES (?,?,?,?,?,?,?,?,?)"
)
_CLUSTER_SQL = (
    "INSERT INTO compute__clusters "
    "(workspace_id, cluster_id, cluster_name, cluster_source, driver_node_type, worker_node_type, "
    "worker_count, min_autoscale_workers, max_autoscale_workers, dbr_version, change_time) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
)
_NODE_SQL = (
    "INSERT INTO compute__node_timeline "
    "(workspace_id, cluster_id, instance_id, start_time, end_time, driver, cpu_user_percent, "
    "cpu_system_percent, cpu_wait_percent, mem_used_percent, mem_swap_percent, "
    "network_sent_bytes, network_received_bytes, node_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_NTYPE_SQL = (
    "INSERT INTO compute__node_types (account_id, node_type, core_count, memory_mb, gpu_count) "
    "VALUES (?,?,?,?,?)"
)


def _write_cluster(con, cluster_id, *, worker_count=None, min_as=None, max_as=None):
    con.execute(_CLUSTER_SQL, [
        WS, cluster_id, cluster_id.replace("ovj_c_", "ovj_cluster_"), "JOB", "ovj_node_drv",
        NODE_TYPE, worker_count, min_as, max_as, "15.4.x-scala2.12", AS_OF - 30 * DAY,
    ])


def _write_run(con, job_id, run_id, cluster_id, start, minutes, n_workers, cpu_pct, mem_pct,
               spike_mem=None, spike_swap=0.0):
    """One task run row (a real, executed task) plus per-minute node telemetry for the driver and
    n_workers workers, all at the SAME constant cpu/mem so percentile_approx's p90 = that value.
    spike_mem / spike_swap replace worker 0's first minute only, so the peak moves but p90 doesn't."""
    end = start + minutes * M
    con.execute(_TASK_SQL, [WS, job_id, run_id, start, end, "main", [cluster_id], "SUCCEEDED", run_id])
    rows = []
    for i in range(minutes):
        a = start + i * M
        b = a + M
        rows.append((WS, cluster_id, f"{cluster_id}_d", a, b, True,
                     cpu_pct * 0.75, cpu_pct * 0.25, 0.0, mem_pct, 0.0, 1000, 5_000_000, "ovj_node_drv"))
        for w in range(n_workers):
            spike = i == 0 and w == 0
            mem = spike_mem if spike and spike_mem is not None else mem_pct
            swap = spike_swap if spike else 0.0
            rows.append((WS, cluster_id, f"{cluster_id}_w{w}", a, b, False,
                         cpu_pct * 0.75, cpu_pct * 0.25, 0.0, mem, swap, 1000, 5_000_000, NODE_TYPE))
    con.executemany(_NODE_SQL, rows)


# ---------------------------------------------------------------------------------------------
# billing.usage / list_prices -- the exact struct shapes tests/fixtures/ddl.py's DDL expects
# (mirrors tests/fixtures/billing.py's own helpers; duplicated locally so this builder stays
# self-contained, per the convention every module under tests/fixtures/ follows).
# ---------------------------------------------------------------------------------------------
def _usage_metadata(**over):
    base = {
        "cluster_id": None, "job_id": None, "warehouse_id": None, "instance_pool_id": None,
        "node_type": None, "job_run_id": None, "notebook_id": None, "dlt_pipeline_id": None,
        "endpoint_name": None, "endpoint_id": None, "dlt_update_id": None, "dlt_maintenance_id": None,
        "run_name": None, "job_name": None, "notebook_path": None, "central_clean_room_id": None,
        "source_region": None, "destination_region": None, "app_id": None, "app_name": None,
        "metastore_id": None, "private_endpoint_name": None, "storage_api_type": None,
        "budget_policy_id": None, "ai_runtime_pool_id": None, "catalog_id": None,
        "networking_client": None, "recipient_id": None, "usage_policy_id": None,
    }
    base.update(over)
    return base


def _identity_metadata(**over):
    base = {"run_as": None, "created_by": None, "owned_by": None, "run_by": None}
    base.update(over)
    return base


def _product_features(**over):
    base = {
        "jobs_tier": None, "sql_tier": None, "dlt_tier": None,
        "is_serverless": False, "is_photon": False, "serving_type": None,
        "networking": {"connectivity_type": None},
        "ai_runtime": {"compute_type": None},
        "model_serving": {"offering_type": None},
        "ai_gateway": {"feature_type": None},
        "serverless_gpu": {"workload_type": None},
        "agent_bricks": {"problem_type": None, "workload_type": None},
        "performance_target": None,
        "ai_functions": {"ai_function": None},
        "apps": {"compute_size": None},
        "lakeflow_connect": {"task_type": None, "zerobus_request_type": None},
        "lakebase": {"storage_type": None, "compute_type": None},
        "ai_bi_genie": {"capability_type": None, "offering_type": None},
        "genie": {"offering_type": None},
    }
    base.update(over)
    return base


_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _usage_row(con, record_id, *, job_id, job_run_id, usage_quantity, hour_offset=0):
    start = T0 + hour_offset * H
    end = start + H
    con.execute(_USAGE_SQL, [
        ACCOUNT_ID, WS, record_id, "ovj_JOBS_COMPUTE", "aws", start, end, start.date(), {},
        "DBU", usage_quantity,
        _usage_metadata(job_id=job_id, job_run_id=job_run_id), _identity_metadata(),
        "ORIGINAL", start.date(), "JOBS", _product_features(), "COMPUTE_TIME",
    ])


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def _price(con):
    con.execute(_LP_SQL, [
        ACCOUNT_ID, AS_OF - 400 * DAY, None, "ovj_JOBS_COMPUTE", "aws", "USD", "DBU",
        {"default": 0.5, "promotional": {"default": None}, "effective_list": {"default": 0.5}},
    ])


_WS_SQL = (
    "INSERT INTO access__workspaces_latest (workspace_id, workspace_name, workspace_url) "
    "VALUES (?,?,?)"
)


def build(con: duckdb.DuckDBPyConnection) -> None:
    # Named, so dim_workspace.name is never NULL for this workspace (waste_usd.py's / chargeback.py's
    # identical precedent).
    con.execute(_WS_SQL, [WS, "ovj-oversized", "https://dbc-ovj.cloud.databricks.com/"])
    con.execute(_NTYPE_SQL, [ACCOUNT_ID, NODE_TYPE, 4.0, 16384, 0])
    _price(con)

    # ---- ovj_crit: autoscale (max 4), 2 of 4 workers, CPU 10 / mem 15, $1000 -> CRITICAL ----
    _write_cluster(con, "ovj_c_crit", min_as=2, max_as=4)
    con.execute(_job_sql(), [WS, "ovj_crit", "ovj_job_crit", AS_OF - 60 * DAY])
    for i in range(3):
        run_id = f"ovj_R_crit_{i}"
        _write_run(con, "ovj_crit", run_id, "ovj_c_crit", T0 + i * H, 20, 2, 10.0, 15.0)
        _usage_row(con, f"ovj_u_crit_{i}", job_id="ovj_crit", job_run_id=run_id,
                   usage_quantity=2000.0 / 3, hour_offset=i)  # 3 runs x ~$333.33 @ $0.5/DBU = $1000

    # ---- ovj_warn: fixed 4 workers, CPU 25 / mem 35, $400 -> WARN ----
    _write_cluster(con, "ovj_c_warn", worker_count=4)
    con.execute(_job_sql(), [WS, "ovj_warn", "ovj_job_warn", AS_OF - 60 * DAY])
    for i in range(3):
        run_id = f"ovj_R_warn_{i}"
        _write_run(con, "ovj_warn", run_id, "ovj_c_warn", T0 + i * H, 20, 4, 25.0, 35.0)
        _usage_row(con, f"ovj_u_warn_{i}", job_id="ovj_warn", job_run_id=run_id,
                   usage_quantity=800.0 / 3, hour_offset=i)  # 3 runs x ~$266.67 @ $0.5/DBU = $400

    # ---- ovj_ok: CPU 60 / mem 60 (not oversized) -> OK ----
    _write_cluster(con, "ovj_c_ok", worker_count=2)
    con.execute(_job_sql(), [WS, "ovj_ok", "ovj_job_ok", AS_OF - 60 * DAY])
    for i in range(3):
        run_id = f"ovj_R_ok_{i}"
        _write_run(con, "ovj_ok", run_id, "ovj_c_ok", T0 + i * H, 20, 2, 60.0, 60.0)
        _usage_row(con, f"ovj_u_ok_{i}", job_id="ovj_ok", job_run_id=run_id,
                   usage_quantity=600.0 / 3, hour_offset=i)  # $300 total; unused since not oversized

    # ---- ovj_na: 1 run only (< :min_runs default 3) -> NOT_ASSESSED / too_few_runs ----
    _write_cluster(con, "ovj_c_na", worker_count=2)
    con.execute(_job_sql(), [WS, "ovj_na", "ovj_job_na", AS_OF - 60 * DAY])
    _write_run(con, "ovj_na", "ovj_R_na_0", "ovj_c_na", T0, 20, 2, 10.0, 10.0)

    # ---- ovj_spike: low p90, memory peak 70 -> not oversized -> OK ----
    _write_cluster(con, "ovj_c_spike", worker_count=2)
    con.execute(_job_sql(), [WS, "ovj_spike", "ovj_job_spike", AS_OF - 60 * DAY])
    for i in range(3):
        _write_run(con, "ovj_spike", f"ovj_R_spike_{i}", "ovj_c_spike", T0 + i * H, 20, 2, 10.0, 15.0,
                   spike_mem=70.0)

    # ---- ovj_swap: low p90 and peak, one minute of swap -> not oversized -> OK ----
    _write_cluster(con, "ovj_c_swap", worker_count=2)
    con.execute(_job_sql(), [WS, "ovj_swap", "ovj_job_swap", AS_OF - 60 * DAY])
    for i in range(3):
        _write_run(con, "ovj_swap", f"ovj_R_swap_{i}", "ovj_c_swap", T0 + i * H, 20, 2, 10.0, 15.0,
                   spike_swap=2.0)
