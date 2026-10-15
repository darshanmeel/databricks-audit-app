"""tools/dashboard_kit.py

One page layout for every per-tab dashboard, so each app tab reads the same way: the data-as-of
banner, the page title and scope, a KPI row (a value and one "worst" line each),
charts, curated tables, then every check on the page with its own rows.

Screens (tools/dashboard_screens/<tab>.py) describe a tab's pages with this kit. Their SQL reads
only snapshot tables and may use these placeholders:

  {S}            the snapshot schema
  {named:<id>}   a check's table with names added for whichever ids it carries: ws_name (plain
                 workspace name; for a check with no workspace_id, the warehouse's or cluster's own
                 workspace), nm_workspace / nm_job / nm_warehouse / nm_cluster / nm_pipeline /
                 nm_notebook (plain name), lbl_<same> ("name · workspace", the chart label, so
                 same-named resources in two workspaces stay two bars) and n_<same> ("name (id)",
                 for tables)
  {ws}           the workspace filter on ws_name; {ws:r} filters on r.ws_name
  {disc}         (1 - the discount in settings), to multiply every $ by

Every KPI, chart and table takes how="...": one line under its title saying how its number is
worked out (the rule, the source, the window).

Databricks rules a screen must keep (a dashboard import fails or a widget breaks otherwise): a
WHERE never names an alias of its own SELECT; a chart's dataset has one row per category (and
series), since the chart sums its value; table columns are plain values, timestamps formatted as
text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from tools import build_databricks_dashboards as bdd

MONEY, INT, NUM, PCT, TEXT = "money", "int", "num", "pct", "text"
CRIT, WARN, OK, INK = "#a8261d", "#c2610c", "#2f6b4b", "#1c1b18"
_TONES = {"crit": CRIT, "warn": WARN, "ok": OK, None: INK}
_TABLE_FORMATS = {MONEY: "$0,0", INT: "0,0", NUM: "0,0.[0]", PCT: "0.[0]"}
# Most series a stacked chart draws, "Other" included: the app's ui.max_chart_categories default.
LINK = "link"  # a table column of URLs, shown as "Open"
SERIES_LIMIT = 8
# A KPI tile's rows: title, a how line of up to KPI_HOW_MAX characters, the number and its note.
KPI_HEIGHT = 4
KPI_HOW_MAX = 150
_ID_RE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")


def ws_clause(col: str) -> str:
    return f"(array_contains(:workspace, COALESCE({col}, '')) OR array_contains(:workspace, 'all'))"


def named_sql(schema: str, query_id: str, cols: list[tuple[str, str]]) -> str:
    """The check's table with names joined from the dims (see the module docstring)."""
    by_lower = {c.lower(): c for c, _ in cols}
    adds, joins, names = [], [], {}
    for id_col, name_col, dim, dim_name, key in bdd._NAME_JOINS:
        if id_col not in by_lower:
            continue
        alias = f"d_{key}"
        keys = [id_col] if id_col == "workspace_id" or "workspace_id" not in by_lower else ["workspace_id", id_col]
        key_list = ", ".join(f"CAST({k} AS STRING) AS {k}" for k in keys)
        group = ", ".join(f"CAST({k} AS STRING)" for k in keys)
        on = " AND ".join(f"{alias}.{k} = CAST(t.`{by_lower[k]}` AS STRING)" for k in keys)
        joins.append(f"LEFT JOIN (SELECT {key_list}, max({dim_name}) AS n FROM {schema}.{dim} GROUP BY {group}) {alias} ON {on}")
        name = f"COALESCE(t.`{by_lower[name_col]}`, {alias}.n)" if name_col in by_lower else f"{alias}.n"
        id_text = f"CAST(t.`{by_lower[id_col]}` AS STRING)"
        names[key] = f"COALESCE({name}, {id_text})"
        adds.append(f"{names[key]} AS nm_{key}")
        adds.append(f"COALESCE(concat({name}, ' (', {id_text}, ')'), {id_text}) AS n_{key}")
    if "workspace" in names:
        ws = names["workspace"]
    else:
        # no workspace_id of its own: the warehouse's or cluster's workspace, from its dim
        src = next(((c, d) for c, _, d, _, k in bdd._NAME_JOINS if k in ("warehouse", "cluster") and c in by_lower), None)
        ws = "CAST(NULL AS STRING)"
        if src:
            joins.append(f"LEFT JOIN (SELECT CAST({src[0]} AS STRING) AS k, max(workspace_id) AS workspace_id "
                         f"FROM {schema}.{src[1]} GROUP BY 1) d_wv ON d_wv.k = CAST(t.`{by_lower[src[0]]}` AS STRING)")
            joins.append(f"LEFT JOIN (SELECT workspace_id, max(name) AS n FROM {schema}.dim_workspace GROUP BY 1) d_wn "
                         "ON d_wn.workspace_id = d_wv.workspace_id")
            ws = "COALESCE(d_wn.n, d_wv.workspace_id)"
    adds.append(f"{ws} AS ws_name")
    for key, name in names.items():
        if key != "workspace":
            adds.append(f"CASE WHEN {ws} IS NULL THEN {name} ELSE concat({name}, ' · ', {ws}) END AS lbl_{key}")
    return f"(SELECT t.*, {', '.join(adds)} FROM {schema}.{query_id} t {' '.join(joins)})"


# ------------------------------------------------------------------------------------------------
# Widget specs: built by screens, placed by Page
# ------------------------------------------------------------------------------------------------

@dataclass
class Kpi:
    title: str
    sql: str
    value: str = "value"
    target: str | None = None  # a text column: the "worst" line under the number
    fmt: str = INT
    tone: str | None = None  # "crit" | "warn" | "ok"
    how: str = ""


@dataclass
class Bar:
    title: str
    sql: str
    category: str
    value: str
    series: str | None = None  # stacked by this column
    time: bool = False  # category is a date: vertical bars over time
    fmt: str = NUM
    category_title: str = ""
    value_title: str = ""
    height: int = 7
    how: str = ""
    sort: str = "value"  # "category": bars in the category's own order (hours, bands)


@dataclass
class Line:
    title: str
    sql: str
    date: str
    value: str
    series: str | None = None
    fmt: str = NUM
    value_title: str = ""
    height: int = 7
    how: str = ""


@dataclass
class Table:
    title: str
    sql: str
    columns: list = field(default_factory=list)  # (field, title) or (field, title, fmt)
    height: int = 8
    how: str = ""


def kpi(title, sql, value="value", target=None, fmt=INT, tone=None, *, how="") -> Kpi:
    return Kpi(title, sql, value, target, fmt, tone, how)


def bar(title, sql, category, value, *, series=None, time=False, fmt=NUM, category_title="", value_title="",
        height=7, how="", sort="value") -> Bar:
    return Bar(title, sql, category, value, series, time, fmt, category_title, value_title, height, how, sort)


def line(title, sql, date, value, *, series=None, fmt=NUM, value_title="", height=7, how="") -> Line:
    return Line(title, sql, date, value, series, fmt, value_title, height, how)


def table(title, sql, columns, height=8, *, how="") -> Table:
    return Table(title, sql, columns, height, how)


def describe(widget: dict, how: str) -> dict:
    """The widget's description line under its title: how its number is worked out."""
    if how:
        frame = widget["widget"]["spec"]["frame"]
        frame["description"] = how.strip()
        frame["showDescription"] = True
    return widget


def fold_series(sql: str, keep: list[str], value: str, series: str, limit: int = SERIES_LIMIT) -> str:
    """`sql` with every series past the biggest limit - 1 renamed "Other", so a legend never scrolls."""
    cols = "".join(f"q.`{c}`, " for c in keep)
    return (f"WITH q AS (\n{sql.strip()}\n),\n"
            f"r AS (SELECT `{series}` AS s, ROW_NUMBER() OVER (ORDER BY SUM(`{value}`) DESC) AS rk FROM q "
            f"WHERE `{series}` IS NOT NULL GROUP BY `{series}`),\n"
            "n AS (SELECT COUNT(*) AS n FROM r)\n"
            f"SELECT {cols}q.`{value}`,\n"
            f"       CASE WHEN q.`{series}` IS NULL OR r.rk < {limit} OR n.n <= {limit} "
            f"THEN CAST(q.`{series}` AS STRING) ELSE 'Other' END AS `{series}`\n"
            f"FROM q LEFT JOIN r ON r.s = q.`{series}` CROSS JOIN n")


def _searchable(widget: dict, first_text_column: bool) -> dict:
    """Search box only where a long list needs one: on the first text column, or nowhere."""
    found = False
    for col in widget["widget"]["spec"]["encodings"]["columns"]:
        on = first_text_column and not found and col["type"] == "string"
        found = found or on
        col["allowSearch"] = on
    return widget


def _number_format(fmt: str) -> dict | None:
    if fmt == MONEY:
        return dict(bdd.CURRENCY_FORMAT)
    return None


# ------------------------------------------------------------------------------------------------
# Dashboard and pages
# ------------------------------------------------------------------------------------------------

class Dashboard:
    def __init__(self, schema: str, tab: str, title: str, name: str | None = None):
        self.schema, self.tab, self.title = schema, tab, title
        self.name = name or title
        self.area = bdd.area_registry().get(tab) or {"label": title, "scope": "", "pages": []}
        self.labels = bdd.check_labels()
        self.curated = bdd._curated_columns()
        self.columns = bdd.load_columns()
        self.datasets: list[dict] = []
        self._ds: dict[str, str] = {}
        self.filter_targets: list[tuple[str, str]] = []
        self.pages: list[Page] = []
        self.banner_ds = self.dataset(
            "snapshot_banner",
            "SELECT window_days AS days, concat('as of ', date_format(snapshot_at, 'MMM d, yyyy HH:mm'), ' UTC, list price') "
            "AS as_of FROM {S}.snapshot_info",
        )
        self.options_ds = self.dataset(
            "workspace_options",
            "SELECT DISTINCT name AS workspace_name FROM {S}.dim_workspace WHERE name IS NOT NULL ORDER BY 1",
        )

    def expand(self, sql: str) -> str:
        sql = re.sub(r"\{named:(\w+)\}", lambda m: named_sql(self.schema, m.group(1), self.columns[m.group(1)]), sql)
        sql = re.sub(r"\{ws:(\w+)\}", lambda m: ws_clause(f"{m.group(1)}.ws_name"), sql)
        sql = sql.replace("{ws}", ws_clause("ws_name"))
        sql = sql.replace("{disc}", bdd.discount_factor_sql())
        return sql.replace("{S}", self.schema)

    def dataset(self, key: str, sql: str) -> str:
        """Register a dataset once per key; one that reads :workspace joins the workspace filter."""
        if key in self._ds:
            return self._ds[key]
        text = self.expand(sql).strip() + "\n"
        name = bdd.hid(f"dataset:{self.tab}:{key}")
        ds = {"name": name, "displayName": key, "queryLines": bdd.query_lines(text)}
        if ":workspace" in text:
            ds["parameters"] = [dict(bdd.WORKSPACE_PARAM)]
            self.filter_targets.append((f"{self.tab}:{key}", name))
        self.datasets.append(ds)
        self._ds[key] = name
        return name

    def title_of(self, query_id: str) -> str:
        return bdd.check_title(query_id, self.labels)

    def plain(self, how: str) -> str:
        """A how line with check ids read as the check's title and name tables as plain words."""
        def word(m: re.Match) -> str:
            ident = m.group(0)
            if ident in self.labels:
                return f'"{self.title_of(ident)}"'
            return f"{ident[4:]} names" if ident.startswith("dim_") else ident
        return _ID_RE.sub(word, how)

    def why_of(self, query_id: str) -> str | None:
        return self.labels.get(query_id, (None, None))[1]

    def page(self, key: str, title: str | None = None, scope: str | None = None) -> "Page":
        reg = {k: (label, ids, refs) for k, label, ids, refs in self.area["pages"]}
        label, ids, refs = reg.get(key, (title or self.title, [], []))
        p = Page(self, key, title or label, self.area["scope"] if scope is None else scope, ids, refs)
        self.pages.append(p)
        return p

    def to_json(self) -> dict:
        built = {p.key for p in self.pages}
        for key, *_ in self.area["pages"]:
            if key not in built:
                self.page(key).checks()
        order = [k for k, *_ in self.area["pages"]]
        pages = sorted(self.pages, key=lambda p: order.index(p.key) if p.key in order else len(order))
        out = [p.to_json() for p in pages]
        out.append({
            "name": bdd.hid(f"page:{self.tab}:global_filters"),
            "displayName": "Global Filters",
            "pageType": "PAGE_TYPE_GLOBAL_FILTERS",
            "layoutVersion": "GRID_V1",
            "layout": [bdd.workspace_filter_widget(self.tab, self.options_ds, self.filter_targets,
                                                   x=0, y=0, width=3, height=2)],
        })
        return {"datasets": self.datasets, "pages": out, "uiSettings": bdd.build_ui_settings()}


class Page:
    def __init__(self, dash: Dashboard, key: str, title: str, scope: str, ids: list[str], refs: list[str]):
        self.d, self.key, self.title = dash, key, title
        self.ids, self.refs = ids, refs
        self.layout: list[dict] = []
        self.y = 0
        self.n = 0
        # title on the left, the snapshot's window and date on the right
        lines = [f"# **{title}**", " · ".join([dash.name] + ([scope.rstrip(".")] if scope else []))]
        self.layout.append(bdd.text_widget(self._k("text"), [ln + "\n" for ln in lines], width=9, height=3, x=0, y=0))
        self.layout.append(describe(bdd.counter_widget(self._k("banner"), dash.banner_ds, "days", "Days covered",
                                                       target_col="as_of", agg="MAX", width=3, height=3, x=9, y=0),
                                    "Days in the snapshot. New numbers: run the notebook again."))
        self.y = 3

    def _k(self, kind: str) -> str:
        self.n += 1
        return f"{self.d.tab}:{self.key}:{kind}{self.n}"

    def _place(self, widget: dict, height: int) -> None:
        self.layout.append(widget)
        self.y += height

    # -- static text ------------------------------------------------------------------------------
    def text(self, lines: list[str], height: int = 2) -> "Page":
        self._place(bdd.text_widget(self._k("text"), [ln.rstrip("\n") + "\n" for ln in lines],
                                    width=12, height=height, x=0, y=self.y), height)
        return self

    # -- KPI row ----------------------------------------------------------------------------------
    def kpis(self, *items: Kpi) -> "Page":
        width = 12 // max(1, len(items))
        for i, k in enumerate(items):
            ds = self.d.dataset(f"{self.key}:kpi:{k.title}", k.sql)
            w = bdd.counter_widget(self._k("kpi"), ds, k.value, k.title, target_col=k.target,
                                   currency=k.fmt == MONEY, color=_TONES.get(k.tone, INK), agg=_agg(k.fmt),
                                   width=width, height=KPI_HEIGHT, x=i * width, y=self.y)
            self.layout.append(describe(w, self.d.plain(k.how)))
        self.y += KPI_HEIGHT
        return self

    # -- charts and tables --------------------------------------------------------------------------
    def chart(self, spec) -> "Page":
        return self.charts(spec)

    def charts(self, *specs) -> "Page":
        width = 12 // max(1, len(specs))
        height = max(s.height for s in specs)
        for i, s in enumerate(specs):
            self.layout.append(self._widget(s, x=i * width, width=width, height=height))
        self.y += height
        return self

    def table(self, spec: Table) -> "Page":
        self.layout.append(self._widget(spec, x=0, width=12, height=spec.height))
        self.y += spec.height
        return self

    def _widget(self, s, *, x: int, width: int, height: int) -> dict:
        sql = s.sql
        if isinstance(s, Bar) and s.series:
            sql = fold_series(sql, [s.category], s.value, s.series)
        if isinstance(s, Line) and s.series:
            sql = fold_series(sql, [s.date], s.value, s.series)
        ds = self.d.dataset(f"{self.key}:{s.title}", sql)
        how = self.d.plain(s.how)
        if isinstance(s, Bar):
            return describe(_bar(self._k("bar"), ds, s, x=x, y=self.y, width=width, height=height), how)
        if isinstance(s, Line):
            return describe(_line(self._k("line"), ds, s, x=x, y=self.y, width=width, height=height), how)
        if isinstance(s, Table):
            cols = []
            for c in s.columns:
                f, t, fmt = (c + (TEXT,))[:3]
                kind = "string" if fmt in (TEXT, LINK) else "number"
                cols.append((f, t, kind, _TABLE_FORMATS.get(fmt)))
            w = _searchable(bdd.data_table_widget(self._k("table"), ds, cols, s.title, x=x, y=self.y,
                                                  width=width, height=height), False)
            for spec, c in zip(w["widget"]["spec"]["encodings"]["columns"], s.columns):
                if (c + (TEXT,))[2] == LINK:
                    spec.update(displayAs="link", linkTextTemplate="Open", linkTitleTemplate="{{ @ }}")
            return describe(w, how)
        raise TypeError(s)

    # -- every check on the page, the app's drill-down ------------------------------------------------
    def checks(self, ids: list[str] | None = None, refs: list[str] | None = None, heading: bool = True) -> "Page":
        ids = self.ids if ids is None else ids
        refs = self.refs if refs is None else refs
        items = [(q, False) for q in ids] + [(q, True) for q in refs]
        if not items:
            return self
        if heading and len(self.layout) > 2:
            self.text(["## Every check on this page", "Each check's own rows, worst first."])
        entries = [(q, self.d.title_of(q), self.title) for q, _ in items]
        status_ds = self.d.dataset(f"{self.key}:status", bdd._status_sql("{S}", entries))
        h = min(12, 3 + len(items))
        self._place(describe(_searchable(bdd.data_table_widget(
            self._k("status"), status_ds, [c for c in bdd._STATUS_COLUMNS if c[0] != "page"], "Checks on this page",
            x=0, y=self.y, width=12, height=h), False),
            "Each check's verdict from the snapshot: Critical or Warn when any row is flagged, Not assessed "
            "when the check could not run (the reason is in the last column)."), h)
        for q, is_ref in items:
            sql, shown, _ = bdd.check_dataset("{S}", q, self.d.columns[q], self.d.curated)
            ds = self.d.dataset(f"check:{q}", sql)
            note = " ".join(x for x in (self.d.why_of(q), "Reference list, not judged." if is_ref else None) if x)
            self._place(describe(_searchable(bdd.data_table_widget(
                self._k("rows"), ds, shown, self.d.title_of(q), x=0, y=self.y, width=12, height=8), True),
                note or "The check's own rows, worst first."), 8)
        return self

    def to_json(self) -> dict:
        return {
            "name": bdd.hid(f"page:{self.d.tab}:{self.key}"),
            "displayName": self.title,
            "pageType": "PAGE_TYPE_CANVAS",
            "layoutVersion": "GRID_V1",
            "layout": self.layout,
        }


def _agg(fmt: str) -> str:
    # one row per group, so SUM is the value; ratios use MAX so a stray duplicate never adds up
    return "MAX" if fmt in (PCT, NUM) else "SUM"


def _bar(key: str, ds: str, s: Bar, *, x: int, y: int, width: int, height: int) -> dict:
    w = bdd.bar_widget(key, ds, s.category, s.value, s.title, category_label=s.category_title or s.category,
                       value_label=s.value_title or s.value, color_col=s.series, temporal=s.time,
                       currency=s.fmt == MONEY, agg=_agg(s.fmt), width=width, height=height, x=x, y=y)
    if s.sort == "category" and not s.time:
        w["widget"]["spec"]["encodings"]["y"]["scale"]["sort"] = {"by": "natural-order"}
    return w


def _line(key: str, ds: str, s: Line, *, x: int, y: int, width: int, height: int) -> dict:
    agg = _agg(s.fmt)
    y_enc: dict = {"fieldName": bdd.measure_name(s.value, agg), "displayName": s.value_title or s.value,
                   "scale": {"type": "quantitative"}}
    fmt = _number_format(s.fmt)
    if fmt:
        y_enc["format"] = fmt
    enc: dict = {"x": {"fieldName": s.date, "displayName": "Day", "scale": {"type": "temporal"}}, "y": y_enc}
    if s.series:
        enc["color"] = {"fieldName": s.series, "displayName": s.series, "legend": {"position": "bottom"},
                        "scale": {"type": "categorical"}}
    return {
        "widget": {
            "name": bdd.hid(f"widget:line:{key}"),
            "queries": [bdd.chart_query(f"q_{bdd.hid('qry:' + key)}", ds, [s.date] + ([s.series] if s.series else []),
                                        s.value, agg)],
            "spec": {"version": 3, "widgetType": "line", "encodings": enc,
                     "frame": {"title": s.title, "showTitle": True}},
        },
        "position": {"x": x, "y": y, "width": width, "height": height},
    }
