"""tests/test_databricks_dashboards.py

Checks every databricks_dashboards/*.lvdash.json and the snapshot notebook: shape (per
docs/lakeview-dashboard-format.md), every widget/field reference resolves against a column the
snapshot tables actually produce, every :workspace reference is both declared and reachable from
the global filter, no __MARKER__ survives, the committed files equal a fresh regeneration, every
dataset reads only <schema>.* snapshot tables (never system.*), and every SQL statement parses
under sqlglot's databricks dialect. Run:
python -m pytest -q tests/test_databricks_dashboards.py
"""
from __future__ import annotations

import json
import re

import pytest
import sqlglot
from sqlglot import exp

from tools import build_databricks_dashboards as bdd
from tools import dashboard_kit as kit

OUT_DIR = bdd.OUT_DIR
DASHBOARD_PATHS = {tab: OUT_DIR / bdd.dashboard_file(tab) for tab in bdd.TABS}
NOTEBOOK_PATH = OUT_DIR / bdd.NOTEBOOK_FILE
# __REDACTED__ is a value Databricks writes for a hidden grantee, not a marker
MARKER_RE = re.compile(r"__(?!REDACTED__)[A-Z_]+__")


@pytest.fixture(scope="module", params=bdd.TABS)
def dashboard(request) -> dict:
    return json.loads(DASHBOARD_PATHS[request.param].read_text(encoding="utf-8"))


def dataset_output_columns(dataset: dict) -> set[str]:
    """The dataset's own output column names, read from its SQL's outermost SELECT list (every
    column here is given an explicit alias, per docs/lakeview-dashboard-format.md)."""
    sql = "".join(dataset["queryLines"])
    tree = sqlglot.parse_one(sql, dialect="databricks")
    return {e.alias_or_name for e in tree.selects}


def iter_data_widgets(dashboard: dict):
    """Yields (page, widget) for every widget with queries/spec -- skips text widgets."""
    for page in dashboard["pages"]:
        for item in page["layout"]:
            w = item["widget"]
            if "multilineTextboxSpec" not in w:
                yield page, w


# ---------------------------------------------------------------------------
# Structure, per docs/lakeview-dashboard-format.md "Minimum keys an import needs"
# ---------------------------------------------------------------------------

def test_top_level_has_exactly_the_three_keys(dashboard):
    assert set(dashboard.keys()) == {"datasets", "pages", "uiSettings"}
    assert isinstance(dashboard["datasets"], list) and dashboard["datasets"]
    assert isinstance(dashboard["pages"], list) and dashboard["pages"]


def test_every_dataset_has_required_keys(dashboard):
    seen_names = set()
    for d in dashboard["datasets"]:
        assert {"name", "displayName", "queryLines"} <= d.keys()
        lines = d["queryLines"]
        assert isinstance(lines, list) and lines
        # format doc: queryLines is the SQL split on real newlines, each element (but the last)
        # keeping its own trailing \n -- concatenation must reproduce the SQL exactly.
        for line in lines[:-1]:
            assert line.endswith("\n"), f"dataset {d['displayName']}: queryLines element {line!r} has no trailing newline"
        assert d["name"] not in seen_names, f"duplicate dataset name {d['name']}"
        seen_names.add(d["name"])


def test_every_page_has_required_keys_and_valid_type(dashboard):
    types = []
    for page in dashboard["pages"]:
        assert {"name", "displayName", "pageType", "layout"} <= page.keys()
        assert page["pageType"] in ("PAGE_TYPE_CANVAS", "PAGE_TYPE_GLOBAL_FILTERS")
        types.append(page["pageType"])
        for item in page["layout"]:
            assert {"widget", "position"} <= item.keys()
            pos = item["position"]
            assert {"x", "y", "width", "height"} == pos.keys()
            for k in pos:
                assert isinstance(pos[k], int)
    assert types.count("PAGE_TYPE_GLOBAL_FILTERS") == 1
    assert types.count("PAGE_TYPE_CANVAS") == len(types) - 1


def test_every_data_widget_has_required_keys(dashboard):
    for page, w in iter_data_widgets(dashboard):
        assert "name" in w and "queries" in w and "spec" in w
        spec = w["spec"]
        assert {"version", "widgetType", "encodings"} <= spec.keys()
        assert "frame" in spec, f"{w['name']} on {page['displayName']} has no frame"
        for q in w["queries"]:
            assert {"name", "query"} <= q.keys()
            query = q["query"]
            assert "datasetName" in query and "disaggregated" in query
            assert "fields" in query or "parameters" in query


def test_text_widgets_carry_only_the_textbox_spec(dashboard):
    for page in dashboard["pages"]:
        for item in page["layout"]:
            w = item["widget"]
            if "multilineTextboxSpec" in w:
                assert "spec" not in w and "queries" not in w
                assert isinstance(w["multilineTextboxSpec"]["lines"], list)


def test_widget_grid_positions_fit_the_page_width(dashboard):
    for page in dashboard["pages"]:
        max_width = 12 if page.get("layoutVersion") == "GRID_V1" else 6
        for item in page["layout"]:
            pos = item["position"]
            assert pos["x"] + pos["width"] <= max_width, (page["displayName"], item["widget"]["name"], pos)
            assert pos["x"] >= 0 and pos["y"] >= 0 and pos["width"] > 0 and pos["height"] > 0


def test_widgets_on_one_page_never_overlap(dashboard):
    """Two widgets occupying the same grid cells would silently hide one another -- not covered
    by the per-widget bounds check above."""
    for page in dashboard["pages"]:
        cells: dict[tuple[int, int], str] = {}
        for item in page["layout"]:
            pos = item["position"]
            for gx in range(pos["x"], pos["x"] + pos["width"]):
                for gy in range(pos["y"], pos["y"] + pos["height"]):
                    key = (gx, gy)
                    prev = cells.get(key)
                    assert prev is None, (
                        f"{page['displayName']}: {item['widget']['name']} overlaps {prev} at {key}"
                    )
                    cells[key] = item["widget"]["name"]


# ---------------------------------------------------------------------------
# Field/dataset wiring
# ---------------------------------------------------------------------------

def test_every_widgets_dataset_name_exists(dashboard):
    dataset_ids = {d["name"] for d in dashboard["datasets"]}
    for page, w in iter_data_widgets(dashboard):
        for q in w["queries"]:
            assert q["query"]["datasetName"] in dataset_ids, (page["displayName"], w["name"], q["query"]["datasetName"])


def test_every_field_expression_names_a_column_the_dataset_returns(dashboard):
    ds_by_id = {d["name"]: d for d in dashboard["datasets"]}
    ds_cols = {did: dataset_output_columns(d) for did, d in ds_by_id.items()}
    for page, w in iter_data_widgets(dashboard):
        spec = w["spec"]
        if spec["widgetType"] == "filter-multi-select":
            continue  # checked separately: it references parameters, not plain dataset columns
        for q in w["queries"]:
            cols = ds_cols[q["query"]["datasetName"]]
            for f in q["query"].get("fields", []):
                col = re.fullmatch(r"(?:[A-Z_]+\()?`([^`]+)`\)?", f["expression"]).group(1)
                assert col in cols, (
                    f"{w['name']} on {page['displayName']}: field {f['expression']!r} not in "
                    f"dataset {q['query']['datasetName']} columns {sorted(cols)}"
                )
        # every encodings.<channel>.fieldName must match one of this widget's own query fields
        field_names = {f["name"] for q in w["queries"] for f in q["query"].get("fields", [])}

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("fieldName"), str):
                    assert node["fieldName"] in field_names, (
                        f"{w['name']} on {page['displayName']}: encodings fieldName "
                        f"{node['fieldName']!r} not among query fields {sorted(field_names)}"
                    )
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(spec["encodings"])


def test_filter_multi_select_fields_resolve_to_a_lookup_column_or_a_declared_parameter(dashboard):
    ds_by_id = {d["name"]: d for d in dashboard["datasets"]}
    found_filter = False
    for page, w in iter_data_widgets(dashboard):
        if w["spec"]["widgetType"] != "filter-multi-select":
            continue
        found_filter = True
        query_by_name = {q["name"]: q for q in w["queries"]}
        for enc in w["spec"]["encodings"]["fields"]:
            q = query_by_name[enc["queryName"]]
            dataset = ds_by_id[q["query"]["datasetName"]]
            if "fieldName" in enc:
                assert enc["fieldName"] in dataset_output_columns(dataset)
            else:
                keywords = {p["keyword"] for p in dataset.get("parameters", [])}
                assert enc["parameterName"] in keywords, (
                    f"{w['name']}: parameter {enc['parameterName']!r} not declared on dataset "
                    f"{dataset['displayName']} (has {keywords})"
                )
    assert found_filter, "expected at least one filter-multi-select widget (the workspace filter)"


def test_filter_multi_select_lookup_query_is_disaggregated_false_with_associativity_field(dashboard):
    """docs/lakeview-dashboard-format.md: the lookup query (option list) is disaggregated:false
    and carries a COUNT_IF(`associative_filter_predicate_group`) companion field for live counts --
    both true in every filter-multi-select example in the verified sample."""
    for page, w in iter_data_widgets(dashboard):
        if w["spec"]["widgetType"] != "filter-multi-select":
            continue
        lookup_field = w["spec"]["encodings"]["fields"][0]
        lookup_query = next(q for q in w["queries"] if q["name"] == lookup_field["queryName"])
        assert lookup_query["query"]["disaggregated"] is False
        expressions = [f.get("expression", "") for f in lookup_query["query"]["fields"]]
        assert any("associative_filter_predicate_group" in e for e in expressions), (
            f"{w['name']}: lookup query has no associative_filter_predicate_group companion field"
        )


def test_workspace_parameter_declared_used_and_filter_reachable_agree(dashboard):
    """Every dataset whose SQL contains ':workspace' must declare the workspace parameter AND be
    one of the global filter's targets -- and vice versa. Catches a dataset the global filter
    skips (it would stay account-wide while every other card is filtered) as well as a target
    that declares the parameter but never actually uses it."""
    filters_page = next(p for p in dashboard["pages"] if p["pageType"] == "PAGE_TYPE_GLOBAL_FILTERS")
    filter_widget = filters_page["layout"][0]["widget"]
    targeted_dataset_ids = {
        q["query"]["datasetName"] for q in filter_widget["queries"] if "parameters" in q["query"]
    }

    for d in dashboard["datasets"]:
        sql = "".join(d["queryLines"])
        uses_workspace = ":workspace" in sql
        declares_param = any(p.get("keyword") == "workspace" for p in d.get("parameters", []))
        is_target = d["name"] in targeted_dataset_ids
        assert uses_workspace == declares_param, (
            f"dataset {d['displayName']}: :workspace in SQL is {uses_workspace} but the workspace "
            f"parameter is declared is {declares_param}"
        )
        assert uses_workspace == is_target, (
            f"dataset {d['displayName']}: :workspace in SQL is {uses_workspace} but it is a global "
            f"filter target is {is_target}"
        )


# ---------------------------------------------------------------------------
# Chart shape, per docs/lakeview-dashboard-format.md
# ---------------------------------------------------------------------------

def test_temporal_scale_only_ever_on_the_x_channel(dashboard):
    """Every temporal bar/line/area in the verified sample has x=temporal, y=quantitative --
    never the reverse."""
    for page, w in iter_data_widgets(dashboard):
        for channel, enc in w["spec"]["encodings"].items():
            if not isinstance(enc, dict):
                continue
            scale_type = enc.get("scale", {}).get("type")
            if scale_type == "temporal":
                assert channel == "x", (
                    f"{w['name']} on {page['displayName']}: temporal scale on channel {channel!r}, "
                    "not x"
                )


def test_counters_and_bars_and_lines_have_no_color_without_a_fieldname(dashboard):
    """A color/legend channel with no fieldName is a widget that would render blank."""
    for page, w in iter_data_widgets(dashboard):
        color = w["spec"]["encodings"].get("color")
        if color is not None:
            assert isinstance(color.get("fieldName"), str) and color["fieldName"]


# ---------------------------------------------------------------------------
# No leftover placeholders
# ---------------------------------------------------------------------------

def test_no_marker_left_in_dashboard_json():
    for path in DASHBOARD_PATHS.values():
        assert MARKER_RE.findall(path.read_text(encoding="utf-8")) == [], path.name


def test_every_file_of_the_set_shares_the_prefix():
    names = sorted(f.name for f in OUT_DIR.iterdir() if f.name != "README.md")
    assert names == sorted([bdd.NOTEBOOK_FILE] + [bdd.dashboard_file(t) for t in bdd.TABS])


# ---------------------------------------------------------------------------
# Generator determinism: committed files == a fresh regeneration
# ---------------------------------------------------------------------------

def test_generator_output_equals_committed_files(tmp_path):
    files = bdd.build()
    for name, content in files.items():
        committed = (OUT_DIR / name).read_text(encoding="utf-8")
        assert content == committed, f"{name} differs from a fresh regeneration -- regenerate and recommit"


# ---------------------------------------------------------------------------
# Snapshot isolation: dashboard datasets read only <schema>.* tables
# ---------------------------------------------------------------------------

SNAPSHOT_TABLES = set(bdd.all_checks()) | set(bdd.DIMS) | {"snapshot_info", "snapshot_log", bdd.LINKS_TABLE}


def _tables_read(sql: str) -> list[exp.Table]:
    tree = sqlglot.parse_one(sql, dialect="databricks")
    ctes = {c.alias for c in tree.find_all(exp.CTE)}
    return [t for t in tree.find_all(exp.Table) if t.name and not (not t.db and t.name in ctes)]


def test_every_dataset_reads_only_snapshot_tables(dashboard):
    for d in dashboard["datasets"]:
        for t in _tables_read("".join(d["queryLines"])):
            assert t.db == bdd.SCHEMA_NAME, f"dataset {d['displayName']} reads {t.sql()}, not a snapshot table"
            assert t.name in SNAPSHOT_TABLES, f"dataset {d['displayName']} reads {t.name}, which the snapshot never creates"


def test_notebook_creates_exactly_checks_dims_info_and_log():
    src = NOTEBOOK_PATH.read_text(encoding="utf-8")
    compile(src, "snapshot_notebook.py", "exec")
    in_notebook = set(re.findall(r'^CHECKS\["(\w+)"\]', src, re.M)) | set(re.findall(r'^DIMS\["(\w+)"\]', src, re.M))
    assert in_notebook | {"snapshot_info", "snapshot_log", bdd.LINKS_TABLE} == SNAPSHOT_TABLES
    assert MARKER_RE.findall(src) == []


# ---------------------------------------------------------------------------
# Dataset columns resolve to a real column the snapshotted check/dim SQL produces -- catches a
# renamed snapshot column (e.g. est_wasted_usd_list -> est_wasted_usd) that sqlglot's own parsing
# can't, since it never resolves columns against another query's output.
# ---------------------------------------------------------------------------

def _star_source(node: exp.Select, alias: str | None, cte_by_name: dict[str, exp.Select]) -> exp.Select | None:
    """The Select a `*` or `q.*` in `node`'s own SELECT list stands for: a FROM/JOIN subquery (by
    alias, or the lone source for a bare `*`), or a CTE of the same name -- `cte_by_name` is every
    CTE anywhere in the statement (gathered once up front), since the CTE a deeply-UNION-nested
    `SELECT * FROM flagged` resolves against is usually declared several scopes above `node`
    itself, never on `node`'s own (often absent) `with_`."""
    sources = []
    from_ = node.args.get("from_")
    if from_ is not None:
        sources.append(from_.this)
    for j in node.args.get("joins") or []:
        sources.append(j.this)
    for src in sources:
        if alias is not None and src.alias_or_name != alias:
            continue
        if isinstance(src, exp.Subquery):
            return src.this
        if isinstance(src, exp.Table) and src.name in cte_by_name:
            return cte_by_name[src.name]
    return None


def _select_aliases(tree: exp.Select) -> set[str]:
    """tree.selects's own aliases, resolving a `*` or `q.*` (every check/dim file's outer SELECT
    does one or the other, sometimes through a UNION ALL of same-shaped branches too) to the real
    source's own aliases instead of the literal, useless '*'."""
    cte_by_name = {c.alias: c.this for c in tree.find_all(exp.CTE)}

    def resolve(node: exp.Select) -> set[str]:
        while isinstance(node, exp.Union):
            node = node.this  # same-shaped branches -- the left branch's columns apply
        cols: set[str] = set()
        for e in node.selects:
            if isinstance(e, exp.Star):
                source = _star_source(node, None, cte_by_name)
            elif isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
                source = _star_source(node, e.table, cte_by_name)
            else:
                cols.add(e.alias_or_name)
                continue
            if source is not None:
                cols |= resolve(source)
        return cols

    return resolve(tree)


def _real_snapshot_columns(query_id: str, read_fn) -> set[str]:
    tree = sqlglot.parse_one(read_fn(query_id), dialect="databricks")
    return _select_aliases(tree) | {"window_days", "snapshot_at"}


REAL_COLUMNS: dict[str, set[str]] = {
    **{qid: _real_snapshot_columns(qid, bdd.read_check) for qid in bdd.all_checks()},
    **{name: _real_snapshot_columns(name, bdd.read_dim) for name in bdd.DIMS},
}


def test_dataset_columns_match_the_real_snapshot_tables(dashboard):
    for d in dashboard["datasets"]:
        sql = "".join(d["queryLines"])
        tree = sqlglot.parse_one(sql, dialect="databricks")
        alias_to_table: dict[str, str] = {}
        for t in tree.find_all(exp.Table):
            if t.name in REAL_COLUMNS:
                alias_to_table[t.alias_or_name] = t.name
        for col in tree.find_all(exp.Column):
            if isinstance(col.this, exp.Star):
                continue  # `m.*` -- a real risk only if the table itself doesn't exist, checked elsewhere
            table_name = alias_to_table.get(col.table)
            if table_name is None:
                continue
            real = REAL_COLUMNS[table_name]
            # Databricks resolves column names case-insensitively
            assert col.name.lower() in {r.lower() for r in real}, (
                f"dataset {d['displayName']}: column {col.name!r} not among {table_name}'s own "
                f"columns {sorted(real)}"
            )


# ---------------------------------------------------------------------------
# SQL parses under sqlglot's databricks dialect
# ---------------------------------------------------------------------------

def test_every_dataset_sql_parses_as_databricks(dashboard):
    for d in dashboard["datasets"]:
        sql = "".join(d["queryLines"])
        sqlglot.parse_one(sql, dialect="databricks")  # raises on failure


def test_each_check_sql_with_markers_filled_parses_as_databricks():
    for query_id in bdd.all_checks():
        sqlglot.parse_one(bdd.read_check(query_id), dialect="databricks")
    for name in bdd.DIMS:
        sqlglot.parse_one(bdd.read_dim(name), dialect="databricks")


# ---------------------------------------------------------------------------
# Area dashboards: inside Databricks' limits, every check placed once, columns from the real SQL
# ---------------------------------------------------------------------------

def test_dashboard_limits(dashboard):
    # docs.databricks.com/aws/en/dashboards/limits
    assert len(dashboard["datasets"]) <= 100
    assert len(dashboard["pages"]) <= 15
    assert all(len(p["layout"]) <= 100 for p in dashboard["pages"])


def test_every_placed_check_has_its_rows_on_its_tab():
    reg = bdd.area_registry()
    for tab in bdd.TABS:
        d = json.loads(DASHBOARD_PATHS[tab].read_text(encoding="utf-8"))
        shown = {ds["displayName"] for ds in d["datasets"]}
        for _, _, ids, refs in reg[tab]["pages"]:
            for q in ids + refs:
                assert f"check:{q}" in shown, f"{tab}: {q} has no table"


# ---------------------------------------------------------------------------
# What a Databricks import rejects or renders blank, as learned from real imports
# ---------------------------------------------------------------------------

def test_visual_widgets_read_main_query(dashboard):
    # a chart, counter or table draws only from the query named main_query
    for page, w in iter_data_widgets(dashboard):
        if w["spec"]["widgetType"].startswith("filter"):
            continue
        assert [q["name"] for q in w["queries"]] == ["main_query"], (page["displayName"], w["name"])


def test_every_kpi_chart_and_table_says_how_it_is_worked_out(dashboard):
    for page, w in iter_data_widgets(dashboard):
        if w["spec"]["widgetType"].startswith("filter"):
            continue
        frame = w["spec"]["frame"]
        assert frame.get("showDescription") and frame.get("description", "").strip(), (
            f"{page['displayName']}: {frame.get('title')!r} has no how-it-works line"
        )


def test_kpi_how_lines_fit_their_tile_and_name_no_check_id(dashboard):
    ids = set(bdd.all_checks())
    for page, w in iter_data_widgets(dashboard):
        frame = w["spec"].get("frame") or {}
        desc = frame.get("description", "")
        if w["spec"]["widgetType"] == "counter":
            assert len(desc) <= kit.KPI_HOW_MAX, f"{page['displayName']}: {frame.get('title')!r} how line too long"
        assert not ids & set(re.findall(r"\w+", desc)), f"{page['displayName']}: {frame.get('title')!r} names a check id"


def test_stacked_charts_fold_small_series_into_other(dashboard):
    for page, w in iter_data_widgets(dashboard):
        if w["spec"]["widgetType"] in ("bar", "line") and "color" in w["spec"]["encodings"]:
            ds = w["queries"][0]["query"]["datasetName"]
            sql = next("".join(d["queryLines"]) for d in dashboard["datasets"] if d["name"] == ds)
            assert "'Other'" in sql, f"{page['displayName']}: {w['spec']['frame']['title']!r} draws every series"


def test_tables_list_rows_and_charts_aggregate(dashboard):
    for page, w in iter_data_widgets(dashboard):
        t = w["spec"]["widgetType"]
        if t.startswith("filter"):
            continue
        q = w["queries"][0]["query"]
        if t == "table":
            assert q["disaggregated"] is True
            assert "invisibleColumns" in w["spec"], (page["displayName"], w["name"])
            assert all(c["title"] for c in w["spec"]["encodings"]["columns"]), (page["displayName"], w["name"])
        else:
            assert q["disaggregated"] is False, (page["displayName"], w["name"])
            measures = [f for f in q["fields"] if f["expression"][:1] != "`"]
            assert len(measures) == 1, (page["displayName"], w["name"])


def test_no_where_names_an_alias_of_its_own_select(dashboard):
    # DuckDB allows it, Databricks fails with UNRESOLVED_COLUMN
    for d in dashboard["datasets"]:
        tree = sqlglot.parse_one("".join(d["queryLines"]), dialect="databricks")
        for sel in tree.find_all(exp.Select):
            where = sel.args.get("where")
            if where is None:
                continue
            aliases = {e.alias for e in sel.selects if isinstance(e, exp.Alias) and e.alias != e.this.sql()}
            sources = {c.name for c in sel.find_all(exp.Column)} - aliases
            for col in where.find_all(exp.Column):
                assert col.table or col.name not in aliases or col.name in sources, (
                    f"dataset {d['displayName']}: WHERE names {col.name!r}, an alias of its own SELECT"
                )


def test_dashboard_columns_json_matches_each_check_sql():
    columns = bdd.load_columns()
    for qid in bdd.all_checks():
        listed = {c.lower() for c, _ in columns[qid]}
        assert listed == {c.lower() for c in REAL_COLUMNS[qid]} - {"snapshot_at"}, qid


def test_every_dataset_runs_on_the_test_duckdb(dashboard):
    """Each dataset, translated to DuckDB, runs over views shaped like the snapshot tables."""
    duckdb = pytest.importorskip("duckdb")
    db = bdd.TEST_DB
    if not db.exists():
        pytest.skip("tests/db_audit_test.duckdb not built")
    con = duckdb.connect()
    con.execute(f"ATTACH '{db.as_posix()}' AS t (READ_ONLY)")
    s = bdd.SCHEMA_NAME
    con.execute(f"CREATE SCHEMA {s}")
    for q in bdd.all_checks():
        con.execute(f"CREATE VIEW {s}.{q} AS SELECT TIMESTAMP '2026-01-01' AS snapshot_at, * "
                    f"FROM t.findings.\"f_{q}\" WHERE window_days = 30")
    for d in bdd.DIMS:
        con.execute(f"CREATE VIEW {s}.{d} AS SELECT * FROM t.dims.{d}")
    con.execute(f"CREATE TABLE {s}.snapshot_info AS SELECT TIMESTAMP '2026-01-01' AS snapshot_at, 30 AS window_days, "
                f"'{','.join(bdd.tag_keys())}' AS tag_keys, '{bdd.CHARGEBACK_KEYS}' AS chargeback_keys")
    con.execute(f"CREATE TABLE {s}.snapshot_log AS SELECT 'x' AS query_id, false AS ok, 'e' AS error, 0 AS row_count, "
                f"0 AS critical, 0 AS warn, false AS has_status, 0.0 AS seconds")
    con.execute(f"CREATE TABLE {s}.{bdd.LINKS_TABLE} AS SELECT 'Lakehouse Health 02 - Actions' AS dashboard, "
                f"'https://example.cloud.databricks.com/dashboardsv3/x/published' AS url")
    for ds in dashboard["datasets"]:
        sql = re.sub(r":workspace\b", "array('all')", "".join(ds["queryLines"]))
        try:
            con.execute(sqlglot.transpile(sql, read="databricks", write="duckdb")[0]).fetchall()
        except Exception as e:
            raise AssertionError(f"dataset {ds['displayName']}: {e}") from None
