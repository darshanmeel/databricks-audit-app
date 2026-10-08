"""tests/fixtures/compute.py -- batch B (T-14), the one builder for `system.compute.*`
(warehouse_events, node_timeline, clusters, warehouses, node_types, instance_pools,
instance_events), plus the `system.billing.usage` rows this batch's own resource ids need for
their cost rollups. Fills the 5 "B1 activity" query ids this task owns
(compute_warehouse_idle_gaps, compute_warehouse_autoscale_churn, sql_warehouse_events_activity,
compute_idle_node_ratio, node_timeline_utilization) and writes the warehouses / node_types /
instance_pools / instance_events rows the later B2 task (T-15, compute_snapshots) reads -- T-15
does not extend this file (it owns only its own grain/test files, per PLAN.md 7.2/7.3).

Per DEC-15 this builder reuses the three SHARED fixture workspaces (1111 acme-prod, 2222
acme-dev, 3333 acme-uat -- written by billing.py) and gives every id it writes (warehouse_id,
cluster_id, instance_pool_id, instance_id, node_type) the `cp_` prefix, disjoint from every other
builder's own prefix (dd_ drilldown, bl_ billing, qh_ query_history, gv_ governance, pt_ ports).

DECIDE resolution (task file's DECIDE block, PLAN 6.5 vs 7.2/7.3 conflict): DEC-15/DEC-20
(tasks/DECISIONS.md) already closes this -- "system.compute.instance_pools IS populated by
compute.py", superseding PLAN 6.5's "fixture ships instance_pools empty". This builder therefore
writes real instance_pools rows (option (a) of the task file's DECIDE prompt); the
Public-Preview/empty-fixture path PLAN 6.5 wanted is exercised elsewhere via T-25/T-33 scenario
artefacts (DEC-20), not by leaving this table empty. Recorded again in this file's Hand-off notes.

AS_OF = 2026-09-21 12:00:00 (base.AS_OF); dbt's `test` target pins audit_today() = DATE
'2026-09-21' and audit_now() = TIMESTAMP '2026-09-21 12:00:00' (dbt/macros/audit_time.sql) to the
same instant. Per this task's Inputs section, the 5 B1 query bodies do NOT share one window
shape: compute_warehouse_autoscale_churn / sql_warehouse_events_activity filter warehouse_events
with `event_time >= audit_now() - INTERVAL :period_days DAYS` (lower bound only, no upper bound --
"today" is never excluded); compute_warehouse_idle_gaps now covers the SAME [today -
:period_days, today) span as its own cost rollup, so "today" IS excluded there (state-segment
logic ported from compute_warehouse_idle_minutes); compute_idle_node_ratio / node_timeline_
utilization filter node_timeline with `start_time >= dateadd(DAY, -LEAST(:period_days, 90),
audit_today())` (also lower bound only). Only the `system.billing.usage` cost-rollup CTEs inside
idle_gaps / autoscale_churn / idle_node_ratio carry the current-day exclusion (`usage_date >= ...
AND usage_date < current_date()`). Five reusable time anchors (the same offsets query_history.py /
governance.py use) cover every window boundary named in PLAN.md 5.7 / this task's Inputs:

    D7    AS_OF - 3d    inside the 7d window (and 30d, 90d)
    D30   AS_OF - 16d   inside the 30d window, outside the 7d window
    D90   AS_OF - 68d   inside the 90d window, outside the 30d window
    DTODAY 2026-09-21 08:00:00   on AS_OF's own calendar day -- INCLUDED at every window for
                                  autoscale_churn/sql_warehouse_events_activity/node_timeline-based
                                  ids (no upper bound), EXCLUDED at every window for
                                  compute_warehouse_idle_gaps (bounded like billing.usage).
    DOLD  AS_OF - 143d  older than the 90d window -- excluded everywhere, even at window_days=90.

Scenario -> row map (warehouse_id / cluster_id prefixes; see build() below for literal values):

  compute_warehouse_idle_gaps (warn_idle_hours=4, crit_idle_hours=24; grain [warehouse_id])
    CRITICAL      cp_wh_idle_crit  (RUNNING -> next event 30h later; 30h >= 24h)
    WARN          cp_wh_idle_warn  (RUNNING -> next event 6h later; 4h <= 6h < 24h)
    NOT_ASSESSED  cp_wh_single     (one STARTING event, never RUNNING -> no running period)
    cost rollup (net_dbus/est_usd_list): cp_wh_idle_crit, billing.usage sku bl_DBSQL_COMPUTE,
      an in-window row (D(5), 40 DBU) and an AS_OF-date row (D(0), 999 DBU, must be excluded).

  compute_warehouse_autoscale_churn (min_observed_hours=1, warn=4/h, crit=10/h; grain [warehouse_id])
    CRITICAL      cp_wh_churn       (18 SCALED_UP/DOWN events over 1.5h -> 12 events/h; the span
                                      is comfortably above min_observed_hours=1, not pinned to it,
                                      so float jitter on the event timestamps can never flip this
                                      row to NOT_ASSESSED -- see review fix item 9)
    WARN          cp_wh_churn_warn  (9 events over 1.5h -> 6 events/h)
    OK            cp_wh_churn_ok    (3 events over 1.5h -> 2 events/h)
    NOT_ASSESSED  the window-boundary warehouses below (each a single event -> observed_hours=0)
    cost rollup: cp_wh_churn, sku bl_DBSQL_COMPUTE, D(5)=25 DBU in-window, D(0)=999 DBU excluded.

  sql_warehouse_events_activity (warn_stale_days=7, crit_stale_days=14; grain [warehouse_id,
  event_type]; status only on RUNNING/STARTING rows, tested at window_days=30)
    CRITICAL  cp_wh_stale_crit  (RUNNING 20 days before AS_OF; 14 <= 20 < 30)
    WARN      cp_wh_stale_warn  (STARTING 10 days before AS_OF; 7 <= 10 < 14)
    OK        cp_wh_stale_ok    (RUNNING 2 days before AS_OF; < 7)
    NOT_ASSESSED  any non-RUNNING/STARTING row, e.g. cp_wh_idle_crit's own STOPPED row.

  compute_idle_node_ratio (idle_cpu_pct=5, min_slices=60, warn=0.5, crit=0.8; grain [cluster_id])
    CRITICAL      cp_cl_idle   (100 slices, 95 idle (cpu<5%) -> idle_ratio 0.95; avg cpu ~2.9%)
    WARN          cp_cl_warn   (100 slices, 60 idle -> idle_ratio 0.6)
    OK            cp_cl_busy   (100 slices, all busy (cpu 60%) -> idle_ratio 0.0)
    NOT_ASSESSED  cp_cl_small  (10 slices, below :min_slices)
    cost rollup: cp_cl_idle/cp_cl_warn/cp_cl_busy each get an in-window (D(5)) and an AS_OF-date
      (D(0), excluded) billing.usage row, sku bl_ALL_PURPOSE_COMPUTE, so est_wasted_usd_list
      ranks CRITICAL > WARN > OK (worst-first, order_by est_wasted_usd_list DESC).

  node_timeline_utilization (min_slices=15, oversized_cpu_pct=20, oversized_mem_pct=30; grain
  [cluster_id, node_type, driver]; status is OK | WARN | NOT_ASSESSED only -- no CRITICAL branch)
    WARN          cp_cl_idle   (avg_cpu ~2.9% < 20 AND avg_mem 8% < 30)
    OK            cp_cl_busy   (avg_cpu 60% >= 20)
    NOT_ASSESSED  cp_cl_small  (10 minute_rows < 15)
    Every scenario cluster is written as a single-node cluster (one instance_id, driver=True) so
    the (cluster_id, node_type, driver) grouping this query uses lands the whole slice count in
    ONE group -- splitting slices across a driver/worker pair here would starve both groups below
    :min_slices and hide the WARN/OK bands this fixture is trying to prove.

  Window-boundary series (proves the lower-bound-only 7/30/90 filter on warehouse_events /
  node_timeline itself, per this task's Inputs -- "an event/slice at AS_OF-45d present at
  window_days=90, absent at 30 and 7"): 5 warehouses (cp_wh_win_t7/t30/t90/ttoday/told90), each
  one RUNNING event at its anchor, read by all three warehouse_events-based ids; 5 clusters
  (cp_cl_win_t7/t30/t90/ttoday/told90), each one node_timeline row at its anchor, read by both
  node_timeline-based ids.

Resources for T-15 (compute_snapshots, not tested here): `clusters` SCD2 (>=2 rows per key) for
cp_cl_idle/cp_cl_warn/cp_cl_busy/cp_cl_small, plus `cp_cl_deleted` (latest row carries a non-NULL
delete_time, so classic_clusters_config_current's `delete_time IS NULL` filter has something real
to drop -- review fix item 5); `warehouses` SCD2 (>=2 rows per key) for cp_wh_idle_crit/
cp_wh_idle_warn/cp_wh_churn, plus `cp_wh_deleted` (same deleted-latest-row shape, for
sql_warehouse_config_current); `node_types` reference rows for the two node types used above
(cp_node_type_a, cp_node_type_b); `instance_pools` SCD2 (>=2 rows per key) for four pools spanning
OK/WARN/CRITICAL/NOT_ASSESSED on instance_pools_idle_capacity's own warn_min_idle_instances=5 /
crit_min_idle_instances=20 defaults (cp_pool_ok=3, cp_pool_warn=10, cp_pool_crit=25,
cp_pool_na=NULL min_idle_instances -- review fix item 3), plus `cp_pool_deleted` (deleted-latest-
row shape, review fix item 5); a billing.usage row per pool at D(5) (in-window), D(0) (must be
excluded) and D(95) (excluded at every window, even 90) -- review fix item 6, which also makes
cp_pool_warn/cp_pool_crit's old and current SCD2 rows share one workspace_id instead of drifting
across builds; `instance_events` rows spread across all 5 window-boundary anchors and varying
state/event_type/availability_type so the query's 5-column grain (workspace_id, node_type,
availability_type, state, event_type) is actually exercised, not collapsed to two groups that
differ only by state -- review fix item 4.

Rows are inserted with parameterized `?` INSERTs, one Python dict per STRUCT column (duckdb 1.5.1
binds a dict to a STRUCT by field NAME) and `{}`/`[]` for empty MAP/ARRAY columns -- the
usage_metadata / identity_metadata / product_features struct-default helpers below are copied
from tests/fixtures/billing.py's own shape (this builder owns its own copy per DEC-15: "a builder
that needs rows in another builder's source table writes its own parquet file into that source
folder", independent of billing.py's code). Cloud-attribute STRUCT columns on clusters /
instance_pools (aws_attributes etc.) are left as whole-NULL structs (None) since no query in this
repo reads them.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()  # 2026-09-21 -- "today" on the pinned test target


def D(n: int):
    return D0 - timedelta(days=n)


ACCOUNT_ID = "cp_acct"
WS_PROD = "1111"
WS_DEV = "2222"
WS_UAT = "3333"

NODE_A = "cp_node_type_a"
NODE_B = "cp_node_type_b"

# Window-boundary anchors (same offsets tests/fixtures/query_history.py and governance.py use).
D7 = AS_OF - timedelta(days=3)
D30 = AS_OF - timedelta(days=16)
D90 = AS_OF - timedelta(days=68)
DTODAY = datetime(2026, 9, 21, 8, 0, 0)
DOLD = AS_OF - timedelta(days=143)


# ---------------------------------------------------------------------------------------------
# compute.warehouse_events
# ---------------------------------------------------------------------------------------------
_WE_SQL = (
    "INSERT INTO compute__warehouse_events "
    "(account_id, workspace_id, warehouse_id, event_type, cluster_count, event_time) "
    "VALUES (?,?,?,?,?,?)"
)


def _we(con, warehouse_id, event_type, event_time, *, workspace_id=WS_PROD, cluster_count=1):
    con.execute(_WE_SQL, [ACCOUNT_ID, workspace_id, warehouse_id, event_type, cluster_count, event_time])


# ---------------------------------------------------------------------------------------------
# compute.node_timeline
# ---------------------------------------------------------------------------------------------
_NT_SQL = (
    "INSERT INTO compute__node_timeline "
    "(account_id, workspace_id, cluster_id, instance_id, start_time, end_time, driver, "
    "cpu_user_percent, cpu_system_percent, cpu_wait_percent, mem_used_percent, mem_swap_percent, "
    "network_sent_bytes, network_received_bytes, disk_free_bytes_per_mount_point, node_type, "
    "private_ip) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _nt(con, cluster_id, instance_id, start_time, *, workspace_id=WS_PROD, driver=True,
        cpu_user=1.0, cpu_system=1.0, cpu_wait=0.0, mem_used=10.0, mem_swap=0.0,
        node_type=NODE_A):
    end_time = start_time + timedelta(minutes=1)
    con.execute(_NT_SQL, [
        ACCOUNT_ID, workspace_id, cluster_id, instance_id, start_time, end_time, driver,
        cpu_user, cpu_system, cpu_wait, mem_used, mem_swap, 1000, 1000, {}, node_type, "10.0.0.1",
    ])


def _bulk_nt(con, cluster_id, node_type, start, rows, *, workspace_id=WS_PROD):
    """`rows`: a list of (cpu_user, cpu_system, mem_used) tuples, one per consecutive minute from
    `start`, all written on ONE driver=True instance (single-node cluster shape) -- see module
    docstring on why node_timeline_utilization's scenario clusters are single-node."""
    instance_id = f"cp_inst_{cluster_id}"
    for i, (cpu_u, cpu_s, mem) in enumerate(rows):
        _nt(con, cluster_id, instance_id, start + timedelta(minutes=i), workspace_id=workspace_id,
            driver=True, cpu_user=cpu_u, cpu_system=cpu_s, mem_used=mem, node_type=node_type)


# ---------------------------------------------------------------------------------------------
# compute.clusters (SCD2)
# ---------------------------------------------------------------------------------------------
_CL_SQL = (
    "INSERT INTO compute__clusters "
    "(account_id, workspace_id, cluster_id, cluster_name, owned_by, create_time, delete_time, "
    "driver_node_type, worker_node_type, worker_count, min_autoscale_workers, "
    "max_autoscale_workers, auto_termination_minutes, enable_elastic_disk, tags, cluster_source, "
    "init_scripts, aws_attributes, azure_attributes, gcp_attributes, driver_instance_pool_id, "
    "worker_instance_pool_id, dbr_version, change_time, change_date, data_security_mode, "
    "policy_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _cluster(con, cluster_id, *, name, change_time, worker_count, driver_node_type,
             worker_node_type, auto_term, cluster_source, workspace_id=WS_PROD, delete_time=None):
    con.execute(_CL_SQL, [
        ACCOUNT_ID, workspace_id, cluster_id, name, "cp_owner@example.com",
        change_time - timedelta(days=1), delete_time, driver_node_type, worker_node_type,
        worker_count, None, None, auto_term, True, {}, cluster_source, [], None, None, None,
        None, None, "14.3.x-scala2.12", change_time, change_time.date(), "SINGLE_USER", None,
    ])


# ---------------------------------------------------------------------------------------------
# compute.warehouses (SCD2)
# ---------------------------------------------------------------------------------------------
_WH_SQL = (
    "INSERT INTO compute__warehouses "
    "(warehouse_id, workspace_id, account_id, warehouse_name, warehouse_type, "
    "warehouse_channel, warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, "
    "change_time, delete_time, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _warehouse(con, warehouse_id, *, name, change_time, size, min_clusters, max_clusters,
               auto_stop, workspace_id=WS_PROD, delete_time=None):
    con.execute(_WH_SQL, [
        warehouse_id, workspace_id, ACCOUNT_ID, name, "PRO", "CHANNEL_NAME_CURRENT", size,
        min_clusters, max_clusters, auto_stop, {}, change_time, delete_time,
        "cp_creator@example.com",
    ])


# ---------------------------------------------------------------------------------------------
# compute.node_types
# ---------------------------------------------------------------------------------------------
_NTYPE_SQL = (
    "INSERT INTO compute__node_types (account_id, node_type, core_count, memory_mb, gpu_count) "
    "VALUES (?,?,?,?,?)"
)


def _node_type(con, node_type, core_count, memory_mb, gpu_count=0):
    con.execute(_NTYPE_SQL, [ACCOUNT_ID, node_type, core_count, memory_mb, gpu_count])


# ---------------------------------------------------------------------------------------------
# compute.instance_pools (SCD2)
# ---------------------------------------------------------------------------------------------
_POOL_SQL = (
    "INSERT INTO compute__instance_pools "
    "(account_id, workspace_id, instance_pool_id, change_time, create_time, delete_time, "
    "instance_pool_name, tags, node_type, idle_instance_autotermination_minutes, "
    "min_idle_instances, max_capacity, enable_elastic_disk, disk_spec, preloaded_docker_images, "
    "preloaded_spark_version, aws_attributes, azure_attributes, gcp_attributes) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _pool(con, pool_id, *, name, change_time, create_time, min_idle, max_capacity, node_type,
          workspace_id=WS_PROD, idle_autoterm=10, delete_time=None):
    con.execute(_POOL_SQL, [
        ACCOUNT_ID, workspace_id, pool_id, change_time, create_time, delete_time, name, {},
        node_type, idle_autoterm, min_idle, max_capacity, True, None, [], None, None, None, None,
    ])


# ---------------------------------------------------------------------------------------------
# compute.instance_events
# ---------------------------------------------------------------------------------------------
_IE_SQL = (
    "INSERT INTO compute__instance_events "
    "(account_id, workspace_id, instance_id, event_time, event_type, instance_pool_id, "
    "cluster_id, node_type, state, availability_type) VALUES (?,?,?,?,?,?,?,?,?,?)"
)


def _instance_event(con, instance_id, event_time, event_type, state, *, workspace_id=WS_PROD,
                     instance_pool_id=None, cluster_id=None, node_type=NODE_A,
                     availability_type="ON_DEMAND"):
    con.execute(_IE_SQL, [
        ACCOUNT_ID, workspace_id, instance_id, event_time, event_type, instance_pool_id,
        cluster_id, node_type, state, availability_type,
    ])


# ---------------------------------------------------------------------------------------------
# billing.usage -- struct-default helpers copied from tests/fixtures/billing.py's own shape
# (this builder's own copy, per DEC-15/module docstring).
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


def _identity_metadata():
    return {"run_as": None, "created_by": None, "owned_by": None, "run_by": None}


def _product_features():
    return {
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


_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _usage(con, record_id, *, workspace_id, sku_name, usage_date_, usage_quantity,
           billing_origin_product, usage_metadata, usage_unit="DBU", usage_type="COMPUTE_TIME",
           cloud="aws"):
    start = datetime.combine(usage_date_, datetime.min.time())
    end = start + timedelta(hours=1)
    con.execute(_USAGE_SQL, [
        ACCOUNT_ID, workspace_id, record_id, sku_name, cloud, start, end, usage_date_, {},
        usage_unit, usage_quantity, usage_metadata, _identity_metadata(), "ORIGINAL", usage_date_,
        billing_origin_product, _product_features(), usage_type,
    ])


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17): a module-level build(con), no registration list.
# ---------------------------------------------------------------------------------------------
def build(con: duckdb.DuckDBPyConnection) -> None:
    # ---- node_types: reference dimension (2 rows, used by T-15) -----------------------------
    _node_type(con, NODE_A, core_count=4.0, memory_mb=16384, gpu_count=0)
    _node_type(con, NODE_B, core_count=16.0, memory_mb=65536, gpu_count=0)

    # =========================================================================================
    # SEC A -- compute_warehouse_idle_gaps status bands.
    # =========================================================================================
    # CRITICAL: 30h continuous RUNNING gap (>= crit_idle_hours=24h).
    e1 = AS_OF - timedelta(days=3)
    _we(con, "cp_wh_idle_crit", "RUNNING", e1)
    _we(con, "cp_wh_idle_crit", "STOPPED", e1 + timedelta(hours=30))

    # WARN: 6h continuous RUNNING gap (>= warn_idle_hours=4h, < crit_idle_hours=24h).
    e2 = AS_OF - timedelta(days=4)
    _we(con, "cp_wh_idle_warn", "RUNNING", e2)
    _we(con, "cp_wh_idle_warn", "STOPPED", e2 + timedelta(hours=6))

    # NOT_ASSESSED: one STARTING event only, never RUNNING -> no contiguous running period ->
    # max_running_gap_seconds NULL. Never reused as one of SEC C's staleness demo warehouses (see
    # module docstring).
    _we(con, "cp_wh_single", "STARTING", AS_OF - timedelta(days=5))

    # =========================================================================================
    # SEC B -- compute_warehouse_autoscale_churn status bands. Every span below is comfortably
    # clear of the min_observed_hours(1) NOT_ASSESSED boundary (1.5h, not 1.0h exactly) so no
    # amount of float jitter on the event timestamps can flip a row's status for an unrelated
    # reason (review fix item 9 -- the original fixture sat exactly on that boundary).
    # =========================================================================================
    # CRITICAL: 18 SCALED_UP/DOWN events over 1.5 observed hours -> 12 events/h >= crit(10).
    churn_base = AS_OF - timedelta(days=6)
    for i in range(18):
        event_type = "SCALED_UP" if i % 2 == 0 else "SCALED_DOWN"
        _we(con, "cp_wh_churn", event_type, churn_base + timedelta(minutes=90 * i / 17),
            cluster_count=1 + i % 4)

    # WARN: 9 events over 1.5h -> 6 events/h (4 <= 6 < 10).
    churn_warn_base = AS_OF - timedelta(days=7)
    for i in range(9):
        event_type = "SCALED_UP" if i % 2 == 0 else "SCALED_DOWN"
        _we(con, "cp_wh_churn_warn", event_type, churn_warn_base + timedelta(minutes=90 * i / 8),
            cluster_count=1 + i % 3)

    # OK: 3 events over 1.5h -> 2 events/h (< 4).
    churn_ok_base = AS_OF - timedelta(days=8)
    for i in range(3):
        event_type = "SCALED_UP" if i % 2 == 0 else "SCALED_DOWN"
        _we(con, "cp_wh_churn_ok", event_type, churn_ok_base + timedelta(minutes=90 * i / 2),
            cluster_count=1)

    # =========================================================================================
    # SEC C -- sql_warehouse_events_activity staleness bands (status only on RUNNING/STARTING
    # rows; tested at window_days=30). Each of these three is otherwise unused (never doubles as
    # an idle-gap/churn scenario warehouse -- see module docstring).
    # =========================================================================================
    _we(con, "cp_wh_stale_crit", "RUNNING", AS_OF - timedelta(days=20))   # 14 <= 20 < 30 -> CRITICAL
    _we(con, "cp_wh_stale_warn", "STARTING", AS_OF - timedelta(days=10))  # 7 <= 10 < 14 -> WARN
    _we(con, "cp_wh_stale_ok", "RUNNING", AS_OF - timedelta(days=2))      # < 7 -> OK

    # =========================================================================================
    # SEC D -- window-boundary series, warehouse_events (shared by idle_gaps / autoscale_churn /
    # sql_warehouse_events_activity, all lower-bound-only on event_time).
    # =========================================================================================
    for warehouse_id, ts in [
        ("cp_wh_win_t7", D7), ("cp_wh_win_t30", D30), ("cp_wh_win_t90", D90),
        ("cp_wh_win_ttoday", DTODAY), ("cp_wh_win_told90", DOLD),
    ]:
        _we(con, warehouse_id, "RUNNING", ts)

    # =========================================================================================
    # SEC E -- node_timeline scenarios: idle / warn / busy / below-min_slices clusters. Each
    # written single-node (one instance, driver=True) -- see _bulk_nt's docstring.
    # =========================================================================================
    idle_start = AS_OF - timedelta(days=2)
    # 95 idle slices (cpu<5%) + 5 busy slices -> idle_ratio 0.95 (CRITICAL); avg cpu ~2.9% (<5%,
    # matches the fixture fact "avg CPU below 5%"), avg mem 8% (<30%) -> WARN on
    # node_timeline_utilization.
    idle_rows = [(1.0, 1.0, 8.0)] * 95 + [(12.0, 8.0, 8.0)] * 5
    _bulk_nt(con, "cp_cl_idle", NODE_A, idle_start, idle_rows)

    warn_start = AS_OF - timedelta(days=2)
    # 60 idle + 40 busy -> idle_ratio 0.6 (WARN on compute_idle_node_ratio).
    warn_rows = [(1.0, 1.0, 15.0)] * 60 + [(15.0, 10.0, 15.0)] * 40
    _bulk_nt(con, "cp_cl_warn", NODE_A, warn_start, warn_rows)

    busy_start = AS_OF - timedelta(days=2)
    # 100 busy slices (cpu 60%) -> idle_ratio 0.0 (OK); avg cpu 60% >= oversized_cpu_pct(20) ->
    # OK on node_timeline_utilization regardless of mem.
    busy_rows = [(40.0, 20.0, 50.0)] * 100
    _bulk_nt(con, "cp_cl_busy", NODE_B, busy_start, busy_rows)

    small_start = AS_OF - timedelta(days=2)
    # 10 slices only, below both ids' :min_slices (60 and 15) -> NOT_ASSESSED on both.
    small_rows = [(50.0, 10.0, 20.0)] * 10
    _bulk_nt(con, "cp_cl_small", NODE_A, small_start, small_rows)

    # =========================================================================================
    # SEC F -- window-boundary series, node_timeline (shared by compute_idle_node_ratio /
    # node_timeline_utilization, both lower-bound-only on start_time, capped LEAST(w,90)).
    # =========================================================================================
    for cluster_id, ts in [
        ("cp_cl_win_t7", D7), ("cp_cl_win_t30", D30), ("cp_cl_win_t90", D90),
        ("cp_cl_win_ttoday", DTODAY), ("cp_cl_win_told90", DOLD),
    ]:
        _nt(con, cluster_id, f"cp_inst_{cluster_id}", ts, driver=True,
            cpu_user=10.0, cpu_system=5.0, mem_used=20.0, node_type=NODE_A)

    # =========================================================================================
    # SEC G -- clusters SCD2 config (>=2 rows per key; for T-15's classic_clusters_config_current).
    # =========================================================================================
    _cluster(con, "cp_cl_idle", name="cp-idle-cluster-old", change_time=AS_OF - timedelta(days=60),
              worker_count=2, driver_node_type=NODE_A, worker_node_type=NODE_A, auto_term=20,
              cluster_source="UI")
    _cluster(con, "cp_cl_idle", name="cp-idle-cluster", change_time=AS_OF - timedelta(days=10),
              worker_count=2, driver_node_type=NODE_A, worker_node_type=NODE_A, auto_term=30,
              cluster_source="UI")

    _cluster(con, "cp_cl_warn", name="cp-warn-cluster-old", change_time=AS_OF - timedelta(days=60),
              worker_count=2, driver_node_type=NODE_A, worker_node_type=NODE_A, auto_term=20,
              cluster_source="UI")
    _cluster(con, "cp_cl_warn", name="cp-warn-cluster", change_time=AS_OF - timedelta(days=10),
              worker_count=3, driver_node_type=NODE_A, worker_node_type=NODE_A, auto_term=30,
              cluster_source="API")

    _cluster(con, "cp_cl_busy", name="cp-busy-cluster-old", change_time=AS_OF - timedelta(days=60),
              worker_count=4, driver_node_type=NODE_B, worker_node_type=NODE_B, auto_term=20,
              cluster_source="JOB")
    _cluster(con, "cp_cl_busy", name="cp-busy-cluster", change_time=AS_OF - timedelta(days=10),
              worker_count=6, driver_node_type=NODE_B, worker_node_type=NODE_B, auto_term=45,
              cluster_source="JOB")

    _cluster(con, "cp_cl_small", name="cp-small-cluster-old", change_time=AS_OF - timedelta(days=60),
              worker_count=1, driver_node_type=NODE_A, worker_node_type=NODE_A, auto_term=15,
              cluster_source="UI")
    _cluster(con, "cp_cl_small", name="cp-small-cluster", change_time=AS_OF - timedelta(days=10),
              worker_count=1, driver_node_type=NODE_A, worker_node_type=NODE_A, auto_term=15,
              cluster_source="UI")

    # cp_cl_deleted: latest row carries a non-NULL delete_time, so
    # classic_clusters_config_current's `WHERE delete_time IS NULL` filter has a real row to drop
    # (review fix item 5 -- no SCD2 key anywhere previously exercised that filter).
    _cluster(con, "cp_cl_deleted", name="cp-deleted-cluster-old", change_time=AS_OF - timedelta(days=60),
              worker_count=2, driver_node_type=NODE_A, worker_node_type=NODE_A, auto_term=20,
              cluster_source="UI")
    _cluster(con, "cp_cl_deleted", name="cp-deleted-cluster", change_time=AS_OF - timedelta(days=10),
              worker_count=2, driver_node_type=NODE_A, worker_node_type=NODE_A, auto_term=20,
              cluster_source="UI", delete_time=AS_OF - timedelta(days=5))

    # =========================================================================================
    # SEC H -- warehouses SCD2 config (>=2 rows per key; for T-15's sql_warehouse_config_current).
    # =========================================================================================
    for warehouse_id, nm in [
        ("cp_wh_idle_crit", "cp-wh-crit"), ("cp_wh_idle_warn", "cp-wh-warn"),
        ("cp_wh_churn", "cp-wh-churn"),
    ]:
        _warehouse(con, warehouse_id, name=nm + "-old", change_time=AS_OF - timedelta(days=60),
                   size="SMALL", min_clusters=1, max_clusters=1, auto_stop=10)
        _warehouse(con, warehouse_id, name=nm, change_time=AS_OF - timedelta(days=10),
                   size="MEDIUM", min_clusters=1, max_clusters=4, auto_stop=30)

    # cp_wh_deleted: same deleted-latest-row shape as cp_cl_deleted, for
    # sql_warehouse_config_current's own `WHERE delete_time IS NULL` filter (review fix item 5).
    _warehouse(con, "cp_wh_deleted", name="cp-wh-deleted-old", change_time=AS_OF - timedelta(days=60),
               size="SMALL", min_clusters=1, max_clusters=1, auto_stop=10)
    _warehouse(con, "cp_wh_deleted", name="cp-wh-deleted", change_time=AS_OF - timedelta(days=10),
               size="SMALL", min_clusters=1, max_clusters=1, auto_stop=10,
               delete_time=AS_OF - timedelta(days=5))

    # =========================================================================================
    # SEC I -- instance_pools SCD2 (>=2 rows per key; for T-15's instance_pools_idle_capacity,
    # warn_min_idle_instances=5 / crit_min_idle_instances=20 defaults). Each pool's old and
    # current SCD2 rows now share one workspace_id (review fix item 6 -- cp_pool_warn/cp_pool_crit
    # previously drifted workspace between their two rows, which is harmless to the `rn = 1`
    # latest-row-wins query but was inconsistent fixture data).
    # =========================================================================================
    _pool(con, "cp_pool_ok", name="cp-pool-ok-old", change_time=AS_OF - timedelta(days=60),
          create_time=AS_OF - timedelta(days=200), min_idle=1, max_capacity=20, node_type=NODE_A,
          workspace_id=WS_PROD)
    _pool(con, "cp_pool_ok", name="cp-pool-ok", change_time=AS_OF - timedelta(days=10),
          create_time=AS_OF - timedelta(days=200), min_idle=3, max_capacity=50, node_type=NODE_A,
          workspace_id=WS_PROD)  # OK: min_idle_instances=3 < warn(5)

    _pool(con, "cp_pool_warn", name="cp-pool-warn-old", change_time=AS_OF - timedelta(days=60),
          create_time=AS_OF - timedelta(days=200), min_idle=1, max_capacity=20, node_type=NODE_A,
          workspace_id=WS_DEV)
    _pool(con, "cp_pool_warn", name="cp-pool-warn", change_time=AS_OF - timedelta(days=10),
          create_time=AS_OF - timedelta(days=200), min_idle=10, max_capacity=50, node_type=NODE_B,
          workspace_id=WS_DEV)  # WARN: 5 <= 10 < 20

    _pool(con, "cp_pool_crit", name="cp-pool-crit-old", change_time=AS_OF - timedelta(days=60),
          create_time=AS_OF - timedelta(days=200), min_idle=1, max_capacity=20, node_type=NODE_A,
          workspace_id=WS_UAT)
    _pool(con, "cp_pool_crit", name="cp-pool-crit", change_time=AS_OF - timedelta(days=10),
          create_time=AS_OF - timedelta(days=200), min_idle=25, max_capacity=80, node_type=NODE_B,
          workspace_id=WS_UAT)  # CRITICAL: 25 >= 20

    # cp_pool_na: current row's min_idle_instances is NULL -> instance_pools_idle_capacity's own
    # `WHEN p.min_idle_instances IS NULL` NOT_ASSESSED branch, unreachable before this fix (review
    # fix item 3 -- all three pools above always had a concrete min_idle_instances).
    _pool(con, "cp_pool_na", name="cp-pool-na-old", change_time=AS_OF - timedelta(days=60),
          create_time=AS_OF - timedelta(days=200), min_idle=1, max_capacity=20, node_type=NODE_A,
          workspace_id=WS_PROD)
    _pool(con, "cp_pool_na", name="cp-pool-na", change_time=AS_OF - timedelta(days=10),
          create_time=AS_OF - timedelta(days=200), min_idle=None, max_capacity=50, node_type=NODE_B,
          workspace_id=WS_PROD)  # NOT_ASSESSED: min_idle_instances IS NULL

    # cp_pool_deleted: deleted-latest-row shape (review fix item 5).
    _pool(con, "cp_pool_deleted", name="cp-pool-deleted-old", change_time=AS_OF - timedelta(days=60),
          create_time=AS_OF - timedelta(days=200), min_idle=2, max_capacity=20, node_type=NODE_A,
          workspace_id=WS_PROD)
    _pool(con, "cp_pool_deleted", name="cp-pool-deleted", change_time=AS_OF - timedelta(days=10),
          create_time=AS_OF - timedelta(days=200), min_idle=2, max_capacity=20, node_type=NODE_A,
          workspace_id=WS_PROD, delete_time=AS_OF - timedelta(days=5))

    # =========================================================================================
    # SEC J -- instance_events (for T-15's instance_events_idle_active). Spread across all 5
    # window-boundary anchors (the query's own filter is `event_time >= current_timestamp() -
    # INTERVAL :period_days DAYS`) and varying state / event_type / availability_type / node_type
    # so the query's 5-column grain (workspace_id, node_type, availability_type, state,
    # event_type) is actually exercised instead of collapsing to two groups that differ only by
    # state (review fix item 4 -- the original 6 rows were all at one anchor, one node_type, one
    # availability_type, one event_type).
    # =========================================================================================
    for suffix, ts in [("t7", D7), ("t30", D30), ("t90", D90), ("ttoday", DTODAY), ("told90", DOLD)]:
        _instance_event(con, f"cp_inst_ev_ready_{suffix}", ts, "STATE_TRANSITION",
                         "INSTANCE_READY", instance_pool_id="cp_pool_ok", node_type=NODE_A,
                         availability_type="ON_DEMAND")
        _instance_event(con, f"cp_inst_ev_placed_{suffix}", ts + timedelta(minutes=5),
                         "STATE_TRANSITION", "INSTANCE_PLACED", instance_pool_id="cp_pool_ok",
                         cluster_id="cp_cl_busy", node_type=NODE_A, availability_type="ON_DEMAND")
    # extra rows varying availability_type (SPOT) / event_type (INSTANCE_LAUNCHING) / node_type.
    _instance_event(con, "cp_inst_ev_spot_launch", D7, "INSTANCE_LAUNCHING", "INSTANCE_LAUNCHING",
                     instance_pool_id="cp_pool_ok", node_type=NODE_B, availability_type="SPOT")
    _instance_event(con, "cp_inst_ev_spot_ready", D7, "STATE_TRANSITION", "INSTANCE_READY",
                     instance_pool_id="cp_pool_ok", node_type=NODE_B, availability_type="SPOT")

    # =========================================================================================
    # SEC K -- billing.usage rows for the cost-rollup checks (idle_gaps, autoscale_churn,
    # idle_node_ratio). Each resource gets an in-window row (D(5), inside 7/30/90) and an
    # AS_OF-date row (D(0), must be excluded from net_dbus/est_usd_list at every window).
    # Reuses T-09's already-priced SKUs (bl_DBSQL_COMPUTE=0.22/DBU, bl_ALL_PURPOSE_COMPUTE=
    # 0.55/DBU current rate) rather than inventing new list_prices rows (Inputs section).
    # =========================================================================================
    _usage(con, "cp_u_wh_crit_5d", workspace_id=WS_PROD, sku_name="bl_DBSQL_COMPUTE",
           usage_date_=D(5), usage_quantity=40.0, billing_origin_product="SQL",
           usage_metadata=_usage_metadata(warehouse_id="cp_wh_idle_crit"))
    _usage(con, "cp_u_wh_crit_d0", workspace_id=WS_PROD, sku_name="bl_DBSQL_COMPUTE",
           usage_date_=D(0), usage_quantity=999.0, billing_origin_product="SQL",
           usage_metadata=_usage_metadata(warehouse_id="cp_wh_idle_crit"))

    _usage(con, "cp_u_wh_churn_5d", workspace_id=WS_PROD, sku_name="bl_DBSQL_COMPUTE",
           usage_date_=D(5), usage_quantity=25.0, billing_origin_product="SQL",
           usage_metadata=_usage_metadata(warehouse_id="cp_wh_churn"))
    _usage(con, "cp_u_wh_churn_d0", workspace_id=WS_PROD, sku_name="bl_DBSQL_COMPUTE",
           usage_date_=D(0), usage_quantity=999.0, billing_origin_product="SQL",
           usage_metadata=_usage_metadata(warehouse_id="cp_wh_churn"))

    _usage(con, "cp_u_cl_idle_5d", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(5), usage_quantity=15.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(cluster_id="cp_cl_idle"))
    _usage(con, "cp_u_cl_idle_d0", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(0), usage_quantity=999.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(cluster_id="cp_cl_idle"))

    _usage(con, "cp_u_cl_warn_5d", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(5), usage_quantity=5.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(cluster_id="cp_cl_warn"))
    _usage(con, "cp_u_cl_warn_d0", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(0), usage_quantity=999.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(cluster_id="cp_cl_warn"))

    _usage(con, "cp_u_cl_busy_5d", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(5), usage_quantity=20.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(cluster_id="cp_cl_busy"))
    _usage(con, "cp_u_cl_busy_d0", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(0), usage_quantity=999.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(cluster_id="cp_cl_busy"))

    # instance_pool usage (for T-15; not asserted here). Each pool gets an in-window row (D(5)),
    # an AS_OF-date row (D(0), must be excluded from instance_pools_idle_capacity's own
    # `usage_date < current_date()` rollup filter) and an out-of-window row (D(95), excluded at
    # every window, even 90) -- review fix item 6, which also matches each pool's usage
    # workspace_id to its SCD2 rows' workspace_id above.
    _usage(con, "cp_u_pool_ok_5d", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(5), usage_quantity=8.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_ok"))
    _usage(con, "cp_u_pool_ok_d0", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(0), usage_quantity=999.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_ok"))
    _usage(con, "cp_u_pool_ok_d95", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(95), usage_quantity=50.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_ok"))

    _usage(con, "cp_u_pool_warn_5d", workspace_id=WS_DEV, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(5), usage_quantity=12.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_warn"))
    _usage(con, "cp_u_pool_warn_d0", workspace_id=WS_DEV, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(0), usage_quantity=999.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_warn"))
    _usage(con, "cp_u_pool_warn_d95", workspace_id=WS_DEV, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(95), usage_quantity=50.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_warn"))

    _usage(con, "cp_u_pool_crit_5d", workspace_id=WS_UAT, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(5), usage_quantity=30.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_crit"))
    _usage(con, "cp_u_pool_crit_d0", workspace_id=WS_UAT, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(0), usage_quantity=999.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_crit"))
    _usage(con, "cp_u_pool_crit_d95", workspace_id=WS_UAT, sku_name="bl_ALL_PURPOSE_COMPUTE",
           usage_date_=D(95), usage_quantity=50.0, billing_origin_product="ALL_PURPOSE",
           usage_metadata=_usage_metadata(instance_pool_id="cp_pool_crit"))
