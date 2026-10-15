"""tests/fixtures/chargeback_b.py -- the one builder for this file's three new chargeback views:
cost_chargeback_by_job (grain [workspace_id, job_id], windowed), cost_chargeback_by_cluster (grain
[workspace_id, cluster_kind, entity_id], windowed) and cost_chargeback_by_tag_value (grain
[tag_key, tag_value], windowed). Follows tests/fixtures/finops.py's / chargeback.py's
builder shape (module-level build(con), AS_OF from base.py, write_parquet re-exported for
build_fixtures.py, parameterized `?` INSERTs, one Python dict per STRUCT column) and fills
system.billing.usage / system.billing.list_prices plus system.lakeflow.jobs,
system.lakeflow.pipelines and system.compute.clusters for the two entity-naming queries.

Own id namespace, `cb2_` prefix throughout (tests/fixtures/chargeback.py already owns `cb_`).
Account-level usage (workspace_id always NULL, the DEC-15 amendment pattern finops.py's own
docstring explains): all three queries here have no product/sku/workspace filter of their own, so
a dedicated NEW workspace would join dims.dim_workspace and shift the pinned workspace universe
several existing tests enumerate (tests/test_api.py, tests/test_scope_disclosure.py) -- files this
builder may not edit. Account-level rows are invisible to those (they all filter
`WHERE workspace_id IS NOT NULL`), exactly like finops.py's own rows.

Because workspace_id is NULL here, the by_job/by_cluster queries' own dimension lookups
(system.lakeflow.jobs/pipelines, system.compute.clusters) must join on workspace_id with
`IS NOT DISTINCT FROM`, not a plain "=" (NULL = NULL is never TRUE in SQL) -- both query files
were written with that join, and this builder gives every jobs/pipelines/clusters row workspace_id
= NULL to match. One consequence: cost_chargeback_by_cluster's own workspace_name lookup
(system.access.workspaces_latest) is never exercised by this builder (no access.workspaces_latest
row anywhere has a NULL workspace_id, nor should one be added) -- workspace_name reads NULL for
every row here, which the cluster test file documents rather than works around.

------------------------------------------------------------------------------------------------
cost_chargeback_by_job / cost_chargeback_by_cluster (w=7 primary: current=[D7..D1], previous=
[D14..D8], the same window finops.py/chargeback.py use; SKU_A priced $1.00/DBU so usage_quantity
IS the dollar amount; DEC-64 account-wide coverage is already satisfied by finops.py's/
chargeback.py's own D(250)+ anchors, so this builder needs none of its own)
------------------------------------------------------------------------------------------------
JOB (usage_metadata.job_id set, usage_unit=DBU)
  cb2_j_warn        cur=130, prev=100 (+30.0%) -> WARN. Also a D0=9999 row that must NOT move
                     cur (today is excluded). Named via lakeflow.jobs ("Warn ETL").
  cb2_j_crit        cur=200, prev=100 (+100.0%) -> CRITICAL. Named ("Critical Job").
  cb2_j_floor       cur=15 (< :min_spend_usd=20), prev=5 -> OK regardless of the swing.
  cb2_j_new         cur=80, NO previous-window row at all -> eff_previous=0 -> CRITICAL
                     (brand-new spend), change_pct NULL.
  cb2_j_unpriced_cur    cur=40 qty on an unpriced (non-free) SKU only, prev=100 priced ->
                     NOT_ASSESSED (current_period_unpriced): current_cost's own SUM is NULL.
  cb2_j_unpriced_prev   cur=100 priced, prev=40 qty unpriced-only -> NOT_ASSESSED
                     (previous_period_unpriced), the mirror case.
  cb2_j_free        cur=999, prev=500, both on a FREE_USAGE-named SKU (never registered with a
                     price) -> a real $0 both sides, price_basis='free', OK.
  cb2_j_correction  cur = ORIGINAL 100 + RETRACTION -20 (net 80), prev=80 -> 0% -> OK, proves
                     record_type is never filtered.
  cb2_j_noname      cur=50, prev=40 (+25.0%, the WARN boundary, inclusive) -> WARN. NO
                     lakeflow.jobs row at all -> job_name/run_as/creator_user_name all NULL
                     (the SUBMIT_RUN-shaped gap this query's own caveats describe).
  cb2_j_partial     cur = 60 priced + 15 qty unpriced (same job), prev=60 priced -> 0% -> OK,
                     price_basis='unpriced': proves a PARTIALLY unpriced job still gets a real
                     verdict (current_cost is 60, not NULL) rather than NOT_ASSESSED.

CLUSTER (usage_metadata.cluster_id always set; entity_id/cluster_kind per the query's own split)
  cb2_cl_ap1               all_purpose (job_id AND dlt_pipeline_id both NULL): cur=130, prev=100
                            (+30.0%) -> WARN. Also a D0=9999 row (excluded). Named via
                            compute.clusters ("AP Warn Cluster" / owned_by "owner-ap@example.com").
  cb2_cl_jc1 (job_id, the  job_cluster (dlt_pipeline_id NULL, job_id set): cur=200, prev=100
  entity_id; cluster_id     (+100.0%) -> CRITICAL. Named via lakeflow.jobs ("JC Crit Job").
  cb2_cl_jc1_ephemeral is
  the throwaway cluster_id)
  cb2_cl_pipe1 (pipeline_id, pipeline (dlt_pipeline_id set, job_id NULL): cur=50, prev=48
  the entity_id)            (+4.2%) -> OK. Named via lakeflow.pipelines ("OK Pipeline").
  cb2_cl_pipe2 (dlt_pipeline pipeline PRECEDENCE case: dlt_pipeline_id AND job_id BOTH set on the
  _id AND job_id both set   same row -> must classify as 'pipeline' (entity_id=cb2_cl_pipe2), never
  on the same usage row)    'job_cluster' keyed by the job_id -- the fix the original draft's own
                            docstring calls out. cur=30, no previous -> CRITICAL (brand-new).
                            Named via lakeflow.pipelines ("Precedence Pipeline").
  cb2_cl_na (all_purpose)   current period entirely on SKU_UNPRICED (no list_prices row) -> NOT_
                            ASSESSED (current_period_unpriced). Previous period (D(10), 30.0) is
                            priced but never reached since the row is already NOT_ASSESSED.

------------------------------------------------------------------------------------------------
cost_chargeback_by_tag_value (own dedicated usage_unit 'cb2_TAGDBU', priced $1.00/unit via
SKU_TAG -- this query's totals are account-wide across every usage_unit, not scoped to this one,
so its own (untagged)/share_of_total_pct rows are checked against
tests/dbutil.py's priced_usage_total(), never a hand-derived whole-suite total)
------------------------------------------------------------------------------------------------
Two tag keys, 'cb2_team' (4 real values) and 'cb2_cc' (60 real values, to force the
:top_values_per_key=10 default into a real "(other)" rollup), plus one group of usage that carries
NEITHER key at all (cur=70, prev=50) -- every real key's own "(untagged)" bucket is (the account's
whole total) minus (that key's own present total), so it picks up BOTH the other key's tagged rows
AND the fully-untagged rows, not just the latter (see the query's own read_this: "even when the
resource carries other, unrelated tags").

  cb2_team_a   cur=130, prev=100 (+30.0%) -> WARN
  cb2_team_b   cur=200, prev=100 (+100.0%) -> CRITICAL
  cb2_team_c   cur=15 (< floor), prev=5 -> OK
  cb2_team_d   cur=40, no previous -> CRITICAL (brand-new)
  cb2_cc_000..059  cur = (60 - i) for i=0..59 (60 down to 1, strictly decreasing, no rank ties);
                   prev = same as cur for every i EXCEPT i=0 (prev=20, cur=60 -> +200% ->
                   CRITICAL); every other cc_i is 0% change -> OK (via the pct check or the floor).
                   Ranks 1-10 (cc_000..cc_009, amounts 60..51) stay individual rows; ranks 11-60
                   (cc_010..cc_059, amounts 50..1) roll into one (other) row, cur=prev=1275 (sum
                   1..50) -> OK.
  (untagged)   no custom_tags at all: cur=70, prev=50.

This fixture's own present totals: team=385 cur / 205 prev, cc=1830 cur / 1790 prev -- see
test_cost_chargeback_by_tag_value.py for how the (untagged) rows are checked against those against
the account's real (whole-suite) total.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()  # 2026-09-21 -- "today" on the pinned test target


def D(n: int) -> date:
    return D0 - timedelta(days=n)


ACCOUNT_ID = "cb2_acct"
WS = None  # account-level usage throughout (see module docstring).

SKU_A = "cb2_SKU_A"                 # priced $1.00/DBU
SKU_UNPRICED = "cb2_SKU_UNPRICED"   # deliberately no list_prices row (a real coverage gap)
SKU_FREE = "cb2_GENIE_FREE_USAGE"   # deliberately no list_prices row (name matches FREE_USAGE -- a real $0)
SKU_TAG = "cb2_SKU_TAG"             # priced $1.00/cb2_TAGDBU

TAG_UNIT = "cb2_TAGDBU"             # this builder's own dedicated usage_unit (DEC-24 isolation)


# ---------------------------------------------------------------------------------------------
# STRUCT defaults -- mirrors tests/fixtures/billing.py's / finops.py's own field lists exactly.
# ---------------------------------------------------------------------------------------------
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


def _usage_metadata(job_id=None, cluster_id=None, dlt_pipeline_id=None):
    return {
        "cluster_id": cluster_id, "job_id": job_id, "warehouse_id": None, "instance_pool_id": None,
        "node_type": None, "job_run_id": None, "notebook_id": None, "dlt_pipeline_id": dlt_pipeline_id,
        "endpoint_name": None, "endpoint_id": None, "dlt_update_id": None, "dlt_maintenance_id": None,
        "run_name": None, "job_name": None, "notebook_path": None, "central_clean_room_id": None,
        "source_region": None, "destination_region": None, "app_id": None, "app_name": None,
        "metastore_id": None, "private_endpoint_name": None, "storage_api_type": None,
        "budget_policy_id": None, "ai_runtime_pool_id": None, "catalog_id": None,
        "networking_client": None, "recipient_id": None, "usage_policy_id": None,
    }


# ---------------------------------------------------------------------------------------------
# Insert helpers -- one row per call, named-column INSERT.
# ---------------------------------------------------------------------------------------------
_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_seq = [0]


def _next_record_id() -> str:
    _seq[0] += 1
    return f"cb2_u_{_seq[0]:04d}"


def usage(
    con: duckdb.DuckDBPyConnection,
    *,
    sku_name: str,
    usage_date_: date,
    usage_quantity: float,
    usage_unit: str = "DBU",
    custom_tags: dict | None = None,
    job_id: str | None = None,
    cluster_id: str | None = None,
    dlt_pipeline_id: str | None = None,
    record_type: str = "ORIGINAL",
    hour: int = 0,
) -> None:
    """One system.billing.usage row, workspace_id = WS (None, account-level -- see module
    docstring)."""
    start = datetime.combine(usage_date_, datetime.min.time()) + timedelta(hours=hour)
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, WS, _next_record_id(), sku_name, "aws", start, end, usage_date_,
            custom_tags if custom_tags is not None else {},
            usage_unit, usage_quantity,
            _usage_metadata(job_id=job_id, cluster_id=cluster_id, dlt_pipeline_id=dlt_pipeline_id),
            _identity_metadata(), record_type, usage_date_, "ALL_PURPOSE",
            _product_features(), "COMPUTE_TIME",
        ],
    )


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def list_price(con: duckdb.DuckDBPyConnection, sku_name: str, usage_unit: str, effective_list_default: float) -> None:
    """One CURRENT (price_end_time NULL) list_prices row, price_start_time well before every
    usage row this builder writes (D(20) is the oldest)."""
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, AS_OF - timedelta(days=60), None, sku_name, "aws", "USD", usage_unit,
            {"default": effective_list_default, "promotional": {"default": None},
             "effective_list": {"default": effective_list_default}},
        ],
    )


_JOB_SQL = (
    "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, run_as, creator_user_name, change_time) "
    "VALUES (?,?,?,?,?,?)"
)
_PIPELINE_SQL = (
    "INSERT INTO lakeflow__pipelines (workspace_id, pipeline_id, name, run_as, change_time) "
    "VALUES (?,?,?,?,?)"
)
_CLUSTER_SQL = (
    "INSERT INTO compute__clusters (workspace_id, cluster_id, cluster_name, owned_by, change_time) "
    "VALUES (?,?,?,?,?)"
)


def lakeflow_job(con: duckdb.DuckDBPyConnection, job_id: str, name: str, run_as: str, creator: str) -> None:
    con.execute(_JOB_SQL, [WS, job_id, name, run_as, creator, AS_OF - timedelta(days=100)])


def lakeflow_pipeline(con: duckdb.DuckDBPyConnection, pipeline_id: str, name: str, run_as: str) -> None:
    con.execute(_PIPELINE_SQL, [WS, pipeline_id, name, run_as, AS_OF - timedelta(days=100)])


def compute_cluster(con: duckdb.DuckDBPyConnection, cluster_id: str, name: str, owned_by: str) -> None:
    con.execute(_CLUSTER_SQL, [WS, cluster_id, name, owned_by, AS_OF - timedelta(days=100)])


# ---------------------------------------------------------------------------------------------
# cost_chargeback_by_job
# ---------------------------------------------------------------------------------------------
def _build_by_job(con: duckdb.DuckDBPyConnection) -> None:
    lakeflow_job(con, "cb2_j_warn", "Warn ETL", "svc-warn@example.com", "alice")
    lakeflow_job(con, "cb2_j_crit", "Critical Job", "svc-crit@example.com", "bob")

    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=130.0, job_id="cb2_j_warn")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=100.0, job_id="cb2_j_warn")
    usage(con, sku_name=SKU_A, usage_date_=D0, usage_quantity=9999.0, job_id="cb2_j_warn")  # today, excluded

    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=200.0, job_id="cb2_j_crit")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=100.0, job_id="cb2_j_crit")

    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=15.0, job_id="cb2_j_floor")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=5.0, job_id="cb2_j_floor")

    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=80.0, job_id="cb2_j_new")

    usage(con, sku_name=SKU_UNPRICED, usage_date_=D(3), usage_quantity=40.0, job_id="cb2_j_unpriced_cur")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=100.0, job_id="cb2_j_unpriced_cur")

    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=100.0, job_id="cb2_j_unpriced_prev")
    usage(con, sku_name=SKU_UNPRICED, usage_date_=D(10), usage_quantity=40.0, job_id="cb2_j_unpriced_prev")

    usage(con, sku_name=SKU_FREE, usage_date_=D(3), usage_quantity=999.0, job_id="cb2_j_free")
    usage(con, sku_name=SKU_FREE, usage_date_=D(10), usage_quantity=500.0, job_id="cb2_j_free")

    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=100.0, job_id="cb2_j_correction")
    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=-20.0, job_id="cb2_j_correction",
          record_type="RETRACTION")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=80.0, job_id="cb2_j_correction")

    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=50.0, job_id="cb2_j_noname")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=40.0, job_id="cb2_j_noname")

    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=60.0, job_id="cb2_j_partial")
    usage(con, sku_name=SKU_UNPRICED, usage_date_=D(3), usage_quantity=15.0, job_id="cb2_j_partial")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=60.0, job_id="cb2_j_partial")


# ---------------------------------------------------------------------------------------------
# cost_chargeback_by_cluster
# ---------------------------------------------------------------------------------------------
def _build_by_cluster(con: duckdb.DuckDBPyConnection) -> None:
    compute_cluster(con, "cb2_cl_ap1", "AP Warn Cluster", "owner-ap@example.com")
    lakeflow_job(con, "cb2_cl_jc1", "JC Crit Job", "svc-jc@example.com", "carol")
    lakeflow_pipeline(con, "cb2_cl_pipe1", "OK Pipeline", "svc-pipe@example.com")
    lakeflow_pipeline(con, "cb2_cl_pipe2", "Precedence Pipeline", "svc-pipe2@example.com")

    # all_purpose: job_id and dlt_pipeline_id both NULL -> entity_id = cluster_id itself
    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=130.0, cluster_id="cb2_cl_ap1")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=100.0, cluster_id="cb2_cl_ap1")
    usage(con, sku_name=SKU_A, usage_date_=D0, usage_quantity=9999.0, cluster_id="cb2_cl_ap1")  # today, excluded

    # job_cluster: dlt_pipeline_id NULL, job_id set -> entity_id = job_id (rolled up to the job)
    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=200.0,
          cluster_id="cb2_cl_jc1_ephemeral", job_id="cb2_cl_jc1")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=100.0,
          cluster_id="cb2_cl_jc1_ephemeral", job_id="cb2_cl_jc1")

    # pipeline: dlt_pipeline_id set, job_id NULL -> entity_id = dlt_pipeline_id
    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=50.0,
          cluster_id="cb2_cl_pipe1_c", dlt_pipeline_id="cb2_cl_pipe1")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=48.0,
          cluster_id="cb2_cl_pipe1_c", dlt_pipeline_id="cb2_cl_pipe1")

    # precedence: dlt_pipeline_id AND job_id BOTH set on the same row -> must still classify as
    # 'pipeline' (entity_id=cb2_cl_pipe2), never 'job_cluster' keyed by the job_id.
    usage(con, sku_name=SKU_A, usage_date_=D(3), usage_quantity=30.0,
          cluster_id="cb2_cl_pipe2_c", dlt_pipeline_id="cb2_cl_pipe2",
          job_id="cb2_cl_pipe2_job_should_be_ignored")

    # all_purpose, current period fully unpriced (no list_prices row for SKU_UNPRICED) -> NOT_ASSESSED
    compute_cluster(con, "cb2_cl_na", "NA Cluster", "owner-na@example.com")
    usage(con, sku_name=SKU_UNPRICED, usage_date_=D(3), usage_quantity=40.0, cluster_id="cb2_cl_na")
    usage(con, sku_name=SKU_A, usage_date_=D(10), usage_quantity=30.0, cluster_id="cb2_cl_na")


# ---------------------------------------------------------------------------------------------
# cost_chargeback_by_tag_value
# ---------------------------------------------------------------------------------------------
def _build_by_tag_value(con: duckdb.DuckDBPyConnection) -> None:
    list_price(con, SKU_TAG, TAG_UNIT, 1.00)

    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(3), usage_quantity=130.0,
          custom_tags={"cb2_team": "cb2_team_a"})
    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(10), usage_quantity=100.0,
          custom_tags={"cb2_team": "cb2_team_a"})

    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(3), usage_quantity=200.0,
          custom_tags={"cb2_team": "cb2_team_b"})
    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(10), usage_quantity=100.0,
          custom_tags={"cb2_team": "cb2_team_b"})

    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(3), usage_quantity=15.0,
          custom_tags={"cb2_team": "cb2_team_c"})
    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(10), usage_quantity=5.0,
          custom_tags={"cb2_team": "cb2_team_c"})

    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(3), usage_quantity=40.0,
          custom_tags={"cb2_team": "cb2_team_d"})

    # 60 distinct 'cb2_cc' values, strictly decreasing current-period amounts (60 down to 1, no
    # rank ties) so ranks 1-50 (60..11) stay individual rows and ranks 51-60 (10..1) roll into
    # (other) (sum 55). cc_000 alone gets a previous-period row (20 -> CRITICAL, +200%); every
    # other cc_i's previous equals its current (0% change).
    for i in range(60):
        amount = float(60 - i)
        tag_value = f"cb2_cc_{i:03d}"
        usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(3), usage_quantity=amount,
              custom_tags={"cb2_cc": tag_value})
        prev_amount = 20.0 if i == 0 else amount
        usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(10), usage_quantity=prev_amount,
              custom_tags={"cb2_cc": tag_value})

    # fully untagged usage in the same unit -- flows into BOTH keys' own (untagged) bucket.
    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(3), usage_quantity=70.0,
          custom_tags={})
    usage(con, sku_name=SKU_TAG, usage_unit=TAG_UNIT, usage_date_=D(10), usage_quantity=50.0,
          custom_tags={})


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17): a module-level build(con), no registration list.
# ---------------------------------------------------------------------------------------------
def build(con: duckdb.DuckDBPyConnection) -> None:
    list_price(con, SKU_A, "DBU", 1.00)
    # SKU_UNPRICED and SKU_FREE deliberately carry no list_price row.
    _build_by_job(con)
    _build_by_cluster(con)
    _build_by_tag_value(con)
