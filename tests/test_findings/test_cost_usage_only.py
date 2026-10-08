"""tests/test_findings/test_cost_usage_only.py -- T-12 (batch A1: the 14 usage-only cost queries
under app/queries/vendored/cost/, billed in native units straight off system.billing.usage, no
list_prices join). One test function per query id (14 total), each proving: grain uniqueness at
window_days=30 (config/grains/cost_usage_only.yml, DEC-24/DEC-48); the status enum and worst-first
ordering (via the model's own meta.order_by in dbt/models/findings/cost/_findings__cost.yml) for
the 4 finding ids (cost_by_compute_resource, cost_by_job, cost_by_notebook,
cost_usage_policy_coverage); at least one independently-computed total per id via
tests/dbutil.py's usage_sum() (or a raw-parquet re-derivation for an aggregate usage_sum() cannot
express, e.g. ABS()/COUNT(DISTINCT)) against tests/fixtures/parquet/billing__usage -- never a
hard-coded number; window behaviour at 7/30/90 plus the AS_OF-day exclusion; and
rows(id, 0) == [] (all 14 are windowed queries, none is a snapshot query). Per
tests/test_findings/README.md's checklist and this task's own Inputs/Steps.

tests/fixtures/billing.py (T-09) is read-only here -- imported as `bl` for its literal
ids/dates/workspace constants (the same style tests/test_findings/test_compute_activity.py uses
for tests/fixtures/compute.py), never modified. This module does not own it and does not touch it.

IMPORTANT, discovered empirically against the already-built tests/db_audit_test.duckdb before
writing these assertions (T-14/T-16/T-19/T-20/... builders share system.billing.usage with this
batch per PLAN.md 7.2, and DEC-15's three workspaces 1111/2222/3333 are shared by every builder):
several of these 14 ids' GROUP BY has no bl_-prefixed identifier column in its OUTPUT at all (e.g.
cost_by_billing_origin_product's (billing_origin_product, usage_unit, cloud), or
cost_restatement_trust_metric's (cloud) alone, or cost_chargeback_by_identity/
cost_usage_policy_coverage, whose grain columns are themselves masked/derived), so their built
rows are NOT exclusively billing.py's own data -- a hand-computed literal total for one of the
model's own output cells would be wrong there (verified: cost_by_billing_origin_product's
MODEL_SERVING/DBU/aws cell is 150.0 DBU over 11 records on the live build, not the 98.0 DBU over 5
records billing.py's own contributing rows alone sum to -- see test_cost_by_billing_origin_product
for the review-fixed version of this exact mistake). Every assertion below uses one of three
techniques, in order of preference: (a) scope the MODEL's own output row by a bl_-prefixed
identifier column that IS in its grain (job_id/cluster_id/warehouse_id/instance_pool_id/
notebook_id/endpoint_id/catalog_id/sku_name) -- collision-free per billing.py's own module
docstring, and the most pinpoint technique, since it also catches a wrong band threshold, a wrong
CASE branch or a wrong mask; (b) where no such grain column exists but an independent total is
still needed, scope a raw-parquet usage_sum()/_raw_scalar() call by record_id directly (also
collision-free) rather than by a broader combination of workspace_id/usage_date/other output
columns, since workspace_id is shared by every builder (DEC-15) and a combination of non-id output
columns can, and in review did, silently include another builder's row (see
test_cost_chargeback_by_tag, test_cost_chargeback_by_identity, test_cost_usage_policy_coverage);
(c) for the few ids whose own GROUP BY genuinely has no bl_-prefixed column in its grain
(cost_by_billing_origin_product, cost_restatement_trust_metric), independently re-derive the
model's own broader aggregate predicate straight from the raw billing__usage parquet via
usage_sum() (mirroring test_overview_spend_estimate.py's _spend_by_workspace and
test_compute_activity.py's _cost_rollup precedent) -- this matches the model's SUM exactly
regardless of how many other builders' rows land in that cell, because both sides compute the
identical aggregate over the identical source, but on its OWN it is a tautology on the aggregate
(it would still pass if a specific billing.py row were deleted) and must be paired with a
record_id-scoped positive check per (b) wherever the review flagged it as the only assertion.

Window-growth monotonicity (rows(id, 90) sum >= rows(id, 30) sum >= rows(id, 7) sum, Steps item d)
is asserted only for the ids whose SQL filters WHERE usage_unit = 'DBU' (a single, comparable
family): cost_by_compute_resource, cost_by_job, cost_by_notebook, cost_by_serving_endpoint. It is
skipped for every other id here, either because each mixes usage_unit families within one result
set (cost_by_billing_origin_product across all units; cost_chargeback_by_tag and
cost_totals_by_sku_day/cost_restatement_trust_metric have no usage_unit filter at all;
cost_genai_token_gpu mixes TOKEN and DBU; cost_networking_egress mixes BYTES and HOURS;
cost_default_storage_dsu's single fixture row makes any "growth" claim vacuous) or, for
cost_chargeback_by_identity/cost_premium_serverless_photon/cost_usage_policy_coverage (now also
filtered to usage_unit = 'DBU'), because the per-id tests below already prove the whole-result
total against the DBU-only raw total directly -- per this task's own Inputs section, which names
cost_premium_serverless_photon and cost_by_billing_origin_product as the pattern to skip. Where the
aggregate growth check is skipped, a scoped per-cell window-presence/window-value check is used
instead (e.g. cost_by_job's dedicated bl_win_job series at exactly 5.0 / 12.0 / 23.0 DBU for
window_days 7 / 30 / 90, straight from tests/fixtures/billing.py's own SEC K comment; the
cost_chargeback_by_tag untagged row and cost_totals_by_sku_day's correction cell present/absent at
the right windows).
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import billing as bl  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_usage_only.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}


def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


GRAINS = _grains()


def _win(days: int) -> str:
    """The exact WHERE-clause fragment every one of these 14 bodies applies:
    usage_date >= today - days AND usage_date < today, today = tests/fixtures/base.AS_OF's own
    date (bl.D0), never a hard-coded literal."""
    lower = (bl.D0 - timedelta(days=days)).isoformat()
    upper = bl.D0.isoformat()
    return f"usage_date >= DATE '{lower}' AND usage_date < DATE '{upper}'"


def _raw_scalar(select_expr: str, where: str):
    """Raw-parquet re-derivation for an aggregate tests/dbutil.py's fixed usage_sum() (always
    SUM(usage_quantity)) cannot express -- SUM(ABS(...)), COUNT(*), COUNT(DISTINCT ...) -- straight
    from tests/fixtures/parquet/billing__usage, the identical source usage_sum() and the dbt model
    itself both read."""
    pattern = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT {select_expr} FROM read_parquet('{pattern}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def _assert_unique_grain(qid: str, window: int = 30) -> list[dict]:
    cols = GRAINS[qid]
    out = dbutil.rows(qid, window)
    assert out, f"{qid}: expected >= 1 row at window_days={window}"
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days={window}"
    return out


def _assert_status_enum(rows_: list[dict]) -> None:
    for r in rows_:
        assert r["status"] in STATUS_VALUES, f"bad status {r['status']!r} in {r}"


def _one(rows_: list[dict], **where) -> dict:
    matches = [r for r in rows_ if all(r[k] == v for k, v in where.items())]
    assert len(matches) == 1, f"expected exactly 1 row matching {where}, got {len(matches)}: {matches}"
    return matches[0]


# -------------------------------------------------------------------------------------------
# cost_by_billing_origin_product -- inventory; grain [billing_origin_product, usage_unit, cloud,
# workspace_id]; order_by: billing_origin_product, usage_unit, cloud, workspace_id (ascending --
# not worst-first, there is no status column). No usage_unit filter -> mixed families -> window-
# growth check skipped.
# -------------------------------------------------------------------------------------------
def test_cost_by_billing_origin_product():
    qid = "cost_by_billing_origin_product"
    out = _assert_unique_grain(qid, 30)

    # order_by is a plain ascending tuple sort with no status column -- verify directly. DuckDB's
    # default for ASC is NULLS LAST, so a NULL billing_origin_product sorts after every real value.
    def _nulls_last(value):
        return (value is None, value)

    key = [(r["billing_origin_product"], r["usage_unit"], r["cloud"]) for r in out]
    assert key == sorted(key, key=lambda k: tuple(_nulls_last(v) for v in k)), (
        f"{qid}: rows not in declared order_by order"
    )

    # Cross-builder reconciliation: the MODEL_SERVING/DBU/aws cell is NOT exclusive to billing.py
    # (other builders also emit MODEL_SERVING DBU usage on the shared workspaces -- live cell is
    # 150.0 DBU / 11 records, not billing.py's own 98.0/5 alone), so this pair mirrors the model's
    # own WHERE/GROUP BY over the raw parquet and matches it exactly regardless of who else
    # contributes. On its own this is a tautology on the aggregate (it would still hold even if a
    # specific billing.py row here were deleted, since both sides recompute the same broader sum) --
    # the positive, per-row proof immediately below is the one that actually bites. The grain now
    # carries workspace_id too (one row per workspace on this cell, plus a NULL/account-level row),
    # so it takes summing the cell's own rows back together, never one row read as the whole cell.
    cell = [r for r in out if r["billing_origin_product"] == "MODEL_SERVING" and r["usage_unit"] == "DBU" and r["cloud"] == "aws"]
    assert cell, "expected at least one MODEL_SERVING/DBU/aws row"
    cell_net = sum(r["net_usage_quantity"] for r in cell)
    cell_count = sum(r["record_count"] for r in cell)
    where = f"billing_origin_product = 'MODEL_SERVING' AND usage_unit = 'DBU' AND cloud = 'aws' AND {_win(30)}"
    expected_net = dbutil.usage_sum(where)
    expected_count = _raw_scalar("COUNT(*)", where)
    assert abs(cell_net - expected_net) < 1e-6, (cell_net, expected_net)
    assert cell_count == expected_count, (cell_count, expected_count)

    # Positive proof (review fix, ship-blocker item 1): billing.py's five own record_ids that feed
    # this cell (SEC D's bl_u_ep_launch/ep_normal/genai_gpu/genai_answer, all MODEL_SERVING/DBU/aws
    # on D(4), plus SEC H's account-level bl_u_acct_level, also MODEL_SERVING/DBU/aws, on D(8)) sum
    # to exactly 98.0 DBU over exactly 5 records -- scoped by record_id (collision-free across
    # builders per billing.py's own module docstring), so deleting any one of them changes this
    # exact number, unlike the cross-builder mirror above which stays green either way.
    bl_ids = ("bl_u_ep_launch", "bl_u_ep_normal", "bl_u_genai_gpu", "bl_u_genai_answer", "bl_u_acct_level")
    id_list = ", ".join(f"'{i}'" for i in bl_ids)
    bl_net = _raw_scalar("SUM(usage_quantity)", f"record_id IN ({id_list})")
    bl_count = _raw_scalar("COUNT(*)", f"record_id IN ({id_list})")
    assert bl_net == 98.0, bl_net
    assert bl_count == 5, bl_count
    # and those five rows really are a subset of the cell the model's own row reports:
    assert bl_net <= expected_net and bl_count <= expected_count

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_by_compute_resource -- finding; grain [usage_date, cloud, workspace_id,
# billing_origin_product, cluster_id, warehouse_id, instance_pool_id, is_serverless];
# warn=50/crit=200 DBU/day; order_by: net_usage_quantity DESC. usage_unit='DBU' filtered ->
# window-growth check included.
#
# NOT_ASSESSED (the CASE's `SUM(usage_quantity) IS NULL` branch) is REACHABLE, not structurally
# impossible (review correction, should-fix item 5): SUM over a group whose values are all NULL
# returns NULL on both DuckDB and Spark, and tests/fixtures/ddl.py types usage_quantity as a
# plain nullable DOUBLE -- there is nothing in the schema or the query that rules this branch out.
# It is a FIXTURE GAP, not a structural non-issue: tests/fixtures/billing.py (T-09) has zero
# usage_quantity IS NULL rows today (confirmed against the live parquet: COUNT(*) WHERE
# usage_quantity IS NULL = 0), so no NOT_ASSESSED row exists for cost_by_compute_resource,
# cost_by_job or cost_by_notebook (all three share this identical CASE branch) on the current
# build. Closing it needs one usage() call in billing.py with usage_quantity=None on its own
# cluster_id/job_id/notebook_id -- out of scope here (tests/fixtures/billing.py is off-limits this
# round; billing.py is T-09's file, cost_usage_only.yml/test_cost_usage_only.py are T-12's) and
# recorded in tasks/T-12-cost-usage-tests.md's Hand-off notes for T-09/T-24 to pick up. Only
# WARN/CRITICAL/OK are asserted below for all three ids as a result.
# -------------------------------------------------------------------------------------------
def test_cost_by_compute_resource():
    qid = "cost_by_compute_resource"
    out = _assert_unique_grain(qid, 30)
    _assert_status_enum(out)

    crit = _one(out, cluster_id="bl_cluster_crit")
    assert crit["status"] == "CRITICAL"
    assert abs(crit["net_usage_quantity"] - 250.0) < 1e-6

    warn = _one(out, cluster_id="bl_cluster_warn")
    assert warn["status"] == "WARN"
    assert abs(warn["net_usage_quantity"] - 100.0) < 1e-6

    ok_wh = _one(out, warehouse_id="bl_wh_ok")
    assert ok_wh["status"] == "OK"
    assert abs(ok_wh["net_usage_quantity"] - 20.0) < 1e-6

    ok_pool = _one(out, instance_pool_id="bl_pool1")
    assert ok_pool["status"] == "OK"
    assert abs(ok_pool["net_usage_quantity"] - 15.0) < 1e-6

    # worst-first: order_by is net_usage_quantity DESC -- verify the relative order of these four
    # known rows within the full (unfiltered) returned list, not a re-sort by the test.
    seq = [r["status"] for r in out
           if r.get("cluster_id") in ("bl_cluster_crit", "bl_cluster_warn")
           or r.get("warehouse_id") == "bl_wh_ok" or r.get("instance_pool_id") == "bl_pool1"]
    assert seq == ["CRITICAL", "WARN", "OK", "OK"], seq

    # independent totals, scoped by the bl_-prefixed resource id (collision-free across builders
    # per tests/fixtures/billing.py's own module docstring).
    for col, rid, expected in (
        ("cluster_id", "bl_cluster_crit", 250.0),
        ("cluster_id", "bl_cluster_warn", 100.0),
        ("warehouse_id", "bl_wh_ok", 20.0),
        ("instance_pool_id", "bl_pool1", 15.0),
    ):
        where = f"usage_unit = 'DBU' AND usage_metadata.{col} = '{rid}' AND {_win(30)}"
        assert abs(dbutil.usage_sum(where) - expected) < 1e-6, (col, rid, expected)

    # window growth: usage_unit='DBU' is the only family this id emits -> total sum across all
    # returned rows must be non-decreasing as the window widens.
    sums = {w: sum(r["net_usage_quantity"] for r in dbutil.rows(qid, w)) for w in WINDOWS}
    assert sums[90] >= sums[30] >= sums[7], sums

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_by_job -- finding; grain [usage_date, cloud, workspace_id, billing_origin_product, job_id,
# is_serverless]; warn=50/crit=200 DBU/day; order_by: net_usage_quantity DESC; also emits
# distinct_runs = COUNT(DISTINCT job_run_id). SEC K's bl_win_job series pins the 7/30/90 window
# behaviour precisely (5.0 / 12.0 / 23.0 DBU) and the AS_OF-day exclusion (999.0 DBU on D0, always
# dropped).
#
# NOT_ASSESSED (SUM(usage_quantity) IS NULL) is reachable here too -- see
# test_cost_by_compute_resource's header comment above (same CASE branch, same fixture gap: zero
# usage_quantity IS NULL rows in tests/fixtures/billing.py today). Only WARN/CRITICAL/OK are
# asserted below.
# -------------------------------------------------------------------------------------------
def test_cost_by_job():
    qid = "cost_by_job"
    out = _assert_unique_grain(qid, 30)
    _assert_status_enum(out)

    crit = _one(out, job_id="bl_job_crit")
    assert crit["status"] == "CRITICAL"
    assert abs(crit["net_usage_quantity"] - 250.0) < 1e-6
    assert crit["distinct_runs"] == 2

    warn = _one(out, job_id="bl_job_warn")
    assert warn["status"] == "WARN"
    assert abs(warn["net_usage_quantity"] - 100.0) < 1e-6
    assert warn["distinct_runs"] == 1

    ok = _one(out, job_id="bl_job_ok")
    assert ok["status"] == "OK"
    assert abs(ok["net_usage_quantity"] - 20.0) < 1e-6

    # worst-first among these three known rows.
    seq = [r["status"] for r in out if r["job_id"] in ("bl_job_crit", "bl_job_warn", "bl_job_ok")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq

    # independent totals + distinct_runs, scoped by job_id.
    crit_where = f"usage_unit = 'DBU' AND usage_metadata.job_id = 'bl_job_crit' AND {_win(30)}"
    assert abs(dbutil.usage_sum(crit_where) - 250.0) < 1e-6
    assert _raw_scalar("COUNT(DISTINCT usage_metadata.job_run_id)", crit_where) == 2

    # SEC K window-boundary series: bl_win_job sums to 5.0 / 12.0 / 23.0 DBU at window_days
    # 7 / 30 / 90 respectively (D(5)=5, D(20)=7, D(45)=11; D(0)=999 always excluded).
    expected_by_window = {7: 5.0, 30: 12.0, 90: 23.0}
    for w, expected in expected_by_window.items():
        rows_w = [r for r in dbutil.rows(qid, w) if r["job_id"] == "bl_win_job"]
        actual = sum(r["net_usage_quantity"] for r in rows_w)
        assert abs(actual - expected) < 1e-6, (w, actual, expected)
        where_w = f"usage_unit = 'DBU' AND usage_metadata.job_id = 'bl_win_job' AND {_win(w)}"
        assert abs(dbutil.usage_sum(where_w) - expected) < 1e-6, (w, expected)

    # AS_OF-day exclusion anchor: the D0 row (999.0 DBU) genuinely exists in the raw parquet, but
    # is excluded from every window above -- an unwindowed sum must include it while the 90-day
    # window sum (23.0) does not, so a silently-deleted or silently-included D0 row would be
    # caught either way.
    incl_today = dbutil.usage_sum("usage_metadata.job_id = 'bl_win_job'")
    assert abs(incl_today - (23.0 + 999.0)) < 1e-6, incl_today

    # generic window growth (usage_unit='DBU' filtered -> single family).
    sums = {w: sum(r["net_usage_quantity"] for r in dbutil.rows(qid, w)) for w in WINDOWS}
    assert sums[90] >= sums[30] >= sums[7], sums

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_by_notebook -- finding; grain [usage_date, cloud, workspace_id, billing_origin_product,
# notebook_id, is_serverless]; warn=10/crit=50 DBU/day; order_by: net_usage_quantity DESC;
# empty_if: no_activity.
#
# NOT_ASSESSED (SUM(usage_quantity) IS NULL) is reachable here too -- see
# test_cost_by_compute_resource's header comment above (same CASE branch, same fixture gap: zero
# usage_quantity IS NULL rows in tests/fixtures/billing.py today). Only WARN/CRITICAL/OK are
# asserted below.
# -------------------------------------------------------------------------------------------
def test_cost_by_notebook():
    qid = "cost_by_notebook"
    out = _assert_unique_grain(qid, 30)
    _assert_status_enum(out)

    crit = _one(out, notebook_id="bl_nb_crit")
    assert crit["status"] == "CRITICAL"
    assert abs(crit["net_usage_quantity"] - 60.0) < 1e-6

    warn = _one(out, notebook_id="bl_nb_warn")
    assert warn["status"] == "WARN"
    assert abs(warn["net_usage_quantity"] - 25.0) < 1e-6

    ok = _one(out, notebook_id="bl_nb_ok")
    assert ok["status"] == "OK"
    assert abs(ok["net_usage_quantity"] - 5.0) < 1e-6

    seq = [r["status"] for r in out if r["notebook_id"] in ("bl_nb_crit", "bl_nb_warn", "bl_nb_ok")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq

    for nb_id, expected in (("bl_nb_crit", 60.0), ("bl_nb_warn", 25.0), ("bl_nb_ok", 5.0)):
        where = f"usage_unit = 'DBU' AND usage_metadata.notebook_id = '{nb_id}' AND {_win(30)}"
        assert abs(dbutil.usage_sum(where) - expected) < 1e-6, (nb_id, expected)

    sums = {w: sum(r["net_usage_quantity"] for r in dbutil.rows(qid, w)) for w in WINDOWS}
    assert sums[90] >= sums[30] >= sums[7], sums

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_by_serving_endpoint -- inventory; grain [usage_date, cloud, workspace_id,
# billing_origin_product, endpoint_id, endpoint_name, usage_type]; endpoint_name is a resource
# name and is never masked. order_by: usage_date DESC, workspace_id, endpoint_id
# (usage_type has no tie-break in the vendored ORDER BY, so only the date-group partition is
# asserted, not a full total order).
# -------------------------------------------------------------------------------------------
def test_cost_by_serving_endpoint():
    qid = "cost_by_serving_endpoint"
    out = _assert_unique_grain(qid, 30)

    known = [
        ("bl_ep_launch", "COMPUTE_TIME", 30.0, "bl_launch_ep"),
        ("bl_ep_normal", "COMPUTE_TIME", 20.0, "bl_normal_ep"),
        ("bl_ep_genai", "GPU_TIME", 40.0, "bl_genai_ep"),
        ("bl_ep_genai", "ANSWER", 5.0, "bl_genai_ep"),
        ("bl_vs_ep1", "COMPUTE_TIME", 18.0, "bl_vs_endpoint"),
    ]
    for endpoint_id, usage_type, expected, name in known:
        row = _one(out, endpoint_id=endpoint_id, usage_type=usage_type)
        assert abs(row["net_usage_quantity"] - expected) < 1e-6, (endpoint_id, usage_type, expected)
        assert row["endpoint_name"] == name, row["endpoint_name"]

        where = (
            f"usage_unit = 'DBU' AND billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH') "
            f"AND usage_metadata.endpoint_id = '{endpoint_id}' AND usage_type = '{usage_type}' AND {_win(30)}"
        )
        assert abs(dbutil.usage_sum(where) - expected) < 1e-6, (endpoint_id, usage_type)

    # order_by primary key (usage_date DESC): the four D(4) endpoints must all sort before the one
    # D(5) endpoint (bl_vs_ep1) -- tie-break among same-date/same-endpoint_id rows (the two
    # bl_ep_genai usage_type rows) is left unspecified by the vendored ORDER BY, so only the
    # date-group partition is checked.
    positions = {i: r for i, r in enumerate(out)
                 if (r["endpoint_id"], r["usage_type"]) in {(e, t) for e, t, _, _ in known}}
    d4_idx = [i for i, r in positions.items() if r["endpoint_id"] != "bl_vs_ep1"]
    d5_idx = [i for i, r in positions.items() if r["endpoint_id"] == "bl_vs_ep1"]
    assert d4_idx and d5_idx
    assert max(d4_idx) < min(d5_idx), (d4_idx, d5_idx)

    # window growth (usage_unit='DBU' filtered -> single family).
    sums = {w: sum(r["net_usage_quantity"] for r in dbutil.rows(qid, w)) for w in WINDOWS}
    assert sums[90] >= sums[30] >= sums[7], sums

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_chargeback_by_identity -- inventory; grain [usage_date, cloud, workspace_id,
# billing_origin_product, identity_type, identity_run_as, identity_owned_by, identity_created_by]
# -- the three identity_* columns are raw by default (masking is off unless
# privacy.mask_user_identities is set). Filtered to usage_unit = 'DBU' -> window-growth check
# still skipped (mixed products, just no longer mixed units).
# -------------------------------------------------------------------------------------------
def test_cost_chargeback_by_identity():
    qid = "cost_chargeback_by_identity"
    out = _assert_unique_grain(qid, 30)

    # the whole-result total is the DBU-only raw total for the window -- would fail if the query
    # still mixed in bytes/tokens/hours from other usage_unit families.
    total = sum(r["net_usage_quantity"] for r in out)
    assert abs(total - dbutil.usage_sum(f"usage_unit = 'DBU' AND {_win(30)}")) < 1e-6, total

    # Review fix item 11: this id's grain carries no bl_-prefixed id column at all (masking is the
    # whole point), so the _one() selections below can only scope by (workspace_id, usage_date,
    # identity_*) -- workspace_id is shared by every builder (DEC-15) and identity_type='user' has
    # no further literal to narrow on at the model-row level. Tightened as far as possible without
    # touching billing.py by adding usage_date=bl.D(6) (SEC F's own date) to each selection, which
    # rules out any other builder's identity row on a different day; a same-day, same-workspace,
    # same-identity-type row from a future builder would still collide -- noted, not closeable here.
    d6 = bl.D(6)

    # service_principal: GUID-shaped run_as is kept as-is.
    sp = _one(out, workspace_id=bl.WS_PROD, usage_date=d6, identity_run_as="11111111-2222-3333-4444-555555555555")
    assert sp["identity_type"] == "service_principal"
    assert sp["identity_owned_by"] == "11111111-2222-3333-4444-555555555555"
    assert sp["identity_created_by"] == "11111111-2222-3333-4444-555555555555"
    assert abs(sp["net_usage_quantity"] - 6.0) < 1e-6

    # user: mask_user() is off by default, so the raw email passes through.
    user = _one(out, workspace_id=bl.WS_PROD, usage_date=d6, identity_type="user")
    assert user["identity_run_as"] == "alice@example.com"
    assert user["identity_owned_by"] == "alice@example.com"
    assert user["identity_created_by"] == "alice@example.com"
    assert abs(user["net_usage_quantity"] - 9.0) < 1e-6

    # unknown via '__REDACTED__' (kept as-is, not NULL).
    redacted = _one(out, workspace_id=bl.WS_PROD, usage_date=d6, identity_run_as="__REDACTED__")
    assert redacted["identity_type"] == "unknown"
    assert redacted["identity_owned_by"] is None
    assert redacted["identity_created_by"] is None
    assert abs(redacted["net_usage_quantity"] - 4.0) < 1e-6

    # independent totals, scoped by record_id (billing.py's own bl_u_id_sp/bl_u_id_email/
    # bl_u_id_unknown, collision-free across builders per its module docstring) -- tighter than the
    # previous identity_metadata.run_as + workspace_id scoping, which would also have matched a
    # same-run_as row from another workspace's identity.
    for record_id, expected in (("bl_u_id_sp", 6.0), ("bl_u_id_email", 9.0), ("bl_u_id_unknown", 4.0)):
        assert abs(dbutil.usage_sum(f"record_id = '{record_id}'") - expected) < 1e-6, (record_id, expected)

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_chargeback_by_tag -- inventory; grain [usage_date, cloud, workspace_id,
# billing_origin_product, tag_key, tag_value]; LATERAL VIEW OUTER explode(custom_tags) -- an
# empty/NULL custom_tags map must still produce one row with tag_key/tag_value NULL (the
# untagged-share denominator). No usage_unit filter -> aggregate window-growth check skipped, but
# the untagged row's own window presence is checked directly instead.
# -------------------------------------------------------------------------------------------
def test_cost_chargeback_by_tag():
    qid = "cost_chargeback_by_tag"
    out = _assert_unique_grain(qid, 30)

    # the untagged row (bl_u_tag_empty, custom_tags={}) IS represented, not silently dropped by a
    # non-OUTER explode -- this is the load-bearing assertion for this id.
    untagged = _one(out, workspace_id=bl.WS_UAT, tag_key=None, usage_date=bl.D(6))
    assert untagged["tag_value"] is None
    assert abs(untagged["net_usage_quantity"] - 7.0) < 1e-6
    assert untagged["record_count"] == 1

    tag_a = _one(out, workspace_id=bl.WS_PROD, tag_key="team", tag_value="a", usage_date=bl.D(6))
    assert abs(tag_a["net_usage_quantity"] - 10.0) < 1e-6

    tag_b = _one(out, workspace_id=bl.WS_DEV, tag_key="team", tag_value="b", usage_date=bl.D(6))
    assert abs(tag_b["net_usage_quantity"] - 12.0) < 1e-6

    # independent totals, scoped by record_id (review fix item 11: (workspace_id, usage_date,
    # billing_origin_product) alone is not sufficient to isolate a single row here -- WS_PROD/D(6)/
    # ALL_PURPOSE also carries SEC F's identity rows, and workspace_id itself is shared by every
    # builder per DEC-15 -- record_id is billing.py's own collision-free namespace, the tightest
    # scoping available without touching the fixture).
    assert abs(dbutil.usage_sum("record_id = 'bl_u_tag_empty'") - 7.0) < 1e-6
    assert abs(dbutil.usage_sum("record_id = 'bl_u_tag_a'") - 10.0) < 1e-6
    assert abs(dbutil.usage_sum("record_id = 'bl_u_tag_b'") - 12.0) < 1e-6

    # window behaviour for the untagged row specifically: D(6) is inside every window (7/30/90),
    # so the same row/value must appear at all three; D0 is never relevant here (no tag row is
    # dated D0), but rows(id, 0) must still be empty structurally. This loop reads the MODEL's own
    # output (findings.f_cost_chargeback_by_tag), which carries no record_id column at all (tag_key/
    # tag_value replace the raw row identity by construction), so it can only be scoped by
    # (workspace_id, tag_key, usage_date) as below -- not tightenable further without the fixture
    # exposing a raw column this query deliberately does not emit; noted, not closeable here.
    for w in WINDOWS:
        rows_w = [r for r in dbutil.rows(qid, w)
                  if r["workspace_id"] == bl.WS_UAT and r["tag_key"] is None and r["usage_date"] == bl.D(6)]
        assert len(rows_w) == 1, (w, rows_w)
        assert abs(rows_w[0]["net_usage_quantity"] - 7.0) < 1e-6, (w, rows_w[0])

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_default_storage_dsu -- inventory; grain [usage_date, cloud, sku_name, usage_type,
# usage_unit, storage_api_type, catalog_id]; filtered to usage_metadata.storage_api_type IS NOT
# NULL (never the unverified billing_origin_product='DEFAULT_STORAGE' literal); empty_if:
# ingestion_lag. Only one fixture row (bl_u_storage1) -- "window growth" is checked as a flat
# presence-at-every-window fact rather than a meaningful family-wide aggregate.
# -------------------------------------------------------------------------------------------
def test_cost_default_storage_dsu():
    qid = "cost_default_storage_dsu"
    out = _assert_unique_grain(qid, 30)

    row = _one(out, catalog_id="bl_catalog1", storage_api_type="TIER_1")
    assert row["usage_date"] == bl.D(6)
    assert row["sku_name"] == "bl_DEFAULT_STORAGE_TIER1"
    assert abs(row["net_usage_quantity"] - 8.0) < 1e-6

    where = f"usage_metadata.catalog_id = 'bl_catalog1' AND usage_metadata.storage_api_type IS NOT NULL AND {_win(30)}"
    assert abs(dbutil.usage_sum(where) - 8.0) < 1e-6

    # D(6) is inside every window; the row's value must not change as the window widens (a
    # regression that double-counted it on a wider window, or dropped it, would show up here).
    for w in WINDOWS:
        rows_w = [r for r in dbutil.rows(qid, w) if r["catalog_id"] == "bl_catalog1"]
        assert len(rows_w) == 1, (w, rows_w)
        assert abs(rows_w[0]["net_usage_quantity"] - 8.0) < 1e-6, (w, rows_w[0])

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_genai_token_gpu -- inventory; grain [usage_date, cloud, sku_name, billing_origin_product,
# usage_type, usage_unit, serving_type, endpoint_name, endpoint_id]; filtered to usage_type IN
# ('TOKEN','GPU_TIME','ANSWER'); empty_if: no_activity. Mixed usage_unit (TOKEN vs DBU) ->
# window-growth check skipped.
# -------------------------------------------------------------------------------------------
def test_cost_genai_token_gpu():
    qid = "cost_genai_token_gpu"
    out = _assert_unique_grain(qid, 30)

    for usage_type, expected, unit in (("TOKEN", 1000.0, "TOKEN"), ("GPU_TIME", 40.0, "DBU"), ("ANSWER", 5.0, "DBU")):
        row = _one(out, endpoint_id="bl_ep_genai", usage_type=usage_type)
        assert row["usage_unit"] == unit
        assert row["endpoint_name"] == "bl_genai_ep"
        assert abs(row["net_usage_quantity"] - expected) < 1e-6, (usage_type, expected)

        where = f"usage_type = '{usage_type}' AND usage_metadata.endpoint_id = 'bl_ep_genai' AND {_win(30)}"
        assert abs(dbutil.usage_sum(where) - expected) < 1e-6, (usage_type, expected)

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_networking_egress -- inventory; grain [usage_date, cloud, sku_name, usage_type, usage_unit,
# source_region, destination_region, networking_client, recipient_id]; filtered to usage_type IN
# ('NETWORK_BYTE','NETWORK_HOUR'); empty_if: ingestion_lag. Mixed usage_unit (BYTES vs HOURS) ->
# window-growth check skipped.
# -------------------------------------------------------------------------------------------
def test_cost_networking_egress():
    qid = "cost_networking_egress"
    out = _assert_unique_grain(qid, 30)

    bytes_row = _one(out, recipient_id="bl_recipient1")
    assert bytes_row["usage_type"] == "NETWORK_BYTE"
    assert bytes_row["usage_unit"] == "BYTES"
    assert bytes_row["cloud"] == "aws"
    assert bytes_row["source_region"] == "us-east-1"
    assert bytes_row["destination_region"] == "us-west-2"
    assert abs(bytes_row["net_usage_quantity"] - 500000.0) < 1e-6

    hours_row = _one(out, recipient_id="bl_recipient2")
    assert hours_row["usage_type"] == "NETWORK_HOUR"
    assert hours_row["usage_unit"] == "HOURS"
    assert hours_row["cloud"] == "gcp"
    # caveat: source_region/destination_region are always NULL on GCP.
    assert hours_row["source_region"] is None
    assert hours_row["destination_region"] is None
    assert abs(hours_row["net_usage_quantity"] - 12.0) < 1e-6

    for recipient_id, expected in (("bl_recipient1", 500000.0), ("bl_recipient2", 12.0)):
        where = f"usage_metadata.recipient_id = '{recipient_id}' AND {_win(30)}"
        assert abs(dbutil.usage_sum(where) - expected) < 1e-6, (recipient_id, expected)

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_premium_serverless_photon -- inventory; grain [usage_date, cloud, sku_name,
# billing_origin_product, is_serverless, is_photon, jobs_tier, sql_tier, dlt_tier,
# performance_target]; filtered to usage_unit = 'DBU' so volumes are comparable across sku_name
# families; window-growth check still skipped (mixed sku families, just no longer mixed units)
# per this task's own Inputs section (named as the pattern example). SEC G's bl_u_nonphoton1 and
# bl_u_classic1 share every grain column (both is_serverless=false, is_photon=false, sku
# bl_ALL_PURPOSE_COMPUTE, D(7)) and must collapse into ONE row summing 14+11 -- a naive test
# picking either raw id alone would not catch a broken/over-fine GROUP BY here.
# -------------------------------------------------------------------------------------------
def test_cost_premium_serverless_photon():
    qid = "cost_premium_serverless_photon"
    out = _assert_unique_grain(qid, 30)
    d7 = bl.D(7)

    # the whole-result total is the DBU-only raw total for the window -- would fail if the query
    # still mixed in bytes/tokens/hours from other usage_unit families.
    total = sum(r["net_usage_quantity"] for r in out)
    assert abs(total - dbutil.usage_sum(f"usage_unit = 'DBU' AND {_win(30)}")) < 1e-6, total

    classic = _one(out, sku_name="bl_ALL_PURPOSE_COMPUTE", usage_date=d7, is_serverless=False, is_photon=False)
    assert abs(classic["net_usage_quantity"] - (14.0 + 11.0)) < 1e-6, classic
    where_classic = (
        f"sku_name = 'bl_ALL_PURPOSE_COMPUTE' AND usage_date = DATE '{d7.isoformat()}' "
        f"AND product_features.is_serverless = false AND product_features.is_photon = false"
    )
    assert abs(dbutil.usage_sum(where_classic) - 25.0) < 1e-6

    serverless = _one(out, sku_name="bl_ALL_PURPOSE_COMPUTE", usage_date=d7, is_serverless=True)
    assert abs(serverless["net_usage_quantity"] - 9.0) < 1e-6

    photon = _one(out, sku_name="bl_ALL_PURPOSE_COMPUTE_(PHOTON)", usage_date=d7)
    assert photon["is_photon"] is True
    assert abs(photon["net_usage_quantity"] - 22.0) < 1e-6

    tiered = _one(out, sku_name="bl_JOBS_COMPUTE_CLASSIC", usage_date=d7, jobs_tier="LIGHT")
    assert tiered["sql_tier"] == "CLASSIC"
    assert tiered["dlt_tier"] == "CORE"
    assert abs(tiered["net_usage_quantity"] - 16.0) < 1e-6

    where_tiered = (
        f"sku_name = 'bl_JOBS_COMPUTE_CLASSIC' AND usage_date = DATE '{d7.isoformat()}' "
        f"AND product_features.jobs_tier = 'LIGHT'"
    )
    assert abs(dbutil.usage_sum(where_tiered) - 16.0) < 1e-6

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


def test_cost_premium_serverless_photon_serverless_share_matches_overview_split():
    # Both ids are restricted to usage_unit = 'DBU', so they agree on the total. The split also
    # counts serverless-only products and SERVERLESS SKUs whose is_serverless flag is false; this
    # id groups by the raw flag, so those rows are added back before the shares are compared.
    rows_7 = dbutil.rows("cost_premium_serverless_photon", 7)
    total = sum(r["net_usage_quantity"] for r in rows_7)
    serverless = sum(r["net_usage_quantity"] for r in rows_7 if r["is_serverless"] is True)
    serverless += dbutil.usage_sum(
        f"usage_unit = 'DBU' AND {_win(7)} AND NOT COALESCE(product_features.is_serverless, FALSE) AND (product_features.is_serverless = TRUE OR upper(sku_name) LIKE '%SERVERLESS%' OR billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS', 'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS'))"
    )
    premium_share_pct = (serverless / total) * 100 if total else None

    split_rows = dbutil.rows("overview_serverless_classic_split", 7)
    split_total = sum(r["total_net_dbus"] for r in split_rows)
    split_serverless = sum(r["serverless_net_dbus"] for r in split_rows)
    split_share_pct = (split_serverless / split_total) * 100 if split_total else None

    assert premium_share_pct is not None and split_share_pct is not None
    assert abs(premium_share_pct - split_share_pct) < 0.1, (premium_share_pct, split_share_pct)


# -------------------------------------------------------------------------------------------
# cost_restatement_trust_metric -- inventory; grain [workspace_id, cloud] (workspace_id added so
# this check obeys the workspace/env filter -- one row per workspace + cloud per window, so
# cloud='aws' can now be several rows, one per contributing workspace_id, a NULL one included for
# a genuine account-level usage row); empty_if: ingestion_lag. No column filter at all -> mixes
# every usage_unit family in the whole fixture, and every builder's cloud='aws' rows contribute to
# the same aggregate -- so every expectation here mirrors the model's own broad predicate via
# usage_sum()/a raw ABS() query, summed across every workspace_id's own cloud='aws' row, rather
# than a hand-derived literal (see module docstring). The one thing that IS pinned precisely is
# the ORIGINAL/RETRACTION/RESTATEMENT correction pair's effect, scoped to bl_corr_job
# (collision-free), and the AS_OF-day exclusion via max_ingestion_date.
# -------------------------------------------------------------------------------------------
def test_cost_restatement_trust_metric():
    qid = "cost_restatement_trust_metric"

    for w in WINDOWS:
        out = dbutil.rows(qid, w)
        cols = GRAINS[qid]
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"{qid} w={w}: duplicate workspace/cloud rows"
        aws_rows = [r for r in out if r["cloud"] == "aws"]
        gcp_rows = [r for r in out if r["cloud"] == "gcp"]
        assert aws_rows and gcp_rows, out

        win = _win(w)
        expected_net = dbutil.usage_sum(f"cloud = 'aws' AND {win}")
        expected_orig = dbutil.usage_sum(f"cloud = 'aws' AND record_type = 'ORIGINAL' AND {win}")
        expected_restatement = dbutil.usage_sum(f"cloud = 'aws' AND record_type = 'RESTATEMENT' AND {win}")
        expected_retracted_abs = _raw_scalar(
            "SUM(ABS(usage_quantity))", f"cloud = 'aws' AND record_type = 'RETRACTION' AND {win}"
        )
        expected_retracted_abs = float(expected_retracted_abs) if expected_retracted_abs is not None else 0.0

        # workspace_id is now part of the grain, so cloud='aws' may be several rows -- sum across
        # them to recover the same account-wide total this test pinned before the split.
        net_sum = sum(r["net_usage_quantity"] for r in aws_rows)
        orig_sum = sum(r["original_usage_quantity"] for r in aws_rows)
        restatement_sum = sum(r["restatement_usage_quantity"] for r in aws_rows)
        retracted_sum = sum(r["retracted_abs_quantity"] for r in aws_rows)

        assert abs(net_sum - expected_net) < 1e-6, (w, net_sum, expected_net)
        assert abs(orig_sum - expected_orig) < 1e-6, (w, orig_sum, expected_orig)
        assert abs(restatement_sum - expected_restatement) < 1e-6, (w, restatement_sum, expected_restatement)
        assert abs(retracted_sum - expected_retracted_abs) < 1e-6, (w, retracted_sum, expected_retracted_abs)

        # max_ingestion_date, mirrored against the model's own predicate straight from the raw
        # parquet (review fix item 8: this used to pin a literal date -- bl.D0/bl.D(1) -- which is
        # MAX(ingestion_date) over every builder's cloud='aws' rows and holds only because every
        # builder today sets ingestion_date = usage_date; a builder modelling real ingestion lag
        # would break a pinned literal without this being a real bug. Recomputed here instead.)
        # The account-wide ceiling is the MAX across every workspace's own row for this cloud.
        expected_max_ingestion = _raw_scalar("MAX(ingestion_date)", f"cloud = 'aws' AND {win}")
        max_ingestion = max(r["max_ingestion_date"] for r in aws_rows)
        assert max_ingestion == expected_max_ingestion, (w, max_ingestion, expected_max_ingestion)

    # AS_OF-day exclusion, proven without pinning a literal date: tests/fixtures/billing.py's SEC K
    # sets bl_u_win_d0's ingestion_date = usage_date = D0 for cloud='aws' (record_id
    # 'bl_u_win_d0'), so an UNWINDOWED MAX(ingestion_date) over the same cloud must be >= D0 --
    # while every windowed max above (which all apply usage_date < today via _win()) is proven
    # equal to the model's own rows, which in turn must be strictly less than this unwindowed
    # ceiling whenever the ceiling is reached by a same-day row. This holds regardless of how many
    # other builders contribute, and regardless of what ingestion_date convention they use.
    unwindowed_max = _raw_scalar("MAX(ingestion_date)", "cloud = 'aws'")
    assert unwindowed_max >= bl.D0, unwindowed_max  # bl_u_win_d0 alone guarantees this
    aws90_rows = [r for r in dbutil.rows(qid, 90) if r["cloud"] == "aws"]
    max_ingestion_90 = max(r["max_ingestion_date"] for r in aws90_rows)
    assert max_ingestion_90 < unwindowed_max, (max_ingestion_90, unwindowed_max)

    # the correction pair (bl_corr_job, SEC H) genuinely moves the metric, not merely "some rows
    # exist": scoped to job_id (collision-free), net (100 - 100 + 110 = 110) differs from
    # original-only (100).
    scoped_net = dbutil.usage_sum("usage_metadata.job_id = 'bl_corr_job'")
    scoped_orig = dbutil.usage_sum("usage_metadata.job_id = 'bl_corr_job' AND record_type = 'ORIGINAL'")
    assert abs(scoped_net - 110.0) < 1e-6, scoped_net
    assert abs(scoped_orig - 100.0) < 1e-6, scoped_orig
    assert scoped_net != scoped_orig, "the RETRACTION/RESTATEMENT pair must change the net, not no-op"

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_totals_by_sku_day -- inventory; grain [usage_date, cloud, workspace_id, sku_name,
# billing_origin_product, usage_type, usage_unit, is_serverless]; empty_if: ingestion_lag;
# workspace_id may be NULL (account-level) -- those rows are kept, not filtered out.
# -------------------------------------------------------------------------------------------
def test_cost_totals_by_sku_day():
    qid = "cost_totals_by_sku_day"
    out = _assert_unique_grain(qid, 30)

    # correction cell (SEC H): net = original + retraction(signed, -100) + restatement -- NOT the
    # ABS()'d version cost_restatement_trust_metric uses (retraction_usage_quantity is signed here).
    corr = _one(out, sku_name="bl_JOBS_COMPUTE_CLASSIC", workspace_id=bl.WS_PROD, usage_date=bl.D(8))
    assert abs(corr["net_usage_quantity"] - 110.0) < 1e-6
    assert abs(corr["original_usage_quantity"] - 100.0) < 1e-6
    assert abs(corr["retraction_usage_quantity"] - (-100.0)) < 1e-6
    assert abs(corr["restatement_usage_quantity"] - 110.0) < 1e-6
    assert corr["record_count"] == 3
    assert corr["net_usage_quantity"] != corr["original_usage_quantity"], "correction must move the metric"

    where_corr = f"usage_metadata.job_id = 'bl_corr_job' AND {_win(30)}"
    assert abs(dbutil.usage_sum(where_corr) - 110.0) < 1e-6
    where_corr_orig = where_corr + " AND record_type = 'ORIGINAL'"
    assert abs(dbutil.usage_sum(where_corr_orig) - 100.0) < 1e-6

    # window boundary: D(8) is outside the 7-day window, inside 30/90.
    assert not [r for r in dbutil.rows(qid, 7)
                if r.get("sku_name") == "bl_JOBS_COMPUTE_CLASSIC" and r.get("workspace_id") == bl.WS_PROD
                and r.get("usage_date") == bl.D(8)]
    for w in (30, 90):
        present = [r for r in dbutil.rows(qid, w)
                   if r["sku_name"] == "bl_JOBS_COMPUTE_CLASSIC" and r["workspace_id"] == bl.WS_PROD
                   and r["usage_date"] == bl.D(8)]
        assert len(present) == 1, (w, present)
        assert abs(present[0]["net_usage_quantity"] - 110.0) < 1e-6

    # account-level row: workspace_id IS NULL and is kept, not dropped.
    acct = _one(out, workspace_id=None, sku_name="bl_MODEL_SERVING_STANDARD")
    assert acct["workspace_id"] is None
    assert abs(acct["net_usage_quantity"] - 3.0) < 1e-6
    assert acct["record_count"] == 1
    where_acct = f"workspace_id IS NULL AND sku_name = 'bl_MODEL_SERVING_STANDARD' AND {_win(30)}"
    assert abs(dbutil.usage_sum(where_acct) - 3.0) < 1e-6

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


# -------------------------------------------------------------------------------------------
# cost_usage_policy_coverage -- finding; grain [usage_date, cloud, workspace_id,
# billing_origin_product, is_serverless, policy_coverage, tag_coverage]; status CASE has NO
# CRITICAL branch (is_serverless NULL -> NOT_ASSESSED; is_serverless=true AND policy_coverage=
# 'none' -> WARN; else OK); order_by: status DESC, net_usage_quantity DESC.
# -------------------------------------------------------------------------------------------
def test_cost_usage_policy_coverage():
    qid = "cost_usage_policy_coverage"
    out = _assert_unique_grain(qid, 30)
    _assert_status_enum(out)

    # the whole-result total is the DBU-only raw total for the window -- would fail if the query
    # still mixed in bytes/tokens/hours from other usage_unit families.
    total = sum(r["net_usage_quantity"] for r in out)
    assert abs(total - dbutil.usage_sum(f"usage_unit = 'DBU' AND {_win(30)}")) < 1e-6, total

    # Review fix item 11: this id's grain carries no bl_-prefixed id column either, so the _one()
    # selections below can only scope by (workspace_id, usage_date, policy_coverage/is_serverless)
    # -- workspace_id is shared by every builder (DEC-15); D(10) narrows it as far as possible at
    # the model-row level without touching billing.py. Noted, not closeable here.
    d10 = bl.D(10)
    usage_pol = _one(out, workspace_id=bl.WS_DEV, usage_date=d10, policy_coverage="usage_policy")
    assert usage_pol["status"] == "OK"
    assert usage_pol["tag_coverage"] == "untagged"
    assert abs(usage_pol["net_usage_quantity"] - 8.0) < 1e-6

    budget_pol = _one(out, workspace_id=bl.WS_DEV, usage_date=d10, policy_coverage="budget_policy_legacy")
    assert budget_pol["status"] == "OK"
    assert budget_pol["tag_coverage"] == "tagged"
    assert abs(budget_pol["net_usage_quantity"] - 6.0) < 1e-6

    none_serverless = _one(
        out, workspace_id=bl.WS_DEV, usage_date=d10, policy_coverage="none", is_serverless=True,
    )
    assert none_serverless["status"] == "WARN"
    assert none_serverless["tag_coverage"] == "untagged"
    assert abs(none_serverless["net_usage_quantity"] - 5.0) < 1e-6

    not_assessed = _one(out, workspace_id=bl.WS_DEV, usage_date=d10, is_serverless=None)
    assert not_assessed["status"] == "NOT_ASSESSED"
    assert not_assessed["policy_coverage"] == "none"
    assert abs(not_assessed["net_usage_quantity"] - 3.0) < 1e-6

    # this id's CASE has no CRITICAL branch at all -- confirmed structurally across every window,
    # not just this fixture's own rows.
    for w in WINDOWS:
        assert not any(r["status"] == "CRITICAL" for r in dbutil.rows(qid, w)), (w, "unexpected CRITICAL")

    # worst-first: order_by is status DESC, net_usage_quantity DESC -- a plain descending tuple
    # sort (status alphabetically DESC puts WARN before OK before NOT_ASSESSED, NOT a
    # CRITICAL-first convention -- this id never emits CRITICAL). Verify the FULL returned list
    # matches that sort, not just the four known rows.
    key = [(r["status"], r["net_usage_quantity"]) for r in out]
    assert key == sorted(key, reverse=True), f"{qid}: rows not in declared order_by order"

    # independent totals, scoped by record_id (review fix item 11: tighter than the previous
    # workspace_id + usage_date + policy-id scoping, which is safe today only because no other
    # builder currently lands a row on WS_DEV/D(10) -- record_id is billing.py's own
    # collision-free namespace regardless of what other builders do).
    assert abs(dbutil.usage_sum("record_id = 'bl_u_pol_usage'") - 8.0) < 1e-6
    assert abs(dbutil.usage_sum("record_id = 'bl_u_pol_budget'") - 6.0) < 1e-6
    assert abs(dbutil.usage_sum("record_id = 'bl_u_pol_none'") - 5.0) < 1e-6
    assert abs(dbutil.usage_sum("record_id = 'bl_u_pol_null'") - 3.0) < 1e-6

    assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"
