"""dbt/models/tags/bill_tag_dates.sql: "since" is the start of the current value's latest unbroken
run on the bill, not the key's first day ever."""
from __future__ import annotations

import re
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "dbt" / "models" / "tags" / "bill_tag_dates.sql"


def _sql() -> str:
    s = MODEL.read_text(encoding="utf-8")
    s = re.sub(r"\{\{ config\(.*?\) \}\}", "", s)
    s = re.sub(r"\{% if .*? %\}.*?\{% endif %\}", "", s, flags=re.S)
    s = s.replace("{{ source('system_billing', 'usage') }}", "usage")
    s = s.replace("{{ audit_today() }}", "DATE '2026-10-06'")
    return re.sub(r"\{\{ norm_tag_key\('([^']+)'\) \}\}", r"regexp_replace(lower(\1), '[ _-]+', '', 'g')", s)


def test_since_restarts_after_a_value_change_or_a_day_without_the_tag():
    con = duckdb.connect()
    con.execute("CREATE TABLE usage (usage_date DATE, usage_end_time TIMESTAMP, workspace_id VARCHAR, usage_unit VARCHAR, "
                "custom_tags MAP(VARCHAR, VARCHAR), usage_metadata STRUCT(warehouse_id VARCHAR, cluster_id VARCHAR, "
                "job_id VARCHAR, dlt_pipeline_id VARCHAR, notebook_id VARCHAR, budget_policy_id VARCHAR, usage_policy_id VARCHAR))")
    days = [
        # day, cost-center, domain
        ("2026-09-01", "A", "d1"),
        ("2026-09-02", "B", "d1"),
        ("2026-09-03", None, "d1"),
        ("2026-09-04", "B", "d1"),
        ("2026-09-05", "B", "d1"),
    ]
    for day, cc, dom in days:
        tags = {"domain": dom, **({"cost-center": cc} if cc else {})}
        con.execute("INSERT INTO usage VALUES (?, CAST(? AS DATE) + INTERVAL 1 HOUR, 'w1', 'DBU', ?, {'warehouse_id': 'wh1', 'cluster_id': NULL, "
                    "'job_id': NULL, 'dlt_pipeline_id': NULL, 'notebook_id': NULL, 'budget_policy_id': NULL, 'usage_policy_id': NULL})", [day, day, tags])
    # Billed again on 09-06 with no tags: both keys are gone after 09-05.
    con.execute("INSERT INTO usage VALUES ('2026-09-06', TIMESTAMP '2026-09-06 01:00', 'w1', 'DBU', MAP {}, {'warehouse_id': 'wh1', "
                "'cluster_id': NULL, 'job_id': NULL, 'dlt_pipeline_id': NULL, 'notebook_id': NULL, 'budget_policy_id': NULL, 'usage_policy_id': NULL})")
    rows = {r[0]: r[1:] for r in con.execute(
        f"SELECT tag_key, last_value, all_values, CAST(first_date AS VARCHAR), CAST(last_date AS VARCHAR), "
        f"CAST(entity_last_date AS VARCHAR) FROM ({_sql()}) WHERE entity_id = 'wh1'").fetchall()}
    assert rows["costcenter"] == ("B", ["A", "B"], "2026-09-04", "2026-09-05", "2026-09-06")
    assert rows["domain"] == ("d1", ["d1"], "2026-09-01", "2026-09-05", "2026-09-06")


def test_a_retag_on_the_last_day_takes_the_later_hours_value_and_key():
    con = duckdb.connect()
    con.execute("CREATE TABLE usage (usage_date DATE, usage_end_time TIMESTAMP, workspace_id VARCHAR, usage_unit VARCHAR, "
                "custom_tags MAP(VARCHAR, VARCHAR), usage_metadata STRUCT(warehouse_id VARCHAR, cluster_id VARCHAR, "
                "job_id VARCHAR, dlt_pipeline_id VARCHAR, notebook_id VARCHAR, budget_policy_id VARCHAR, usage_policy_id VARCHAR))")
    # Retagged mid-day: 'zz' (old, sorts last) at 01:00, then 'Team' = 'aa' at 02:00.
    for end, key, val in [("2026-09-01 01:00", "team", "zz"), ("2026-09-01 02:00", "Team", "aa")]:
        con.execute("INSERT INTO usage VALUES (DATE '2026-09-01', ?, 'w1', 'DBU', MAP {?: ?}, {'warehouse_id': 'wh1', "
                    "'cluster_id': NULL, 'job_id': NULL, 'dlt_pipeline_id': NULL, 'notebook_id': NULL, "
                    "'budget_policy_id': NULL, 'usage_policy_id': NULL})", [end, key, val])
    assert con.execute(f"SELECT raw_key, last_value FROM ({_sql()})").fetchall() == [("Team", "aa")]
