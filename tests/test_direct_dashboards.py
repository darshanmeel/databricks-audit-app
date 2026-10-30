"""tools/build_direct_dashboards.py: committed files are current, and every dataset is valid
Databricks SQL that reads the picked Time Window, not a fixed one."""
import json

import sqlglot

from tools import build_direct_dashboards as bdd_direct


def test_committed_files_equal_a_fresh_build():
    for name, content in bdd_direct.build().items():
        assert (bdd_direct.OUT_DIR / name).read_text(encoding="utf-8") == content, f"regenerate {name}"


def _all_datasets():
    return [d for *_, datasets in bdd_direct.dashboards() for d in datasets]


def _dashboards():
    return [make() for _, _, make, _ in bdd_direct.dashboards()]


def test_every_dataset_parses_and_uses_the_time_window():
    windowed = {d.name: d.window for d in _all_datasets()}
    for name, content in bdd_direct.build().items():
        if not name.endswith(".json"):
            continue
        for ds in json.loads(content)["datasets"]:
            sql = bdd_direct.bdd.untagged_sql(ds["queryLines"])
            if windowed[ds["name"]]:
                assert ":time_range.min" in sql and ":time_range.max" in sql
                assert [p["keyword"] for p in ds["parameters"]][:1] == ["time_range"]
            else:
                assert ":time_range" not in sql
            assert "INTERVAL 30 DAYS" not in sql and "SELECT 30 AS window_days" not in sql
            assert "FLOOR(30 / 2)" not in sql and "LEAST(30, 90)" not in sql
            bound = (sql.replace(":time_range.min", "DATE'2026-01-01'").replace(":time_range.max", "DATE'2026-01-30'")
                     .replace(":month", "DATE'2026-01-15'"))
            sqlglot.parse_one(bound, read="databricks")


def test_every_step_in_the_guide_has_a_note():
    from tools import direct_dashboard_doc as doc

    assert doc.missing_notes(_all_datasets()) == []


def test_every_table_widget_has_the_import_keys():
    for page in (p for dash in _dashboards() for p in dash["pages"]):
        assert page["layoutVersion"] == "GRID_V1"
        for item in page["layout"]:
            spec = item["widget"].get("spec", {})
            if spec.get("widgetType") == "table":
                assert spec["invisibleColumns"] == []
            assert item["position"]["x"] + item["position"]["width"] <= 12


def test_every_query_carries_the_query_tags():
    for ds in (ds for dash in _dashboards() for ds in dash["datasets"]):
        tags = [l for l in ds["queryLines"] if l.startswith("SET QUERY_TAGS[")]
        keys = {l.split("'")[1] for l in tags}
        assert {"dashboard", "tab"} <= keys and set(bdd_direct.bdd.query_tags()) <= keys
        assert all("-" not in l for l in tags), "a hyphen in a query tag fails the query"


def test_every_column_and_query_in_the_detailed_guides_is_explained():
    from tools import direct_dashboard_doc as doc
    from tools import direct_guides as guides

    for stem, _, _, datasets in bdd_direct.dashboards():
        if stem not in guides.GUIDES:
            continue
        for d in datasets:
            assert d.key in guides.WHY, d.key
            cols = guides.columns(d.key)
            assert [c for c in doc.output_columns(d) if c not in cols] == [], d.key


def test_step_examples_name_real_steps():
    from tools import direct_dashboard_doc as doc
    from tools.direct_stories import STEP_EXAMPLES

    by_key = {d.key: d for d in _all_datasets()}
    for key, steps in STEP_EXAMPLES.items():
        assert set(steps) <= set(doc._step_sql(by_key[key])), key


def test_summary_main_queries_parse():
    from tools import direct_guides as guides

    for stem, short in guides.SHORT.items():
        assert [e for e in sqlglot.parse(short["sql"], read="databricks") if e is not None], stem
