"""tests/test_findings/test_query_top_by_cost.py -- proves findings.f_query_top_by_cost against
tests/fixtures/top_by_cost.py's own `tbc_` rows (workspace `tbc_ws1`).

Per tests/test_findings/README.md's own checklist:
  - grain [window_days, workspace_id, warehouse_id, group_kind, group_id, statement_type] unique
    at 7 / 30 / 90, and nothing at 0;
  - the output columns in the contract order, the generator's own window_days first;
  - the four fixture scenarios: a redacted dashboard run 20 times (CRITICAL), a redacted job task
    (WARN), two real-text runs of the same shape that collapse to one text-hash group (OK),
    and two source-less redacted statements that form two separate ad-hoc groups, one per user
    (OK);
  - mask_user_identities is OFF by default (dbt/macros/mask_user.sql): top_user and an ad-hoc
    group's own group_id are the raw executed_by by default, and DEC-66.3's masked format only
    when that var is turned on (proven by rendering the real generated model with jinja_stub);
  - invariants that hold on every row the model returns, across every builder in the shared
    fixture: share_of_warehouse_cost and from_result_cache_share both sit in [0, 1], every group
    has at least one user (a NULL-identity group still counts as 1, not 0), and the result is
    ordered worst-cost-first (NULLS last).
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import dbutil  # noqa: E402
import ddl  # noqa: E402
import jinja_stub  # noqa: E402
import top_by_cost as tbc  # noqa: E402
from app.core.registry import by_id  # noqa: E402
from tools.generate_models import render_model  # noqa: E402

QUERY_ID = "query_top_by_cost"
SQL_PATH = ROOT / "app" / "queries" / "app" / "performance" / f"{QUERY_ID}.sql"
MODEL_PATH = ROOT / "dbt" / "models" / "findings" / "performance" / f"f_{QUERY_ID}.sql"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WINDOWS = (7, 30, 90)

COLUMNS = [
    "window_days", "workspace_id", "warehouse_id", "group_kind", "group_id", "source_kind",
    "source_id", "statement_type", "runs", "distinct_users", "top_user", "est_cost_usd_list",
    "share_of_warehouse_cost", "total_exec_ms", "avg_exec_ms", "max_exec_ms", "p50_duration_ms",
    "p95_duration_ms", "read_bytes", "spilled_local_bytes", "read_files", "pruned_files", "from_result_cache_share",
    "sample_statement_id", "first_seen", "last_seen", "status",
]

# mask_user_identities defaults to false, so by default these columns carry the raw identity.
TOP_USER_DASH = "tbc_dash_user1@example.com"   # 15 of 20 runs
TOP_USER_HASH = "tbc_hash_userA@example.com"   # tie-break: alphabetically-first raw executed_by
GROUP_ADHOC_1 = "tbc_adhoc_user1@example.com"
GROUP_ADHOC_2 = "tbc_adhoc_user2@example.com"
JOB_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # passes through the mask unchanged either way

# DEC-66.3 masked identities (sha256(lower(trim(email)))[:8] + ' ' + email[:2] + '***'), computed
# independently of the query, for the one test that turns masking on.
TOP_USER_DASH_MASKED = "6aeac6d6 tb***"
GROUP_ADHOC_1_MASKED = "41f58938 tb***"


def _rows(window_days: int) -> list[dict]:
    return dbutil.rows(QUERY_ID, window_days)


def _tbc(rows: list[dict]) -> list[dict]:
    return [r for r in rows if (r["warehouse_id"] or "").startswith("tbc_wh_")]


def _close(a, b, tol=1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) < tol


def test_grain_columns_and_window_zero():
    for w in WINDOWS:
        out = _rows(w)
        assert out, f"no rows at window {w}"
        assert list(out[0].keys()) == COLUMNS, list(out[0].keys())
        keys = [
            (r["window_days"], r["workspace_id"], r["warehouse_id"], r["group_kind"], r["group_id"],
             r["statement_type"])
            for r in out
        ]
        assert len(keys) == len(set(keys)), f"duplicate grain at window {w}"
    assert _rows(0) == []


def test_dashboard_redacted_critical():
    by_wh = {r["warehouse_id"]: r for r in _tbc(_rows(30))}
    r = by_wh["tbc_wh_dash"]
    assert r["group_kind"] == "dashboard"
    assert r["group_id"] == "tbc_dash_1"
    assert r["source_kind"] == "dashboard"
    assert r["source_id"] == "tbc_dash_1"
    assert r["statement_type"] == "SELECT"
    assert r["runs"] == 20
    assert r["distinct_users"] == 2
    assert r["top_user"] == TOP_USER_DASH
    assert _close(r["est_cost_usd_list"], 200.0)
    assert _close(r["share_of_warehouse_cost"], 1.0)
    assert r["total_exec_ms"] == 1800
    assert r["avg_exec_ms"] == 90
    assert r["max_exec_ms"] == 90
    assert r["p50_duration_ms"] == 90
    assert r["p95_duration_ms"] == 90
    assert r["read_bytes"] == 20_000
    assert r["spilled_local_bytes"] == 0
    assert _close(r["from_result_cache_share"], 0.0)
    assert r["sample_statement_id"] == "tbc_st_dash_19"
    assert r["status"] == "CRITICAL"


def test_job_task_redacted_warn():
    rows = [r for r in _tbc(_rows(30)) if r["warehouse_id"] == "tbc_wh_job"]
    assert len(rows) == 1
    r = rows[0]
    assert r["group_kind"] == "job"
    assert r["group_id"] == "tbc_job_1"        # job groups key on job_id alone
    assert r["source_kind"] == "job"
    assert r["source_id"] == "tbc_job_1"
    assert r["runs"] == 3
    assert r["distinct_users"] == 1
    assert r["top_user"] == JOB_GUID          # a GUID passes through the identity mask unchanged
    assert _close(r["est_cost_usd_list"], 60.0)
    assert _close(r["share_of_warehouse_cost"], 0.3)
    assert r["total_exec_ms"] == 2700
    assert r["avg_exec_ms"] == 900
    assert r["max_exec_ms"] == 900
    assert r["p50_duration_ms"] == 900
    assert r["p95_duration_ms"] == 900
    assert r["read_bytes"] == 6000
    assert r["spilled_local_bytes"] == 1_500_000
    assert r["sample_statement_id"] == "tbc_st_job_2"
    assert r["status"] == "WARN"


def test_real_text_hashes_to_one_group():
    rows = [r for r in _tbc(_rows(30)) if r["warehouse_id"] == "tbc_wh_hash"]
    assert len(rows) == 1, "the two same-shape, different-literal runs must collapse to one group"
    r = rows[0]
    assert r["group_kind"] == "text hash"
    assert r["group_id"] not in ("tbc_dash_1", None)  # a real sha2 hash, not an origin id
    assert len(r["group_id"]) == 64                    # sha2(..., 256) hex digest length
    assert r["source_kind"] is None                    # no query_source captured for these rows
    assert r["source_id"] is None
    assert r["runs"] == 2
    assert r["distinct_users"] == 2
    assert r["top_user"] == TOP_USER_HASH
    assert _close(r["est_cost_usd_list"], 2.0)
    assert _close(r["share_of_warehouse_cost"], 0.01)
    assert r["total_exec_ms"] == 90
    assert r["avg_exec_ms"] == 45
    assert r["max_exec_ms"] == 45
    assert r["p50_duration_ms"] == 45
    assert r["p95_duration_ms"] == 45
    assert r["read_bytes"] == 1000
    assert _close(r["from_result_cache_share"], 0.5)   # one of the two runs was cached
    assert r["sample_statement_id"] == "tbc_st_hash_west"
    assert r["status"] == "OK"


def test_adhoc_redacted_splits_by_user():
    rows = {r["group_id"]: r for r in _tbc(_rows(30)) if r["warehouse_id"] == "tbc_wh_adhoc"}
    assert set(rows) == {GROUP_ADHOC_1, GROUP_ADHOC_2}, sorted(rows)
    for gid, sample_id, read_bytes, exec_ms in (
        (GROUP_ADHOC_1, "tbc_st_adhoc_1", 10_000, 200),
        (GROUP_ADHOC_2, "tbc_st_adhoc_2", 12_000, 210),
    ):
        r = rows[gid]
        assert r["group_kind"] == "ad-hoc, text hidden"
        assert r["source_kind"] is None    # no query_source captured -- genuinely ad-hoc
        assert r["source_id"] is None
        assert r["top_user"] == gid   # one user per group -> top_user is the group's own identity
        assert r["runs"] == 1
        assert r["distinct_users"] == 1
        assert _close(r["est_cost_usd_list"], 2.5)
        assert _close(r["share_of_warehouse_cost"], 0.05)
        assert r["read_bytes"] == read_bytes
        assert r["total_exec_ms"] == exec_ms
        assert r["sample_statement_id"] == sample_id
        assert r["status"] == "OK"


def test_present_at_7_and_90_same_as_30():
    for w in (7, 90):
        by_wh = {r["warehouse_id"]: r for r in _tbc(_rows(w))}
        assert by_wh["tbc_wh_dash"]["status"] == "CRITICAL"
        assert by_wh["tbc_wh_job"]["status"] == "WARN"


# -------------------------------------------------------------------------------------------
# invariants on every row, every builder in the shared fixture.
# -------------------------------------------------------------------------------------------
def test_share_and_cache_fractions_bounded():
    for w in WINDOWS:
        for r in _rows(w):
            if r["share_of_warehouse_cost"] is not None:
                assert -1e-9 <= r["share_of_warehouse_cost"] <= 1.0 + 1e-6, r
            if r["from_result_cache_share"] is not None:
                assert -1e-9 <= r["from_result_cache_share"] <= 1.0 + 1e-6, r
            assert r["runs"] >= 1, r
            assert 1 <= r["distinct_users"] <= r["runs"], r


def test_read_and_pruned_files_add_up_to_raw():
    pattern = (PARQUET_DIR / "query__history" / "*.parquet").as_posix()
    for w in WINDOWS:
        con = duckdb.connect()
        try:
            raw = con.execute(f"""
                SELECT SUM(COALESCE(read_files, 0)), SUM(COALESCE(pruned_files, 0))
                FROM read_parquet('{pattern}', union_by_name=true)
                WHERE compute.warehouse_id IS NOT NULL
                  AND start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {w} DAY
                  AND start_time < DATE '{dbutil.TEST_TODAY}'
            """).fetchone()
        finally:
            con.close()
        out = _rows(w)
        assert (sum(r["read_files"] for r in out), sum(r["pruned_files"] for r in out)) == (raw[0], raw[1]), w


def test_cost_ordering_worst_first():
    for w in WINDOWS:
        costs = [r["est_cost_usd_list"] for r in _rows(w)]
        as_sortable = [c if c is not None else float("-inf") for c in costs]
        assert as_sortable == sorted(as_sortable, reverse=True), w


# -------------------------------------------------------------------------------------------
# masking, rendered from the real generated dbt model (mask_user_identities is a var, not a
# threshold, so this renders directly rather than going through dbutil's already-built database).
# -------------------------------------------------------------------------------------------
def _render_and_run(con) -> list[dict]:
    spec = by_id(QUERY_ID)
    text = render_model(spec, target="duckdb")
    sql = jinja_stub.render(text, windows=(30,), source=jinja_stub.memory_source)
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _insert_stmt(con, row: dict) -> None:
    cols_sql = ", ".join(f'"{c}"' for c in tbc.QH_COLUMNS)
    placeholders = ", ".join("?" for _ in tbc.QH_COLUMNS)
    con.execute(f'INSERT INTO query__history ({cols_sql}) VALUES ({placeholders})',
                [row[c] for c in tbc.QH_COLUMNS])


# -------------------------------------------------------------------------------------------
# Synthetic, in-memory: the review's case -- a statement's weight is split across every billed
# hour it overlaps (by overlap seconds), not billed whole to its start hour.
# -------------------------------------------------------------------------------------------
def test_hour_split_review_case():
    con = duckdb.connect()
    try:
        for _name, create_sql in ddl.DDL.items():
            con.execute(create_sql)

        wh = "tbc_wh_hoursplit"
        tbc._list_price(con, "tbc_SQL_SPLIT", 1.0)
        for hour in (1, 2, 3):
            tbc._billing(con, f"tbc_bl_split_{hour}", warehouse_id=wh, sku_name="tbc_SQL_SPLIT",
                          hour=hour, qty=60.0)

        # A: starts 1:30, runs 2h30m -> ends exactly at 4:00. Overlaps hour1 (30 of 60 min),
        # hour2 and hour3 in full.
        long_row = tbc._stmt("tbc_st_split_long", wh, tbc.DT(1, 30), executed_by="tbc_split_u@example.com",
                              statement_text="<REDACTED>", weight_ms=1000, exec_ms=150 * 60_000)
        # B: 1 second, fully inside hour1, alongside A's own hour1 share.
        short_row = tbc._stmt("tbc_st_split_short", wh, tbc.DT(1, 45), executed_by="tbc_split_u2@example.com",
                               statement_text="<REDACTED>", weight_ms=1000, exec_ms=1000)
        _insert_stmt(con, long_row)
        _insert_stmt(con, short_row)

        rows = _render_and_run(con)
        by_sid = {r["sample_statement_id"]: r for r in rows if r["warehouse_id"] == wh}
        long_r = by_sid["tbc_st_split_long"]
        short_r = by_sid["tbc_st_split_short"]

        # hour1's $60 splits 0.2/(0.2+1.0) to the long query, the rest to the short one; hour2 and
        # hour3 ($60 each) go to the long query alone, in full.
        assert _close(long_r["est_cost_usd_list"], 10.0 + 60.0 + 60.0)
        assert _close(short_r["est_cost_usd_list"], 50.0)
    finally:
        con.close()


# -------------------------------------------------------------------------------------------
# Synthetic, in-memory: a job group is keyed by job_id alone -- two task runs of the same job,
# from two different job runs, collapse into one group.
# -------------------------------------------------------------------------------------------
def test_job_group_keyed_by_job_id_alone():
    con = duckdb.connect()
    try:
        for _name, create_sql in ddl.DDL.items():
            con.execute(create_sql)

        wh = "tbc_wh_jobgroup"
        tbc._list_price(con, "tbc_SQL_JG", 1.0)
        tbc._billing(con, "tbc_bl_jobgroup", warehouse_id=wh, sku_name="tbc_SQL_JG", hour=5, qty=60.0)

        src1 = tbc._qsource(job_id="tbc_job_multi", job_run_id="tbc_run_a", job_task_run_id="tbc_task_a")
        src2 = tbc._qsource(job_id="tbc_job_multi", job_run_id="tbc_run_b", job_task_run_id="tbc_task_b")
        r1 = tbc._stmt("tbc_st_jg_1", wh, tbc.DT(5, 0), executed_by="tbc_jg_user@example.com",
                        statement_text="<REDACTED>", query_source=src1, weight_ms=1000, exec_ms=900)
        r2 = tbc._stmt("tbc_st_jg_2", wh, tbc.DT(5, 10), executed_by="tbc_jg_user@example.com",
                        statement_text="<REDACTED>", query_source=src2, weight_ms=1000, exec_ms=900)
        _insert_stmt(con, r1)
        _insert_stmt(con, r2)

        rows = [r for r in _render_and_run(con) if r["warehouse_id"] == wh]
        assert len(rows) == 1, "two task runs of the same job must form one group"
        assert rows[0]["group_kind"] == "job"
        assert rows[0]["group_id"] == "tbc_job_multi"
        assert rows[0]["source_kind"] == "job"
        assert rows[0]["source_id"] == "tbc_job_multi"
        assert rows[0]["runs"] == 2
    finally:
        con.close()


def test_mask_user_identities_on_masks_top_user_and_adhoc_group_id():
    text = MODEL_PATH.read_text(encoding="utf-8")
    sql = jinja_stub.render(
        text, windows=(30,), source=jinja_stub.parquet_source(PARQUET_DIR.as_posix()),
        mask_user_identities=True,
    )
    con = duckdb.connect()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()
    by_wh = {r["warehouse_id"]: r for r in rows if (r["warehouse_id"] or "").startswith("tbc_wh_")}
    assert by_wh["tbc_wh_dash"]["top_user"] == TOP_USER_DASH_MASKED
    assert by_wh["tbc_wh_job"]["top_user"] == JOB_GUID  # a GUID still passes through when masking is on
    adhoc = {r["group_id"] for r in rows if r.get("warehouse_id") == "tbc_wh_adhoc"}
    assert GROUP_ADHOC_1_MASKED in adhoc
