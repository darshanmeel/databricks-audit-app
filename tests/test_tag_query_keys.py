"""A query tag with a blank value is not a tag: dbt/models/tags/query_tag_keys.sql and
tag_compliance._slice_counts both leave its key out of own_keys."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import jinja2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import tag_compliance  # noqa: E402

MODEL = ROOT / "dbt" / "models" / "tags" / "query_tag_keys.sql"


def _sql() -> str:
    return jinja2.Template(MODEL.read_text(encoding="utf-8")).render(
        config=lambda **_: "", source=lambda *_: "history", var=lambda _: [7],
        audit_today=lambda: "DATE '2026-10-06'", target={"type": "duckdb"})


def test_query_tag_keys_skips_blank_values():
    con = duckdb.connect()
    con.execute("CREATE TABLE history (workspace_id VARCHAR, start_time TIMESTAMP, client_application VARCHAR, "
                "query_tags MAP(VARCHAR, VARCHAR), compute STRUCT(warehouse_id VARCHAR, cluster_id VARCHAR), "
                "query_source STRUCT(job_info STRUCT(job_id VARCHAR), pipeline_info STRUCT(pipeline_id VARCHAR), "
                "dashboard_id VARCHAR, legacy_dashboard_id VARCHAR, genie_space_id VARCHAR, alert_id VARCHAR, "
                "notebook_id VARCHAR, sql_query_id VARCHAR))")
    con.execute("INSERT INTO history VALUES ('w1', TIMESTAMP '2026-10-05 10:00', NULL, "
                "MAP {'Team': '  ', 'cost_center': '', 'domain': 'd1'}, {'warehouse_id': 'wh1', 'cluster_id': NULL}, NULL)")
    assert con.execute(f"SELECT own_keys FROM ({_sql()})").fetchall() == [("domain",)]


def test_slice_counts_skips_blank_values():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA findings")
    con.execute("CREATE TABLE findings.f_query_slices AS SELECT 30 AS window_days, 'w1' AS workspace_id, "
                "'wh1' AS warehouse_id, 'tool' AS origin, NULL AS job_id, NULL AS pipeline_id, "
                "'{\"Team\": \"  \", \"domain\": \"d1\"}' AS query_tags, 3 AS runs")
    empty = {"job": {}, "pipeline": {}, "warehouse": {}}
    bills = {k: (None, {}) for k in empty}
    out = tag_compliance._slice_counts(con, 30, lambda levels: True, empty, bills, {}, {})
    assert out == {("w1", "wh1", "tool", "None", "None", "domain"): 3}
