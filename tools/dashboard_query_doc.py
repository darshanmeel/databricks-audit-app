"""Write an HTML guide to one dashboard's queries for new readers: the system tables behind it, then
per query the tables it reads, each CTE step, the final query, and the SQL with a comment per CTE.

    python tools/dashboard_query_doc.py            # Waste & savings -> docs/waste-dashboard-queries.html
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "databricks_dashboards" / "Lakehouse Health 07 - Waste & savings.lvdash.json"
OUT = ROOT / "docs" / "waste-dashboard-queries.html"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core import config as app_config  # noqa: E402

SCHEMA = app_config.dashboards()["snapshot_schema"]  # settings dashboards

PAGES = {
    "snapshot_banner": "Every page", "workspace_options": "Global Filters",
    "priced": "Priced", "whatif": "What-if", "check": "What-if", "unpriced": "Flagged, not priced",
    "method": "How it's counted",
}

# Comment above each CTE, by CTE name. "w" means two things, told apart by the SQL around it.
CTE_NOTES = {
    "src": "Stack the 5 priced waste checks into one list: one row per flagged resource, with its source, "
           "status and possible-waste $ (list price less your discount).",
    "eff": "Apply the $50-per-30-days floor: a resource under it is treated as OK, so small amounts never count.",
    "named_all": "Same rows with the workspace name added (kept for other widgets; this one may not use it).",
    "flagged": "Keep only resources still Critical or Warn after the floor, with more than $0 of waste.",
    "named": "Turn ids into names: workspace, and warehouse / cluster / job / endpoint by kind. "
             "LEFT JOINs, so a resource with no name row still shows by its id.",
    "named_ws": "Copy workspace_name into ws_name, the column the Workspace filter reads.",
    "ranked": "Number the flagged resources from the most waste down.",
    "w@autostop": "Every warehouse from the idle-time check, with its warehouse and workspace names joined on.",
    "w@whatif": "Warehouses we can replay: measured, with an auto-stop setting, a price per minute, and inside "
                "the Workspace filter.",
    "autostop": "Pick each warehouse's lowest allowed auto-stop: 1 min on serverless, 10 min on pro and classic, "
                "with the idle minutes and cold starts the replay found at that setting.",
    "saving": "Saving = (idle minutes today - idle minutes at the lowest setting) x $ per minute x discount. "
              "Only when that setting is shorter than today's (or auto-stop is off). Extra cold starts too.",
    "saving_ws": "Copy workspace_name into ws_name, the column the Workspace filter reads.",
    "opt": "The auto-stop settings to try, in minutes.",
    "replay": "One row per warehouse and setting it could take: serverless from 1 min, pro and classic from 10, "
              "only settings shorter than today's. Picks that setting's replayed idle minutes and cold starts.",
    "whatif": "Per warehouse and setting: $ saved, idle hours saved and extra cold starts versus today. "
              "The label is zero-padded (\"05 min\") so the chart sorts 1, 2, 5, 10, 30.",
    "best": "Number each warehouse's settings from the shortest; row 1 is its lowest allowed setting.",
    "top@saved": "The 10 warehouses that save the most at their lowest setting.",
    "top@setting": "The 8 warehouses with the biggest saving at any setting.",
    "x": "Add each warehouse's lowest setting, so the chart can show that one plus 5 and 10 min.",
    "checks": "The checks this widget reports on, typed in: id, title and the tab it lives on.",
    "status": "Each check's verdict from the snapshot log (Critical, Warn, OK, Not assessed, ...) and how many "
              "rows it flagged. LEFT JOIN, so a check that never ran still gets a row.",
    "q": "A wrapper around the steps below: it holds the chart's rows so the last steps (r, n) can cap the legend.",
    "r": "Rank the legend entries by size.",
    "n": "Count the legend entries.",
}
NAMED_NOTE = ("Name lookup: the check's table with names joined from the dim tables (workspace, warehouse, ...). "
              "nm_* = name, n_* = \"name (id)\", lbl_* = \"name · workspace\", ws_name = workspace for the filter.")
WS_NOTE = "Workspace filter: keep rows whose workspace is picked in Global Filters, or all when 'all' is picked."
FOLD_NOTE = "Legend cap: past the 8 biggest entries, the rest are renamed 'Other' so the legend never scrolls."
TAGS_NOTE = ("Query tags: they tell this query apart in system.query.history (settings query_tags, "
             "then the dashboard and tab). The query starts after them.")
FINAL_NOTE = "Final SELECT: the rows this widget reads."

# What a system table holds, in one line. Which system tables feed each check table is read from
# the snapshot notebook; this list only explains them.
SYSTEM_TABLES = {
    "system.billing.usage": "Every billed usage record: which SKU, how many DBUs, when, and on which "
                            "warehouse, cluster, job or endpoint.",
    "system.billing.list_prices": "The list price of each SKU over time; DBUs x price = $.",
    "system.billing.attributed_usage": "Warehouse cost split down to the statements that used it.",
    "system.compute.warehouses": "Each SQL warehouse's settings over time: size, type, auto-stop minutes.",
    "system.compute.warehouse_events": "Warehouse starts, stops and scale-ups/downs with their times.",
    "system.compute.clusters": "Each cluster's settings over time: node type, workers, auto-termination.",
    "system.compute.node_timeline": "Per cluster node, per minute: CPU and memory in use.",
    "system.compute.instance_pools": "Each instance pool's settings: idle instances kept warm.",
    "system.query.history": "Every SQL statement: who ran it, on which warehouse, start, end, status, bytes read.",
    "system.lakeflow.jobs": "Each job's name and settings.",
    "system.lakeflow.job_run_timeline": "Each job run: start, end and result (succeeded, failed, ...).",
    "system.lakeflow.job_task_run_timeline": "Each task inside a job run, with the compute it ran on.",
    "system.lakeflow.pipelines": "Each Lakeflow (DLT) pipeline's name and settings.",
    "system.lakeflow.pipeline_update_timeline": "Each pipeline update: start, end and result.",
    "system.serving.served_entities": "What each model-serving endpoint serves.",
    "system.serving.endpoint_usage": "Requests and tokens per serving endpoint.",
    "system.access.workspaces_latest": "Each workspace's id and name.",
}

# Tables in the snapshot schema: what one row is and what it is for.
SNAPSHOT_TABLES = {
    "compute_warehouse_idle_minutes": "One row per SQL warehouse: minutes running, busy and idle in the window, "
        "the $ of idle time, its auto-stop setting, and the replay columns autostop_N_* (idle minutes and cold "
        "starts had auto-stop been N minutes).",
    "compute_idle_node_ratio": "One row per classic cluster: how much of its node time had almost no CPU in use, "
        "and that idle time's $.",
    "lakeflow_failed_jobs_wasted_dbus": "One row per job: DBUs and $ spent on runs that failed.",
    "cost_failed_statement_waste": "One row per warehouse: $ spent on SQL statements that failed.",
    "compute_serving_endpoint_cost_status": "One row per serving endpoint: its cost, and whether it billed "
        "with zero requests.",
    "compute_warehouse_cache_reuse": "One row per warehouse: how often reads hit the disk cache.",
    "cost_premium_serverless_photon": "Billing rows by SKU, with flags for serverless and Photon.",
    "instance_pools_idle_capacity": "One row per instance pool: idle instances kept warm.",
    "lakeflow_jobs_on_all_purpose": "One row per job that runs on an all-purpose cluster.",
    "lakeflow_stale_zombie_jobs": "One row per job with no recent run.",
    "lakeflow_pipeline_idle_tail_duration": "One row per pipeline: compute left running after its last update.",
    "compute_serving_dormant_endpoints": "One row per serving endpoint with no traffic.",
    "dim_workspace": "Workspace id -> name.",
    "dim_warehouse": "Warehouse id -> name.",
    "dim_cluster": "Cluster id -> name.",
    "dim_job": "Job id -> name.",
    "snapshot_log": "One row per check the notebook ran: ok or not, the error, rows, Critical and Warn rows.",
    "snapshot_info": "One row: when the snapshot ran and how many days it covers.",
}

# Dataset -> (what it does, why, what the final SELECT does, what comes back)
NOTES = {
    "snapshot_banner": ("Reads the snapshot's window and time.", "The \"as of\" line at the top of each page.",
        "Formats the snapshot time as text.", "days, as_of"),
    "workspace_options": ("Lists every workspace name.", "Fills the Workspace drop-down on Global Filters.",
        "Distinct names, sorted.", "workspace_name, one row each"),
    "priced:kpi:Possible waste": (
        "Adds up possible waste over the 5 priced checks after the $50 floor.",
        "The headline number: what could be saved.",
        "Sums wasted $ over the flagged resources in the picked workspaces and counts them.",
        "value ($), target (\"N resources flagged\")"),
    "priced:kpi:Top 3 resources": (
        "Same flagged list, ranked; sums the 3 biggest.", "Shows how much a few fixes would cover.",
        "Sums the rows ranked 1 to 3.", "value ($), target"),
    "priced:kpi:Biggest single fix": (
        "Same flagged list; the largest single resource and its name.", "The one fix to do first.",
        "Two small sub-queries: the biggest $ (MAX) and the name of the row with it (ORDER BY ... LIMIT 1).",
        "value ($), target (\"Resource: name\")"),
    "priced:kpi:Resources flagged": (
        "Counts flagged resources and how many of the 5 sources flagged any.", "Size of the clean-up.",
        "count(*) of rows and count(DISTINCT source_label).", "value (count), target (\"N of 5 priced sources\")"),
    "priced:Possible waste by source": (
        "Same flagged list, summed per source check.", "Which kind of waste is biggest.",
        "GROUP BY source, SUM of wasted $.", "source, usd"),
    "priced:Possible waste by resource": (
        "Same flagged list, summed per resource; top 10.", "Which resources to fix.",
        "GROUP BY resource, biggest 10 by $.", "resource, usd"),
    "priced:Warehouses with the most idle time": (
        "Same flagged list, only the warehouse idle-time source; top 8 warehouses.",
        "Idle warehouses are usually the biggest waste.",
        "Keeps source 'Warehouse idle time', labels each \"name · workspace\", biggest 8 by $.",
        "warehouse, usd"),
    "priced:Auto-stop saving available": (
        "Saving per warehouse if auto-stop were at its lowest allowed setting; top 8.",
        "The quickest fix for idle warehouses.",
        "Keeps warehouses with a saving above $0, biggest 8.", "warehouse, usd"),
    "priced:Cache reuse, lowest first": (
        "Disk-cache hit rate per warehouse, lowest 8.",
        "Low reuse means repeat reads go back to cloud storage: slower and costlier.",
        "One value per warehouse, lowest 8 first.", "warehouse, pct"),
    "priced:Compute burned on failed job runs": (
        "$ of failed job runs per job, discounted; top 8.", "Failed runs paid for nothing.",
        "Keeps Critical and Warn jobs, sums $ per job, biggest 8.", "job, usd"),
    "priced:Top resources by possible waste": (
        "Every flagged resource, worst first; up to 100.", "The full to-do list behind the charts.",
        "Turns entity_kind into a readable kind and sorts by $.",
        "source, resource, workspace, kind, possible_usd"),
    "whatif:kpi:Serverless at 1 min": (
        "Saving if every serverless warehouse stopped after 1 minute idle.",
        "The most a serverless warehouse can save (1 min is set through the API).",
        "Keeps serverless rows at setting 1; sums $ and extra cold starts, counts warehouses.",
        "value ($), target (\"+N cold starts, N warehouses\")"),
    "whatif:kpi:Serverless at 5 min": (
        "Saving if every serverless warehouse stopped after 5 minutes.", "5 min is the lowest the UI allows.",
        "Keeps serverless rows at setting 5; same sums.", "value ($), target"),
    "whatif:kpi:Pro / classic at 10 min": (
        "Saving if every pro and classic warehouse stopped after 10 minutes.", "10 min is their lowest.",
        "Keeps non-serverless rows at setting 10; same sums.", "value ($), target"),
    "whatif:kpi:Idle hours saved, lowest setting": (
        "Idle hours removed at each warehouse's lowest setting, against the idle hours today.",
        "Shows the effect in time, not only $.",
        "Sums saved hours on each warehouse's row 1; a sub-query sums today's idle hours.",
        "value (hours), target (\"of N h idle now\")"),
    "whatif:Saving by auto-stop setting": (
        "$ saved at each setting (1, 2, 5, 10, 30 min), split by warehouse type.",
        "The curve: how the saving grows as auto-stop gets shorter.",
        "GROUP BY setting and type, SUM of $; then the legend cap.", "setting, usd, kind"),
    "whatif:Extra cold starts by auto-stop setting": (
        "Extra warehouse starts at each setting, split by type.",
        "The cost of a shorter setting: users wait for a start.",
        "GROUP BY setting and type, SUM of extra starts; then the legend cap.", "setting, starts, kind"),
    "whatif:Idle cost: saved vs left, at the lowest setting": (
        "For the top 10 warehouses: today's idle $ split into saved and still idle at the lowest setting.",
        "Shows how close to zero idle each warehouse gets.",
        "UNION ALL of two rows per warehouse: 'Saved' $ and 'Still idle' $ (idle minutes left x price).",
        "warehouse, usd, part"),
    "whatif:Saving by warehouse and setting": (
        "For the top 8 warehouses: saving at their lowest setting, at 5 and at 10 min.",
        "Compare settings per warehouse.",
        "Joins the rows to the top 8 and keeps the lowest setting, 5 and 10.", "warehouse, usd, setting"),
    "whatif:Saving and extra cold starts, by warehouse and setting": (
        "Every warehouse and shorter setting that saves $1 or more.", "The full table to pick a setting from.",
        "One row per warehouse and setting, biggest saving first, up to 200.",
        "warehouse, kind, auto_stop_now, setting, saving, hours_saved, extra_starts"),
    "whatif:status": (
        "The verdict of the check this page reads, from the snapshot log.",
        "Says whether the check ran, failed or flagged rows.",
        "A typed-in list of checks LEFT JOINed to snapshot_log; CASE turns the counts into a verdict.",
        "check_title, page, verdict, row_count, critical_rows, warn_rows, error"),
    "check:compute_warehouse_idle_minutes": (
        "Every row of the warehouse idle-time check, ids shown as \"name (id)\".",
        "The raw drill-down behind the What-if page.",
        "Selects every column; LEFT JOINs to the name tables for workspace and warehouse names.",
        "every column of the check"),
    "unpriced:kpi:Flagged, not priced": (
        "Counts unpriced checks at Critical or Warn and names the worst.",
        "Waste we can see but not price.",
        "Counts Critical/Warn checks; COALESCE picks the worst Critical, else the worst Warn, else 'Nothing flagged'.",
        "value (count), target (\"Worst: check\")"),
    "unpriced:kpi:Rows flagged": (
        "Critical plus Warn rows over the unpriced checks.", "Size of the unpriced clean-up.",
        "SUM of Critical and Warn rows over flagged checks.", "value (rows), target"),
    "unpriced:kpi:Not flagged": (
        "Unpriced checks with nothing flagged.", "What is already fine.",
        "Counts checks not at Critical or Warn.", "value (count), target"),
    "unpriced:kpi:Serverless & Photon DBUs": (
        "DBUs billed on serverless or Photon SKUs.", "Premium-priced usage worth a sanity check.",
        "SUM of DBUs where is_serverless or is_photon.", "value (DBUs), target"),
    "unpriced:Flagged rows by check": (
        "Critical plus Warn rows per unpriced check.", "Which unpriced check flags most.",
        "One row per check: Critical + Warn rows.", "check_name, rows_flagged"),
    "unpriced:Every check here, worst first": (
        "Every unpriced check with its verdict and rows flagged.", "The list to work through.",
        "Sorts Critical, then Warn, then the rest; most rows first.", "status, check_name, area, rows_flagged"),
    "method:Priced sources": (
        "The 5 priced checks and why each counts, typed in.", "Explains the Priced total.",
        "A VALUES list; reads no table.", "check_name, area, why"),
    "method:Flagged, no dollar figure": (
        "The unpriced checks and why each matters, typed in.", "Explains the Flagged, not priced page.",
        "A VALUES list; reads no table.", "check_name, area, why"),
}

_CTE_RE = re.compile(r"^(\s*)(?:WITH\s+|,\s*)?(\w+) AS \(")


def _cte_note(name: str, sql: str, line: str) -> str | None:
    if name == "w":
        return CTE_NOTES["w@whatif" if "opt AS (" in sql else "w@autostop"]
    if name == "top":
        return CTE_NOTES["top@saved" if "rn = 1" in line or "LIMIT 10" in line else "top@setting"]
    return CTE_NOTES.get(name)


def _split_named(line: str) -> list[str]:
    """Break a one-line name lookup over lines at FROM / LEFT JOIN; whitespace only."""
    if "(SELECT t.*" not in line:
        return [line]
    pad = re.match(r"\s*", line).group(0) + "    "
    s = re.sub(rf" (FROM {SCHEMA}\.\w+ t) ", lambda m: f"\n{pad}{m.group(1)}\n{pad}", line)
    s = s.replace(" LEFT JOIN (SELECT", f"\n{pad}LEFT JOIN (SELECT")
    return [ln for ln in s.split("\n") if ln.strip()]


def commented(sql: str) -> str:
    """The dataset's SQL with -- comments added above CTEs and known steps; the SQL itself is unchanged."""
    lines = sql.rstrip("\n").split("\n")
    folded = lines[0].startswith("WITH q AS (")
    top_selects = [i for i, ln in enumerate(lines) if ln.startswith("SELECT")]
    out: list[str] = []
    for i, ln in enumerate(lines):
        pad = re.match(r"\s*", ln).group(0)
        if ln.startswith("SET QUERY_TAGS[") and (i == 0 or not lines[i - 1].startswith("SET QUERY_TAGS[")):
            out.append(f"-- {TAGS_NOTE}")
        m = _CTE_RE.match(ln)
        if m:
            nxt = "\n".join(lines[i:i + 4])
            note = _cte_note(m.group(2), sql, nxt)
            if folded and m.group(2) == "r":
                out.append(f"{pad}-- {FOLD_NOTE}")
            elif note:
                out.append(f"{pad}-- {note}")
        if ln.startswith("SELECT"):
            if folded and i != top_selects[-1]:
                out.append("-- The chart's rows, before the legend cap below.")
            elif i == top_selects[-1] and i > 0:
                out.append(f"-- {FINAL_NOTE}")
        if "(SELECT t.*" in ln:
            out.append(f"{pad}-- {NAMED_NOTE}")
        if "array_contains(:workspace" in ln:
            out.append(f"{pad}-- {WS_NOTE}")
        out.extend(_split_named(ln))
    return "\n".join(out) + "\n"


CSS = """
:root { --bg:#fbfbfa; --fg:#1d1d1b; --muted:#5f5f5a; --line:#e2e1dc; --card:#ffffff; --code:#f4f3ef; --accent:#1f5f8b; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ecebe6; --muted:#a3a29b; --line:#33332f;
  --card:#1e1e1c; --code:#232321; --accent:#7fb4dc; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 19px; margin: 32px 0 8px; padding-top: 8px;
  border-top: 1px solid var(--line); } h3 { font-size: 16px; margin: 0 0 6px; }
p, li { color: var(--fg); } .muted { color: var(--muted); }
nav a, a { color: var(--accent); } nav { margin: 12px 0 0; display:flex; flex-wrap:wrap; gap: 6px 14px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; margin: 12px 0; }
dl { display: grid; grid-template-columns: 110px 1fr; gap: 4px 12px; margin: 0 0 8px; }
dt { color: var(--muted); } dd { margin: 0; }
code { font: 13px/1.45 ui-monospace, Consolas, monospace; }
pre { background: var(--code); border: 1px solid var(--line); border-radius: 6px; padding: 12px; overflow-x: auto;
  font: 12.5px/1.45 ui-monospace, Consolas, monospace; white-space: pre; }
.c { color: #2e7d32; } @media (prefers-color-scheme: dark) { .c { color: #8fcf8f; } }
summary { cursor: pointer; color: var(--accent); }
h4 { font-size: 14px; margin: 12px 0 4px; color: var(--muted); } ol, ul { margin: 4px 0; padding-left: 22px; }
.table-wrap { overflow-x: auto; } table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; vertical-align: top; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; } td:first-child { white-space: nowrap; }
"""


def _pre(sql: str) -> str:
    rows = []
    for ln in sql.rstrip("\n").split("\n"):
        e = html.escape(ln)
        rows.append(f'<span class="c">{e}</span>' if ln.lstrip().startswith("--") else e)
    return "<pre><code>" + "\n".join(rows) + "</code></pre>"


INTRO = """
<ul>
  <li><b>Two steps.</b> The notebook <code>Lakehouse Health 00 - Snapshot</code> reads the Databricks
    <b>system tables</b> once and saves one small table per check in <code>databricks_audit_dashboard</code>.
    The dashboard queries below read only those saved tables, so they are fast and never touch the system tables.</li>
  <li><b>Each query reads like a recipe:</b> the tables it starts from, then a chain of named steps
    (<code>WITH step AS (...)</code>, called CTEs, each building on the one before), then one final
    <code>SELECT</code> that gives the widget its rows.</li>
  <li><b>Name lookup:</b> ids are turned into names by LEFT JOINs to the <code>dim_*</code> tables. A LEFT JOIN keeps
    the row even when no name is found; it then shows its id.</li>
  <li><b>Workspace filter:</b> <code>:workspace</code> is what is picked on Global Filters;
    <code>array_contains(:workspace, ...)</code> keeps rows of the picked workspaces, or all of them.</li>
  <li><b>$:</b> list price, less the discount set when the dashboards were built:
    <code>(1.0 - 0.0)</code> means no discount.</li>
  <li><b>$50 floor:</b> a resource counts as waste only at $50 per 30 days or more.</li>
  <li><b>Comments in the SQL</b> (green, starting <code>--</code>) are added on this page only. Long name lookups
    are broken over lines; nothing else differs from the dashboard.</li>
</ul>
"""

NOTEBOOK = ROOT / "databricks_dashboards" / "Lakehouse Health 00 - Snapshot.py"


def built_from() -> dict[str, list[str]]:
    """Check or dim table -> the system tables its notebook query reads."""
    text = NOTEBOOK.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r'(?:CHECKS|DIMS)\["(\w+)"\] = r"""(.*?)"""', text, re.S):
        found = re.findall(r"`?system`?\.`?(\w+)`?\.`?(\w+)`?", m.group(2))
        out[m.group(1)] = sorted({f"system.{a}.{b}" for a, b in found})
    return out


def _ctes(sql: str) -> list[tuple[str, str]]:
    lines = sql.split("\n")
    folded = lines[0].startswith("WITH q AS (")
    seen, out = set(), []
    for i, ln in enumerate(lines):
        m = _CTE_RE.match(ln)
        if not m or m.group(2) in seen:
            continue
        name = m.group(2)
        seen.add(name)
        note = FOLD_NOTE if folded and name == "r" else _cte_note(name, sql, "\n".join(lines[i:i + 4]))
        out.append((name, note or ""))
    return out


def _anchor(name: str) -> str:
    return "t-" + re.sub(r"[^a-z0-9]+", "-", name.lower())


def _link(t: str) -> str:
    return f'<a href="#{_anchor(t)}"><code>{t}</code></a>'


def _table_item(t: str, sources: dict[str, list[str]]) -> str:
    src = sources.get(t, [])
    if src:
        made = "Built by the notebook from " + ", ".join(_link(x) for x in src) + "."
    else:
        made = "Written by the notebook itself." if t.startswith("snapshot_") else ""
    return f'<li>{_link(t)}: {html.escape(SNAPSHOT_TABLES.get(t, ""))} <span class="muted">{made}</span></li>'


def _card(key: str, ds: dict, sources: dict[str, list[str]]) -> str:
    sql = "".join(ds["queryLines"])
    what, why, final, ret = NOTES.get(key, ("", "", "", ""))
    title = key.split(":")[-1]
    kind = "KPI" if ":kpi:" in key else ("Check rows" if key.startswith("check:") else "Widget")
    tables = sorted(set(re.findall(rf"{SCHEMA}\.(\w+)", sql)))
    reads = "".join(_table_item(t, sources) for t in tables) or "<li>No table: the rows are typed into the query.</li>"
    named = [t for t in SNAPSHOT_TABLES if f"'{t}'" in sql and t not in tables]
    if named:
        reads += ("<li>Reports on these checks through <code>snapshot_log</code>, without reading their rows: "
                  + ", ".join(_link(t) for t in named) + ".</li>")
    steps = "".join(f"<li><code>{n}</code>: {html.escape(note)}</li>" for n, note in _ctes(sql))
    steps_html = f"<ol>{steps}</ol>" if steps else '<p class="muted">No steps: one SELECT straight from the table.</p>'
    anchor = html.escape(re.sub(r"[^a-z0-9]+", "-", key.lower()))
    return f"""
<div class="card" id="{anchor}">
  <h3>{html.escape(title)} <span class="muted">· {kind}</span></h3>
  <p><b>Shows:</b> {html.escape(what)} <b>Why:</b> {html.escape(why)}</p>
  <h4>1. Tables it reads</h4><ul>{reads}</ul>
  <h4>2. Steps (CTEs), in order</h4>{steps_html}
  <h4>3. Final query</h4><p>{html.escape(final)} Returns <code>{html.escape(ret)}</code>.</p>
  <details><summary>SQL with comments ({len(ds['queryLines'])} lines)</summary>{_pre(commented(sql))}</details>
</div>"""


def _glossary(dash: dict, sources: dict[str, list[str]]) -> str:
    used: set[str] = set()
    for ds in dash["datasets"]:
        sql = "".join(ds["queryLines"])
        used |= set(re.findall(rf"{SCHEMA}\.(\w+)", sql))
        used |= {t for t in SNAPSHOT_TABLES if f"'{t}'" in sql}
    snap = [t for t in SNAPSHOT_TABLES if t in used]
    system = sorted({x for t in snap for x in sources.get(t, [])})
    sys_rows = "".join(f'<tr id="{_anchor(x)}"><td><code>{x}</code></td><td>{html.escape(SYSTEM_TABLES.get(x, ""))}</td></tr>'
                       for x in system)
    snap_rows = "".join(
        f'<tr id="{_anchor(t)}"><td><code>{t}</code></td><td>{html.escape(SNAPSHOT_TABLES[t])}</td>'
        f'<td>{", ".join(_link(x) for x in sources.get(t, [])) or "the notebook"}</td></tr>' for t in snap)
    return f"""
<h2 id="tables">The tables</h2>
<h3>Databricks system tables</h3>
<p class="muted">Read only by the snapshot notebook. Databricks fills them for the whole account.</p>
<div class="table-wrap"><table><tr><th>System table</th><th>What it holds</th></tr>{sys_rows}</table></div>
<h3>Tables the dashboard reads</h3>
<p class="muted">In <code>{SCHEMA}</code>, written by the snapshot notebook.</p>
<div class="table-wrap"><table><tr><th>Table</th><th>One row is</th><th>Built from</th></tr>{snap_rows}</table></div>
"""


def build(dash_path: Path = DASH) -> str:
    dash = json.loads(dash_path.read_text(encoding="utf-8"))
    sources = built_from()
    groups: dict[str, list[str]] = {}
    for ds in dash["datasets"]:
        key = ds["displayName"]
        page = PAGES.get(key, PAGES.get(key.split(":")[0], "Other"))
        groups.setdefault(page, []).append(_card(key, ds, sources))
    order = ["Priced", "What-if", "Flagged, not priced", "How it's counted", "Every page", "Global Filters", "Other"]
    pages = [p for p in order if p in groups]
    nav = '<a href="#tables">The tables</a> ' + " ".join(
        f'<a href="#p-{i}">{html.escape(p)}</a>' for i, p in enumerate(pages))
    body = "".join(f'<h2 id="p-{i}">Page: {html.escape(p)}</h2>' + "".join(groups[p]) for i, p in enumerate(pages))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Waste dashboard queries</title><style>{CSS}</style></head>
<body><main>
<h1>Lakehouse Health 07 - Waste &amp; savings: the queries</h1>
<p class="muted">{len(dash['datasets'])} queries, grouped by dashboard page. Each one: the tables it reads, its steps,
its final query, then the SQL with comments. Generated by <code>tools/dashboard_query_doc.py</code>.</p>
<nav>{nav}</nav>
<h2>How to read a query</h2>{INTRO.replace("databricks_audit_dashboard", SCHEMA)}
{_glossary(dash, sources)}
{body}
</main></body></html>
"""


def main(argv: list[str]) -> int:
    OUT.write_text(build(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
