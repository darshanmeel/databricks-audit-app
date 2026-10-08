"""tests/fixtures/lakeflow.py -- batch C (T-16), the one builder for `system.lakeflow.*`
(job_run_timeline, job_task_run_timeline, jobs, job_tasks, pipelines, pipeline_update_timeline)
plus this batch's own `system.compute.clusters` rows (DEC-21) and the `system.billing.usage` rows
those jobs/pipelines need for their cost rollups. Follows tests/fixtures/billing.py's builder shape
(module-level build(con), AS_OF from base.py, write_parquet re-exported, parameterized `?` INSERTs,
one Python dict per STRUCT column, one Python list per LIST/ARRAY column) and
tests/fixtures/drilldown.py's row-shape precedent for job_run_timeline / job_task_run_timeline
(period slicing; result_state / termination_code / termination_type / the five *_duration_seconds
columns populated ONLY on the end row).

Per DEC-15 this builder reuses the three SHARED fixture workspaces (1111 acme-prod, 2222 acme-dev,
3333 acme-uat, written by billing.py) and owns every other id under the `lf_` prefix. Per
build_fixtures.py's per-builder isolation (each builder runs against its OWN fresh in-memory
connection, seeded with all 47 sources from ddl.py), this module never reads another builder's
rows and writes only what it needs directly: billing__usage rows for its own job/pipeline ids
(parquet lands at tests/fixtures/parquet/billing__usage/lakeflow.parquet, unioned by name with
T-09's/T-14's own slices) and compute__clusters rows for lakeflow_jobs_on_all_purpose (parquet at
tests/fixtures/parquet/compute__clusters/lakeflow.parquet, unioned with T-14's compute.parquet).

This single builder must carry enough shape to serve all 21 lakeflow_* finding queries across three
fixture batches: this task's own C1 "pipeline" batch (5 ids, tested by tests/test_findings/
test_lakeflow_pipelines.py), T-17's C2 "run timeline" batch (7 ids, T-17 owns those tests) and
T-18's C3 "dims + usage-joined" batch (9 ids, T-18 owns those tests). See the Hand-off notes at the
bottom of tasks/T-16-lakeflow-builder-and-pipeline-tests.md for the exact ids/counts T-17 and T-18
read directly.

Shared family contracts (see the query bodies under
app/queries/vendored/jobs_pipelines/lakeflow_*.sql for the authoritative text):
  - job_run_timeline / job_task_run_timeline are timeline tables: result_state / termination_code /
    termination_type / the five *_duration_seconds columns are populated ONLY on the end row (the
    row where result_state IS NOT NULL). A run under 1h is exactly one row (that row is its own end
    row); a run over 1h is sliced hourly with NULL-state rows before the end row. SEC M below is the
    one dedicated multi-period example.
  - job_task_run_timeline.job_run_id = job_run_timeline.run_id, always additionally on
    workspace_id. job_id / task_key / pipeline_id are unique only WITHIN a workspace.
  - jobs / job_tasks / pipelines are SCD2 (change_time, delete_time); every id this builder gives a
    dimension row gets >= 2 change rows, both before AS_OF, latest = the row a QUALIFY
    ROW_NUMBER()...ORDER BY change_time DESC take would pick.
  - Most families bound the window with `period_start_time >= dateadd(-period_days) AND
    period_end_time < date_trunc('DAY', current_timestamp())`. Two exceptions this builder proves
    directly: lakeflow_workload_mix_hours bounds on `period_start_time < date_trunc('DAY', now)`
    instead (SEC K), and lakeflow_termination_type_probe has NO upper bound at all (SEC K) -- the
    same AS_OF-day row (lf_job_probe_asof) is excluded from the first, included in the second.
  - usage_metadata.dlt_pipeline_id / dlt_update_id / dlt_maintenance_id / job_id attribute
    pipeline/job DBUs on system.billing.usage; usage_unit='DBU', summed across ALL record_types.

AS_OF = 2026-09-21 12:00:00 (from base.py); D0 = AS_OF's own date; D(n) = D0 minus n days.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()


def D(n: int) -> date:
    return D0 - timedelta(days=n)


def DT(n: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime.combine(D(n), datetime.min.time()) + timedelta(hours=hour, minutes=minute)


H = timedelta(hours=1)
M = timedelta(minutes=1)
S = timedelta(seconds=1)

ACCOUNT_ID = "lf_acct"

# DEC-15: shared fixture workspaces (billing.py owns the workspaces_latest rows for these).
WS1 = "1111"  # acme-prod
WS2 = "2222"  # acme-dev
WS3 = "3333"  # acme-uat


# =================================================================================================
# Column-explicit insert helpers. Every column tests/fixtures/ddl.py's DDL gives each table is
# always given an explicit value (None where the DDL allows NULL), so no column is ever silently
# omitted -- same convention as tests/fixtures/billing.py.
# =================================================================================================
_JRT_SQL = (
    "INSERT INTO lakeflow__job_run_timeline (account_id, workspace_id, job_id, run_id, "
    "period_start_time, period_end_time, trigger_type, result_state, run_type, run_name, "
    "compute_ids, termination_code, job_parameters, source_task_run_id, root_task_run_id, "
    "compute, termination_type, setup_duration_seconds, queue_duration_seconds, "
    "run_duration_seconds, cleanup_duration_seconds, execution_duration_seconds) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def jrt(
    con: duckdb.DuckDBPyConnection,
    ws: str,
    job_id: str,
    run_id: str,
    start: datetime,
    end: datetime,
    *,
    result_state: str | None = None,
    run_type: str = "JOB_RUN",
    trigger_type: str = "PERIODIC",
    run_name: str | None = None,
    compute_ids: list | None = None,
    termination_code: str | None = None,
    termination_type: str | None = None,
    setup: int | None = None,
    queue: int | None = None,
    run_dur: int | None = None,
    cleanup: int | None = None,
    execution: int | None = None,
) -> None:
    con.execute(
        _JRT_SQL,
        [
            ACCOUNT_ID, ws, job_id, run_id, start, end, trigger_type, result_state, run_type,
            run_name, compute_ids, termination_code, {}, None, None, None, termination_type,
            setup, queue, run_dur, cleanup, execution,
        ],
    )


_JTRT_SQL = (
    "INSERT INTO lakeflow__job_task_run_timeline (account_id, workspace_id, job_id, run_id, "
    "period_start_time, period_end_time, task_key, compute_ids, result_state, job_run_id, "
    "parent_run_id, termination_code, compute, termination_type, task_parameters, "
    "setup_duration_seconds, cleanup_duration_seconds, execution_duration_seconds) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def jtrt(
    con: duckdb.DuckDBPyConnection,
    ws: str,
    job_id: str,
    run_id: str,
    job_run_id: str,
    task_key: str,
    start: datetime,
    end: datetime,
    *,
    result_state: str | None = None,
    compute_ids: list | None = None,
    compute: list | None = None,
    termination_code: str | None = None,
    termination_type: str | None = None,
    setup: int | None = None,
    cleanup: int | None = None,
    execution: int | None = None,
) -> None:
    """`compute` is the struct-array parallel to `compute_ids` (one {"type", "cluster_id",
    "warehouse_id"} dict per compute_ids entry) that lakeflow_jobs_on_all_purpose now reads to
    tell a SQL-warehouse id apart from a cluster id -- None (the default) for every caller that
    does not need it, matching compute_ids' own optional shape."""
    con.execute(
        _JTRT_SQL,
        [
            ACCOUNT_ID, ws, job_id, run_id, start, end, task_key, compute_ids, result_state,
            job_run_id, job_run_id, termination_code, compute, termination_type, {}, setup, cleanup,
            execution,
        ],
    )


_JOBS_SQL = (
    "INSERT INTO lakeflow__jobs (account_id, workspace_id, job_id, name, creator_id, tags, "
    "run_as, change_time, delete_time, description, trigger, trigger_type, run_as_user_name, "
    "creator_user_name, paused, timeout_seconds, health_rules, deployment, create_time) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def job_row(
    con: duckdb.DuckDBPyConnection,
    ws: str,
    job_id: str,
    name: str,
    change_time: datetime,
    *,
    delete_time: datetime | None = None,
    creator_user_name: str | None = None,
    run_as_user_name: str | None = None,
    trigger_type: str | None = None,
    paused: bool = False,
    timeout_seconds: int | None = None,
    health_rules: list | None = None,
) -> None:
    con.execute(
        _JOBS_SQL,
        [
            ACCOUNT_ID, ws, job_id, name, None, {}, None, change_time, delete_time, None, None,
            trigger_type, run_as_user_name, creator_user_name, paused, timeout_seconds, health_rules,
            None, change_time - timedelta(days=200),
        ],
    )


def _sp_uuid(n: int) -> str:
    """A syntactically valid (hex-only) service-principal application-id UUID, distinct per n --
    lakeflow_job_ownership_orphans.sql (and access_runas_escalation.sql) treat a run_as identity
    shaped like this as SERVICE_PRINCIPAL rather than HUMAN."""
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def job_scd2(con, ws, job_id, name, **kw) -> None:
    """Two SCD2 rows (older + current, both before AS_OF) sharing `**kw`'s field values on the
    CURRENT (later change_time) row; the older row is a bare placeholder with no fields set, so a
    QUALIFY ROW_NUMBER() OVER (... ORDER BY change_time DESC) = 1 always picks the row carrying
    `**kw`."""
    job_row(con, ws, job_id, name, AS_OF - timedelta(days=250))
    job_row(con, ws, job_id, name, AS_OF - timedelta(days=100), **kw)


_TASKS_SQL = (
    "INSERT INTO lakeflow__job_tasks (account_id, workspace_id, job_id, task_key, "
    "depends_on_keys, change_time, delete_time, timeout_seconds, health_rules) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)


def task_row(
    con: duckdb.DuckDBPyConnection,
    ws: str,
    job_id: str,
    task_key: str,
    change_time: datetime,
    *,
    delete_time: datetime | None = None,
    timeout_seconds: int | None = None,
    health_rules: list | None = None,
) -> None:
    con.execute(
        _TASKS_SQL,
        [ACCOUNT_ID, ws, job_id, task_key, None, change_time, delete_time, timeout_seconds, health_rules],
    )


def task_scd2(con, ws, job_id, task_key, **kw) -> None:
    task_row(con, ws, job_id, task_key, AS_OF - timedelta(days=250))
    task_row(con, ws, job_id, task_key, AS_OF - timedelta(days=100), **kw)


_PIPE_SQL = (
    "INSERT INTO lakeflow__pipelines (workspace_id, pipeline_id, pipeline_type, name, "
    "created_by, run_as, tags, settings, configuration, change_time, delete_time, account_id, "
    "create_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def pipeline_row(
    con: duckdb.DuckDBPyConnection,
    ws: str,
    pipeline_id: str,
    name: str,
    change_time: datetime,
    *,
    pipeline_type: str = "WORKSPACE",
    photon: bool = True,
    development: bool = False,
    continuous: bool = False,
    serverless: bool = True,
    edition: str = "ADVANCED",
    channel: str = "CURRENT",
    delete_time: datetime | None = None,
) -> None:
    settings = {
        "photon": photon, "development": development, "continuous": continuous,
        "serverless": serverless, "edition": edition, "channel": channel,
    }
    con.execute(
        _PIPE_SQL,
        [
            ws, pipeline_id, pipeline_type, name, None, None, {}, settings, {}, change_time,
            delete_time, ACCOUNT_ID, change_time - timedelta(days=200),
        ],
    )


def pipeline_scd2(con, ws, pipeline_id, name, **kw) -> None:
    """Two SCD2 rows (older + current, both before AS_OF), BOTH carrying `**kw`'s field values --
    deliberately different from job_scd2/task_scd2 above, which give `**kw` only to the current
    (later change_time) row and leave the older row a bare placeholder. Every one of this batch's 5
    C1 queries only ever reads the QUALIFY-selected latest row, so which row carries a given
    SETTING never changes any query's output either way; the difference is load-bearing specifically
    for `delete_time` on lf_pl_deleted (_build_pipelines below): a raw-parquet scan that does not go
    through QUALIFY (e.g. a `_raw_row_exists`-style anchor a test adds, per
    tests/test_findings/test_lakeflow_pipelines.py) must see delete_time set on EVERY row for that
    pipeline_id, not just its current one, or it would find the older, still-delete_time-IS-NULL row
    and wrongly conclude the pipeline was never deleted. Do not change this to job_scd2's pattern."""
    pipeline_row(con, ws, pipeline_id, name, AS_OF - timedelta(days=250), **kw)
    pipeline_row(con, ws, pipeline_id, name, AS_OF - timedelta(days=100), **kw)


_PUT_SQL = (
    "INSERT INTO lakeflow__pipeline_update_timeline (workspace_id, pipeline_id, update_id, "
    "update_type, request_id, run_as_user_name, trigger_type, trigger_details, result_state, "
    "compute, period_start_time, period_end_time, refresh_selection, full_refresh_selection, "
    "reset_checkpoint_selection, account_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def pipeline_update(
    con: duckdb.DuckDBPyConnection,
    ws: str,
    pipeline_id: str,
    update_id: str,
    start: datetime,
    end: datetime,
    *,
    update_type: str = "REFRESH",
    trigger_type: str = "SCHEDULE",
    result_state: str | None = "COMPLETED",
) -> None:
    con.execute(
        _PUT_SQL,
        [
            ws, pipeline_id, update_id, update_type, f"{update_id}_req", None, trigger_type, None,
            result_state, None, start, end, None, None, None, ACCOUNT_ID,
        ],
    )


_CLUSTER_SQL = (
    "INSERT INTO compute__clusters (account_id, workspace_id, cluster_id, cluster_name, "
    "owned_by, create_time, delete_time, driver_node_type, worker_node_type, worker_count, "
    "min_autoscale_workers, max_autoscale_workers, auto_termination_minutes, "
    "enable_elastic_disk, tags, cluster_source, init_scripts, aws_attributes, azure_attributes, "
    "gcp_attributes, driver_instance_pool_id, worker_instance_pool_id, dbr_version, change_time, "
    "change_date, data_security_mode, policy_id) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def cluster_row(
    con: duckdb.DuckDBPyConnection,
    ws: str,
    cluster_id: str,
    cluster_source: str,
    change_time: datetime,
    *,
    worker_count: int = 2,
) -> None:
    con.execute(
        _CLUSTER_SQL,
        [
            ACCOUNT_ID, ws, cluster_id, f"name-{cluster_id}", None, change_time - timedelta(days=200),
            None, "m5.xlarge", "m5.xlarge", worker_count, None, None, 30, True, {}, cluster_source,
            None, None, None, None, None, None, "15.4.x", change_time, change_time.date(), None, None,
        ],
    )


def cluster_scd2(con, ws, cluster_id, cluster_source) -> None:
    cluster_row(con, ws, cluster_id, cluster_source, AS_OF - timedelta(days=250), worker_count=2)
    cluster_row(con, ws, cluster_id, cluster_source, AS_OF - timedelta(days=100), worker_count=4)


# ---- billing.usage: same STRUCT default-dict convention as tests/fixtures/billing.py -----------
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


def usage(
    con: duckdb.DuckDBPyConnection,
    record_id: str,
    *,
    workspace_id: str,
    sku_name: str,
    usage_date_: date,
    usage_quantity: float,
    billing_origin_product: str = "JOBS",
    usage_metadata: dict | None = None,
    product_features: dict | None = None,
    hour: int = 0,
) -> None:
    start = datetime.combine(usage_date_, datetime.min.time()) + timedelta(hours=hour)
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, workspace_id, record_id, sku_name, "aws", start, end, usage_date_, {},
            "DBU", usage_quantity, usage_metadata if usage_metadata is not None else _usage_metadata(),
            _identity_metadata(), "ORIGINAL", usage_date_, billing_origin_product,
            product_features if product_features is not None else _product_features(), "COMPUTE_TIME",
        ],
    )


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def list_price(con: duckdb.DuckDBPyConnection, sku_name: str, default_rate: float, effective_list_default: float) -> None:
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, AS_OF - timedelta(days=300), None, sku_name, "aws", "USD", "DBU",
            {"default": default_rate, "promotional": {"default": None}, "effective_list": {"default": effective_list_default}},
        ],
    )


# =================================================================================================
# build(con) -- auto-discovered by build_fixtures.py (DEC-17).
# =================================================================================================
def build(con: duckdb.DuckDBPyConnection) -> None:
    _build_pipelines(con)
    _build_pipeline_updates_and_usage(con)
    _build_swf_jobs(con)
    _build_failed_runs_jobs(con)
    _build_phase_jobs(con)
    _build_phase_multitask(con)
    _build_phase_failstart(con)
    _build_never_started_jobs(con)
    _build_probe_and_general_asof(con)
    _build_multiperiod(con)
    _build_tasks_near_timeout(con)
    _build_all_purpose(con)
    _build_zombie_jobs(con)
    _build_retries_jobs(con)
    _build_job_reliability(con)
    _build_job_duration_regression(con)
    _build_job_run_cost(con)
    _build_bulk_workspace_jobs(con)
    _build_blanket_usage(con)


# =================================================================================================
# SEC O -- blanket billing.usage coverage: every job_id / pipeline_id this builder creates gets at
# least one usage row keyed to it, per the fixture facts' "usage rows keyed to every job/pipeline
# id this builder creates" -- including ids whose own C1/C2/C3 query never reads billing.usage at
# all (never_started_runs, the multi-period proof, tasks_near_timeout, termination_type_probe /
# workload_mix_hours, the general AS_OF negative control, the update_failures_retries pipelines,
# lf_pl_deleted, lf_job_swf_asof, the lf_job_phase_* set) or reads usage keyed by a DIFFERENT
# column (lakeflow_jobs_on_all_purpose's cost_rollup is keyed by usage_metadata.cluster_id, not
# job_id, so the lf_job_allpurpose_* rows below never feed that id's own cost figures).
# lf_job_zombie_crit and lf_job_zombie_warn_null are the ONLY two exceptions in the whole builder
# (SEC J proves "a genuinely stale job correctly shows ~0 net_dbus" by omission, not a zero-value
# row) -- every other job_id and every pipeline_id this file creates gets a row below. These rows
# are small (5 DBU) and land D(12), well inside every window; verified (in-memory duckdb, this
# review round) to add only harmless extra low-magnitude OK-band rows to lakeflow_pipeline_cost /
# lakeflow_pipeline_update_failures_retries / lakeflow_retries_repairs, never changing any status
# band this batch's or T-17's/T-18's own tests assert on (all of which are scoped to named ids).
# =================================================================================================
def _build_blanket_usage(con: duckdb.DuckDBPyConnection) -> None:
    for job_id in (
        "lf_job_never_crit", "lf_job_never_warn", "lf_job_never_ok", "lf_job_multiperiod",
        "lf_job_tnt", "lf_job_probe_asof", "lf_job_asof_general",
        "lf_job_swf_asof",
        "lf_job_phase_crit", "lf_job_phase_warn", "lf_job_phase_ok", "lf_job_phase_null",
        "lf_job_phase_zero", "lf_job_phase_multitask",
        "lf_job_allpurpose_crit", "lf_job_allpurpose_warn", "lf_job_allpurpose_ok",
        "lf_job_allpurpose_null", "lf_job_allpurpose_unknown_cluster", "lf_job_allpurpose_empty",
        "lf_job_allpurpose_shared_a", "lf_job_allpurpose_shared_b", "lf_job_allpurpose_warehouse",
    ):
        usage(con, f"lf_u_blanket_{job_id}", workspace_id=WS1, sku_name="lf_JOBS_COMPUTE",
              usage_date_=D(12), usage_quantity=5.0, billing_origin_product="JOBS",
              usage_metadata=_usage_metadata(job_id=job_id))
    for pipeline_id in ("lf_pl_upd_crit", "lf_pl_upd_warn", "lf_pl_upd_retry", "lf_pl_deleted"):
        usage(con, f"lf_u_blanket_{pipeline_id}", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
              usage_date_=D(12), usage_quantity=5.0, billing_origin_product="DLT",
              usage_metadata=_usage_metadata(dlt_pipeline_id=pipeline_id))


# =================================================================================================
# SEC A -- pipelines dimension (SCD2, >=2 rows each). Combo A/B/C give lakeflow_pipelines_inventory_
# tier >= 2 distinct (pipeline_type, setting_*) combinations; lf_pl_deleted proves the
# delete_time IS NULL filter.
# =================================================================================================
def _build_pipelines(con: duckdb.DuckDBPyConnection) -> None:
    combo_a = dict(pipeline_type="WORKSPACE", photon=True, development=False, continuous=False,
                    serverless=True, edition="ADVANCED", channel="CURRENT")
    combo_b = dict(pipeline_type="WORKSPACE", photon=False, development=False, continuous=True,
                    serverless=False, edition="CORE", channel="CURRENT")
    combo_c = dict(pipeline_type="WORKSPACE", photon=True, development=True, continuous=False,
                    serverless=True, edition="PRO", channel="PREVIEW")

    for pid in ("lf_pl_cost_crit", "lf_pl_cost_warn", "lf_pl_cost_ok", "lf_pl_idle_crit",
                "lf_pl_idle_warn", "lf_pl_idle_ok", "lf_pl_asof_excl"):
        pipeline_scd2(con, WS1, pid, f"pipeline {pid}", **combo_a)
    pipeline_scd2(con, WS1, "lf_pl_idle_continuous", "pipeline lf_pl_idle_continuous", **combo_b)
    for pid in ("lf_pl_upd_crit", "lf_pl_upd_warn", "lf_pl_upd_retry"):
        pipeline_scd2(con, WS1, pid, f"pipeline {pid}", **combo_c)
    pipeline_scd2(con, WS1, "lf_pl_deleted", "pipeline lf_pl_deleted", delete_time=AS_OF - timedelta(days=10), **combo_a)


# =================================================================================================
# SEC B -- pipeline_update_timeline + billing.usage for the 5 C1 ids.
#   lakeflow_pipeline_cost:   status now needs nothing_delivered (P4-FIXES28), so crit/warn's own
#                             update is FAILED, not COMPLETED: lf_pl_cost_crit=2500 (>=crit 2000,
#                             nothing delivered) CRITICAL, warn=800 (in [warn 500, crit 2000),
#                             nothing delivered) WARN, ok=100 (<500, delivers) OK. lf_pl_asof_excl's
#                             only usage row lands on D0 (today) -> excluded, pipeline never appears
#                             in the id's output.
#   lakeflow_pipeline_idle_tail_duration: lf_pl_idle_continuous -> NOT_ASSESSED regardless of DBUs;
#                             lf_pl_idle_crit=600 (>=crit 500) CRITICAL, warn=150 (>=100) WARN,
#                             ok=20 (<100) OK. lf_pl_asof_excl's only update lands on D0 -> excluded.
#   lakeflow_pipeline_update_failures_retries: lf_pl_upd_crit 12 FAILED (>=crit 10) CRITICAL,
#                             lf_pl_upd_warn 5 FAILED (>=warn 3, <10) WARN, lf_pl_upd_retry 2 FAILED
#                             with trigger_type=RETRY_ON_FAILURE (OK band, proves retry_triggered_
#                             rows), lf_pl_cost_ok gets 1 extra FAILED update (OK band, distinct
#                             pipeline_id keeps the grain unique), lf_pl_upd_crit also gets 3
#                             COMPLETED rows (a distinct result_state group, proves the enum).
# =================================================================================================
def _build_pipeline_updates_and_usage(con: duckdb.DuckDBPyConnection) -> None:
    list_price(con, "lf_DLT_CORE_COMPUTE", 0.5, 0.6)

    # -- cost bands (billing.usage keyed on dlt_pipeline_id) --
    usage(con, "lf_u_pl_cost_crit_a", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(2), usage_quantity=2100.0, billing_origin_product="DLT",
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_cost_crit"))
    usage(con, "lf_u_pl_cost_crit_maint", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(2), usage_quantity=400.0, billing_origin_product="DLT", hour=2,
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_cost_crit", dlt_maintenance_id="lf_maint_1"))
    usage(con, "lf_u_pl_cost_warn", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(2), usage_quantity=800.0, billing_origin_product="DLT",
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_cost_warn"))
    usage(con, "lf_u_pl_cost_ok", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(2), usage_quantity=100.0, billing_origin_product="DLT",
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_cost_ok"))
    # AS_OF-day (D0) row: must be excluded by usage_date < current_date() on every C1 id that reads it.
    usage(con, "lf_u_pl_asof_excl", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(0), usage_quantity=999.0, billing_origin_product="DLT",
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_asof_excl"))

    # -- idle-tail bands --
    usage(con, "lf_u_pl_idle_crit", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(2), usage_quantity=600.0, billing_origin_product="DLT",
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_idle_crit"))
    usage(con, "lf_u_pl_idle_warn", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(2), usage_quantity=150.0, billing_origin_product="DLT",
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_idle_warn"))
    usage(con, "lf_u_pl_idle_ok", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(2), usage_quantity=20.0, billing_origin_product="DLT",
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_idle_ok"))
    usage(con, "lf_u_pl_idle_continuous", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(2), usage_quantity=50.0, billing_origin_product="DLT",
          usage_metadata=_usage_metadata(dlt_pipeline_id="lf_pl_idle_continuous"))

    # -- pipeline_update_timeline rows (drive both idle_tail_duration and update_failures_retries) --
    def upd(pid, uid, day, hour, **kw):
        start = DT(day, hour)
        pipeline_update(con, WS1, pid, uid, start, start + H, **kw)

    # FAILED, not COMPLETED: status now requires nothing_delivered for CRITICAL, not size alone.
    upd("lf_pl_cost_crit", "lf_upd_cc_1", 2, 1, result_state="FAILED")
    # FAILED, not COMPLETED: status now requires nothing_delivered for WARN below crit, not size alone.
    upd("lf_pl_cost_warn", "lf_upd_cw_1", 2, 1, result_state="FAILED")
    upd("lf_pl_cost_ok", "lf_upd_co_1", 2, 1, result_state="COMPLETED")
    upd("lf_pl_cost_ok", "lf_upd_co_fail", 2, 3, update_type="REFRESH", trigger_type="SCHEDULE", result_state="FAILED")

    upd("lf_pl_idle_crit", "lf_upd_ic_1", 2, 1, result_state="COMPLETED")
    upd("lf_pl_idle_warn", "lf_upd_iw_1", 2, 1, result_state="COMPLETED")
    upd("lf_pl_idle_ok", "lf_upd_io_1", 2, 1, result_state="COMPLETED")
    upd("lf_pl_idle_continuous", "lf_upd_icont_1", 2, 1, result_state="COMPLETED")
    # AS_OF-day (D0) update: period_end_time on D0 must be excluded (period_end_time < date_trunc).
    upd("lf_pl_asof_excl", "lf_upd_asof_1", 0, 9, result_state="FAILED")

    for i in range(12):
        upd("lf_pl_upd_crit", f"lf_upd_crit_{i:02d}", 3, i % 20, update_type="REFRESH",
            trigger_type="SCHEDULE", result_state="FAILED")
    for i in range(3):
        upd("lf_pl_upd_crit", f"lf_upd_crit_ok_{i:02d}", 4, i, update_type="REFRESH",
            trigger_type="SCHEDULE", result_state="COMPLETED")
    for i in range(5):
        upd("lf_pl_upd_warn", f"lf_upd_warn_{i:02d}", 3, i, update_type="FULL_REFRESH",
            trigger_type="SCHEDULE", result_state="FAILED")
    for i in range(2):
        upd("lf_pl_upd_retry", f"lf_upd_retry_{i:02d}", 3, i, update_type="REFRESH",
            trigger_type="RETRY_ON_FAILURE", result_state="FAILED")


# =================================================================================================
# SEC C -- lakeflow_succeeded_with_failed_tasks (grain [workspace_id, job_id]). warn=3, crit=10.
#   lf_job_swf_crit: 12 SUCCEEDED runs each with a FAILED task -> CRITICAL.
#   lf_job_swf_warn: 5 -> WARN. lf_job_swf_ok: 3 runs, only 1 with a failed task -> OK (1<3).
#   lf_job_swf_asof: one SUCCEEDED run whose period_end_time lands on D0 (AS_OF day) -> excluded
#   from job_end entirely (proves the family's general period_end_time < date_trunc exclusion).
# =================================================================================================
def _build_swf_jobs(con: duckdb.DuckDBPyConnection) -> None:
    def scenario(job_id, n, n_failed, day):
        for i in range(n):
            run_id = f"{job_id}_r{i:02d}"
            start = DT(day, 1 + (i % 18))
            end = start + 5 * M
            jrt(con, WS1, job_id, run_id, start, end, result_state="SUCCEEDED",
                run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
                cleanup=0, execution=0)
            if i < n_failed:
                jtrt(con, WS1, job_id, run_id, run_id, "risky_task", start, end,
                     result_state="FAILED", execution=300)
            else:
                jtrt(con, WS1, job_id, run_id, run_id, "risky_task", start, end,
                     result_state="SUCCEEDED", execution=300)
        usage(con, f"lf_u_{job_id}", workspace_id=WS1, sku_name="lf_JOBS_COMPUTE",
              usage_date_=D(day), usage_quantity=float(10 * n), billing_origin_product="JOBS",
              usage_metadata=_usage_metadata(job_id=job_id))

    scenario("lf_job_swf_crit", 12, 12, 3)
    scenario("lf_job_swf_warn", 5, 5, 3)
    scenario("lf_job_swf_ok", 3, 1, 3)

    # AS_OF-day exclusion proof.
    asof_start = DT(0, 9, 0)
    asof_end = DT(0, 9, 5)
    jrt(con, WS1, "lf_job_swf_asof", "lf_job_swf_asof_r1", asof_start, asof_end,
        result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="SCHEDULE",
        setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
    jtrt(con, WS1, "lf_job_swf_asof", "lf_job_swf_asof_r1", "lf_job_swf_asof_r1", "risky_task",
         asof_start, asof_end, result_state="FAILED", execution=300)


# =================================================================================================
# SEC D -- lakeflow_failed_runs (grain [workspace_id, run_type, trigger_type, result_state,
# termination_code]) warn=5, crit=20 -- and lakeflow_failed_jobs_wasted_dbus (grain
# [workspace_id, job_id]), REWRITTEN IN PLACE by P4-01-W2 (tasks/P4-WASTE-SPEC.md section 3) to
# price failed runs per run from usage_metadata.job_run_id, not from a whole-job failed-run-share
# proxy. Three separate (job, termination_code) FAILED-run groups so lakeflow_failed_runs's grain
# (which does NOT include job_id) never blends them: lf_job_fr_a 20 rows/CLUSTER_ERROR,
# lf_job_fr_b 12 rows/STORAGE_ACCESS_ERROR, lf_job_fr_c 4 rows/WORKSPACE_RUN_LIMIT_EXCEEDED. All
# distinct_runs == failed_runs in each group (every run in the group is FAILED). Every group's
# `usage()` call below carries `usage_metadata(job_id=job_id)` ONLY, no job_run_id at all -- so
# under the rewritten query all three now read status NOT_ASSESSED, not_assessed_reason
# no_run_id_in_billing, and est_wasted_usd_list NULL, whatever their failure count: this is the
# fixture that proves that reason (test_lakeflow_dims.py's own
# test_lakeflow_failed_jobs_wasted_dbus). lf_JOBS_COMPUTE is deliberately unpriced, so
# price_basis='unpriced' and est_usd_list is also NULL on lf_job_fr_a/b/c; lf_job_fr_mixed below
# sits on the PRICED lf_DLT_CORE_COMPUTE SKU instead, so its est_usd_list is a real number and,
# since none of its usage carries a run id either, ALL of it reads as est_unattributed_usd_list.
# =================================================================================================
def _build_failed_runs_jobs(con: duckdb.DuckDBPyConnection) -> None:
    def failed_group(job_id, n, termination_code, day, dbus):
        for i in range(n):
            start = DT(day, i % 20)
            jrt(con, WS1, job_id, f"{job_id}_r{i:02d}", start, start + 10 * M,
                result_state="FAILED", run_type="JOB_RUN", trigger_type="PERIODIC",
                termination_code=termination_code, termination_type="INTERNAL_ERROR",
                setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
        usage(con, f"lf_u_{job_id}", workspace_id=WS1, sku_name="lf_JOBS_COMPUTE",
              usage_date_=D(day), usage_quantity=float(dbus), billing_origin_product="JOBS",
              usage_metadata=_usage_metadata(job_id=job_id))

    failed_group("lf_job_fr_a", 20, "CLUSTER_ERROR", 4, 40)
    failed_group("lf_job_fr_b", 12, "STORAGE_ACCESS_ERROR", 4, 120)
    failed_group("lf_job_fr_c", 4, "WORKSPACE_RUN_LIMIT_EXCEEDED", 4, 15)

    # lf_job_fr_mixed proves est_usd_list is a real priced figure (100 DBU on the PRICED
    # lf_DLT_CORE_COMPUTE SKU, registered above: effective_list rate 0.6/DBU -> 60.0) with NONE of
    # it carrying a run id, so est_unattributed_usd_list equals it exactly and the job still reads
    # NOT_ASSESSED no_run_id_in_billing under the rewritten P4-01-W2 query -- unlike lf_job_fr_a/b/c
    # above, whose lf_JOBS_COMPUTE usage is deliberately unpriced so est_usd_list is NULL there too.
    # 4 FAILED + 6 SUCCEEDED runs. termination_code FR_MIXED_ERROR is used, not lf_job_fr_a's
    # CLUSTER_ERROR (T-75B review fix): both are WS1/JOB_RUN/PERIODIC/FAILED on day 4, and
    # lakeflow_failed_runs's grain has no job_id, so sharing a code would silently merge this
    # group's 4 rows into lf_job_fr_a's 20 and break test_lakeflow_failed_runs's exact-count
    # assertions.
    for i in range(4):
        start = DT(4, i)
        jrt(con, WS1, "lf_job_fr_mixed", f"lf_job_fr_mixed_r{i:02d}", start, start + 10 * M,
            result_state="FAILED", run_type="JOB_RUN", trigger_type="PERIODIC",
            termination_code="FR_MIXED_ERROR", termination_type="INTERNAL_ERROR")
    for i in range(6):
        start = DT(4, 4 + i)
        jrt(con, WS1, "lf_job_fr_mixed", f"lf_job_fr_mixed_s{i:02d}", start, start + 10 * M,
            result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="PERIODIC")
    usage(con, "lf_u_fr_mixed", workspace_id=WS1, sku_name="lf_DLT_CORE_COMPUTE",
          usage_date_=D(4), usage_quantity=100.0, billing_origin_product="JOBS",
          usage_metadata=_usage_metadata(job_id="lf_job_fr_mixed"))


# =================================================================================================
# SEC E -- lakeflow_job_queue_time (grain [workspace_id, job_id], warn_queue_p95_s=60,
# crit_queue_p95_s=300) and lakeflow_phase_cold_start (same grain, warn_setup_p95_s=60,
# crit_setup_p95_s=300 -- same numbers, so one set of 4 "legacy single-task" jobs (real, non-zero
# run-level setup/queue/execution/cleanup breakdown per the fixture facts) proves both ids at once:
# lf_job_phase_crit (queue=setup=350s, CRITICAL both), lf_job_phase_warn (150s, WARN both),
# lf_job_phase_ok (10s, OK both), lf_job_phase_null (all five duration columns NULL -> NOT_ASSESSED
# both -- proves the "not populated before Nov/Dec 2025" degrade path).
# lf_job_phase_zero (T-75B review fix): run_duration_seconds=300 (> 0, a REAL legacy single-task
# run, not a multi-task 0-sentinel) but setup=queue=0 on all 3 runs -- proves a GENUINE 0-second
# setup/queue is now kept as a real 0 (status OK, runs_queue_null=0/rows_setup_null=0), not folded
# to NULL the way a blind NULLIF(column, 0) would have.
# =================================================================================================
def _build_phase_jobs(con: duckdb.DuckDBPyConnection) -> None:
    def phase_group(job_id, seconds, day):
        for i in range(3):
            start = DT(day, 1 + i)
            end = start + H
            jrt(con, WS1, job_id, f"{job_id}_r{i}", start, end, result_state="SUCCEEDED",
                run_type="JOB_RUN", trigger_type="SCHEDULE",
                setup=seconds, queue=seconds, run_dur=(seconds * 3 if seconds is not None else None),
                cleanup=(seconds // 10 if seconds is not None else None),
                execution=(seconds * 2 if seconds is not None else None))
            # lakeflow_phase_cold_start now reads setup time from job_task_run_timeline
            # (task-level, real on multi-task jobs too, see its own header) -- mirror the
            # run-level setup value here so this single-task scenario still bands the same way.
            jtrt(con, WS1, job_id, f"{job_id}_r{i}", f"{job_id}_r{i}", "main", start, end,
                 result_state="SUCCEEDED", setup=seconds,
                 execution=(seconds * 2 if seconds is not None else None))

    phase_group("lf_job_phase_crit", 350, 5)
    phase_group("lf_job_phase_warn", 150, 5)
    phase_group("lf_job_phase_ok", 10, 5)
    phase_group("lf_job_phase_null", None, 5)

    for i in range(3):
        start = DT(5, 1 + i)
        end = start + H
        jrt(con, WS1, "lf_job_phase_zero", f"lf_job_phase_zero_r{i}", start, end,
            result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="SCHEDULE",
            setup=0, queue=0, run_dur=300, cleanup=0, execution=300)
        jtrt(con, WS1, "lf_job_phase_zero", f"lf_job_phase_zero_r{i}", f"lf_job_phase_zero_r{i}",
             "main", start, end, result_state="SUCCEEDED", setup=0, execution=300)


# =================================================================================================
# SEC E2 -- lakeflow_phase_cold_start's own multi-task fix. lf_job_phase_multitask: 3 runs, each
# with 2 tasks, whose RUN-level row folds all five phase columns to Databricks' documented
# multi-task 0-sentinel (setup=queue=run_dur=cleanup=execution=0, never assessable from
# job_run_timeline alone -- would read NOT_ASSESSED forever under the old run-level-only query).
# Every task row instead carries a real, non-zero setup_duration_seconds=320 (>= crit_setup_p95_s
# 300) -> setup_s_p95=320.0, status CRITICAL, proving job_task_run_timeline now gives a multi-task
# job a genuine cold-start read.
# =================================================================================================
def _build_phase_multitask(con: duckdb.DuckDBPyConnection) -> None:
    job_id = "lf_job_phase_multitask"
    for i in range(3):
        start = DT(5, 4 + i)
        end = start + H
        jrt(con, WS1, job_id, f"{job_id}_r{i}", start, end, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE",
            setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
        for task_key in ("task_a", "task_b"):
            jtrt(con, WS1, job_id, f"{job_id}_r{i}", f"{job_id}_r{i}", task_key, start, end,
                 result_state="SUCCEEDED", setup=320, execution=600)


# =================================================================================================
# SEC E3 -- lakeflow_phase_cold_start counts successful task runs only, and
# lakeflow_failed_cluster_starts lists the failed ones. lf_job_phase_failstart: 3 runs, each with a
# first task attempt that FAILED with CLOUD_FAILURE after 1,800 s of setup and no execution, then a
# retry that SUCCEEDED after 20 s of setup. The run itself SUCCEEDED, so run-level checks are unmoved.
# Cold start p95 = 20 s (OK); failed starts: 3 for CLOUD_FAILURE, 5,400 s of setup.
# =================================================================================================
def _build_phase_failstart(con: duckdb.DuckDBPyConnection) -> None:
    job_id = "lf_job_phase_failstart"
    for i in range(3):
        start = DT(5, 8 + i)
        end = start + H
        jrt(con, WS1, job_id, f"{job_id}_r{i}", start, end, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE",
            setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
        jtrt(con, WS1, job_id, f"{job_id}_r{i}_t0", f"{job_id}_r{i}", "main", start, start + timedelta(minutes=30),
             result_state="FAILED", termination_code="CLOUD_FAILURE", setup=1800, execution=0)
        jtrt(con, WS1, job_id, f"{job_id}_r{i}_t1", f"{job_id}_r{i}", "main", start + timedelta(minutes=30), end,
             result_state="SUCCEEDED", setup=20, execution=1500)


# =================================================================================================
# SEC F -- lakeflow_never_started_runs (grain [workspace_id, job_id, termination_code],
# warn=3, crit=10). period_start_time = period_end_time marks a never-started run.
# =================================================================================================
def _build_never_started_jobs(con: duckdb.DuckDBPyConnection) -> None:
    def never_group(job_id, n, termination_code, day):
        for i in range(n):
            ts = DT(day, i % 20)
            jrt(con, WS1, job_id, f"{job_id}_r{i:02d}", ts, ts, result_state="SKIPPED",
                run_type="JOB_RUN", trigger_type="PERIODIC", termination_code=termination_code,
                setup=0, queue=0, run_dur=0, cleanup=0, execution=0)

    never_group("lf_job_never_crit", 10, "MAX_JOB_QUEUE_SIZE_EXCEEDED", 6)
    never_group("lf_job_never_warn", 5, "WORKSPACE_RUN_LIMIT_EXCEEDED", 6)
    never_group("lf_job_never_ok", 1, "CLUSTER_ERROR", 6)


# =================================================================================================
# SEC G -- lakeflow_termination_type_probe (grain [workspace_id, termination_type], NO upper bound
# on period_end_time -- an AS_OF-day row survives) and its divergence from
# lakeflow_workload_mix_hours (bounds on period_start_time < date_trunc('DAY', now) -- the SAME
# AS_OF-day row is excluded there). lf_job_asof_general additionally proves the general
# period_end_time-bound family's exclusion (failed_runs / job_queue_time / phase_cold_start /
# never_started_runs / retries_repairs / failed_jobs_wasted_dbus all share that bound): its huge
# queue/setup durations and FAILED state would visibly change any of those ids' output if wrongly
# included.
# =================================================================================================
def _build_probe_and_general_asof(con: duckdb.DuckDBPyConnection) -> None:
    start = DT(0, 9, 0)
    end = DT(0, 9, 10)
    jrt(con, WS1, "lf_job_probe_asof", "lf_run_probe_asof", start, end, result_state="FAILED",
        run_type="JOB_RUN", trigger_type="PERIODIC", termination_code="PROBE_CODE",
        termination_type="PROBE_ASOF_SURVIVES", setup=0, queue=0, run_dur=0, cleanup=0, execution=0)

    g_start = DT(0, 9, 0)
    g_end = DT(0, 9, 5)
    jrt(con, WS1, "lf_job_asof_general", "lf_run_asof_general", g_start, g_end,
        result_state="FAILED", run_type="JOB_RUN", trigger_type="PERIODIC",
        termination_code="ASOF_EXCLUDED", termination_type="INTERNAL_ERROR",
        setup=999999, queue=999999, run_dur=999999, cleanup=999999, execution=999999)


# =================================================================================================
# SEC M -- one dedicated multi-period run (>1h, sliced hourly: 2 non-end NULL-state rows + 1 end
# row), proving the family's end-row-only filter on BOTH job_run_timeline and
# job_task_run_timeline. Not part of any status-band scenario (SUCCEEDED, harmless everywhere).
# =================================================================================================
def _build_multiperiod(con: duckdb.DuckDBPyConnection) -> None:
    r_start = DT(10, 6, 0)
    r_mid = r_start + 90 * M
    r_end = r_start + 3 * H
    jrt(con, WS1, "lf_job_multiperiod", "lf_run_multiperiod", r_start, r_mid)
    jrt(con, WS1, "lf_job_multiperiod", "lf_run_multiperiod", r_mid, r_end - 5 * M)
    # setup/queue/run_dur are deliberately small NON-zero values (T-75B): lakeflow_phase_cold_start
    # and lakeflow_job_queue_time now fold a literal 0 into NULL (NULLIF) to treat Databricks'
    # documented multi-task-job 0-sentinel as "not reported" rather than a real 0-second measurement
    # (see their own headers) -- a genuine 0 here would misreport this SUCCEEDED end row as
    # NOT_ASSESSED instead of the OK this scenario is testing. execution/cleanup stay 0: this same
    # end row is also lakeflow_workload_mix_hours's hardcoded execution_s_total==0.0 proof, and that
    # query does not fold 0 (its own separate, documented degrade path uses NULL only).
    jrt(con, WS1, "lf_job_multiperiod", "lf_run_multiperiod", r_end - 5 * M, r_end,
        result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="SCHEDULE",
        setup=5, queue=3, run_dur=int(3 * 3600), cleanup=0, execution=0)

    jtrt(con, WS1, "lf_job_multiperiod", "lf_run_multiperiod", "lf_run_multiperiod", "main",
         r_start, r_mid)
    jtrt(con, WS1, "lf_job_multiperiod", "lf_run_multiperiod", "lf_run_multiperiod", "main",
         r_mid, r_end - 5 * M)
    # setup=5 matches the run-level end row above (T-75B's same non-zero-sentinel reasoning): this
    # is lakeflow_phase_cold_start's own task-level source now (see its header), so its multi-period
    # OK/setup_s_p95==5.0 proof needs a real value here too, not the jtrt() default NULL.
    jtrt(con, WS1, "lf_job_multiperiod", "lf_run_multiperiod", "lf_run_multiperiod", "main",
         r_end - 5 * M, r_end, result_state="SUCCEEDED", setup=5, execution=int(3 * 3600))


# =================================================================================================
# SEC H -- lakeflow_tasks_near_timeout (grain [workspace_id, job_id, task_key],
# near_timeout_ratio=0.8, warn=3, crit=10). Combined count = SUM(near) + SUM(over); a run at/past
# the full timeout satisfies BOTH conditions, so tnt_crit/tnt_warn stay purely in the "near but not
# over" band (execution in [ratio*timeout, timeout)) to keep the arithmetic legible.
# =================================================================================================
def _build_tasks_near_timeout(con: duckdb.DuckDBPyConnection) -> None:
    job_id = "lf_job_tnt"

    def make_task(task_key, timeout_seconds):
        task_scd2(con, WS1, job_id, task_key, timeout_seconds=timeout_seconds)

    make_task("tnt_crit", 100)
    make_task("tnt_warn", 200)
    make_task("tnt_ok", 300)
    make_task("tnt_notimeout", None)
    make_task("tnt_execnull", 100)

    def task_run(task_key, i, exec_s, day=7):
        run_id = f"lf_run_{task_key}_{i:02d}"
        start = DT(day, 1 + (i % 20))
        jrt(con, WS1, job_id, run_id, start, start + 5 * M, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0, cleanup=0,
            execution=0)
        jtrt(con, WS1, job_id, run_id, run_id, task_key, start, start + 5 * M,
             result_state="SUCCEEDED", execution=exec_s)

    for i in range(12):
        task_run("tnt_crit", i, 90)
    for i in range(5):
        task_run("tnt_warn", i, 170)
    task_run("tnt_ok", 0, 50)
    for i in range(3):
        task_run("tnt_notimeout", i, 500)
    for i in range(2):
        task_run("tnt_execnull", i, None)


# =================================================================================================
# SEC I -- lakeflow_jobs_on_all_purpose (grain [workspace_id, job_id, compute_id],
# crit_share_usd=100, top_n=100000 (T-75B review fix; was 500)). DEC-21: this builder's own SCD2
# compute.clusters rows, `lf_` cluster ids, one UI/API source and one JOB source, each with >= 2
# change rows.
#   Cost is now attributed per (cluster, job) from usage_metadata.job_id (see the query's own
#   header), so each cluster below carries TWO usage rows: one with cluster_id only (a stand-in for
#   notebook/other-job use of the same shared cluster, which must now be EXCLUDED) and one with
#   BOTH cluster_id and job_id (this placement's own job, which must be the ONLY thing counted).
#   lf_cluster_interactive (cluster_source=UI): cluster-only usage 300 DBU (excluded); job usage
#     250 DBU * 0.6 = 150.0 -> est_usd_list_share=150.0 >= crit_share_usd=100 -> CRITICAL. (Were the
#     old whole-cluster figure used instead, it would be (300+250)*0.6=330.0, a different number --
#     proves the cluster-only row is genuinely excluded, not just smaller.)
#   lf_cluster_api (cluster_source=API): cluster-only usage 50 DBU (excluded); job usage 30 DBU *
#     0.6 = 18.0 -> share=18.0 < 100 -> WARN.
#   lf_cluster_job (cluster_source=JOB): OK regardless of DBUs (not UI/API).
#   lf_cluster_shared (cluster_source=UI): TWO jobs share it, proving "not an even split" --
#     lf_job_allpurpose_shared_a's own usage is 200 DBU (share=120.0 >= 100 -> CRITICAL),
#     lf_job_allpurpose_shared_b's own usage is 20 DBU (share=12.0 < 100 -> WARN). The old naive
#     even split would have divided the cluster's combined 220 DBU (*0.6=132.0) by
#     jobs_sharing_cluster=2 -> 66.0 for BOTH jobs (WARN for both), masking job_a's real $120 spend
#     entirely -- the two jobs' now-different bands are the proof.
#   lf_job_allpurpose_warehouse: a task end row whose compute_ids names `lf_warehouse_1`, a SQL
#     warehouse, not a cluster (never written to compute.clusters, exactly like
#     lf_job_allpurpose_unknown_cluster below) -- but this row's own `compute` struct marks
#     warehouse_id="lf_warehouse_1" (cluster_id NULL), so the finding leg now reads it as
#     cluster_source=WAREHOUSE, status OK, instead of falling into NOT_ASSESSED the way an actually
#     unknown cluster id still does.
#   lf_job_allpurpose_null: a task end row with compute_ids IS NULL -> the trailing NOT_ASSESSED
#     "dropped" summary row (the `dropped` UNION-ALL leg).
#   lf_job_allpurpose_empty: a task end row with compute_ids = [] (empty, non-NULL) -> T-75B review
#     fix: proves the SAME "dropped" summary row now also catches an empty array, which used to
#     vanish from every branch (EXPLODE() of an empty array yields zero rows).
#   lf_job_allpurpose_unknown_cluster: a task end row whose compute_ids names a cluster id
#     (`lf_cluster_missing`) that has NO row at all in compute.clusters AND no `compute` struct
#     naming it a warehouse either -> the finding leg's OWN `WHEN cluster_source IS NULL THEN
#     'NOT_ASSESSED'` branch (a SECOND, distinct NOT_ASSESSED path from the dropped-summary-row one
#     above: this row keeps its own job_id/compute_id, unlike the dropped row, which reports only a
#     workspace-level count). Added this review round (T-16 fix-list item 7) because every other
#     placement's compute_ids named a cluster this builder itself always writes, so this branch was
#     reachable in principle but never actually produced by this fixture -- T-18 owns the actual
#     assertion on it.
# =================================================================================================
def _build_all_purpose(con: duckdb.DuckDBPyConnection) -> None:
    cluster_scd2(con, WS1, "lf_cluster_interactive", "UI")
    cluster_scd2(con, WS1, "lf_cluster_api", "API")
    cluster_scd2(con, WS1, "lf_cluster_job", "JOB")
    cluster_scd2(con, WS1, "lf_cluster_shared", "UI")

    list_price(con, "lf_ALLPURPOSE_COMPUTE", 0.5, 0.6)
    # Cluster-only usage (no job_id) -- notebook/other-job use of the shared cluster, which the
    # fixed query must now exclude from every job's own $ figure.
    usage(con, "lf_u_cluster_interactive", workspace_id=WS1, sku_name="lf_ALLPURPOSE_COMPUTE",
          usage_date_=D(8), usage_quantity=300.0, billing_origin_product="ALL_PURPOSE",
          usage_metadata=_usage_metadata(cluster_id="lf_cluster_interactive"))
    usage(con, "lf_u_cluster_api", workspace_id=WS1, sku_name="lf_ALLPURPOSE_COMPUTE",
          usage_date_=D(8), usage_quantity=50.0, billing_origin_product="ALL_PURPOSE",
          usage_metadata=_usage_metadata(cluster_id="lf_cluster_api"))
    usage(con, "lf_u_cluster_job", workspace_id=WS1, sku_name="lf_ALLPURPOSE_COMPUTE",
          usage_date_=D(8), usage_quantity=999.0, billing_origin_product="ALL_PURPOSE",
          usage_metadata=_usage_metadata(cluster_id="lf_cluster_job"))
    # Job-tagged usage (cluster_id AND job_id both set) -- this placement's own metered $.
    usage(con, "lf_u_job_allpurpose_crit", workspace_id=WS1, sku_name="lf_ALLPURPOSE_COMPUTE",
          usage_date_=D(8), usage_quantity=250.0, billing_origin_product="ALL_PURPOSE",
          usage_metadata=_usage_metadata(cluster_id="lf_cluster_interactive", job_id="lf_job_allpurpose_crit"))
    usage(con, "lf_u_job_allpurpose_warn", workspace_id=WS1, sku_name="lf_ALLPURPOSE_COMPUTE",
          usage_date_=D(8), usage_quantity=30.0, billing_origin_product="ALL_PURPOSE",
          usage_metadata=_usage_metadata(cluster_id="lf_cluster_api", job_id="lf_job_allpurpose_warn"))
    # 300 DBU (bigger than lf_job_allpurpose_crit's own 250) on purpose: OK still sorts after
    # CRITICAL/WARN (worst-status-first, not a plain $-DESC sort) even with the largest $ of the
    # three named jobs -- same proof the old whole-cluster-bill fixture made with 999 DBU.
    usage(con, "lf_u_job_allpurpose_ok", workspace_id=WS1, sku_name="lf_ALLPURPOSE_COMPUTE",
          usage_date_=D(8), usage_quantity=300.0, billing_origin_product="ALL_PURPOSE",
          usage_metadata=_usage_metadata(cluster_id="lf_cluster_job", job_id="lf_job_allpurpose_ok"))
    usage(con, "lf_u_job_allpurpose_shared_a", workspace_id=WS1, sku_name="lf_ALLPURPOSE_COMPUTE",
          usage_date_=D(8), usage_quantity=200.0, billing_origin_product="ALL_PURPOSE",
          usage_metadata=_usage_metadata(cluster_id="lf_cluster_shared", job_id="lf_job_allpurpose_shared_a"))
    usage(con, "lf_u_job_allpurpose_shared_b", workspace_id=WS1, sku_name="lf_ALLPURPOSE_COMPUTE",
          usage_date_=D(8), usage_quantity=20.0, billing_origin_product="ALL_PURPOSE",
          usage_metadata=_usage_metadata(cluster_id="lf_cluster_shared", job_id="lf_job_allpurpose_shared_b"))

    def placement(job_id, run_id, compute_ids, day=8, compute=None):
        start = DT(day, 2)
        end = start + 30 * M
        jrt(con, WS1, job_id, run_id, start, end, result_state="SUCCEEDED", run_type="JOB_RUN",
            trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
        jtrt(con, WS1, job_id, run_id, run_id, "main", start, end, result_state="SUCCEEDED",
             compute_ids=compute_ids, compute=compute, execution=1800)

    placement("lf_job_allpurpose_crit", "lf_run_ap_crit", ["lf_cluster_interactive"])
    placement("lf_job_allpurpose_warn", "lf_run_ap_warn", ["lf_cluster_api"])
    placement("lf_job_allpurpose_ok", "lf_run_ap_ok", ["lf_cluster_job"])
    placement("lf_job_allpurpose_shared_a", "lf_run_ap_shared_a", ["lf_cluster_shared"])
    placement("lf_job_allpurpose_shared_b", "lf_run_ap_shared_b", ["lf_cluster_shared"])
    # lf_warehouse_1 is deliberately never written to compute.clusters (it is a SQL warehouse, not
    # a cluster) -- its own `compute` struct is what tells the query that, instead of the id
    # falling into NOT_ASSESSED the way lf_cluster_missing below still does.
    placement("lf_job_allpurpose_warehouse", "lf_run_ap_warehouse", ["lf_warehouse_1"],
              compute=[{"type": "SQL_WAREHOUSE", "cluster_id": None, "warehouse_id": "lf_warehouse_1"}])
    placement("lf_job_allpurpose_null", "lf_run_ap_null", None)
    # lf_cluster_missing is deliberately never written to compute.clusters -- see the SEC I comment.
    placement("lf_job_allpurpose_unknown_cluster", "lf_run_ap_unknown", ["lf_cluster_missing"])
    # T-75B review fix: an EMPTY (non-NULL) compute_ids array used to vanish from BOTH the finding
    # AND the dropped-summary branch (EXPLODE() of an empty array yields zero rows), so it was
    # invisible everywhere -- neither flagged, clean, nor counted as not-assessed. Proves the fixed
    # `dropped` CTE's `compute_ids IS NULL OR size(compute_ids) = 0` now catches this row too.
    placement("lf_job_allpurpose_empty", "lf_run_ap_empty", [])


# =================================================================================================
# SEC J -- lakeflow_stale_zombie_jobs (grain [workspace_id, job_id], stale_days=30,
# crit_stale_days=90, period_days=30 cost-lookback only). last_run CTE has NO window of its own
# (MAX(period_start_time) across all history), so these rows are placed freely outside the usual
# 30/90-day windows.
#   lf_job_zombie_crit: last run 100 days before AS_OF (> crit_stale_days=90) -> CRITICAL.
#   lf_job_zombie_warn_old: last run 45 days before AS_OF (> stale_days=30, < 90) -> WARN.
#   lf_job_zombie_warn_null: no job_run_timeline row at all -> last_run_start NULL -> WARN.
#   lf_job_zombie_ok: last run 5 days before AS_OF -> OK.
# Billing usage intentionally omitted for _crit and _warn_null (proves "a genuinely stale job
# correctly shows ~0 net_dbus" per the query's own caveat); added for _warn_old/_ok.
# =================================================================================================
def _build_zombie_jobs(con: duckdb.DuckDBPyConnection) -> None:
    job_scd2(con, WS1, "lf_job_zombie_crit", "zombie crit job")
    job_scd2(con, WS1, "lf_job_zombie_warn_old", "zombie warn-old job")
    job_scd2(con, WS1, "lf_job_zombie_warn_null", "zombie warn-null job")
    job_scd2(con, WS1, "lf_job_zombie_ok", "zombie ok job")

    crit_start = AS_OF - timedelta(days=100)
    jrt(con, WS1, "lf_job_zombie_crit", "lf_run_zombie_crit", crit_start, crit_start + 10 * M,
        result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="SCHEDULE",
        setup=0, queue=0, run_dur=0, cleanup=0, execution=0)

    warn_old_start = AS_OF - timedelta(days=45)
    jrt(con, WS1, "lf_job_zombie_warn_old", "lf_run_zombie_warn_old", warn_old_start,
        warn_old_start + 10 * M, result_state="SUCCEEDED", run_type="JOB_RUN",
        trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
    usage(con, "lf_u_zombie_warn_old", workspace_id=WS1, sku_name="lf_JOBS_COMPUTE",
          usage_date_=D(20), usage_quantity=8.0, billing_origin_product="JOBS",
          usage_metadata=_usage_metadata(job_id="lf_job_zombie_warn_old"))

    # lf_job_zombie_warn_null: no job_run_timeline row at all.

    ok_start = AS_OF - timedelta(days=5)
    jrt(con, WS1, "lf_job_zombie_ok", "lf_run_zombie_ok", ok_start, ok_start + 10 * M,
        result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="SCHEDULE",
        setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
    usage(con, "lf_u_zombie_ok", workspace_id=WS1, sku_name="lf_JOBS_COMPUTE",
          usage_date_=D(3), usage_quantity=8.0, billing_origin_product="JOBS",
          usage_metadata=_usage_metadata(job_id="lf_job_zombie_ok"))


# =================================================================================================
# SEC L -- lakeflow_retries_repairs (grain [workspace_id, job_id], warn_total_retries=5,
# crit_total_retries=20). Retries = (non-NULL-result_state rows per run_id) - 1, summed per job.
# One run_id per job carries all its "attempt" end rows.
# =================================================================================================
def _build_retries_jobs(con: duckdb.DuckDBPyConnection) -> None:
    def retries_group(job_id, n_attempts, day, dbus):
        run_id = f"{job_id}_run"
        for i in range(n_attempts):
            start = DT(day, 1) + i * 10 * M
            jrt(con, WS1, job_id, run_id, start, start + 5 * M, result_state="SUCCEEDED",
                run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
                cleanup=0, execution=0)
        usage(con, f"lf_u_{job_id}", workspace_id=WS1, sku_name="lf_JOBS_COMPUTE",
              usage_date_=D(day), usage_quantity=float(dbus), billing_origin_product="JOBS",
              usage_metadata=_usage_metadata(job_id=job_id))

    retries_group("lf_job_retries_crit", 21, 9, 30)
    retries_group("lf_job_retries_warn", 6, 9, 15)
    retries_group("lf_job_retries_ok", 2, 9, 5)


# =================================================================================================
# SEC P -- lakeflow_job_reliability (P3-JOBHEALTH; grain [workspace_id, job_id]). Defaults:
# min_runs=5, warn_failure_rate_pct=20, crit_consecutive_failures=3, last_n_runs=10. Own id
# namespace `lf_job_rel_` / `lf_run_rel_` (distinct from every other lf_job_* family above, DEC-15).
# Every run below sets setup=queue=run_dur=cleanup=execution=0 (this query reads neither, and 0
# is the file's usual "not a real duration reading" convention elsewhere) -- only result_state and
# timing matter here.
#   lf_job_rel_crit_streak: 5 runs oldest->newest SUCCEEDED, SUCCEEDED, FAILED, FAILED, FAILED ->
#     runs=5, failed_runs=3 (60%), consecutive_failures=3 (>= crit 3) -> CRITICAL (also >= the warn
#     rate, proving CRITICAL wins).
#   lf_job_rel_warn_rate: FAILED, SUCCEEDED, FAILED, SUCCEEDED, SUCCEEDED -> failed_runs=2 (40% >=
#     warn 20%), consecutive_failures=0 (latest run SUCCEEDED) -> WARN, not CRITICAL.
#   lf_job_rel_ok: 5x SUCCEEDED -> OK.
#   lf_job_rel_scarce: 2 runs (SUCCEEDED, FAILED), below min_runs=5, consecutive_failures=1 (< crit
#     3) -> NOT_ASSESSED, not_assessed_reason='too_few_runs'.
#   lf_job_rel_scarce_crit: 3 runs, all FAILED, below min_runs=5 but 3 consecutive failures (>= crit
#     3) -> CRITICAL: proves the precedence rule (a live failure streak is read before the
#     too-few-runs check, regardless of :min_runs).
#   lf_job_rel_repair_success: 4 plain SUCCEEDED runs + 1 run_id with two end-row attempts (FAILED
#     then, later, SUCCEEDED) -> runs=5 (the repaired run counts ONCE, on its final attempt),
#     failed_runs=0, runs_with_retry=1 -> OK. A buggy implementation that counted every attempt row
#     as its own run would read runs=6, failed_runs=1 instead.
#   lf_job_rel_repair_fail: 4 plain SUCCEEDED runs + 1 run_id with two attempts (FAILED, FAILED,
#     final result FAILED) -> runs=5, failed_runs=1 (NOT 2 -- the repeated attempt never double-
#     counts), rate=20% (>= warn 20) -> WARN.
#   lf_job_rel_asof: one SUCCEEDED run whose period_end_time lands on the AS_OF day (D0) -> excluded
#     entirely (general period_end_time < date_trunc bound), so this job_id never appears at any
#     window.
#   lf_job_rel_window: 3 FAILED runs at D(25) (inside the 30/90-day windows, outside the 7-day one)
#     plus 5 SUCCEEDED runs at D(3) (inside all three). At window=7: only the 5 SUCCEEDED runs count
#     -> OK. At window=30/90: runs=8, failed_runs=3 (37.5% >= warn 20), and the latest run (D(3), the
#     most recent) is SUCCEEDED so consecutive_failures=0 -> WARN, not CRITICAL.
#   lf_job_rel_manyruns: 2 FAILED runs at D(6) (oldest) + 10 SUCCEEDED runs at D(2) (most recent) ->
#     runs=12, failed_runs=2 (16.7% < warn 20) -> OK; last_n_runs_considered=min(10,12)=10 and
#     last_n_failed=0, because the 2 failures sit OUTSIDE the most recent 10 runs -- proves
#     last_n_* differs from the job's whole-window totals.
#   lf_job_rel_streak_skip: 5 runs oldest->newest SUCCEEDED, FAILED, FAILED, CANCELLED, FAILED ->
#     consecutive_failures=3 (the CANCELLED run is stepped over, not treated as breaking the
#     streak) and CRITICAL (>= crit 3) -- review fix: a buggy "stop at the first non-failed run"
#     streak would read consecutive_failures=1 (the single latest FAILED run) and miss the CRITICAL.
# =================================================================================================
def _build_job_reliability(con: duckdb.DuckDBPyConnection) -> None:
    def run(job_id, i, day, hour, result_state):
        run_id = f"{job_id}_r{i:02d}"
        start = DT(day, hour)
        jrt(con, WS1, job_id, run_id, start, start + 5 * M, result_state=result_state,
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)

    def repaired_run(job_id, run_id, day, hour_start, states):
        for idx, state in enumerate(states):
            start = DT(day, hour_start + idx)
            jrt(con, WS1, job_id, run_id, start, start + 5 * M, result_state=state,
                run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
                cleanup=0, execution=0)

    for i, state in enumerate(["SUCCEEDED", "SUCCEEDED", "FAILED", "FAILED", "FAILED"]):
        run("lf_job_rel_crit_streak", i, 2, 1 + i, state)

    for i, state in enumerate(["FAILED", "SUCCEEDED", "FAILED", "SUCCEEDED", "SUCCEEDED"]):
        run("lf_job_rel_warn_rate", i, 2, 1 + i, state)

    for i in range(5):
        run("lf_job_rel_ok", i, 2, 1 + i, "SUCCEEDED")

    run("lf_job_rel_scarce", 0, 2, 1, "SUCCEEDED")
    run("lf_job_rel_scarce", 1, 2, 2, "FAILED")

    for i in range(3):
        run("lf_job_rel_scarce_crit", i, 2, 1 + i, "FAILED")

    for i in range(4):
        run("lf_job_rel_repair_success", i, 3, 1 + i, "SUCCEEDED")
    repaired_run("lf_job_rel_repair_success", "lf_run_rel_rs_repaired", 3, 5, ["FAILED", "SUCCEEDED"])

    for i in range(4):
        run("lf_job_rel_repair_fail", i, 3, 1 + i, "SUCCEEDED")
    repaired_run("lf_job_rel_repair_fail", "lf_run_rel_rf_repaired", 3, 5, ["FAILED", "FAILED"])

    asof_start = DT(0, 9, 0)
    jrt(con, WS1, "lf_job_rel_asof", "lf_run_rel_asof", asof_start, asof_start + 5 * M,
        result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="SCHEDULE",
        setup=0, queue=0, run_dur=0, cleanup=0, execution=0)

    for i in range(3):
        run("lf_job_rel_window", i, 25, 1 + i, "FAILED")
    for i in range(5):
        run("lf_job_rel_window", 100 + i, 3, 1 + i, "SUCCEEDED")

    for i in range(2):
        run("lf_job_rel_manyruns", i, 6, 1 + i, "FAILED")
    for i in range(10):
        run("lf_job_rel_manyruns", 100 + i, 2, 1 + i, "SUCCEEDED")

    for i, state in enumerate(["SUCCEEDED", "FAILED", "FAILED", "CANCELLED", "FAILED"]):
        run("lf_job_rel_streak_skip", i, 2, 1 + i, state)


# =================================================================================================
# SEC Q -- lakeflow_job_duration_regression (P3-JOBHEALTH; grain [workspace_id, job_id]). Defaults:
# recent_days=7, min_runs_each_side=5, warn_slowdown_ratio=1.5, crit_slowdown_ratio=2.0. Own id
# namespace `lf_job_dur_` / `lf_run_dur_`. Every run sets run_dur=setup=queue=cleanup=execution=0
# so NULLIF(run_duration_seconds, 0) always folds to NULL and this query's own wall-clock fallback
# (period_start_time to period_end_time of the run's own final attempt) is what is measured -- the
# exact duration is then controlled purely by each row's own start/end timestamps. "baseline" runs
# sit at D(20) (inside the 30/90-day window, outside the default 7-day "recent" slice); "recent"
# runs sit at D(2) (inside the recent slice at every window that has one).
#   lf_job_dur_crit: baseline 5x600s, recent 5x1500s -> ratio 2.5 (>= crit 2.0) -> CRITICAL.
#   lf_job_dur_warn: baseline 5x600s, recent 5x960s -> ratio 1.6 (>= warn 1.5, < crit 2.0) -> WARN.
#   lf_job_dur_ok: baseline 5x600s, recent 5x480s (FASTER, ratio 0.8) -> OK: a job that got faster
#     is never flagged.
#   lf_job_dur_scarce_baseline: baseline 2x600s (< min_runs_each_side 5), recent 5x600s ->
#     NOT_ASSESSED, not_assessed_reason='too_few_baseline_runs'.
#   lf_job_dur_scarce_recent: baseline 5x600s, recent 2x600s -> NOT_ASSESSED,
#     not_assessed_reason='too_few_recent_runs'.
#   lf_job_dur_scarce_both: baseline 1x600s, recent 1x600s -> NOT_ASSESSED,
#     not_assessed_reason='too_few_runs_both_sides'.
#   lf_job_dur_repair: baseline 5 plain SUCCEEDED runs @600s + one run_id with two SUCCEEDED
#     attempts -- the early (abandoned) one is 9999s (2h46m39s), sliced HOURLY the way the real
#     table shapes a run over ~1h (2 NULL-result_state rows + 1 end row), the LATER, final one a
#     single 600s row -> baseline_runs=6 (not 7: the repeated attempt counts once, on its FINAL
#     attempt only), baseline_runs_repaired=1, baseline_median_minutes=10.0 (600s/60 -- proves the
#     median used the final attempt's own 600s, never the abandoned first attempt's 9999s, and
#     never double-counted the run_id). recent 5x1500s -> ratio 2.5 -> CRITICAL.
#   lf_job_dur_slice: review fix -- proves a MEASURED attempt over ~1h is pooled across its own
#     hourly slices, not read off its last slice alone. baseline 5 runs of 2h each (row1 0-1h
#     NULL-state, row2 1-2h the end row, run_dur=0 so the wall-clock fallback is exercised), recent
#     5 runs of 5h each (4 NULL-state hourly rows + 1 end row) -> baseline_median_minutes=120.0 (2h,
#     NOT the last slice's 60.0), recent_median_minutes=300.0 (5h, NOT 60.0) -> ratio 2.5 ->
#     CRITICAL. The pre-fix query (reading only the final slice's own start-to-end) would have read
#     both medians as 60.0 -- ratio 1.0 -> OK, hiding a real 2h -> 5h slowdown entirely.
#   lf_job_dur_win7: review fix -- proves the 7-day window no longer reads NOT_ASSESSED by
#     construction. baseline 5x600s spread across D(4..6), recent 5x1500s spread across D(1..3) ->
#     at window=7, the fixed recent/baseline split (LEAST(recent_days=7, FLOOR(7/2)=3) -> a 3-day
#     recent slice) puts D(1..3) in RECENT and D(4..6) in BASELINE -> ratio 2.5 -> CRITICAL, not
#     NOT_ASSESSED.
#   lf_job_dur_asof: one SUCCEEDED run whose period_end_time lands on the AS_OF day (D0) ->
#     excluded entirely, so this job_id never appears at any window.
# =================================================================================================
def _build_job_duration_regression(con: duckdb.DuckDBPyConnection) -> None:
    def run(job_id, i, day, hour, duration_s, result_state="SUCCEEDED"):
        run_id = f"{job_id}_r{i:02d}"
        start = DT(day, hour)
        end = start + timedelta(seconds=duration_s)
        jrt(con, WS1, job_id, run_id, start, end, result_state=result_state,
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)

    def scenario(job_id, baseline_s, recent_s, *, n_baseline=5, n_recent=5):
        for i in range(n_baseline):
            run(job_id, i, 20, 1 + i, baseline_s)
        for i in range(n_recent):
            run(job_id, 100 + i, 2, 1 + i, recent_s)

    def sliced_run(job_id, i, day, num_hours):
        # a run over ~1h is sliced hourly by the real table: num_hours-1 NULL-result_state rows,
        # then the attempt's own end row. Each run gets its own day so same-hour rows across runs
        # never collide. run_dur stays 0 throughout, same as every other row in this builder, so
        # the wall-clock fallback (pooled across every one of these rows) is what is measured.
        run_id = f"{job_id}_r{i:02d}"
        start = DT(day, 1)
        for h in range(num_hours - 1):
            seg_start = start + h * H
            jrt(con, WS1, job_id, run_id, seg_start, seg_start + H)
        seg_start = start + (num_hours - 1) * H
        jrt(con, WS1, job_id, run_id, seg_start, seg_start + H, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)

    scenario("lf_job_dur_crit", 600, 1500)
    scenario("lf_job_dur_warn", 600, 960)
    scenario("lf_job_dur_ok", 600, 480)
    scenario("lf_job_dur_scarce_baseline", 600, 600, n_baseline=2)
    scenario("lf_job_dur_scarce_recent", 600, 600, n_recent=2)
    scenario("lf_job_dur_scarce_both", 600, 600, n_baseline=1, n_recent=1)

    for i in range(5):
        run("lf_job_dur_repair", i, 20, 1 + i, 600)
    repair_run_id = "lf_run_dur_repair_repaired"
    # the early (abandoned) attempt: 9999s = 2h46m39s, sliced hourly like a real run past ~1h --
    # 2 NULL-result_state rows, then the attempt's own end row (review fix's fixture shape).
    early_start = DT(20, 6)
    jrt(con, WS1, "lf_job_dur_repair", repair_run_id, early_start, early_start + H)
    jrt(con, WS1, "lf_job_dur_repair", repair_run_id, early_start + H, early_start + 2 * H)
    jrt(con, WS1, "lf_job_dur_repair", repair_run_id, early_start + 2 * H,
        early_start + timedelta(seconds=9999), result_state="SUCCEEDED", run_type="JOB_RUN",
        trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
    final_start = DT(20, 15)
    jrt(con, WS1, "lf_job_dur_repair", repair_run_id, final_start, final_start + 600 * S,
        result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="SCHEDULE",
        setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
    for i in range(5):
        run("lf_job_dur_repair", 100 + i, 2, 1 + i, 1500)

    # lf_job_dur_slice: baseline runs of 2h each, recent runs of 5h each, both made of hourly
    # slices -- proves a measured attempt is pooled across its own slices, not read off the last
    # slice alone (review fix). Each run gets its own day (20..24 baseline, 2..6 recent).
    for i, day in enumerate([20, 21, 22, 23, 24]):
        sliced_run("lf_job_dur_slice", i, day, 2)
    for i, day in enumerate([2, 3, 4, 5, 6]):
        sliced_run("lf_job_dur_slice", 100 + i, day, 5)

    # lf_job_dur_win7: baseline runs at D(4..6), recent runs at D(1..3) -- proves the 7-day window
    # split (review fix) no longer reads NOT_ASSESSED by construction.
    for i, day in enumerate([6, 5, 5, 4, 4]):
        run("lf_job_dur_win7", i, day, 1 + i, 600)
    for i, day in enumerate([3, 2, 2, 1, 1]):
        run("lf_job_dur_win7", 100 + i, day, 1 + i, 1500)

    asof_start = DT(0, 9, 0)
    jrt(con, WS1, "lf_job_dur_asof", "lf_run_dur_asof", asof_start, asof_start + 5 * M,
        result_state="SUCCEEDED", run_type="JOB_RUN", trigger_type="SCHEDULE",
        setup=0, queue=0, run_dur=0, cleanup=0, execution=0)


# =================================================================================================
# SEC R -- lakeflow_job_run_cost / lakeflow_job_cost_summary (P3-JOBCOST; grains
# [workspace_id, job_id, job_run_id] / [workspace_id, job_id]). Own id namespace `lf_jrc_` (jobs and
# runs) / `lf_JRC_` (SKUs) / `lf_u_jrc_` (usage record ids) -- distinct from every other lf_job_* /
# lf_run_* / lf_u_* family above, DEC-15. Every usage row below carries BOTH
# usage_metadata.job_id AND job_run_id (the pair both queries require) except lf_u_jrc_orphan,
# which deliberately carries job_id but NO job_run_id -- the one row proving the "no job_run_id ->
# excluded from both queries" caveat. Two priced SKUs (lf_JRC_CLASSIC classic-only,
# lf_JRC_SERVERLESS serverless-only, product_features.is_serverless set to match), one deliberately
# UNpriced SKU (lf_JRC_UNPRICED, no list_price() call -- proves price_basis='unpriced', a real
# pricing-coverage gap) and one FREE_USAGE-named SKU (lf_JRC_FREE_USAGE, also no list_price() call
# -- proves price_basis='free', a genuine $0, never a gap). All usage lands on D(2)/D(3)/D(5)/D(20)/
# D(45)/D(0) (see per-job comments below), so every job is visible at window=7 unless its own
# comment says otherwise.
#   lf_jrc_job_mix: 3 runs proving all three compute_type values and the job-level median/max spread.
#     r1: 100 DBU lf_JRC_CLASSIC only -> net_run_dbus=100, net_list_cost=25.0 (100*0.25),
#       compute_type='classic', SUCCEEDED.
#     r2: 40 DBU lf_JRC_SERVERLESS only -> net_run_dbus=40, net_list_cost=24.0 (40*0.6),
#       compute_type='serverless', FAILED/JRC_TASK_ERROR.
#     r3: 20 DBU lf_JRC_CLASSIC + 10 DBU lf_JRC_SERVERLESS, same job_run_id -> net_run_dbus=30,
#       net_list_cost=11.0 (20*0.25 + 10*0.6), compute_type='mixed', SUCCEEDED.
#     Job totals: runs=3, net_job_dbus=170, net_list_cost=60.0, price_basis='priced';
#     median_run_dbus=40.0 (sorted 30/40/100), max_run_dbus=100.0; est_median_usd_list=24.0
#     (sorted 11.0/24.0/25.0), est_max_usd_list=25.0.
#   lf_jrc_job_unpriced: r1 50 DBU lf_JRC_CLASSIC (priced, cost=12.5), r2 20 DBU lf_JRC_UNPRICED
#     (net_list_cost NULL, price_basis='unpriced' on that run). Job totals: runs=2,
#     net_job_dbus=70, net_list_cost=12.5 (SUM ignores the NULL run), price_basis='unpriced'
#     (any run unpriced -> the job reads unpriced, even though most of it priced fine);
#     median_run_dbus=35.0 (interpolated between 20 and 50), max_run_dbus=50.0;
#     est_median_usd_list=12.5 and est_max_usd_list=12.5 (both skip the one NULL run).
#   lf_jrc_job_free: one run, 15 DBU on lf_JRC_FREE_USAGE (unregistered price, name matches
#     '%FREE_USAGE%') -> net_list_cost NULL, price_basis='free' (a real $0, never a gap -- the
#     opposite disclosure from lf_jrc_job_unpriced above despite both showing a NULL/low dollar
#     figure). Job totals: runs=1, net_job_dbus=15, net_list_cost NULL, price_basis='free'.
#   lf_jrc_job_orphan: r1 (real) 10 DBU lf_JRC_CLASSIC with job_run_id set -> counted normally
#     (net_run_dbus=10, net_list_cost=2.5). lf_u_jrc_orphan: SAME job_id, 500 DBU on
#     lf_JRC_CLASSIC, job_run_id=NULL (no jrt() row either) -- excluded from run_usage by
#     construction (both queries require job_run_id IS NOT NULL), so the job's total must read
#     10 DBU / $2.5, never 510 DBU -- proves the header's "a job_id-attributed row with no
#     job_run_id is excluded" caveat is not just documentation. Job totals: runs=1, net_job_dbus=10,
#     net_list_cost=2.5, price_basis='priced'.
#   lf_jrc_job_inflight: one run, 25 DBU lf_JRC_SERVERLESS (net_list_cost=15.0), and ONE
#     job_run_timeline row with result_state left NULL (no end row landed yet) -> run_start is that
#     row's own period_start_time, in_flight=TRUE, result_state/termination_code NULL,
#     compute_type='serverless'. Proves a genuinely-still-running run keeps its run_start.
#   lf_jrc_job_notimeline: one run, 12 DBU lf_JRC_CLASSIC (net_list_cost=3.0), and NO
#     job_run_timeline row AT ALL for that run_id -> run_start/result_state/termination_code all
#     NULL, in_flight=NULL/None (review must_fix #2: no timeline row at all means genuinely unknown,
#     never read as "running" -- distinct from lf_jrc_job_inflight's in_flight=TRUE), compute_type=
#     'classic'. Distinct from lf_jrc_job_inflight precisely in run_start too: SET there, NULL here.
#   lf_jrc_job_repair: one run repaired after an earlier attempt timed out -- TWO job_run_timeline
#     end rows share run_id lf_jrc_run_repair_r1: attempt 1 TIMED_OUT (hour 1), attempt 2 SUCCEEDED/
#     SUCCESS (hour 3, the later attempt). 8 DBU lf_JRC_CLASSIC -> net_run_dbus=8.0,
#     net_list_cost=2.0 (8*0.25), compute_type='classic'. Proves run_state reads the run's LAST
#     attempt only (result_state='SUCCEEDED', termination_code='SUCCESS', in_flight=False) --
#     never MAX(result_state)/MAX(termination_code), which would alphabetically read back
#     'TIMED_OUT' (T > S) despite the run having actually succeeded on repair.
#   lf_jrc_job_win: the window-boundary proof (bl_win_job's shape, lakeflow_job_run_cost's own
#     usage_date bound). Four runs, each its own job_run_id, all on lf_JRC_CLASSIC (priced):
#     D(5)=5 DBU/$1.25 (inside every window), D(20)=7 DBU/$1.75 (inside 30/90, outside 7),
#     D(45)=11 DBU/$2.75 (inside 90 only), D(0)=999 DBU/$249.75 (today -- usage_date <
#     current_date() must exclude it at EVERY window, the same AS_OF-day rule every cost query in
#     this repo follows). window=7: runs=1, net_job_dbus=5, net_list_cost=1.25. window=30:
#     runs=2, net_job_dbus=12, net_list_cost=3.0. window=90: runs=3, net_job_dbus=23,
#     net_list_cost=5.75. The D(0) run's job_run_id must never appear in
#     lakeflow_job_run_cost at any window.
# =================================================================================================
def _build_job_run_cost(con: duckdb.DuckDBPyConnection) -> None:
    list_price(con, "lf_JRC_CLASSIC", 0.20, 0.25)
    list_price(con, "lf_JRC_SERVERLESS", 0.50, 0.60)
    # lf_JRC_UNPRICED and lf_JRC_FREE_USAGE are deliberately never registered with list_price().

    def run_jrt(job_id, run_id, day, hour, minutes=10, **kw):
        start = DT(day, hour)
        jrt(con, WS1, job_id, run_id, start, start + minutes * M, **kw)

    # ---- lf_jrc_job_mix: classic / serverless / mixed on one job ----------------------------
    # Gate-fix isolation: every lf_jrc_job_* job below gets an explicit, real timeout_seconds so
    # it never leaks into lakeflow_jobs_no_timeout's WS1 scope (job_scd2/job_row default
    # timeout_seconds to NULL, and these priced job-run-cost rows would otherwise silently shift
    # that older, pinned-cost expectation) -- timeout policy is not what this scenario group is
    # testing. This is a job-level timeout only: it has no effect on f_lakeflow_job_tasks_no_timeout
    # (task-level system_lakeflow.job_tasks.timeout_seconds; these jobs write no job_tasks rows) or
    # on f_lakeflow_tasks_near_timeout. These 8 jobs (creator, run_as and health_rules all NULL)
    # deliberately still count toward WS1's other job-level findings -- identity-not-recorded
    # (f_lakeflow_job_ownership_orphans) and health-rules-null (f_lakeflow_health_rule_coverage) --
    # whose expected values are recomputed from the parquet, not pinned against this comment.
    job_scd2(con, WS1, "lf_jrc_job_mix", "job run cost mix", timeout_seconds=3600)
    usage(con, "lf_u_jrc_mix_r1", workspace_id=WS1, sku_name="lf_JRC_CLASSIC",
          usage_date_=D(2), usage_quantity=100.0, billing_origin_product="JOBS", hour=1,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_mix", job_run_id="lf_jrc_run_mix_r1"))
    run_jrt("lf_jrc_job_mix", "lf_jrc_run_mix_r1", 2, 1, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)

    usage(con, "lf_u_jrc_mix_r2", workspace_id=WS1, sku_name="lf_JRC_SERVERLESS",
          usage_date_=D(2), usage_quantity=40.0, billing_origin_product="JOBS", hour=2,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_mix", job_run_id="lf_jrc_run_mix_r2"),
          product_features=_product_features(is_serverless=True))
    run_jrt("lf_jrc_job_mix", "lf_jrc_run_mix_r2", 2, 2, result_state="FAILED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", termination_code="JRC_TASK_ERROR",
            termination_type="INTERNAL_ERROR", setup=0, queue=0, run_dur=0, cleanup=0, execution=0)

    usage(con, "lf_u_jrc_mix_r3a", workspace_id=WS1, sku_name="lf_JRC_CLASSIC",
          usage_date_=D(2), usage_quantity=20.0, billing_origin_product="JOBS", hour=3,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_mix", job_run_id="lf_jrc_run_mix_r3"))
    usage(con, "lf_u_jrc_mix_r3b", workspace_id=WS1, sku_name="lf_JRC_SERVERLESS",
          usage_date_=D(2), usage_quantity=10.0, billing_origin_product="JOBS", hour=3,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_mix", job_run_id="lf_jrc_run_mix_r3"),
          product_features=_product_features(is_serverless=True))
    run_jrt("lf_jrc_job_mix", "lf_jrc_run_mix_r3", 2, 3, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)

    # ---- lf_jrc_job_unpriced: one priced run + one genuinely-unpriced run ------------------
    job_scd2(con, WS1, "lf_jrc_job_unpriced", "job run cost unpriced", timeout_seconds=3600)
    usage(con, "lf_u_jrc_up_r1", workspace_id=WS1, sku_name="lf_JRC_CLASSIC",
          usage_date_=D(3), usage_quantity=50.0, billing_origin_product="JOBS", hour=1,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_unpriced", job_run_id="lf_jrc_run_up_r1"))
    run_jrt("lf_jrc_job_unpriced", "lf_jrc_run_up_r1", 3, 1, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)
    usage(con, "lf_u_jrc_up_r2", workspace_id=WS1, sku_name="lf_JRC_UNPRICED",
          usage_date_=D(3), usage_quantity=20.0, billing_origin_product="JOBS", hour=2,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_unpriced", job_run_id="lf_jrc_run_up_r2"))
    run_jrt("lf_jrc_job_unpriced", "lf_jrc_run_up_r2", 3, 2, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)

    # ---- lf_jrc_job_free: one run on a FREE_USAGE-named SKU (real $0, not a gap) -----------
    job_scd2(con, WS1, "lf_jrc_job_free", "job run cost free", timeout_seconds=3600)
    usage(con, "lf_u_jrc_free_r1", workspace_id=WS1, sku_name="lf_JRC_FREE_USAGE",
          usage_date_=D(3), usage_quantity=15.0, billing_origin_product="JOBS", hour=1,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_free", job_run_id="lf_jrc_run_free_r1"))
    run_jrt("lf_jrc_job_free", "lf_jrc_run_free_r1", 3, 1, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)

    # ---- lf_jrc_job_orphan: a real run + a job_run_id-less orphan row that must be excluded --
    job_scd2(con, WS1, "lf_jrc_job_orphan", "job run cost orphan", timeout_seconds=3600)
    usage(con, "lf_u_jrc_orphan_real", workspace_id=WS1, sku_name="lf_JRC_CLASSIC",
          usage_date_=D(3), usage_quantity=10.0, billing_origin_product="JOBS", hour=1,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_orphan", job_run_id="lf_jrc_run_orphan_r1"))
    run_jrt("lf_jrc_job_orphan", "lf_jrc_run_orphan_r1", 3, 1, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0,
            cleanup=0, execution=0)
    usage(con, "lf_u_jrc_orphan", workspace_id=WS1, sku_name="lf_JRC_CLASSIC",
          usage_date_=D(3), usage_quantity=500.0, billing_origin_product="JOBS", hour=5,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_orphan", job_run_id=None))

    # ---- lf_jrc_job_inflight: a run whose job_run_timeline row has no end row yet ----------
    job_scd2(con, WS1, "lf_jrc_job_inflight", "job run cost inflight", timeout_seconds=3600)
    usage(con, "lf_u_jrc_inflight_r1", workspace_id=WS1, sku_name="lf_JRC_SERVERLESS",
          usage_date_=D(3), usage_quantity=25.0, billing_origin_product="JOBS", hour=1,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_inflight", job_run_id="lf_jrc_run_inflight_r1"),
          product_features=_product_features(is_serverless=True))
    run_jrt("lf_jrc_job_inflight", "lf_jrc_run_inflight_r1", 3, 1)  # result_state defaults to NULL

    # ---- lf_jrc_job_notimeline: a run with usage but NO job_run_timeline row at all --------
    job_scd2(con, WS1, "lf_jrc_job_notimeline", "job run cost notimeline", timeout_seconds=3600)
    usage(con, "lf_u_jrc_notimeline_r1", workspace_id=WS1, sku_name="lf_JRC_CLASSIC",
          usage_date_=D(3), usage_quantity=12.0, billing_origin_product="JOBS", hour=1,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_notimeline", job_run_id="lf_jrc_run_notimeline_r1"))
    # deliberately no run_jrt() call for lf_jrc_run_notimeline_r1

    # ---- lf_jrc_job_repair: a run repaired after an earlier attempt timed out ---------------
    job_scd2(con, WS1, "lf_jrc_job_repair", "job run cost repair", timeout_seconds=3600)
    usage(con, "lf_u_jrc_repair_r1", workspace_id=WS1, sku_name="lf_JRC_CLASSIC",
          usage_date_=D(3), usage_quantity=8.0, billing_origin_product="JOBS", hour=1,
          usage_metadata=_usage_metadata(job_id="lf_jrc_job_repair", job_run_id="lf_jrc_run_repair_r1"))
    # attempt 1: TIMED_OUT at hour 1 -- an end row that lands, but is NOT the run's final outcome.
    run_jrt("lf_jrc_job_repair", "lf_jrc_run_repair_r1", 3, 1, result_state="TIMED_OUT",
            run_type="JOB_RUN", trigger_type="SCHEDULE", termination_code="JRC_RUN_EXECUTION_TIMEOUT",
            termination_type="INTERNAL_ERROR", setup=0, queue=0, run_dur=0, cleanup=0, execution=0)
    # attempt 2 (the repair): SUCCEEDED at hour 3, later period_end_time -> this is the outcome
    # run_state must return; a MAX(result_state)/MAX(termination_code) read would instead pick
    # 'TIMED_OUT' back (alphabetically after 'SUCCEEDED').
    run_jrt("lf_jrc_job_repair", "lf_jrc_run_repair_r1", 3, 3, result_state="SUCCEEDED",
            run_type="JOB_RUN", trigger_type="SCHEDULE", termination_code="SUCCESS",
            setup=0, queue=0, run_dur=0, cleanup=0, execution=0)

    # ---- lf_jrc_job_win: 7 / 30 / 90-day window boundary + AS_OF-day (D0) exclusion --------
    job_scd2(con, WS1, "lf_jrc_job_win", "job run cost window", timeout_seconds=3600)
    for day, run_id, qty in (
        (5, "lf_jrc_run_win_d5", 5.0),
        (20, "lf_jrc_run_win_d20", 7.0),
        (45, "lf_jrc_run_win_d45", 11.0),
        (0, "lf_jrc_run_win_d0", 999.0),
    ):
        usage(con, f"lf_u_jrc_win_{run_id}", workspace_id=WS1, sku_name="lf_JRC_CLASSIC",
              usage_date_=D(day), usage_quantity=qty, billing_origin_product="JOBS",
              usage_metadata=_usage_metadata(job_id="lf_jrc_job_win", job_run_id=run_id))
        run_jrt("lf_jrc_job_win", run_id, day, 1, result_state="SUCCEEDED", run_type="JOB_RUN",
                trigger_type="SCHEDULE", setup=0, queue=0, run_dur=0, cleanup=0, execution=0)


# =================================================================================================
# SEC N -- per-job C3 ids driven purely from the jobs/job_tasks dimension: lakeflow_health_rule_coverage,
# lakeflow_job_ownership_orphans, lakeflow_jobs_no_timeout, lakeflow_job_tasks_no_timeout -- one row
# per job (or task) now, so a tag filter reaches them by the job's own tag; the three shared
# workspaces still give the three MIXES the task's DECIDE section asks for when a job's own rows are
# rolled back up per workspace: WS1 mostly flagged, WS2 partly flagged, WS3 clean, on all four
# simultaneously. Every
# `bad_job()` still has NO health rule and a NULL/0 timeout (unchanged from before -- that shape is
# what health_rule_coverage / jobs_no_timeout / job_tasks_no_timeout are tested on), and now ALSO
# reads as ownership-risk for free: its run_as_user_name (`svc_{ws}_{i}@example.com`) is not shaped
# like a service-principal application-id UUID, so lakeflow_job_ownership_orphans.sql's regex reads
# it as a HUMAN run-as (manual, not scheduled -- T-72 renamed the old "svc_" prefix's intent: it no
# longer stands for a real service principal, since a real one is a UUID, not an email-shaped
# string). Every `good_job()` here now defaults to a genuine service-principal run-as (an
# `_sp_uuid()` value) so it reads OK/healthy on ownership too, plus a handful of explicitly-named
# edge jobs per workspace prove the other T-72 paths: "clean" (creator == run_as, a HUMAN identity
# -- the exact pattern the old creator<>run_as mismatch rule scored healthy and this rewrite now
# flags), "redacted" ('__REDACTED__' creator normalises to unknown but is excluded from
# jobs_principal_missing -- identity_redacted -- so it only counts on its known HUMAN run_as, never
# as a missing/deleted principal), "nulldegrade" (BOTH identities NULL -> jobs_identity_not_recorded,
# excluded from the score -- an unpopulated row, not a missing principal -- and the fully-NULL
# "column not yet populated" degrade row for jobs_health_rules_null / jobs_creator_null /
# jobs_runas_null / jobs_timeout_null / tasks_timeout_null -- it never flips CRITICAL to
# NOT_ASSESSED since it is not the only job in its workspace), "sp_healthy" (a genuine
# service-principal run-as -> OK) and, WS1 only, "human_scheduled" (a HUMAN run-as job that also
# runs on a schedule -- the worse case, counted twice), "sp_scheduled_healthy" (a service-principal
# run-as job that ALSO runs on a schedule, proving a schedule alone never makes a service-principal
# run-as risky), "owner_missing" (creator NULL, run_as a known HUMAN -- exactly one identity
# recorded -> counts as jobs_principal_missing, and its known HUMAN run_as also counts it into
# jobs_runas_human_manual) and "human_paused" (a HUMAN run-as job on a PERIODIC trigger that is
# `paused=True` -> is_scheduled reads False, proving a paused trigger never counts as "scheduled").
# =================================================================================================
def _build_bulk_workspace_jobs(con: duckdb.DuckDBPyConnection) -> None:
    rule = [{"metric": "DURATION_SECONDS", "operator": "GREATER_THAN", "value": 3600}]

    def bad_job(ws, i):
        job_id = f"lf_job_bulk_{ws}_bad_{i:02d}"
        job_scd2(
            con, ws, job_id, f"bulk bad job {i}",
            creator_user_name=f"creator_{ws}_{i}@example.com",
            run_as_user_name=f"svc_{ws}_{i}@example.com",
            timeout_seconds=(0 if i % 11 == 0 else None),
            health_rules=[],
        )
        task_scd2(con, ws, job_id, "main", timeout_seconds=(0 if i % 11 == 0 else None))
        usage(con, f"lf_u_{job_id}", workspace_id=ws, sku_name="lf_BULK_COMPUTE",
              usage_date_=D(15), usage_quantity=5.0, billing_origin_product="JOBS",
              usage_metadata=_usage_metadata(job_id=job_id))

    def good_job(ws, name_suffix, *, creator=None, run_as=None, trigger_type=None,
                 paused=False, timeout=3600, rules=rule):
        job_id = f"lf_job_bulk_{ws}_good_{name_suffix}"
        job_scd2(
            con, ws, job_id, f"bulk good job {name_suffix}",
            creator_user_name=creator, run_as_user_name=run_as, trigger_type=trigger_type,
            paused=paused, timeout_seconds=timeout, health_rules=rules,
        )
        task_scd2(con, ws, job_id, "main", timeout_seconds=timeout)
        usage(con, f"lf_u_{job_id}", workspace_id=ws, sku_name="lf_BULK_COMPUTE",
              usage_date_=D(15), usage_quantity=5.0, billing_origin_product="JOBS",
              usage_metadata=_usage_metadata(job_id=job_id))

    # WS1 (acme-prod): this function alone writes 22 bad + 8 edge-case "good" jobs = 30 jobs/tasks.
    # But these four queries GROUP BY workspace_id alone, over the WHOLE lakeflow.jobs / job_tasks
    # table for that workspace -- not just this function's own ids -- and SEC J's 4 zombie jobs
    # (lf_job_zombie_crit/warn_old/warn_null/ok; no health_rules/creator_user_name/run_as_user_name/
    # timeout_seconds/trigger_type set, so they also read as jobs_identity_not_recorded for
    # ownership_orphans, neither identity ever having been recorded) also land on WS1's
    # lakeflow.jobs -- SEC H's lf_job_tnt writes only to job_tasks, never to lakeflow.jobs, so it
    # does not appear here at all. None of this is hard-coded against the built table -- every
    # count below is derived from the parquet instead, the way test_lakeflow_pipelines.py's own
    # inventory_tier test does (T-18 owns the actual assertions on these four ids); this comment
    # gives the reasoning, not a pinned literal.
    for i in range(22):
        bad_job(WS1, i)
    good_job(WS1, "clean", creator="owner1@example.com", run_as="owner1@example.com")
    good_job(WS1, "redacted", creator="__REDACTED__", run_as="owner2@example.com")
    good_job(WS1, "nulldegrade", creator=None, run_as=None, timeout=None, rules=None)
    good_job(WS1, "sp_healthy", creator="owner3@example.com", run_as=_sp_uuid(101))
    good_job(WS1, "sp_scheduled_healthy", creator="owner6@example.com", run_as=_sp_uuid(102),
             trigger_type="PERIODIC")
    good_job(WS1, "human_scheduled", creator="owner4@example.com", run_as="owner5@example.com",
             trigger_type="PERIODIC")
    good_job(WS1, "owner_missing", creator=None, run_as="owner7@example.com", rules=None)
    good_job(WS1, "human_paused", creator="owner8@example.com", run_as="owner8@example.com",
             trigger_type="PERIODIC", paused=True, rules=None)

    # WS2 (acme-dev): 7 bad + 4 good (3 human run-as + 1 service-principal run-as) = 11 active
    # jobs -> WARN on ownership (well below WS1's CRITICAL score, well above 0).
    for i in range(7):
        bad_job(WS2, i)
    for i in range(3):
        good_job(WS2, f"clean{i}", creator=f"owner_ws2_{i}@example.com", run_as=f"owner_ws2_{i}@example.com")
    good_job(WS2, "sp_healthy", creator="owner_ws2_sp@example.com", run_as=_sp_uuid(202))

    # WS3 (acme-uat): 5 good jobs, every one a genuine service-principal run-as (a real Databricks
    # service principal's run_as_user_name is its application-id UUID, never an email-shaped
    # string) -- healthy on ownership; no bad jobs -> OK on all four ids.
    for i in range(5):
        good_job(WS3, f"clean{i}", creator=f"owner_ws3_{i}@example.com", run_as=_sp_uuid(300 + i))

    # a job (and its one task) with NO timeout_seconds but a qualifying RUN_DURATION_SECONDS/
    # GREATER_THAN health rule instead -- must NOT count toward jobs_no_timeout/tasks_no_timeout
    # (a real time limit, just not via timeout_seconds), counted separately in
    # jobs_time_limit_via_health_rule/tasks_time_limit_via_health_rule. Kept in WS3 (currently 0
    # no-timeout jobs/tasks) so the fix is visible as "still 0" rather than changing a banded count.
    hr_rule = [{"metric": "RUN_DURATION_SECONDS", "operator": "GREATER_THAN", "value": 3600}]
    job_scd2(
        con, WS3, "lf_job_health_rule_limit", "health rule instead of timeout",
        creator_user_name="owner_ws3_health@example.com", run_as_user_name=_sp_uuid(399),
        timeout_seconds=None, health_rules=hr_rule,
    )
    task_scd2(con, WS3, "lf_job_health_rule_limit", "main", timeout_seconds=None, health_rules=hr_rule)
