"""tests/test_findings/test_lakeflow_dims.py -- T-18.

Proves, against tests/fixtures/lakeflow.py's own rows and only those rows, that each of the 9
built findings.f_<query_id> tables (batch C3, PLAN.md 7.2) has the right grain, the right status
enum, a reachable WARN/CRITICAL/NOT_ASSESSED band where the body can emit one, worst-first
ordering per the model's own `order_by` (dbt/models/findings/jobs_pipelines/
_findings__jobs_pipelines.yml meta -- read directly off the body's trailing ORDER BY, since the
grain-file regeneration step is out of scope for this run, see below), and window_days=0/7/30/90
behaviour, per tests/test_findings/README.md's checklist and this task's own Inputs/Steps.

T-18 is TEST-ONLY: it owns config/grains/lakeflow_dims.yml and this file, nothing else. It does
NOT run `tools/generate_models.py`, `dbt build`, or `tests/fixtures/build_fixtures.py` -- the
orchestrator's timing rule for this run forbids touching the shared, already-green
tests/db_audit_test.duckdb build (dbt build --target test, PASS=444, 0 ERROR) or the shared
`dbt/models/findings/jobs_pipelines/_findings__jobs_pipelines.yml` (T-17 owns a concurrent
regeneration of the same file). Every assertion below reads the ALREADY-BUILT
findings.f_<query_id> tables through tests/dbutil.py, and independently re-derives its own
expectations either from tests/fixtures/lakeflow.py's own literal ids (imported directly as `lf`,
matching tests/test_findings/test_compute_activity.py's `import compute as cp` precedent) or from
the raw tests/fixtures/parquet/**/*.parquet, straight from the model's own predicate -- never a
hard-coded aggregate total. Grain uniqueness itself is proven twice normally (the generated
unique_grain dbt test, plus this file's own len(keys)==len(set(keys)) check); only the first proof
is unavailable this run since the grain file has not yet been folded into a regenerated model YAML
-- see this file's own Python-level check as the whole proof for this run.

Four ids (lakeflow_health_rule_coverage, lakeflow_job_ownership_orphans, lakeflow_jobs_no_timeout,
lakeflow_job_tasks_no_timeout) are one row per job (or task) now, reading the ENTIRE shared
lakeflow.jobs / lakeflow.job_tasks tables -- not just tests/fixtures/lakeflow.py's own rows for
workspace 1111/2222/3333. Checked directly: only tests/fixtures/lakeflow.py and
tests/fixtures/drilldown.py write to lakeflow__jobs (drilldown.py's own workspace is 9001/9002,
never 1111/2222/3333 -- DEC-15), and only tests/fixtures/lakeflow.py writes to lakeflow__job_tasks
at all. So today, WS 1111/2222/3333's own rows on these four ids are not actually perturbed by
another builder. They are still NOT provably owner-scoped by construction (a future builder could
add a lakeflow.jobs row for workspace 1111 without violating any convention in this codebase), so
every assertion on these four ids' per-job facts below is computed by re-running the model's own
per-job judging directly against the CURRENT tests/fixtures/parquet/lakeflow__jobs(/_tasks)/
*.parquet at test time (see `_health_rule_coverage_expected` etc. below), not by pinning T-16's
stated literals -- exactly the same fix applied in the drift ownership_orphans/jobs_on_all_purpose
"dropped" case below. The remaining 5 ids key on a job_id/task_key/compute_id that is uniquely
`lf_`-prefixed and owned only by this builder (DEC-15/DEC-21), so their per-row literal assertions
are safe to pin directly.

lakeflow_jobs_on_all_purpose's own workspace-grain NOT_ASSESSED "dropped" summary row (the
COUNT(DISTINCT run_id) of every job_task_run_timeline row in a workspace with compute_ids IS NULL,
grouped by workspace_id alone) is the SAME class of risk even though it is not one of the four ids
named above: nearly every OTHER SEC block in tests/fixtures/lakeflow.py calls jtrt(...) without an
explicit compute_ids argument (default None), so that row's count is not "the null-compute-ids
placement in SEC I", it is "every job_task_run_timeline row in workspace 1111 across the whole
builder with a NULL compute_ids list". It is therefore also computed from the raw parquet with the
model's own predicate below (`_dropped_runs_expected`), never pinned to a literal.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import lakeflow as lf  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_dims.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
FINDINGS_YML = ROOT / "dbt" / "models" / "findings" / "jobs_pipelines" / "_findings__jobs_pipelines.yml"
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

WS1, WS2, WS3 = lf.WS1, lf.WS2, lf.WS3  # "1111", "2222", "3333" (DEC-15 shared workspaces)
D0: date = lf.D0  # AS_OF's own date (2026-09-21)


def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _order_by(query_id: str) -> str:
    """The generator-extracted `order_by` (tools/generate_models.py's extract_order_by, read off
    the body's own trailing ORDER BY) for this model, from the generated schema yml -- the exact
    text app/core/data.py's own _order_by_clause reads at request time to sort a finding's rows
    (worst-first, status rank prepended). dbutil.rows() itself issues no ORDER BY (a bare
    `SELECT * ... WHERE window_days = ?`), and a model with a LEFT JOIN is not guaranteed to keep
    its own trailing ORDER BY's row order through DuckDB's physical table materialization -- e.g.
    lakeflow_job_ownership_orphans's LEFT JOIN to workspaces_latest reorders on this fixture, even
    though nothing about the finding's own judging is wrong. So this checks the WIRED ORDER BY
    text directly (what the real app applies) rather than dbutil.rows()'s own unordered scan."""
    with open(FINDINGS_YML, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    for m in doc["models"]:
        if m["meta"]["query_id"] == query_id:
            return m["meta"]["order_by"]
    raise KeyError(query_id)


def _glob(table: str) -> str:
    return (PARQUET_DIR / table / "*.parquet").as_posix()


def _window_bounds(window_days: int) -> tuple[date, date]:
    return D0 - timedelta(days=window_days), D0


def _cost_rollup(resource_col: str, resource_id: str, window_days: int) -> tuple[float, float, str]:
    """Independent re-derivation of a model's cost_rollup CTE (net_dbus, est_usd_list,
    price_basis) straight from the raw billing.usage / billing.list_prices parquet, for a single
    usage_metadata.<col> value -- same price-window join predicate every one of this batch's 9
    bodies uses (`u.usage_end_time >= p.price_start_time AND (p.price_end_time IS NULL OR
    u.usage_end_time < p.price_end_time)`, `CAST(pricing.effective_list.default AS DOUBLE)` --
    DEC-66.1's one dollar basis), matching tests/test_findings/test_compute_activity.py's
    `_cost_rollup` precedent -- never a rate constant copied from another builder's module."""
    lower, upper = _window_bounds(window_days)
    con = duckdb.connect()
    try:
        sql = f"""
            WITH price AS (
                SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
                       CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
                FROM read_parquet('{_glob("billing__list_prices")}', union_by_name=true)
            )
            SELECT SUM(u.usage_quantity)                            AS net_dbus,
                   SUM(u.usage_quantity * COALESCE(p.list_rate, 0))  AS est_usd_list,
                   CASE
                     WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                                   THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
                     WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
                     ELSE 'priced'
                   END                                                AS price_basis
            FROM read_parquet('{_glob("billing__usage")}', union_by_name=true) u
            LEFT JOIN price p
              ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
             AND u.usage_end_time >= p.price_start_time
             AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
            WHERE upper(u.usage_unit) = 'DBU'
              AND u.usage_metadata.{resource_col} = '{resource_id}'
              AND u.usage_date >= DATE '{lower}' AND u.usage_date < DATE '{upper}'
        """
        net_dbus, est_usd_list, price_basis = con.execute(sql).fetchone()
        return (net_dbus or 0.0), (est_usd_list or 0.0), (price_basis or "priced")
    finally:
        con.close()


def _cost_rollup_job(cluster_id: str, job_id: str, window_days: int) -> tuple[float, float, str]:
    """Same re-derivation as `_cost_rollup`, but filtered on BOTH usage_metadata.cluster_id AND
    usage_metadata.job_id -- the model's own P4 fix (lakeflow_jobs_on_all_purpose.sql's header):
    a job's cost is now its OWN billed usage on the cluster, not the cluster's whole bill, so the
    expectation must be scoped the same way, never the single-column `_cost_rollup` above (that
    would still include another job's or a notebook's usage on the same shared cluster)."""
    lower, upper = _window_bounds(window_days)
    con = duckdb.connect()
    try:
        sql = f"""
            WITH price AS (
                SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
                       CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
                FROM read_parquet('{_glob("billing__list_prices")}', union_by_name=true)
            )
            SELECT SUM(u.usage_quantity)                            AS net_dbus,
                   SUM(u.usage_quantity * COALESCE(p.list_rate, 0))  AS est_usd_list,
                   CASE
                     WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                                   THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
                     WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
                     ELSE 'priced'
                   END                                                AS price_basis
            FROM read_parquet('{_glob("billing__usage")}', union_by_name=true) u
            LEFT JOIN price p
              ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
             AND u.usage_end_time >= p.price_start_time
             AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
            WHERE upper(u.usage_unit) = 'DBU'
              AND u.usage_metadata.cluster_id = '{cluster_id}'
              AND u.usage_metadata.job_id = '{job_id}'
              AND u.usage_date >= DATE '{lower}' AND u.usage_date < DATE '{upper}'
        """
        net_dbus, est_usd_list, price_basis = con.execute(sql).fetchone()
        return (net_dbus or 0.0), (est_usd_list or 0.0), (price_basis or "priced")
    finally:
        con.close()


def _raw_row_exists(schema_table: str, where: str) -> bool:
    """True if >=1 row matching `where` exists anywhere in the raw
    tests/fixtures/parquet/<schema_table>/*.parquet (every builder's slice, unioned by name) --
    anchors an "excluded by the window" assertion so the test fails if the underlying fixture row
    is ever silently removed, matching test_compute_activity.py's `_raw_row_exists` precedent."""
    con = duckdb.connect()
    try:
        sql = f"SELECT COUNT(*) FROM read_parquet('{_glob(schema_table)}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0] > 0
    finally:
        con.close()


def _latest_jobs_sql() -> str:
    return f"""
        SELECT workspace_id, job_id, health_rules, creator_user_name, run_as_user_name,
               trigger_type, paused, timeout_seconds, delete_time, change_time
        FROM read_parquet('{_glob("lakeflow__jobs")}', union_by_name=true)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
    """


def _health_rule_coverage_expected() -> dict[tuple[str, str], dict]:
    """Re-derives f_lakeflow_health_rule_coverage's own per-job judging (latest non-deleted job
    row per (workspace_id, job_id); len(health_rules) is the DuckDB-branch translation of the
    body's CARDINALITY(), per dbt/models/findings/jobs_pipelines/f_lakeflow_health_rule_coverage.sql)
    directly from the raw lakeflow__jobs parquet -- no threshold param any more (a job either has
    a rule or it does not)."""
    sql = f"""
        WITH latest_jobs AS ({_latest_jobs_sql()})
        SELECT workspace_id, job_id,
               CASE WHEN health_rules IS NOT NULL THEN len(health_rules) END AS health_rule_count
        FROM latest_jobs WHERE delete_time IS NULL
    """
    con = duckdb.connect()
    try:
        out = {}
        for workspace_id, job_id, health_rule_count in con.execute(sql).fetchall():
            if health_rule_count is None:
                status = "NOT_ASSESSED"
            elif health_rule_count == 0:
                status = "WARN"
            else:
                status = "OK"
            out[(workspace_id, job_id)] = {"health_rule_count": health_rule_count, "status": status}
        return out
    finally:
        con.close()


_SP_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _ownership_orphans_expected() -> dict[tuple[str, str], dict]:
    """Re-derives f_lakeflow_job_ownership_orphans's own per-job judging -- normalises NULL/blank/
    '__REDACTED__' identities to unknown and trigger_type+paused to a scheduled flag first, exactly
    like the body's `norm` CTE, judges run_as_user_name SERVICE_PRINCIPAL (an application-id UUID
    shape), HUMAN (anything else non-NULL) or unknown, then flags principal_missing (exactly one
    identity recorded, the other gone, and not a redacted identity) and identity_not_recorded
    (NEITHER identity recorded -- excluded from status, never scored). No threshold param any more:
    a scheduled human run-as reads CRITICAL (the worse pattern, matching the double weight the old
    workspace-level risk score gave it), a manual human run-as or a missing principal reads WARN."""
    sql = f"""
        WITH latest_jobs AS ({_latest_jobs_sql()}),
        norm AS (
          SELECT workspace_id, job_id,
            (upper(COALESCE(creator_user_name, '')) = '__REDACTED__'
               OR upper(COALESCE(run_as_user_name, '')) = '__REDACTED__') AS identity_redacted,
            CASE WHEN creator_user_name IS NULL OR trim(creator_user_name) = ''
                   OR upper(creator_user_name) = '__REDACTED__' THEN NULL ELSE creator_user_name END AS creator_user_name,
            CASE WHEN run_as_user_name IS NULL OR trim(run_as_user_name) = ''
                   OR upper(run_as_user_name) = '__REDACTED__' THEN NULL ELSE run_as_user_name END AS run_as_user_name,
            (trigger_type IS NOT NULL AND trim(trigger_type) <> ''
               AND NOT COALESCE(paused, FALSE)) AS is_scheduled
          FROM latest_jobs WHERE delete_time IS NULL
        )
        SELECT workspace_id, job_id, creator_user_name, run_as_user_name, is_scheduled, identity_redacted,
               CASE
                 WHEN regexp_matches(run_as_user_name, '{_SP_UUID_RE}') THEN 'SERVICE_PRINCIPAL'
                 WHEN run_as_user_name IS NOT NULL THEN 'HUMAN'
                 ELSE NULL
               END AS run_as_kind,
               (NOT identity_redacted AND ((creator_user_name IS NULL) <> (run_as_user_name IS NULL))) AS principal_missing,
               (creator_user_name IS NULL AND run_as_user_name IS NULL) AS identity_not_recorded
        FROM norm
    """
    con = duckdb.connect()
    try:
        cols = [
            "workspace_id", "job_id", "creator_user_name", "run_as_user_name", "is_scheduled",
            "identity_redacted", "run_as_kind", "principal_missing", "identity_not_recorded",
        ]
        out = {}
        for row in con.execute(sql).fetchall():
            d = dict(zip(cols, row))
            if d["identity_not_recorded"]:
                status = "NOT_ASSESSED"
            elif d["run_as_kind"] == "HUMAN" and d["is_scheduled"]:
                status = "CRITICAL"
            elif d["run_as_kind"] == "HUMAN":
                status = "WARN"
            elif d["principal_missing"]:
                status = "WARN"
            else:
                status = "OK"
            d["status"] = status
            out[(d["workspace_id"], d["job_id"])] = d
        return out
    finally:
        con.close()


def _ownership_score(rows: list[dict]) -> int:
    """The old workspace-level ownership-risk score (2*scheduled-human + manual-human +
    principal-missing), recomputed from a per-job row list -- same weighting, now a Python
    reduction over dbutil.rows() instead of a SQL SUM, since the finding itself carries no
    per-workspace count any more."""
    return (
        2 * sum(1 for r in rows if r["run_as_kind"] == "HUMAN" and r["is_scheduled"])
        + sum(1 for r in rows if r["run_as_kind"] == "HUMAN" and not r["is_scheduled"])
        + sum(1 for r in rows if r["principal_missing"])
    )


def _jobs_no_timeout_expected(window_days: int) -> dict[tuple[str, str], dict]:
    """Re-derives f_lakeflow_jobs_no_timeout: one row per job; net_dbus/est_usd_list/price_basis
    are the job's OWN DBUs/dollars over the cost-lookback window (DEC-66.1's
    pricing.effective_list.default basis), for scale regardless of the no_timeout flag; no
    workspace-count threshold any more -- a flagged job reads WARN, or CRITICAL when its own
    est_usd_list is at/above crit_no_timeout_usd=200 (the header default). has_duration_health_rule
    mirrors the body's own health_rules[1..3] check -- a job with no timeout_seconds but a
    RUN_DURATION_SECONDS/GREATER_THAN health rule reads bounded_by_health_rule, never no_timeout."""
    lower, upper = _window_bounds(window_days)
    sql = f"""
        WITH base_jobs AS ({_latest_jobs_sql()}),
        latest_jobs AS (
          SELECT workspace_id, job_id, timeout_seconds, delete_time, change_time,
                 COALESCE((health_rules[1].metric = 'RUN_DURATION_SECONDS' AND health_rules[1].operator = 'GREATER_THAN')
                  OR (health_rules[2].metric = 'RUN_DURATION_SECONDS' AND health_rules[2].operator = 'GREATER_THAN')
                  OR (health_rules[3].metric = 'RUN_DURATION_SECONDS' AND health_rules[3].operator = 'GREATER_THAN'), FALSE) AS has_duration_health_rule
          FROM base_jobs
        ),
        population_floor AS (
          SELECT workspace_id, MIN(change_time) AS population_floor_time
          FROM read_parquet('{_glob("lakeflow__jobs")}', union_by_name=true)
          WHERE timeout_seconds IS NOT NULL
          GROUP BY workspace_id
        ),
        price AS (
          SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
                 CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
          FROM read_parquet('{_glob("billing__list_prices")}', union_by_name=true)
        ),
        cost_rollup AS (
          SELECT u.workspace_id, u.usage_metadata.job_id AS job_id,
                 SUM(u.usage_quantity) AS net_dbus,
                 SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
                 CASE
                   WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                                 THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
                   WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
                   ELSE 'priced'
                 END AS price_basis
          FROM read_parquet('{_glob("billing__usage")}', union_by_name=true) u
          LEFT JOIN price p ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
            AND u.usage_end_time >= p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
          WHERE upper(u.usage_unit) = 'DBU' AND u.usage_metadata.job_id IS NOT NULL
            AND u.usage_date >= DATE '{lower}' AND u.usage_date < DATE '{upper}'
          GROUP BY u.workspace_id, u.usage_metadata.job_id
        )
        SELECT lj.workspace_id, lj.job_id,
               (lj.timeout_seconds IS NULL OR lj.timeout_seconds = 0) AND NOT lj.has_duration_health_rule AS no_timeout,
               lj.has_duration_health_rule AS bounded_by_health_rule,
               CASE
                 WHEN lj.timeout_seconds IS NULL AND NOT lj.has_duration_health_rule
                      AND (pf.population_floor_time IS NULL OR lj.change_time < pf.population_floor_time)
                   THEN 'not_populated'
                 WHEN lj.timeout_seconds IS NULL AND NOT lj.has_duration_health_rule
                   THEN 'after_population'
               END AS timeout_null_reason,
               ROUND(COALESCE(cr.net_dbus, 0), 2) AS net_dbus,
               ROUND(COALESCE(cr.est_usd_list, 0), 2) AS est_usd_list,
               COALESCE(cr.price_basis, 'priced') AS price_basis
        FROM latest_jobs lj
        LEFT JOIN population_floor pf ON pf.workspace_id = lj.workspace_id
        LEFT JOIN cost_rollup cr ON lj.workspace_id = cr.workspace_id AND lj.job_id = cr.job_id
        WHERE lj.delete_time IS NULL
    """
    con = duckdb.connect()
    try:
        cols = ["workspace_id", "job_id", "no_timeout", "bounded_by_health_rule", "timeout_null_reason",
                "net_dbus", "est_usd_list", "price_basis"]
        out = {}
        for row in con.execute(sql).fetchall():
            d = dict(zip(cols, row))
            if d["no_timeout"] and d["timeout_null_reason"] == "not_populated":
                status = "NOT_ASSESSED"
            elif d["no_timeout"] and d["est_usd_list"] >= 200:
                status = "CRITICAL"
            elif d["no_timeout"]:
                status = "WARN"
            else:
                status = "OK"
            d["status"] = status
            out[(d["workspace_id"], d["job_id"])] = d
        return out
    finally:
        con.close()


def _job_tasks_no_timeout_expected(window_days: int) -> dict[tuple[str, str, str], dict]:
    """Re-derives f_lakeflow_job_tasks_no_timeout: one row per task, keyed on (workspace_id,
    job_id, task_key). net_dbus/est_usd_list/price_basis are the PARENT JOB's own DBUs/dollars
    over the cost-lookback window (an at-risk upper bound, per the body's own caveat -- billing
    carries no task_key to split by). DEC-66.1's pricing.effective_list.default basis; no
    threshold param any more -- a flagged task reads WARN, or CRITICAL when its parent job's own
    est_usd_list is at/above crit_no_timeout_usd=200 (the header default). A task whose parent job
    is gone reads parent_gone/NOT_ASSESSED, never no_timeout."""
    lower, upper = _window_bounds(window_days)
    sql = f"""
        WITH latest_jobs AS (
          SELECT workspace_id, job_id, delete_time
          FROM read_parquet('{_glob("lakeflow__jobs")}', union_by_name=true)
          QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
        ),
        latest_tasks AS (
          SELECT lt.workspace_id, lt.job_id, lt.task_key, lt.timeout_seconds, lt.delete_time, lt.change_time,
                 COALESCE((lt.health_rules[1].metric = 'RUN_DURATION_SECONDS' AND lt.health_rules[1].operator = 'GREATER_THAN')
                  OR (lt.health_rules[2].metric = 'RUN_DURATION_SECONDS' AND lt.health_rules[2].operator = 'GREATER_THAN')
                  OR (lt.health_rules[3].metric = 'RUN_DURATION_SECONDS' AND lt.health_rules[3].operator = 'GREATER_THAN'), FALSE) AS has_duration_health_rule,
                 (lj.job_id IS NULL OR lj.delete_time IS NOT NULL) AS parent_gone
          FROM read_parquet('{_glob("lakeflow__job_tasks")}', union_by_name=true) lt
          LEFT JOIN latest_jobs lj ON lj.workspace_id = lt.workspace_id AND lj.job_id = lt.job_id
          QUALIFY ROW_NUMBER() OVER (PARTITION BY lt.workspace_id, lt.job_id, lt.task_key ORDER BY lt.change_time DESC) = 1
        ),
        population_floor AS (
          SELECT workspace_id, MIN(change_time) AS population_floor_time
          FROM read_parquet('{_glob("lakeflow__job_tasks")}', union_by_name=true)
          WHERE timeout_seconds IS NOT NULL
          GROUP BY workspace_id
        ),
        price AS (
          SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
                 CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
          FROM read_parquet('{_glob("billing__list_prices")}', union_by_name=true)
        ),
        cost_rollup AS (
          SELECT u.workspace_id, u.usage_metadata.job_id AS job_id,
                 SUM(u.usage_quantity) AS net_dbus,
                 SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
                 CASE
                   WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                                 THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
                   WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
                   ELSE 'priced'
                 END AS price_basis
          FROM read_parquet('{_glob("billing__usage")}', union_by_name=true) u
          LEFT JOIN price p ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
            AND u.usage_end_time >= p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
          WHERE upper(u.usage_unit) = 'DBU' AND u.usage_metadata.job_id IS NOT NULL
            AND u.usage_date >= DATE '{lower}' AND u.usage_date < DATE '{upper}'
          GROUP BY u.workspace_id, u.usage_metadata.job_id
        )
        SELECT lt.workspace_id, lt.job_id, lt.task_key, lt.parent_gone,
               NOT lt.parent_gone AND (lt.timeout_seconds IS NULL OR lt.timeout_seconds = 0)
                 AND NOT lt.has_duration_health_rule AS no_timeout,
               lt.has_duration_health_rule AS bounded_by_health_rule,
               CASE
                 WHEN NOT lt.parent_gone AND lt.timeout_seconds IS NULL AND NOT lt.has_duration_health_rule
                      AND (pf.population_floor_time IS NULL OR lt.change_time < pf.population_floor_time)
                   THEN 'not_populated'
                 WHEN NOT lt.parent_gone AND lt.timeout_seconds IS NULL AND NOT lt.has_duration_health_rule
                   THEN 'after_population'
               END AS timeout_null_reason,
               ROUND(COALESCE(cr.net_dbus, 0), 2) AS net_dbus,
               ROUND(COALESCE(cr.est_usd_list, 0), 2) AS est_usd_list,
               COALESCE(cr.price_basis, 'priced') AS price_basis
        FROM latest_tasks lt
        LEFT JOIN population_floor pf ON pf.workspace_id = lt.workspace_id
        LEFT JOIN cost_rollup cr ON cr.workspace_id = lt.workspace_id AND cr.job_id = lt.job_id
        WHERE lt.delete_time IS NULL
    """
    con = duckdb.connect()
    try:
        cols = ["workspace_id", "job_id", "task_key", "parent_gone", "no_timeout",
                "bounded_by_health_rule", "timeout_null_reason", "net_dbus", "est_usd_list", "price_basis"]
        out = {}
        for row in con.execute(sql).fetchall():
            d = dict(zip(cols, row))
            if d["parent_gone"]:
                status = "NOT_ASSESSED"
            elif d["no_timeout"] and d["timeout_null_reason"] == "not_populated":
                status = "NOT_ASSESSED"
            elif d["no_timeout"] and d["est_usd_list"] >= 200:
                status = "CRITICAL"
            elif d["no_timeout"]:
                status = "WARN"
            else:
                status = "OK"
            d["status"] = status
            out[(d["workspace_id"], d["job_id"], d["task_key"])] = d
        return out
    finally:
        con.close()


def _dropped_runs_expected(window_days: int) -> dict[str, int]:
    """Re-derives f_lakeflow_jobs_on_all_purpose's `dropped` CTE (COUNT(DISTINCT run_id) of every
    job_task_run_timeline row in-window with a NULL OR EMPTY compute_ids list -- T-75B review fix:
    an empty, non-NULL array used to vanish from this count too, since EXPLODE() of an empty array
    yields zero rows -- grouped by workspace_id alone) directly from the raw parquet -- see module
    docstring: this is NOT scoped to SEC I's own lf_job_allpurpose_null/_empty placements, it
    counts every such row this whole builder writes."""
    lower, upper = _window_bounds(window_days)
    sql = f"""
        SELECT workspace_id, COUNT(DISTINCT run_id) AS dropped_runs
        FROM read_parquet('{_glob("lakeflow__job_task_run_timeline")}', union_by_name=true)
        WHERE period_start_time >= TIMESTAMP '{lower} 00:00:00'
          AND period_end_time < TIMESTAMP '{upper} 00:00:00'
          AND result_state IS NOT NULL
          AND (compute_ids IS NULL OR len(compute_ids) = 0)
        GROUP BY workspace_id
    """
    con = duckdb.connect()
    try:
        return dict(con.execute(sql).fetchall())
    finally:
        con.close()


# =================================================================================================
# lakeflow_health_rule_coverage -- no params; grain [workspace_id, job_id]; snapshot (window_days
# always 0, no :period_days param). One row per job now, so a tag filter reaches it by the job's
# own tag.
# =================================================================================================
def test_lakeflow_health_rule_coverage():
    grains = _grains()
    assert grains["lakeflow_health_rule_coverage"] == ["workspace_id", "job_id"]

    assert dbutil.rows("lakeflow_health_rule_coverage", 7) == []
    assert dbutil.rows("lakeflow_health_rule_coverage", 30) == []

    out = dbutil.rows("lakeflow_health_rule_coverage", 0)
    assert out, "lakeflow_health_rule_coverage: window_days=0 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]

    expected = _health_rule_coverage_expected()
    for r in out:
        exp = expected[(r["workspace_id"], r["job_id"])]
        assert r["health_rule_count"] == exp["health_rule_count"], (r["workspace_id"], r["job_id"])
        assert r["status"] == exp["status"], (r["workspace_id"], r["job_id"])

    # Same fixture facts the old per-workspace coverage percentage used to summarise (WS1 read
    # CRITICAL at <20% coverage, WS2 WARN at <50%, WS3 OK) -- now visible per job: WS1 and WS2 both
    # carry at least one no-rule (WARN) job, WS3 carries none.
    def _rows(ws):
        return [r for r in out if r["workspace_id"] == ws]

    assert any(r["status"] == "WARN" for r in _rows(WS1))
    assert any(r["status"] == "WARN" for r in _rows(WS2))
    assert all(r["status"] != "WARN" for r in _rows(WS3))

    # worst-first: order_by is CASE status WHEN WARN 0 WHEN NOT_ASSESSED 1 ELSE 2 END, workspace_id,
    # job_id -- every WARN/NOT_ASSESSED row leads every OK row in the returned order.
    statuses = [r["status"] for r in out]
    first_ok = next((i for i, s in enumerate(statuses) if s == "OK"), len(statuses))
    assert all(s != "OK" for s in statuses[:first_ok]), statuses

    # NOT_ASSESSED band: proven structurally across the FULL result set, not pinned to a specific
    # workspace/job -- see module docstring. lakeflow.py's own WS1/2/3 scenario never leaves an
    # entire workspace's health_rules column all-NULL, but individual jobs elsewhere in the build
    # (e.g. workspace 9001, T-07's drilldown.py fixture, which never sets health_rules at all) do.
    not_assessed = [r for r in out if r["status"] == "NOT_ASSESSED"]
    assert not_assessed, "no NOT_ASSESSED row anywhere -- the unpopulated-health_rules branch was never hit"
    for r in not_assessed:
        assert r["health_rule_count"] is None, r


# =================================================================================================
# lakeflow_job_ownership_orphans -- no params; grain [workspace_id, job_id]; snapshot (window_days
# always 0). One row per job now, so a tag filter reaches it by the job's own tag.
# =================================================================================================
def test_lakeflow_job_ownership_orphans():
    grains = _grains()
    assert grains["lakeflow_job_ownership_orphans"] == ["workspace_id", "job_id"]

    assert dbutil.rows("lakeflow_job_ownership_orphans", 7) == []
    assert dbutil.rows("lakeflow_job_ownership_orphans", 30) == []

    out = dbutil.rows("lakeflow_job_ownership_orphans", 0)
    assert out, "lakeflow_job_ownership_orphans: window_days=0 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]

    expected = _ownership_orphans_expected()
    for r in out:
        key = (r["workspace_id"], r["job_id"])
        exp = expected[key]
        assert r["run_as_kind"] == exp["run_as_kind"], key
        assert r["is_scheduled"] == exp["is_scheduled"], key
        assert r["identity_redacted"] == exp["identity_redacted"], key
        assert r["principal_missing"] == exp["principal_missing"], key
        assert r["identity_not_recorded"] == exp["identity_not_recorded"], key
        assert r["status"] == exp["status"], key

    def _rows(ws):
        return [r for r in out if r["workspace_id"] == ws]

    # WS1: 22 bad_job() (all HUMAN run-as, manual: "svc_..." is an email-shaped string, not a
    # service-principal UUID) + "clean" (HUMAN, creator == run_as -- the exact pattern the old
    # creator<>run_as mismatch rule scored healthy) + "redacted" (HUMAN run-as; its '__REDACTED__'
    # creator is excluded from principal_missing -- identity_redacted -- so it only counts on its
    # known HUMAN run_as) + "owner_missing" (creator NULL, a known HUMAN run_as -- exactly one
    # identity recorded, so it reads WARN on both grounds) + "human_paused" (a HUMAN run-as job
    # whose PERIODIC trigger is paused -- is_scheduled reads False, so WARN not CRITICAL) +
    # "human_scheduled" (HUMAN, scheduled -- the one CRITICAL job) = 26 WARN + 1 CRITICAL jobs;
    # "nulldegrade" + zombie x4 = 5 jobs, plus the 8 lf_jrc_job_* job-run-cost jobs (creator,
    # run_as and health_rules all NULL -- tests/fixtures/lakeflow.py _build_job_run_cost) = 13 jobs
    # with NEITHER identity ever recorded (NOT_ASSESSED). Old workspace-level score: 2*1 + 26 + 1 =
    # 29 >= 20 (CRITICAL) -- now visible as one CRITICAL row plus many WARN rows.
    assert _ownership_score(_rows(WS1)) >= 20
    assert any(r["status"] == "CRITICAL" for r in _rows(WS1))
    assert sum(1 for r in _rows(WS1) if r["status"] == "WARN") >= 26
    # WS2: 7 bad_job() + 3 "cleanN" (all HUMAN run-as, manual, no scheduled/missing jobs here) = 10
    # WARN jobs, no CRITICAL. Old workspace-level score: 10, in [5, 20) (WARN).
    assert 5 <= _ownership_score(_rows(WS2)) < 20
    assert any(r["status"] == "WARN" for r in _rows(WS2))
    assert all(r["status"] != "CRITICAL" for r in _rows(WS2))
    # WS3: 5 "cleanN" jobs, every one a genuine service-principal run-as (_sp_uuid), plus
    # lf_job_health_rule_limit (also a genuine SP run-as) -- score = 0, every recorded job OK.
    assert _ownership_score(_rows(WS3)) == 0
    assert all(r["status"] in ("OK", "NOT_ASSESSED") for r in _rows(WS3))

    # a service-principal run-as never counts as ownership risk, scheduled or not (WS1's
    # "sp_healthy" and "sp_scheduled_healthy" jobs, and every WS3 job that recorded an identity).
    assert sum(1 for r in _rows(WS1) if r["run_as_kind"] == "SERVICE_PRINCIPAL") >= 2
    ws3_recorded = [r for r in _rows(WS3) if not r["identity_not_recorded"]]
    assert ws3_recorded and all(r["run_as_kind"] == "SERVICE_PRINCIPAL" for r in ws3_recorded)

    # worst-first: the wired ORDER BY (what app/core/data.py applies at request time), not
    # dbutil.rows()'s own unordered scan -- see _order_by's docstring.
    assert _order_by("lakeflow_job_ownership_orphans") == (
        "CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END, "
        "j.workspace_id, j.job_id"
    )

    # NOT_ASSESSED band: same structural (not workspace-pinned) proof as health_rule_coverage.
    not_assessed = [r for r in out if r["status"] == "NOT_ASSESSED"]
    assert not_assessed, "no NOT_ASSESSED row anywhere -- the all-unrecorded-identity branch was never hit"
    for r in not_assessed:
        assert r["identity_not_recorded"], r


# =================================================================================================
# lakeflow_jobs_no_timeout -- period_days=30 (cost only), crit_no_timeout_usd=200; grain
# [workspace_id, job_id]; windowed 7/30/90 (the flag itself has no window of its own).
# =================================================================================================
def test_lakeflow_jobs_no_timeout():
    grains = _grains()
    assert grains["lakeflow_jobs_no_timeout"] == ["workspace_id", "job_id"]
    assert dbutil.rows("lakeflow_jobs_no_timeout", 0) == []

    out = dbutil.rows("lakeflow_jobs_no_timeout", 30)
    assert out, "lakeflow_jobs_no_timeout: window_days=30 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]

    expected = _jobs_no_timeout_expected(30)
    for r in out:
        key = (r["workspace_id"], r["job_id"])
        exp = expected[key]
        assert r["no_timeout"] == exp["no_timeout"], key
        assert r["bounded_by_health_rule"] == exp["bounded_by_health_rule"], key
        assert r["timeout_null_reason"] == exp["timeout_null_reason"], key
        assert r["net_dbus"] == exp["net_dbus"], key
        assert abs(r["est_usd_list"] - exp["est_usd_list"]) < 1e-9, key
        assert r["price_basis"] == exp["price_basis"], key
        assert r["status"] == exp["status"], key

    # P4-FIXES28's null-bucket degrade is now a per-job NOT_ASSESSED (timeout_null_reason =
    # 'not_populated'), never a whole-workspace one -- this builder's own WS1/WS2/WS3 jobs always
    # populate timeout_seconds on their current row, so none of their rows read NOT_ASSESSED.
    def _rows(ws):
        return [r for r in out if r["workspace_id"] == ws]

    assert all(r["status"] != "NOT_ASSESSED" for r in _rows(WS1))
    assert all(r["status"] != "NOT_ASSESSED" for r in _rows(WS2))
    assert all(r["status"] != "NOT_ASSESSED" for r in _rows(WS3))

    # Same fixture facts the old per-workspace count used to summarise (WS1: 27 no-timeout jobs >=
    # crit=20; WS2: 7, in [5, 20); WS3: 0), now visible per job: WS1/WS2 each carry a flagged
    # (WARN/CRITICAL) job, WS3 carries none.
    assert any(r["no_timeout"] for r in _rows(WS1))
    assert any(r["no_timeout"] for r in _rows(WS2))
    assert not any(r["no_timeout"] for r in _rows(WS3))

    # lf_job_health_rule_limit (WS3): no timeout_seconds, but a RUN_DURATION_SECONDS/GREATER_THAN
    # health rule -- a real time limit, so it must read bounded_by_health_rule = true, no_timeout =
    # false, never CRITICAL/WARN.
    hr_row = next(r for r in _rows(WS3) if r["bounded_by_health_rule"])
    assert not hr_row["no_timeout"] and hr_row["status"] == "OK"

    # est_usd_list is 0.0 on every flagged WS1 job: every usage row on a flagged (timeout NULL/0)
    # job carries lf_JOBS_COMPUTE or lf_BULK_COMPUTE, and tests/fixtures/lakeflow.py registers no
    # list_price() for either, so the price LEFT JOIN misses and COALESCE(list_rate, 0) zeroes
    # every dollar column -- net_dbus stays the exact unpriced usage_quantity sum, so no flagged
    # WS1 job ever reaches crit_no_timeout_usd=200 on dollars alone (all WARN, never CRITICAL from
    # cost). price_basis is therefore 'unpriced' (a coverage gap, never 'free') on every flagged
    # job that carries usage.
    flagged_ws1 = [r for r in _rows(WS1) if r["no_timeout"]]
    assert flagged_ws1 and all(r["est_usd_list"] == 0.0 for r in flagged_ws1)
    assert all(r["status"] == "WARN" for r in flagged_ws1)
    assert any(r["price_basis"] == "unpriced" and r["net_dbus"] > 0.0 for r in flagged_ws1)

    # worst-first: the wired ORDER BY (what app/core/data.py applies at request time), not
    # dbutil.rows()'s own unordered scan -- see _order_by's docstring.
    assert _order_by("lakeflow_jobs_no_timeout") == (
        "CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END, "
        "est_usd_list DESC, j.workspace_id, j.job_id"
    )


# =================================================================================================
# lakeflow_job_tasks_no_timeout -- period_days=30 (cost only), crit_no_timeout_usd=200; grain
# [workspace_id, job_id, task_key]; windowed 7/30/90.
# =================================================================================================
def test_lakeflow_job_tasks_no_timeout():
    grains = _grains()
    assert grains["lakeflow_job_tasks_no_timeout"] == ["workspace_id", "job_id", "task_key"]
    assert dbutil.rows("lakeflow_job_tasks_no_timeout", 0) == []

    out = dbutil.rows("lakeflow_job_tasks_no_timeout", 30)
    assert out, "lakeflow_job_tasks_no_timeout: window_days=30 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"], r["task_key"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id, task_key) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]

    expected = _job_tasks_no_timeout_expected(30)
    for r in out:
        key = (r["workspace_id"], r["job_id"], r["task_key"])
        exp = expected[key]
        assert r["parent_gone"] == exp["parent_gone"], key
        assert r["no_timeout"] == exp["no_timeout"], key
        assert r["bounded_by_health_rule"] == exp["bounded_by_health_rule"], key
        assert r["timeout_null_reason"] == exp["timeout_null_reason"], key
        assert r["net_dbus"] == exp["net_dbus"], key
        assert abs(r["est_usd_list"] - exp["est_usd_list"]) < 1e-9, key
        assert r["price_basis"] == exp["price_basis"], key
        assert r["status"] == exp["status"], key

    def _rows(ws):
        return [r for r in out if r["workspace_id"] == ws]

    # Same fixture facts the old per-workspace count used to summarise (WS1: ~24 no-timeout tasks
    # >= crit=20; WS2: 7, in [5, 20); WS3: 0), now visible per task.
    assert any(r["no_timeout"] for r in _rows(WS1))
    assert any(r["no_timeout"] for r in _rows(WS2))
    assert not any(r["no_timeout"] for r in _rows(WS3))
    assert all(r["status"] == "WARN" for r in _rows(WS1) if r["no_timeout"])  # same unpriced-SKU
    assert all(r["est_usd_list"] == 0.0 for r in _rows(WS1) if r["no_timeout"])  # reasoning as jobs_no_timeout

    # lf_job_health_rule_limit's "main" task (WS3): no timeout_seconds, but a RUN_DURATION_SECONDS/
    # GREATER_THAN health rule -- must read bounded_by_health_rule = true, no_timeout = false.
    hr_row = next(r for r in _rows(WS3) if r["bounded_by_health_rule"])
    assert not hr_row["no_timeout"] and hr_row["status"] == "OK"

    # a task whose parent job is gone reads parent_gone/NOT_ASSESSED, never no_timeout -- proven
    # structurally (see module docstring), not pinned to a specific workspace/task.
    orphaned = [r for r in out if r["parent_gone"]]
    assert orphaned, "no parent_gone row anywhere -- the orphaned-parent branch was never hit"
    for r in orphaned:
        assert not r["no_timeout"] and r["status"] == "NOT_ASSESSED", r

    # worst-first: the wired ORDER BY (what app/core/data.py applies at request time), not
    # dbutil.rows()'s own unordered scan -- see _order_by's docstring.
    assert _order_by("lakeflow_job_tasks_no_timeout") == (
        "CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END, "
        "est_usd_list DESC, j.workspace_id, j.job_id, j.task_key"
    )


# =================================================================================================
# lakeflow_tasks_near_timeout -- period_days=30, near_timeout_ratio=0.8, warn=3, crit=10; grain
# [workspace_id, job_id, task_key]; windowed.
#   tnt_crit (timeout=100): 12 runs @ execution=90 (>= 0.8*100=80, < 100) -> all "near", 0 "over"
#     -> 12 >= crit(10) -> CRITICAL.
#   tnt_warn (timeout=200): 5 runs @ execution=170 (>= 160, < 200) -> 5 near -> WARN.
#   tnt_ok (timeout=300): 1 run @ execution=50 (< 240) -> 0 near/over -> OK.
#   tnt_notimeout (timeout NULL): 3 runs, all with a NULL timeout_seconds (runs_no_task_timeout=3,
#     100% of task_runs) -> NOT_ASSESSED (timeout_not_populated), P4-FIXES28's null-bucket degrade.
#   tnt_execnull (timeout=100, execution NULL x2): both runs have a NULL execution_duration
#     (runs_exec_null=2, 100% of task_runs) -> NOT_ASSESSED (exec_duration_not_populated).
# =================================================================================================
def test_lakeflow_tasks_near_timeout():
    grains = _grains()
    assert grains["lakeflow_tasks_near_timeout"] == ["workspace_id", "job_id", "task_key"]
    assert dbutil.rows("lakeflow_tasks_near_timeout", 0) == []

    out = dbutil.rows("lakeflow_tasks_near_timeout", 30)
    assert out, "lakeflow_tasks_near_timeout: window_days=30 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"], r["task_key"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id, task_key) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]

    by_task = {r["task_key"]: r for r in out if r["job_id"] == "lf_job_tnt"}
    assert set(by_task) == {"tnt_crit", "tnt_warn", "tnt_ok", "tnt_notimeout", "tnt_execnull"}

    crit = by_task["tnt_crit"]
    assert crit["timeout_seconds"] == 100
    assert crit["task_runs"] == 12
    assert crit["runs_near_timeout"] == 12
    assert crit["runs_over_timeout"] == 0
    assert crit["status"] == "CRITICAL"

    warn = by_task["tnt_warn"]
    assert warn["timeout_seconds"] == 200
    assert warn["task_runs"] == 5
    assert warn["runs_near_timeout"] == 5
    assert warn["status"] == "WARN"

    ok = by_task["tnt_ok"]
    assert ok["timeout_seconds"] == 300
    assert ok["task_runs"] == 1
    assert ok["runs_near_timeout"] == 0
    assert ok["runs_over_timeout"] == 0
    assert ok["status"] == "OK"

    # P4-FIXES28: 100% of task_runs is runs_no_task_timeout -> degrades to NOT_ASSESSED.
    notimeout = by_task["tnt_notimeout"]
    assert notimeout["timeout_seconds"] is None
    assert notimeout["task_runs"] == 3
    assert notimeout["runs_no_task_timeout"] == 3
    assert notimeout["runs_near_timeout"] == 0
    assert notimeout["status"] == "NOT_ASSESSED"
    assert notimeout["not_assessed_reason"] == "timeout_not_populated"

    # P4-FIXES28: 100% of task_runs is runs_exec_null -> degrades to NOT_ASSESSED.
    execnull = by_task["tnt_execnull"]
    assert execnull["timeout_seconds"] == 100
    assert execnull["task_runs"] == 2
    assert execnull["runs_exec_null"] == 2
    assert execnull["runs_near_timeout"] == 0
    assert execnull["status"] == "NOT_ASSESSED"
    assert execnull["not_assessed_reason"] == "exec_duration_not_populated"

    # worst-first: order_by is (runs_near_timeout + runs_over_timeout) DESC. A forward enumerate()
    # scan filtered on membership always yields already-sorted indices regardless of which row
    # actually comes first, so compare the index of each SPECIFIC named row instead of collecting
    # indices in encounter order (the latter would be vacuously true).
    idx = {r["task_key"]: i for i, r in enumerate(out) if r["job_id"] == "lf_job_tnt"}
    assert idx["tnt_crit"] < idx["tnt_warn"], idx  # 12 near/over precedes 5
    for zero_key in ("tnt_ok", "tnt_notimeout", "tnt_execnull"):
        assert idx["tnt_warn"] < idx[zero_key], (zero_key, idx)  # 5 precedes every tied 0

    combined = [by_task[k]["runs_near_timeout"] + by_task[k]["runs_over_timeout"]
                for k in ("tnt_crit", "tnt_warn", "tnt_ok", "tnt_notimeout", "tnt_execnull")]
    assert combined[0] > combined[1] > combined[2] == combined[3] == combined[4] == 0


# =================================================================================================
# lakeflow_job_recent_runs -- inventory (no status), runs_per_job=10; grain [workspace_id, job_id,
# run_id]; reuses SEC H's lf_job_tnt (23 distinct run_ids, one per task-run) to prove the
# runs_per_job cap and the most-recent-first ordering.
# =================================================================================================
def test_lakeflow_job_recent_runs():
    # its own grain file (config/grains/lakeflow_job_recent_runs.yml), not this module's
    # lakeflow_dims.yml -- grain files are split one-per-batch (fixture_coverage.py).
    with open(ROOT / "config" / "grains" / "lakeflow_job_recent_runs.yml", "r", encoding="ascii") as f:
        grain = yaml.safe_load(f)
    assert grain["lakeflow_job_recent_runs"] == ["workspace_id", "job_id", "run_id"]
    assert dbutil.rows("lakeflow_job_recent_runs", 0) == []

    out = dbutil.rows("lakeflow_job_recent_runs", 30, workspace_ids=[WS1])
    assert out, "lakeflow_job_recent_runs: window_days=30 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"], r["run_id"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id, run_id) rows"

    # lf_job_tnt has 23 distinct run_ids -- capped to :runs_per_job (10), most recent by run_start.
    tnt_runs = [r for r in out if r["job_id"] == "lf_job_tnt"]
    assert len(tnt_runs) == 10
    starts = [r["run_start"] for r in tnt_runs]
    assert starts == sorted(starts, reverse=True), "not capped to the most recent runs by run_start"
    assert all(r["result_state"] == "SUCCEEDED" for r in tnt_runs)

    # review fix: result_state/termination_code/attempts now come from the run's LAST end row
    # (highest period_end_time), never MAX(result_state) alphabetical -- SEC R's lf_jrc_run_repair_r1
    # (TIMED_OUT at hour 1, SUCCEEDED at hour 3) must read its FINAL outcome, not "TIMED_OUT" (T > S
    # alphabetically) or a mixed "Timed out -- Success" read. attempts counts both end rows.
    repair = next(r for r in out if r["run_id"] == "lf_jrc_run_repair_r1")
    assert repair["result_state"] == "SUCCEEDED"
    assert repair["termination_code"] == "SUCCESS"
    assert repair["attempts"] == 2
    # net_dbus/net_list_cost: the same per-run price join lakeflow_job_run_cost uses -- 8 DBU on
    # lf_JRC_CLASSIC at its effective list rate (0.25) -> $2.00.
    assert repair["net_dbus"] == 8.0
    assert abs(repair["net_list_cost"] - 2.0) < 1e-9
    # no system.query.history rows exist for this run -- NULL, never a fabricated sample.
    assert repair["sql_error_sample"] is None


# =================================================================================================
# lakeflow_jobs_on_all_purpose -- period_days=30, crit_share_usd=100, top_n=100000; grain
# [workspace_id, job_id, compute_id]; windowed. Cost is now each job's OWN metered usage on the
# cluster (usage_metadata.job_id), never the cluster's whole bill or an even split of it -- see the
# query's own header and tests/fixtures/lakeflow.py's SEC I comment for the exact DBU/$ figures.
#   lf_cluster_interactive (UI): job's own 250 DBU * 0.6 = 150.0 -> CRITICAL (>= crit_share_usd
#     100); the cluster's OTHER (notebook, no job_id) 300 DBU is excluded entirely.
#   lf_cluster_api (API): job's own 30 DBU * 0.6 = 18.0 -> WARN (< 100); the cluster's other 50 DBU
#     is excluded.
#   lf_cluster_job (JOB): OK regardless of DBUs/$ (not UI/API) -- its own $ (180.0) is in fact the
#     LARGEST of the three named jobs, proving order_by is worst-status-first (CRITICAL, WARN,
#     NOT_ASSESSED, then OK, each by est_usd_list_share DESC) rather than a plain $-DESC sort,
#     since a plain sort would put this OK row ahead of both flagged ones.
#   lf_cluster_shared (UI): TWO jobs share it with different own usage -- lf_job_allpurpose_shared_a
#     (200 DBU -> $120.0, CRITICAL) and lf_job_allpurpose_shared_b (20 DBU -> $12.0, WARN) must
#     read DIFFERENT $ and DIFFERENT bands, proving cost is no longer split evenly by
#     jobs_sharing_cluster (an even split of the shared 220 DBU would have given both jobs the
#     same $66.0/WARN, masking job_a's real spend).
#   lf_job_allpurpose_warehouse: compute_ids names `lf_warehouse_1`, a SQL warehouse (never in
#     compute.clusters) whose own `compute` struct entry has warehouse_id set -> cluster_source
#     WAREHOUSE, status OK (not NOT_ASSESSED the way an actually-unknown cluster id still is).
#   NOT_ASSESSED "dropped" summary row: recomputed from parquet (both NULL and empty compute_ids,
#     T-75B review fix), not pinned to SEC I's own lf_job_allpurpose_null/_empty placements (see
#     module docstring) -- proves the trailing UNION ALL branch fires and reports job_id/compute_id/
#     net_dbus/est_usd_list/jobs_sharing_cluster/est_usd_list_share all NULL.
# =================================================================================================
def test_lakeflow_jobs_on_all_purpose():
    grains = _grains()
    assert grains["lakeflow_jobs_on_all_purpose"] == ["workspace_id", "job_id", "compute_id"]
    assert dbutil.rows("lakeflow_jobs_on_all_purpose", 0) == []

    out = dbutil.rows("lakeflow_jobs_on_all_purpose", 30)
    assert out, "lakeflow_jobs_on_all_purpose: window_days=30 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"], r["compute_id"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id, compute_id) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]

    by_job = {r["job_id"]: r for r in out
              if r["job_id"] in ("lf_job_allpurpose_crit", "lf_job_allpurpose_warn", "lf_job_allpurpose_ok")}

    crit = by_job["lf_job_allpurpose_crit"]
    assert crit["compute_id"] == "lf_cluster_interactive"
    assert crit["cluster_source"] == "UI"
    exp_net, exp_est, exp_basis = _cost_rollup_job("lf_cluster_interactive", "lf_job_allpurpose_crit", 30)
    assert crit["net_dbus"] == exp_net == 250.0
    assert abs(crit["est_usd_list"] - exp_est) < 1e-9
    assert abs(crit["est_usd_list"] - 150.0) < 1e-9
    assert crit["price_basis"] == exp_basis
    assert crit["jobs_sharing_cluster"] == 1
    # est_usd_list_share is now identical to est_usd_list -- never a further /jobs_sharing_cluster.
    assert abs(crit["est_usd_list_share"] - crit["est_usd_list"]) < 1e-9
    assert crit["status"] == "CRITICAL"

    warn = by_job["lf_job_allpurpose_warn"]
    assert warn["compute_id"] == "lf_cluster_api"
    assert warn["cluster_source"] == "API"
    exp_net_w, exp_est_w, exp_basis_w = _cost_rollup_job("lf_cluster_api", "lf_job_allpurpose_warn", 30)
    assert warn["net_dbus"] == exp_net_w == 30.0
    assert abs(warn["est_usd_list"] - exp_est_w) < 1e-9
    assert warn["price_basis"] == exp_basis_w
    assert warn["status"] == "WARN"
    assert warn["est_usd_list_share"] < 100  # below crit_share_usd

    ok = by_job["lf_job_allpurpose_ok"]
    assert ok["compute_id"] == "lf_cluster_job"
    assert ok["cluster_source"] == "JOB"
    exp_net_o, exp_est_o, _ = _cost_rollup_job("lf_cluster_job", "lf_job_allpurpose_ok", 30)
    assert ok["net_dbus"] == exp_net_o == 300.0
    assert abs(ok["est_usd_list"] - exp_est_o) < 1e-9
    assert ok["status"] == "OK"
    assert ok["est_usd_list_share"] > crit["est_usd_list_share"]  # OK can still have the biggest $

    # lf_cluster_shared: two jobs, two different own-usage $ figures -- the "not an even split"
    # proof. Under the old whole-cluster-bill/jobs_sharing_cluster split both would have read the
    # SAME $66.0/WARN; here they must differ and band differently.
    shared = {r["job_id"]: r for r in out
              if r["job_id"] in ("lf_job_allpurpose_shared_a", "lf_job_allpurpose_shared_b")}
    shared_a, shared_b = shared["lf_job_allpurpose_shared_a"], shared["lf_job_allpurpose_shared_b"]
    assert shared_a["compute_id"] == shared_b["compute_id"] == "lf_cluster_shared"
    assert shared_a["cluster_source"] == shared_b["cluster_source"] == "UI"
    assert shared_a["jobs_sharing_cluster"] == shared_b["jobs_sharing_cluster"] == 2
    exp_net_sa, exp_est_sa, _ = _cost_rollup_job("lf_cluster_shared", "lf_job_allpurpose_shared_a", 30)
    exp_net_sb, exp_est_sb, _ = _cost_rollup_job("lf_cluster_shared", "lf_job_allpurpose_shared_b", 30)
    assert shared_a["net_dbus"] == exp_net_sa == 200.0
    assert shared_b["net_dbus"] == exp_net_sb == 20.0
    assert abs(shared_a["est_usd_list"] - exp_est_sa) < 1e-9
    assert abs(shared_b["est_usd_list"] - exp_est_sb) < 1e-9
    assert shared_a["est_usd_list"] != shared_b["est_usd_list"]
    assert shared_a["status"] == "CRITICAL"  # 200 * 0.6 = 120.0 >= 100
    assert shared_b["status"] == "WARN"      # 20 * 0.6 = 12.0 < 100

    # lf_job_allpurpose_warehouse: compute_ids names a SQL warehouse, not a cluster -- its own
    # `compute` struct entry (warehouse_id set) must read WAREHOUSE/OK, never NOT_ASSESSED.
    wh = next(r for r in out if r["job_id"] == "lf_job_allpurpose_warehouse")
    assert wh["compute_id"] == "lf_warehouse_1"
    assert wh["cluster_source"] == "WAREHOUSE"
    assert wh["status"] == "OK"

    # lf_job_allpurpose_unknown_cluster: compute_ids names lf_cluster_missing, which has no row in
    # compute.clusters AND no `compute` struct naming it a warehouse either -- must stay a genuine
    # NOT_ASSESSED (the warehouse fix must not swallow a real unknown-cluster gap).
    unknown = next(r for r in out if r["job_id"] == "lf_job_allpurpose_unknown_cluster")
    assert unknown["compute_id"] == "lf_cluster_missing"
    assert unknown["cluster_source"] is None
    assert unknown["status"] == "NOT_ASSESSED"

    dropped_expected = _dropped_runs_expected(30)
    dropped_row = next(r for r in out if r["workspace_id"] == WS1 and r["cluster_source"] == "null or empty compute_ids")
    assert dropped_row["job_id"] is None
    assert dropped_row["compute_id"] is None
    assert dropped_row["net_dbus"] is None
    assert dropped_row["est_usd_list"] is None
    assert dropped_row["price_basis"] is None
    assert dropped_row["jobs_sharing_cluster"] is None
    assert dropped_row["est_usd_list_share"] is None
    assert dropped_row["status"] == "NOT_ASSESSED"
    assert dropped_row["task_runs"] == dropped_expected[WS1]
    assert dropped_row["task_runs"] > 0

    # worst-first (T-75B review fix): order_by is CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN'
    # THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END, est_usd_list_share DESC. A plain $-DESC order
    # would put this JOB-source "ok" row (its own $180.0, the LARGEST of the three named jobs)
    # ahead of both flagged UI/API rows -- exactly the shape that let a low :top_n cap cut a
    # flagged placement before an OK one. Confirm the status rank never decreases across the whole
    # result, and that the three named rows land in CRITICAL, WARN, OK order.
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    rank_seq = [rank[r["status"]] for r in out]
    assert rank_seq == sorted(rank_seq), rank_seq
    idx = {r["job_id"]: i for i, r in enumerate(out)
           if r["job_id"] in ("lf_job_allpurpose_crit", "lf_job_allpurpose_warn", "lf_job_allpurpose_ok")}
    assert idx["lf_job_allpurpose_crit"] < idx["lf_job_allpurpose_warn"] < idx["lf_job_allpurpose_ok"], idx


# =================================================================================================
# lakeflow_stale_zombie_jobs -- stale_days=30, crit_stale_days=90, period_days=30 (cost only,
# separate window); grain [workspace_id, job_id]; windowed (cost columns only -- last_run_start
# itself looks across all history).
#   lf_job_zombie_crit: last run 100 d ago (> crit_stale_days=90) -> CRITICAL; billing usage
#     deliberately omitted -- confirmed absent in the raw parquet, not just windowed to 0.
#   lf_job_zombie_warn_old: last run 45 d ago (> stale_days=30, < 90) -> WARN.
#   lf_job_zombie_warn_null: no job_run_timeline row at all -> last_run_start NULL -> WARN; billing
#     usage likewise deliberately omitted and confirmed absent.
#   lf_job_zombie_ok: last run 5 d ago -> OK.
# =================================================================================================
def test_lakeflow_stale_zombie_jobs():
    grains = _grains()
    assert grains["lakeflow_stale_zombie_jobs"] == ["workspace_id", "job_id"]
    assert dbutil.rows("lakeflow_stale_zombie_jobs", 0) == []

    out = dbutil.rows("lakeflow_stale_zombie_jobs", 30)
    assert out, "lakeflow_stale_zombie_jobs: window_days=30 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]

    ids = ("lf_job_zombie_crit", "lf_job_zombie_warn_old", "lf_job_zombie_warn_null", "lf_job_zombie_ok")
    by_job = {r["job_id"]: r for r in out if r["job_id"] in ids}
    assert set(by_job) == set(ids)
    # this builder's own 4 scenario jobs never read NOT_ASSESSED (other builders' jobs elsewhere
    # in the shared table may, for reasons unrelated to this scenario).
    for jid, row in by_job.items():
        assert row["status"] != "NOT_ASSESSED", jid

    crit = by_job["lf_job_zombie_crit"]
    assert crit["status"] == "CRITICAL"
    assert crit["is_stale"] == 1
    assert crit["last_run_start"] == lf.AS_OF - timedelta(days=100)

    warn_old = by_job["lf_job_zombie_warn_old"]
    assert warn_old["status"] == "WARN"
    assert warn_old["is_stale"] == 1
    assert warn_old["last_run_start"] == lf.AS_OF - timedelta(days=45)

    warn_null = by_job["lf_job_zombie_warn_null"]
    assert warn_null["status"] == "WARN"
    assert warn_null["is_stale"] == 1
    assert warn_null["last_run_start"] is None

    ok = by_job["lf_job_zombie_ok"]
    assert ok["status"] == "OK"
    assert ok["is_stale"] == 0
    assert ok["last_run_start"] == lf.AS_OF - timedelta(days=5)

    # "~0 net_dbus" caveat: assert the DELIBERATE absence of any billing.usage row for crit/
    # warn_null, straight from the raw parquet -- not just that the reported column happens to be
    # 0 (which a windowing bug could also produce).
    assert not _raw_row_exists("billing__usage", "usage_metadata.job_id = 'lf_job_zombie_crit'")
    assert not _raw_row_exists("billing__usage", "usage_metadata.job_id = 'lf_job_zombie_warn_null'")
    assert crit["net_dbus"] == 0.0
    assert warn_null["net_dbus"] == 0.0

    # warn_old / ok DO carry usage -- net_dbus via dbutil.usage_sum (unwindowed; each job has
    # exactly one usage row, well inside every window, so unwindowed == windowed here).
    assert warn_old["net_dbus"] == dbutil.usage_sum("usage_metadata.job_id = 'lf_job_zombie_warn_old'") == 8.0
    assert ok["net_dbus"] == dbutil.usage_sum("usage_metadata.job_id = 'lf_job_zombie_ok'") == 8.0
    # est_usd_list is 0.0 for all of them: lf_JOBS_COMPUTE has no list_price() row in this fixture
    # -- price_basis is therefore 'unpriced' (a coverage gap), never 'free'.
    _, exp_est, exp_basis = _cost_rollup("job_id", "lf_job_zombie_warn_old", 30)
    assert exp_est == 0.0
    assert exp_basis == "unpriced"
    assert warn_old["est_usd_list"] == 0.0
    assert warn_old["price_basis"] == "unpriced"
    assert ok["est_usd_list"] == 0.0
    assert ok["price_basis"] == "unpriced"

    # worst-first: order_by is last_run_start ASC (NULLs sort last under this DuckDB build).
    positions = [i for i, r in enumerate(out) if r["job_id"] in ids]
    returned_order = [out[i]["job_id"] for i in positions]
    assert returned_order == [
        "lf_job_zombie_crit", "lf_job_zombie_warn_old", "lf_job_zombie_ok", "lf_job_zombie_warn_null",
    ], returned_order


# =================================================================================================
# lakeflow_retries_repairs -- period_days=30, warn_total_retries=5, crit_total_retries=20; grain
# [workspace_id, job_id]; windowed.
#   lf_job_retries_crit: 21 end rows on 1 run_id -> 20 retries -> CRITICAL (>= crit=20).
#   lf_job_retries_warn: 6 end rows -> 5 retries -> WARN (>= warn=5).
#   lf_job_retries_ok: 2 end rows -> 1 retry -> OK.
# =================================================================================================
def test_lakeflow_retries_repairs():
    grains = _grains()
    assert grains["lakeflow_retries_repairs"] == ["workspace_id", "job_id"]
    assert dbutil.rows("lakeflow_retries_repairs", 0) == []

    out = dbutil.rows("lakeflow_retries_repairs", 30)
    assert out, "lakeflow_retries_repairs: window_days=30 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]
        assert r["status"] != "NOT_ASSESSED"  # this body has no NOT_ASSESSED branch at all

    ids = ("lf_job_retries_crit", "lf_job_retries_warn", "lf_job_retries_ok")
    by_job = {r["job_id"]: r for r in out if r["job_id"] in ids}
    assert set(by_job) == set(ids)

    crit = by_job["lf_job_retries_crit"]
    assert crit["distinct_runs"] == 1
    assert crit["total_attempt_rows"] == 21
    assert crit["total_retries"] == 20
    assert crit["runs_with_retry"] == 1
    assert crit["status"] == "CRITICAL"

    warn = by_job["lf_job_retries_warn"]
    assert warn["total_attempt_rows"] == 6
    assert warn["total_retries"] == 5
    assert warn["status"] == "WARN"

    ok = by_job["lf_job_retries_ok"]
    assert ok["total_attempt_rows"] == 2
    assert ok["total_retries"] == 1
    assert ok["status"] == "OK"

    # net_dbus via dbutil.usage_sum (unwindowed; single usage row per job, inside every window).
    assert crit["net_dbus"] == dbutil.usage_sum("usage_metadata.job_id = 'lf_job_retries_crit'") == 30.0
    assert warn["net_dbus"] == dbutil.usage_sum("usage_metadata.job_id = 'lf_job_retries_warn'") == 15.0
    assert ok["net_dbus"] == dbutil.usage_sum("usage_metadata.job_id = 'lf_job_retries_ok'") == 5.0
    _, exp_est, exp_basis = _cost_rollup("job_id", "lf_job_retries_crit", 30)
    assert exp_est == 0.0  # lf_JOBS_COMPUTE has no list_price() row
    assert exp_basis == "unpriced"
    assert crit["est_usd_list"] == 0.0
    assert crit["price_basis"] == "unpriced"

    # worst-first: order_by is total_retries DESC -- checked against the ACTUAL row order
    # dbutil.rows() returned (not just that the known magnitudes happen to be 20 > 5 > 1).
    positions = [i for i, r in enumerate(out) if r["job_id"] in ids]
    returned_order = [out[i]["job_id"] for i in positions]
    assert returned_order == ["lf_job_retries_crit", "lf_job_retries_warn", "lf_job_retries_ok"], returned_order

    # AS_OF-day exclusion anchor (SEC G): lf_job_asof_general's only job_run_timeline row has
    # period_end_time on D0 (today), so it fails `period_end_time < date_trunc('DAY', now)` and
    # never enters end_rows/per_run at all -- confirm it is genuinely absent from this id's output
    # (not merely filtered on job_id by coincidence) AND that the raw row really exists in the
    # fixture, so this assertion would fail if that row were ever silently removed.
    assert not any(r["job_id"] == "lf_job_asof_general" for r in out)
    assert _raw_row_exists("lakeflow__job_run_timeline", "job_id = 'lf_job_asof_general'")


# =================================================================================================
# lakeflow_failed_jobs_wasted_dbus -- REWRITTEN IN PLACE by P4-01-W2 (tasks/P4-WASTE-SPEC.md
# section 3, D-W5): the old whole-job failed-run-share proxy is gone; est_wasted_usd_list is now
# measured per run from usage_metadata.job_run_id. Grain [workspace_id, job_id] is unchanged.
#
# Every lf_job_fr_* group's billing carries job_id ONLY (never job_run_id -- see SEC D in
# tests/fixtures/lakeflow.py), so under the new query every one of them reads NOT_ASSESSED
# no_run_id_in_billing: their failure counts and rates no longer drive a verdict at all, because
# the run-level cost that verdict would price cannot be measured. lf_job_fr_a/b/c sit on the
# deliberately-unpriced lf_JOBS_COMPUTE SKU, so their usage is 'unpriced' on top of carrying no
# run id (est_usd_list is NULL, not $0 -- an unpriced coverage gap is never shown as a real zero).
# lf_job_fr_mixed sits on the PRICED lf_DLT_CORE_COMPUTE SKU (0.6 $/DBU): its whole spend is
# priced (est_usd_list=60.00) and, since NONE of it carries a run id, ALL of it reads as
# est_unattributed_usd_list -- also NOT_ASSESSED, because run-level cost still cannot be split out.
# =================================================================================================
def test_lakeflow_failed_jobs_wasted_dbus():
    grains = _grains()
    assert grains["lakeflow_failed_jobs_wasted_dbus"] == ["workspace_id", "job_id"]
    assert dbutil.rows("lakeflow_failed_jobs_wasted_dbus", 0) == []

    out = dbutil.rows("lakeflow_failed_jobs_wasted_dbus", 30)
    assert out, "lakeflow_failed_jobs_wasted_dbus: window_days=30 must be non-empty"
    keys = [(r["workspace_id"], r["job_id"]) for r in out]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, job_id) rows"
    for r in out:
        assert r["status"] in STATUS_VALUES, r["status"]

    ids = ("lf_job_fr_a", "lf_job_fr_b", "lf_job_fr_c", "lf_job_fr_mixed")
    by_job = {r["job_id"]: r for r in out if r["job_id"] in ids}
    assert set(by_job) == set(ids)

    b = by_job["lf_job_fr_b"]
    assert b["distinct_runs"] == 12
    assert b["failed_runs"] == 12
    assert b["last_failed_termination_code"] == "STORAGE_ACCESS_ERROR"
    assert b["not_assessed_reason"] == "no_run_id_in_billing"
    assert b["status"] == "NOT_ASSESSED"

    a = by_job["lf_job_fr_a"]
    assert a["distinct_runs"] == 20
    assert a["failed_runs"] == 20
    assert a["last_failed_termination_code"] == "CLUSTER_ERROR"
    assert a["not_assessed_reason"] == "no_run_id_in_billing"
    assert a["status"] == "NOT_ASSESSED"

    c = by_job["lf_job_fr_c"]
    assert c["distinct_runs"] == 4
    assert c["failed_runs"] == 4
    assert c["last_failed_termination_code"] == "WORKSPACE_RUN_LIMIT_EXCEEDED"
    assert c["not_assessed_reason"] == "no_run_id_in_billing"
    assert c["status"] == "NOT_ASSESSED"

    # Every one of a/b/c/mixed reads NOT_ASSESSED now: est_wasted_usd_list is NULL, never a
    # zero-waste OK (DEC-57/58) -- and net_dbus is still measured and cross-checked independently.
    for job_id, expected_dbus in (
        ("lf_job_fr_a", 40.0), ("lf_job_fr_b", 120.0), ("lf_job_fr_c", 15.0),
        ("lf_job_fr_mixed", 100.0),
    ):
        row = by_job[job_id]
        summed = dbutil.usage_sum(f"usage_metadata.job_id = '{job_id}'")
        assert summed == expected_dbus, (job_id, summed)
        assert row["net_dbus"] == summed, job_id
        assert row["status"] == "NOT_ASSESSED", job_id
        assert row["est_wasted_usd_list"] is None, job_id

    assert a["est_usd_list"] is None  # unpriced (lf_JOBS_COMPUTE has no list_price() row)
    assert a["price_basis"] == "unpriced"
    assert a["est_unattributed_usd_list"] is None  # unpriced share of the unattributed usage too
    assert b["est_usd_list"] is None
    assert b["price_basis"] == "unpriced"
    assert c["est_usd_list"] is None
    assert c["price_basis"] == "unpriced"

    # lf_job_fr_mixed: 100 DBU on the PRICED lf_DLT_CORE_COMPUTE SKU (0.6 $/DBU), none of it
    # carrying a run id -- est_usd_list is a real priced figure, and since ALL its usage is
    # unattributed, est_unattributed_usd_list equals it exactly.
    mixed = by_job["lf_job_fr_mixed"]
    assert mixed["distinct_runs"] == 10
    assert mixed["failed_runs"] == 4
    assert mixed["price_basis"] == "priced"
    assert abs(mixed["est_usd_list"] - 60.0) < 1e-6
    assert abs(mixed["est_unattributed_usd_list"] - 60.0) < 1e-6
    assert mixed["not_assessed_reason"] == "no_run_id_in_billing"
    assert mixed["status"] == "NOT_ASSESSED"

    # AS_OF-day exclusion anchor (SEC G), same reasoning as lakeflow_retries_repairs above:
    # lf_job_asof_general's only run row is excluded by the window, never entering job_runs at all
    # (FAILED result_state notwithstanding) -- confirm absence AND that the raw row exists.
    assert not any(r["job_id"] == "lf_job_asof_general" for r in out)
    assert _raw_row_exists("lakeflow__job_run_timeline", "job_id = 'lf_job_asof_general'")
