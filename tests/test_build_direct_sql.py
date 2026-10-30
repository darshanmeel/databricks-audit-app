"""tools/build_direct_sql.py: the plain-SQL copy of the Databricks-direct models the export runs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

from tools import build_direct_sql as bds
from tools import export_direct_results as edr

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import ddl  # noqa: E402


def test_generated_sql_is_in_sync_with_the_models():
    assert bds.check() == []


def test_every_direct_model_has_plain_sql_with_no_jinja_left():
    manifest = json.loads(bds.DIRECT_MANIFEST.read_text(encoding="utf-8"))
    ids = {m["query_id"] for m in manifest["models"]}
    files = {p.stem for p in bds.OUT_DIR.glob("*.sql")}
    assert files == ids
    for qid in ids:
        sql = (bds.OUT_DIR / f"{qid}.sql").read_text(encoding="utf-8")
        assert "{{" not in sql and "{%" not in sql, qid
        assert "`system`." in sql, qid


def test_export_fills_the_same_window_marker_the_builder_writes():
    assert edr.WINDOW_MARKER == bds.WINDOW_MARKER


def test_params_render_like_the_dbt_macro():
    text = "SELECT {{ param('q1', 'n', 3) }}, {{ param('q1', 's', \"a'b\") }}, {{ param('q1', 'f', true) }}"
    assert bds.render_model(text, "q1", {}, {}) == "SELECT 3, 'a''b', TRUE\n"
    assert bds.render_model(text, "q1", {"_all": {"n": 9}, "q1": {"f": False}}, {}) == "SELECT 9, 'a''b', FALSE\n"


def test_windows_render_once_with_the_marker():
    text = "{%- for w in var('windows', [7, 30, 90]) %}SELECT {{ w }} AS window_days{% endfor %}"
    assert bds.render_model(text, "q1", {}, {}) == f"SELECT {bds.WINDOW_MARKER} AS window_days\n"


def test_unknown_var_or_ref_fails_loudly():
    with pytest.raises(bds.BuildError, match="var"):
        bds.render_model("{{ var('other') }}", "q1", {}, {})
    with pytest.raises(bds.BuildError, match="ref"):
        bds.render_model("{{ ref('m') }}", "q1", {}, {})


def test_list_prices_alias_shapes_explain_on_duckdb():
    """Both list_prices() call shapes the generator emits -- inside an aliased wrapping subquery,
    and inside a CTE with no alias of its own -- render to plain SQL that EXPLAINs cleanly."""
    sources = {("system_billing", "list_prices"): "billing__list_prices"}
    wrapped = (
        "WITH price AS (\n"
        "  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,\n"
        "         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate\n"
        "  FROM (\n"
        "    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time, pricing\n"
        "    FROM {{ list_prices() }} list_prices\n"
        "  ) lp\n"
        ")\n"
        "SELECT * FROM price"
    )
    bare = (
        "WITH price AS (\n"
        "  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,\n"
        "         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate\n"
        "  FROM {{ list_prices() }} list_prices\n"
        ")\n"
        "SELECT * FROM price"
    )
    con = duckdb.connect()
    con.execute(ddl.DDL["billing__list_prices"])
    for text in (wrapped, bare):
        rendered = bds.render_model(text, "q", {}, sources)
        assert "{{" not in rendered and "{%" not in rendered
        assert "LEAD(price_start_time)" in rendered
        con.execute(f"EXPLAIN {rendered}")


def test_audit_today_and_now_render_as_as_of_markers():
    """One UTC as_of for the whole export: build_direct_sql pins audit_today()/audit_now() to the
    same markers tools/export_direct_results.py fills in, never the session's own current_date."""
    text = "SELECT {{ audit_today() }} AS d, {{ audit_now() }} AS t"
    rendered = bds.render_model(text, "q1", {}, {})
    assert rendered == f"SELECT {bds.AS_OF_DATE_MARKER} AS d, {bds.AS_OF_TS_MARKER} AS t\n"
    assert "current_date" not in rendered and "current_timestamp" not in rendered


def test_cost_unit_direct_sql_carries_the_dedup():
    text = (ROOT / "app" / "direct_sql" / "tags" / "cost_unit.sql").read_text(encoding="utf-8")
    assert "{{" not in text and "{%" not in text
    assert "LEAD(price_start_time)" in text
