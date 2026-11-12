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


def test_sizes_notebook_walks_volumes_without_dbutils(tmp_path):
    import os
    import re
    import types

    from tools import direct_data_usage as wdata

    nb = wdata.collect_sizes_notebook()
    assert "dbutils.fs.ls(" not in nb
    walk_src = re.search(r"^def walk\(v\):\n(?:(?:    .*)?\n)+", nb, re.M).group(0)
    (tmp_path / "c/s/v/sub").mkdir(parents=True)
    (tmp_path / "c/s/v/a.csv").write_bytes(b"x" * 100)
    (tmp_path / "c/s/v/sub/b.csv").write_bytes(b"x" * 50)
    def scandir(p):  # stands in for the /Volumes mount
        return os.scandir(str(tmp_path) + p[len("/Volumes"):] if p.startswith("/Volumes/") else p)

    fake_os = types.SimpleNamespace(scandir=scandir)
    v = types.SimpleNamespace(volume_catalog="c", volume_schema="s", volume_name="v")

    def run(cap):
        ns = {"os": fake_os, "cap": cap, "now": None}
        exec(walk_src, ns)
        return ns["walk"](v)

    assert run(10)[3:7] == (2, 150, False, "listed")
    assert run(1)[5] is True


def test_sizes_notebook_describes_only_changed_tables():
    import datetime
    import re
    from collections import namedtuple

    from tools import direct_data_usage as wdata

    nb = wdata.collect_sizes_notebook()
    cell = re.search(r"^tables = spark\.sql\(.*?^print\(f\"\{len\(rows\)\}", nb, re.M | re.S).group(0)
    cell = cell[:cell.rindex("\nprint(")]
    T = namedtuple("T", "table_catalog table_schema table_name last_altered")
    S = namedtuple("S", "catalog schema table_name size_in_bytes num_files last_modified detail collected_at last_altered")
    Detail = namedtuple("Detail", "sizeInBytes numFiles lastModified")
    then = datetime.datetime(2026, 1, 1)
    stored = [S("c", "s", "same", 10, 1, "x", "described", then, "2026-01-01"),
              S("c", "s", "new", 20, 2, "x", "described", then, "2026-01-01")]
    sent, written = [], {}

    class DF:
        def __init__(self, rows, cols=None):
            self.rows, self.columns = rows, cols or list(S._fields)
        def where(self, _):
            return self
        def collect(self):
            return self.rows
        def join(self, keep, on, how):
            keys = {tuple(r) for r in keep.rows}
            return DF([r for r in self.rows if tuple(r[:3]) in keys])
        def unionByName(self, other):
            return DF(self.rows + other.rows)
        @property
        def write(self):
            df = self
            class W:
                def mode(self, _): return self
                def option(self, *_): return self
                def saveAsTable(self, name): written[name] = df.rows
            return W()

    class Spark:
        class catalog:
            tableExists = staticmethod(lambda name: True)
        def sql(self, text, args=None):
            sent.append(text)
            if text.startswith("SELECT"):
                return DF([T("c", "s", "same", "2026-01-01"), T("c", "s", "new", "2026-02-01")])
            if text.startswith("DESCRIBE DETAIL"):
                return DF([Detail(99, 9, "y")])
            return DF([])
        def table(self, name):
            return DF(stored if name.endswith("sizes") else [("c", "s", "same", 1), ("c", "s", "new", 1)])
        def createDataFrame(self, rows, schema):
            return DF(list(rows))

    ns = {"spark": Spark(), "out": "o", "q": lambda x: x, "max_tables": 10, "cats": ["c"], "now": then,
          "threads": 2, "ThreadPoolExecutor": __import__("concurrent.futures").futures.ThreadPoolExecutor}
    exec(cell, ns)
    assert [t for t in sent if t.startswith("DESCRIBE")] == ["DESCRIBE DETAIL c.s.new", "DESCRIBE HISTORY c.s.new LIMIT 100"]
    sizes = {r[2]: r for r in next(v for k, v in written.items() if "sizes" in k)}
    assert sizes["same"][3] == 10 and sizes["new"][3] == 99 and sizes["new"][8] == "2026-02-01"
    hist = next(v for k, v in written.items() if "history" in k)
    assert [r[2] for r in hist] == ["same"]  # the changed table's old history is dropped, its new one comes from DESCRIBE
