"""tools/direct_dashboard_doc.py

The HTML guide written next to each dashboard in databricks_dashboards/system_tables_direct/: the
system tables, each widget and the query it reads, then every query step by step (its checks, their
inner steps, the final SELECT) with the SQL commented. For new readers; tools/build_direct_dashboards.py
calls build().
"""
from __future__ import annotations

import html
import re
import textwrap

from tools import direct_guides as guides
from tools.direct_stories import STEP_EXAMPLES
from tools.dashboard_query_doc import CSS, SYSTEM_TABLES, _pre

SYSTEM_NOTES = {
    **SYSTEM_TABLES,
    "system.billing.usage": "Every billed usage record: SKU, DBUs, the day, and the warehouse, cluster, job or "
                            "endpoint it was for.",
    "system.access.audit": "One row per action in the account: who, when, which workspace, the service and action, "
                           "the request and its response. Kept for 365 days.",
    "system.compute.node_types": "Each machine type: memory and cores.",
    "system.lakeflow.job_tasks": "Each task of each job and its settings (timeout, ...), over time.",
}

# What each app check is, in one line (the top-level steps named after them).
CHECKS = {
    "compute_warehouse_idle_minutes": "Warehouse idle time: per SQL warehouse, minutes running, busy and idle, the "
        "idle $, and the same gaps replayed at shorter auto-stop settings.",
    "compute_idle_node_ratio": "Idle cluster nodes: per cluster, how much node time had almost no CPU in use, and "
        "that time's $.",
    "lakeflow_failed_jobs_wasted_dbus": "Failed job runs: per job, the $ of runs that failed, plus the failed part "
        "of runs that were repaired.",
    "cost_failed_statement_waste": "Failed SQL statements: per warehouse, the $ of statements that failed.",
    "compute_serving_endpoint_cost_status": "Serving endpoints: per endpoint, its $ and requests; billed with no "
        "requests is waste.",
    "instance_pools_idle_capacity": "Instance pools: per pool, the idle instances it keeps warm and its $.",
    "classic_clusters_config_current": "Cluster settings: each classic cluster's current configuration, incl. "
        "auto-termination.",
    "lakeflow_jobs_on_all_purpose": "Jobs on all-purpose clusters: per job and cluster, task runs and the job's own $.",
    "lakeflow_stale_zombie_jobs": "Stale jobs: per job, its last run; Warn after 30 days without one, Critical after 90.",
    "lakeflow_pipeline_idle_tail_duration": "Pipeline idle tail: per pipeline, DBUs billed in hours with no update running.",
    "compute_serving_dormant_endpoints": "Dormant endpoints: per serving endpoint, requests and when it last had one.",
    "lakeflow_job_duration_regression": "Slower than usual: per job, the median run time of the last days against "
        "the rest of the window.",
    "lakeflow_phase_cold_start": "Cold starts: per job, how long its clusters took to start (p95 and total).",
    "lakeflow_failed_cluster_starts": "Failed cluster starts: task runs that failed in setup and never ran, per "
        "job and error code.",
    "lakeflow_jobs_no_timeout": "Jobs with no timeout, with their $ in the window.",
    "lakeflow_job_tasks_no_timeout": "Tasks with no timeout of their own or on their job.",
    "lakeflow_job_ownership_orphans": "Ownership: jobs that run as a person, or whose creator or run-as is missing.",
    "lakeflow_job_oversized": "Oversized jobs: workers that stayed under 30% CPU and 40% memory, and the saving "
        "one size down.",
    "lakeflow_job_compute_pressure": "Compute pressure: per job, what its compute ran short of or was too big for.",
    "lakeflow_pipeline_cost": "Pipeline cost: per pipeline, DBUs, list-price $ and updates.",
}

PRICE = "List price of every SKU over time, with the date each price really ended."

# Inner steps of each check, in plain words.
INNER = {
    "compute_warehouse_idle_minutes": {
        "bounds": "The Time Window as two numbers (seconds since 1970): its first and its last moment.",
        "ev_all": "Every start, running, scale and stop event of every warehouse up to the window's end.",
        "prior": "Each warehouse's last event before the window: was it already running when the window opened?",
        "ev": "The window's events plus that carried-over state; marks each as up (starting or running) and running.",
        "seg": "Events turned into stretches: each event's state lasts until the next event or the window's end.",
        "seg_runs": "A new number each time a warehouse goes from not running to running.",
        "running": "One row per running period: when it started and when it stopped.",
        "wh_time": "Per warehouse: seconds up, seconds running, and cluster-seconds (seconds x clusters running).",
        "running_periods": "Per warehouse: how many running periods it had.",
        "qh": "Is there any query history in the window? None means the data is missing, not that all was idle.",
        "stmt": "Every SQL statement on a warehouse: its start, and when the client finished fetching the result.",
        "stmt_ord": "Statements sorted per warehouse, with the latest end so far, to spot overlaps.",
        "stmt_isl": "Overlapping statements grouped into busy stretches ('islands').",
        "busy": "One row per busy stretch: its start and end.",
        "pb": "Each busy stretch cut to the running period it falls in.",
        "qgap": "The pause after each busy stretch until the next one, or until the warehouse stopped.",
        "busy_agg": "Per warehouse: total busy seconds.",
        "marks": "Markers inside each running period: its start, each busy stretch, its end.",
        "gaps": "The time from one marker to the next: the idle gaps.",
        "idle": "Each idle gap with its kind: before the first query, between queries, after the last, or no query at all.",
        "idle_agg": "Per warehouse: idle seconds; only gaps over 60 seconds count, split by kind.",
        "counted_cw": "Idle cluster-seconds: each counted gap times the clusters running during it.",
        "price": PRICE,
        "bill_rows": "The warehouses' billed DBUs in the window, priced at list.",
        "bill": "Per warehouse: total DBUs and $, and the $ from when its events start (the part we can measure).",
        "cfg": "Each warehouse's current type and auto-stop minutes.",
        "replay": "The gaps an auto-stop acts on: after queries, periods with no query, the wait before the first query.",
        "whatif": "The gaps replayed at 1, 2, 5, 10, 30 minutes and today's setting: idle seconds left, cold starts added.",
        "keys": "Every warehouse seen in events or in billing.",
        "joined": "All of it per warehouse, and why one can't be judged (no events, no query history, never running).",
        "priced": "Idle $ = measured $ x idle cluster-seconds / all cluster-seconds; also $ per cluster-minute.",
    },
    "compute_idle_node_ratio": {
        "price": PRICE,
        "cost_rollup": "Per cluster: DBUs and $ billed in the window.",
        "finding": "Per cluster, from per-minute node data: node-minutes, and those with CPU under 5% (workers, driver).",
        "judged": "Idle is judged on the workers; a single-node cluster is judged on its driver.",
    },
    "lakeflow_failed_jobs_wasted_dbus": {
        "price": PRICE,
        "usage_rows": "Every billed DBU row of a job in the window, priced at list.",
        "job_usage": "Per job: total DBUs and $, and how much billing has no run id.",
        "run_usage": "Per job run: DBUs and $.",
        "cand": "Job runs that ended in the window.",
        "run_rows": "Every time slice of those runs.",
        "slices": "Attempts numbered within each run: a repair starts a new attempt.",
        "attempts": "One row per attempt: how long it ran and how it ended.",
        "att2": "Each run's last attempt number added.",
        "runs": "Per run: its final result, total seconds, and seconds in attempts that failed before a repair.",
        "classed": "Each run labelled failed, cancelled, repaired (failed, then fixed) or ok, with its $.",
        "last_fail": "Each job's most recent failure and its error code.",
        "per_job": "Per job: runs, failures, repairs; $ of failed runs plus the failed share of repaired runs.",
        "judged": "The job's billing, failure rate and wasted $ added; why one can't be priced.",
    },
    "cost_failed_statement_waste": {
        "price": PRICE,
        "stmts": "Every SQL statement on a warehouse in the window: its status and how much work it did.",
        "attr": "Per statement: DBUs and $ from billing.attributed_usage, where Databricks splits cost per statement.",
        "wh_attr": "Which warehouses have that per-statement billing.",
        "bill_hour": "Per warehouse and hour: DBUs and $ billed.",
        "wh_bill": "Per warehouse: total $ billed.",
        "stmt_hours": "Each statement's work spread over the hours it ran in.",
        "hour_totals": "Per warehouse and hour: all statements' work.",
        "hour_split": "Without per-statement billing: each hour's $ shared by work; failed statements summed.",
        "per_wh": "Per warehouse: statements, failures, cancels, and failed $ from per-statement billing.",
        "judged": "The method picked: per-statement billing if the warehouse has it, else the hourly share.",
        "priced": "Failed $ by that method, and why one can't be priced.",
    },
    "compute_serving_endpoint_cost_status": {
        "entities": "Each served model's latest configuration.",
        "entity_agg": "One row per endpoint: what it serves.",
        "ent_map": "Served model to endpoint, so requests roll up to the endpoint.",
        "usage_entity": "Requests per served model in the window.",
        "usage_endpoint": "Requests per endpoint in the window.",
        "usage_ever": "Any request in the last 90 days? None usually means request tracking is off.",
        "retention_history": "How far back the account's request history goes.",
        "price": PRICE,
        "cost": "Billed DBUs and $ per endpoint and product in the window.",
        "cost_ep": "One row per endpoint: its total $.",
        "scored": "Per endpoint: $, requests and status; billed with zero requests is Critical.",
    },
    "instance_pools_idle_capacity": {
        "price": PRICE,
        "cost_rollup": "Per pool: DBUs and $ billed in the window.",
    },
    "lakeflow_jobs_on_all_purpose": {
        "task_compute": "Every task run in the window and each cluster it ran on.",
        "serverless_runs": "Runs that ran on serverless.",
        "dropped": "Per workspace: runs with no compute recorded and not serverless (can't be judged).",
        "warehouse_compute": "Compute ids that are SQL warehouses, not clusters.",
        "cluster_jobs": "Per cluster: how many different jobs ran on it.",
        "price": PRICE,
        "cost_rollup": "Per cluster and job: the job's own billed DBUs and $ on that cluster.",
        "task_hours": "The hours each job had a task running on each cluster.",
        "cluster_hours": "Per cluster and hour: DBUs and $ billed.",
        "hour_cost": "When billing has no job id: the cluster's $ in the hours the job ran on it.",
        "finding": "Per job and cluster: the cluster's source (UI, API, JOB, ...), task runs and $.",
    },
    "lakeflow_stale_zombie_jobs": {
        "latest_jobs": "Each job's latest definition, deleted ones left out.",
        "last_run": "Each job's most recent run start.",
        "capture_floor": "How far back the workspace's run history goes, so a job isn't called stale too early.",
        "price": PRICE,
        "cost_rollup": "Per job: DBUs and $ billed in the window.",
    },
    "lakeflow_pipeline_idle_tail_duration": {
        "price": PRICE,
        "cost_rollup": "Per pipeline: DBUs and $ billed in the window.",
        "billed_hours": "Every billed row of a pipeline in the window.",
        "billed_active": "Whether an update was running during each billed row; serverless never lingers.",
        "idle": "Per pipeline: DBUs billed with no update running.",
    },
    "compute_serving_dormant_endpoints": {
        "history": "Each endpoint's served-entity history: what it serves and when that last changed.",
        "entity_map": "Every entity the endpoint ever served, so traffic to an old version still counts.",
        "traffic": "Requests per served entity in the window, and whether request tracking is on.",
        "billed": "DBUs billed per endpoint in the window.",
        "per_endpoint": "One row per endpoint: requests, last request and billing for its current entities.",
        "judged": "Status: no requests at all is Critical; tracking off or a recent change is not judged.",
    },
    "lakeflow_phase_cold_start": {
        "run_level": "Per job: runs, and seconds spent queued, executing and cleaning up.",
        "task_rows": "Successful task runs with their setup (cluster start) seconds.",
        "run_setup": "A run's cold start: its longest task setup; later tasks reuse the running cluster.",
        "task_setup": "Per job: setup seconds in total and the p95 cold start per run.",
    },
    "lakeflow_failed_cluster_starts": {
        "final_tasks": "Each task run's last row: its result, error code, setup and run seconds.",
    },
    "lakeflow_jobs_no_timeout": {
        "latest_jobs": "Each job's current settings: its timeout and whether a run-time warning is set.",
        "population_floor": "When this workspace's jobs first show a timeout value; an empty one before that isn't judged.",
        "price": PRICE,
        "cost_rollup": "Per job: DBUs and list-price $ in the window.",
        "judged": "No timeout = empty or 0; why it may be empty.",
    },
    "lakeflow_job_tasks_no_timeout": {
        "latest_jobs": "Each job's current name and timeout.",
        "latest_tasks": "Each task's current timeout and whether a run-time warning is set.",
        "population_floor": "When this workspace's tasks first show a timeout value; an empty one before that isn't judged.",
        "scoped_tasks": "Tasks with their job; marks tasks whose job is deleted.",
        "price": PRICE,
        "cost_rollup": "Per job: DBUs and list-price $ in the window.",
        "judged": "No timeout = empty or 0 on the task, with no timeout on its job either.",
    },
    "lakeflow_job_ownership_orphans": {
        "latest_jobs": "Each job's current creator, run-as, trigger and pause state.",
        "norm": "Redacted or empty names cleared; scheduled = has a trigger and isn't paused.",
        "judged": "Run-as kind (service principal id or a person) and whether the creator or run-as is missing.",
    },
    "lakeflow_job_duration_regression": {
        "all_rows": "Every job run row in the window, hourly slices included.",
        "row_marked": "Each row tagged with the end of the run's previous attempt, to tell attempts apart.",
        "attempt_span": "One row per attempt: its real start and end.",
        "run_final": "One row per run: its last attempt.",
        "run_duration": "Each run's minutes, and its queue, setup and execution seconds.",
        "bucketed": "Recent = the last days (up to 7, at most half the window); before = the rest.",
        "agg": "Per job: successful runs and median minutes, recent and before.",
        "grown": "Which part grew more: waiting and cluster start, or the run itself.",
        "latest_jobs": "Each job's current name.",
    },
    "lakeflow_pipeline_cost": {
        "pipe_dbus": "DBUs billed to each pipeline, maintenance split out.",
        "pipe_list": "Each pipeline's list-price $.",
        "pipe_meta": "Each pipeline's current name, type and whether it runs continuously.",
        "pipe_updates_any": "Every update row in the window, ended or not.",
        "pipe_updates": "Per pipeline: updates, and how many completed or failed.",
        "pipe_active": "Per pipeline: seconds an update was running.",
        "ws_timeline": "Workspaces with any update rows; elsewhere nothing can be judged.",
        "combined": "Spend, updates and run time together, and whether anything was delivered.",
    },
    "lakeflow_job_oversized": {
        "task_runs": "Task runs that started in the window, with their cluster.",
        "task_obs": "Each task run's start, end, cluster, and whether it ran at all.",
        "job_task_agg": "Per job: runs that ran, and runs with a cluster recorded.",
        "task_slices": "Every node-minute of each task run's cluster during that task run.",
        "job_slices": "The same, each node-minute counted once when parallel tasks share a cluster.",
        "profile": "Per job: worker CPU and memory p90 and peak over its node-minutes.",
        "latest_clusters": "Each cluster's current node type and worker settings.",
        "latest_jobs": "Each job's current name.",
        "node_mem": "Memory and cores per node type.",
        "latest_cluster": "The cluster of the job's latest run.",
        "run_starts": "Each run's first task start.",
        "run_minutes": "Node-minutes on minute boundaries, to count workers running at once.",
        "run_minute_workers": "Workers running in each minute.",
        "run_peak": "The most workers running at once per run.",
        "worker_ref": "The job's busiest run: workers configured against workers used.",
        "judged": "Per job: runs, telemetry and the CPU and memory profile together.",
        "sized": "Oversized: worker CPU p90 under 30%, memory peak under 40% and no swap, over 3+ measured runs.",
        "measured_clusters": "The run clusters with node telemetry.",
        "cost_usage": "Billed usage of those runs at list price.",
        "job_cost": "Per job: list-price $.",
        "priced": "Oversized jobs with their $.",
        "savings": "The saving: half the $ when under half those limits, else a quarter; $25+ is Warn, $200+ Critical.",
    },
    "lakeflow_job_compute_pressure": {
        "task_runs": "Task runs that started in the window, with their cluster.",
        "task_obs": "Each task run's start, end, cluster, and whether it ran at all.",
        "task_slices": "Every node-minute of each task run's cluster during that task run.",
        "job_slices": "The same, each node-minute counted once when parallel tasks share a cluster.",
        "latest_clusters": "Each cluster's current node types and worker settings.",
        "latest_jobs": "Each job's current name.",
        "node_mem": "Memory and cores per node type.",
        "run_starts": "Per run: its first task start and how many clusters it used.",
        "run_nodes": "Each node of each run's cluster, to count workers and measure skew.",
        "run_minutes": "Node-minutes on minute boundaries, to count workers running at once.",
        "run_minute_workers": "Workers running in each minute.",
        "run_peak": "The most workers running at once per run.",
        "run_cpu": "Each run cluster's worker CPU p50.",
        "run_cluster_agg": "Per run and cluster: workers seen, configured and CPU.",
        "run_cluster": "Whether the run's cluster reached its worker maximum.",
        "ceiling": "Per job: did a CPU-bound run hit its worker maximum.",
        "ceiling_ref": "The run quoted in the reason text.",
        "profile": "Per job: CPU, memory, swap and I/O wait over its node-minutes.",
        "driver_load": "Driver CPU on clusters that had workers.",
        "task_hours": "Hours per task.",
        "task_heat": "Worker memory per task.",
        "hottest": "The task with the highest worker memory.",
        "run_roll": "Per job: runs, task hours and clusters seen.",
        "run_last": "Per job: its last run and most clusters in one run.",
        "job_telemetry_clusters": "The job's clusters with node telemetry.",
        "job_classic": "The job's classic clusters (not SQL warehouses).",
        "latest_cluster": "The cluster of the job's latest run.",
        "judged": "Per job: everything above together.",
        "hinted": "The pressure: memory 85%+ or swap 10%+, skew 40%+, CPU 70%+, I/O wait 20%+, driver busy, "
                  "or idle under 20% CPU.",
    },
}

_TOP_RE = re.compile(r"^(?:WITH )?(\w+) AS \(")
_INNER_RE = re.compile(r"^(\s+)(?:WITH\s+)?(\w+) AS \(")


def _walk(d) -> "list[tuple[str, str | None, str | None, bool]]":
    """(line, top-level step, inner step or None, starts a step) for each line of the query."""
    checks = dict(d.checks)
    top = inner = None
    out = []
    for ln in d.sql().rstrip("\n").split("\n"):
        m = _TOP_RE.match(ln)
        if m:
            top, inner = m.group(1), None
            out.append((ln, top, None, True))
            continue
        m = _INNER_RE.match(ln)
        if m and top in checks:
            inner = m.group(2)
            out.append((ln, top, inner, True))
            continue
        out.append((ln, top, inner, False))
    return out


def _top_note(d, name: str) -> str:
    from tools.build_direct_dashboards import NAME_NOTES

    check = dict(d.checks).get(name)
    if check:
        return f"The shared check {check}. " + CHECKS.get(check, "")
    return d.steps.get(name) or NAME_NOTES.get(name, "")


def _inner_note(d, top: str, name: str) -> str:
    return INNER.get(dict(d.checks).get(top, ""), {}).get(name, "")


def _note(d, top: str, inner: str | None) -> str:
    return _inner_note(d, top, inner) if inner else _top_note(d, top)


def missing_notes(datasets) -> list[str]:
    return sorted({f"{d.key}:{dict(d.checks).get(top, top)}:{inner or ''}"
                   for d in datasets for _, top, inner, starts in _walk(d)
                   if starts and not _note(d, top, inner)})


def _step_sql(d) -> dict[str, str]:
    """Each top-level step's own SQL, with the comment above it."""
    out: dict[str, list[str]] = {}
    cur, pending = None, []
    for ln in commented(d).split("\n"):
        if ln.startswith("-- Final SELECT"):
            break
        m = _TOP_RE.match(ln)
        if m:
            cur = m.group(1)
            out[cur] = pending + [ln.removeprefix("WITH ")]
            pending = []
        elif ln.startswith("-- ") and not ln.startswith("  "):
            pending.append(ln)  # the next step's comment
        elif cur:
            out[cur].append(ln)
    return {k: "\n".join(v).rstrip().rstrip(",") for k, v in out.items()}


def commented(d) -> str:
    lines = _walk(d)
    last_select = max(i for i, (ln, *_rest) in enumerate(lines) if ln.startswith("SELECT"))
    out = []
    for i, (ln, top, inner, starts) in enumerate(lines):
        pad = re.match(r"\s*", ln).group(0)
        if starts and _note(d, top, inner):
            out.append(f"{pad}-- {_note(d, top, inner)}")
        if i == last_select:
            out.append("-- Final SELECT: the rows the widgets read.")
        out.append(ln)
    return "\n".join(out) + "\n"


def _anchor(name: str) -> str:
    return "t-" + re.sub(r"[^a-z0-9]+", "-", name.lower())


def _tables(sql: str) -> list[str]:
    return sorted({f"system.{a}.{b}" for a, b in re.findall(r"`?system`?\.`?(\w+)`?\.`?(\w+)`?", sql)})


def _widgets(dash: dict) -> str:
    names = {d["name"]: d["displayName"] for d in dash["datasets"]}
    rows = []
    for p in dash["pages"]:
        for l in p["layout"]:
            w = l["widget"]
            spec = w.get("spec", {})
            kind = spec.get("widgetType", "text")
            if kind == "text":
                continue
            title = spec.get("frame", {}).get("title", "")
            for q in w.get("queries", []):
                qq = q["query"]
                if "fields" not in qq:
                    keys = [p["keyword"] for p in qq.get("parameters", [])] or ["time_range"]
                    reads = "sets " + ", ".join(f"<code>:{k}</code>" for k in keys)
                else:
                    reads = ", ".join(f"<code>{html.escape(f['expression'])}</code>" for f in qq["fields"]
                                      if "associative" not in f["expression"])
                rows.append(f"<tr><td>{html.escape(p['displayName'])}</td><td>{html.escape(title)}</td>"
                            f"<td>{kind}</td><td><a href='#{qq['datasetName']}'>{html.escape(names[qq['datasetName']])}"
                            f"</a></td><td>{reads}</td></tr>")
    return ("<div class='table-wrap'><table><tr><th>Page</th><th>Widget</th><th>Type</th><th>Query</th>"
            "<th>What it takes from the query</th></tr>" + "".join(rows) + "</table></div>")


def _card(d, rich: bool = False) -> str:
    sql = d.sql()
    tables = "".join(f"<li><a href='#{_anchor(t)}'><code>{t}</code></a></li>" for t in _tables(sql))
    tops: list[str] = []
    inner: dict[str, list[str]] = {}
    for _, top, inn, starts in _walk(d):
        if not starts:
            continue
        if inn is None:
            tops.append(top)
        elif inn not in inner.setdefault(top, []):
            inner[top].append(inn)
    examples = STEP_EXAMPLES.get(d.key, {}) if rich else {}
    step_sql = _step_sql(d) if rich else {}
    steps = []
    for top in tops:
        sub = ""
        if inner.get(top):
            sub = "<ol>" + "".join(f"<li><code>{n}</code>: {html.escape(_inner_note(d, top, n))}</li>"
                                   for n in inner[top]) + "</ol>"
        if top in examples:
            sub += _example_table("What it brings, on the example:", *examples[top])
        if top in step_sql:
            n = step_sql[top].count("\n") + 1
            sub += f"<details><summary>Its SQL ({n} lines)</summary>{_pre(step_sql[top])}</details>"
        steps.append(f"<li><code>{top}</code>: {html.escape(_top_note(d, top))}{sub}</li>")
    n_lines = len(sql.rstrip("\n").split("\n"))
    why = guides.WHY.get(d.key) if rich else None
    extra = ""
    if rich:
        cols = guides.columns(d.key)
        rows = "".join(f"<tr><td><code>{c}</code></td><td>{html.escape(cols.get(c, ''))}</td></tr>"
                       for c in output_columns(d))
        extra = (f"<h4>4. Columns it returns</h4><div class='table-wrap'><table><tr><th>Column</th><th>Meaning</th>"
                 f"</tr>{rows}</table></div>")
    return f"""
<div class="card" id="{d.name}">
  <h3>{html.escape(d.title)}</h3>
  <p><b>Shows:</b> {html.escape(d.shows)} <b>Used by:</b> {html.escape(d.used_by)}</p>
  {f"<p><b>Why it is built this way:</b> {html.escape(why)}</p>" if why else ""}
  {f"<h4>How its tables join</h4>{er_svg(query_er(d))}" if rich else ""}
  <h4>1. System tables it reads</h4><ul>{tables}</ul>
  <h4>2. Steps (CTEs), in order</h4><ol>{''.join(steps)}</ol>
  <h4>3. Final query</h4><p>{html.escape(d.final)}</p>
  {extra}
  <details><summary>SQL with comments ({n_lines} lines)</summary>{_pre(commented(d))}</details>
</div>"""


GUIDE_NAV = ('<a href="#purpose">Purpose</a> <a href="#joins">Joins</a> <a href="#example">Example</a> <a href="#map">Map</a> <a href="#basics">Basics</a> '
             '<a href="#rules">Rules</a> <a href="#limits">Limits</a> <a href="#checks">Checks</a> ')

FLOW_CSS = """
.flow-wrap { overflow-x: auto; margin: 6px 0 4px; }
.flow .edge { fill: none; stroke: var(--muted); stroke-width: 1.2; opacity: .55; }
.flow rect { fill: var(--card); stroke: var(--line); stroke-width: 1.2; }
.flow text { font: 11.5px ui-monospace, Consolas, monospace; fill: var(--fg); }
.flow .table rect { fill: var(--code); }
.flow .check rect { stroke: var(--accent); stroke-width: 1.6; }
.flow .final rect { stroke: #2e7d32; stroke-width: 1.6; }
.er .ent rect { fill: var(--card); stroke: var(--line); stroke-width: 1.2; }
.er .ent .hd { fill: var(--code); }
.er .table .hd { fill: #d9d6cc; } .er .check rect { stroke: var(--accent); stroke-width: 1.6; } .er .result rect { stroke: #2e7d32; stroke-width: 1.6; }
.er .result .hd { fill: #cfe8cf; }
.er .t { font: 600 11.5px system-ui, sans-serif; fill: #1d1d1b; }
.er .c, .er .k { font: 11px ui-monospace, Consolas, monospace; fill: var(--fg); }
.er .k { fill: var(--accent); font-weight: 600; }
.er .n { font: 11px system-ui, sans-serif; fill: var(--muted); }
.er .mark circle { fill: var(--accent); } .er .mark text { font: 600 10px system-ui, sans-serif; fill: #fff; }
.er-notes { font-size: 14px; } .er-notes li { margin: 2px 0; }
.timeline text { font: 12px system-ui, sans-serif; }
.timeline .axis { fill: var(--muted); font-size: 11px; }
.timeline .tick { stroke: var(--line); stroke-dasharray: 3 3; }
.timeline .bar rect { stroke: none; }
.timeline .bar text { font-size: 11px; fill: #1d1d1b; }
.timeline .queue rect { fill: #d9d6cc; } .timeline .setup rect { fill: #f2c46d; }
.timeline .run rect { fill: #9cc3e0; } .timeline .ok rect { fill: #8fcf8f; }
.timeline .failed rect { fill: #ec8c84; } .timeline .idle rect { fill: #e8e6df; }
.legend .key { display: inline-block; width: 12px; height: 12px; border-radius: 2px; margin: 0 4px 0 10px;
  vertical-align: -1px; }
.key.queue { background: #d9d6cc; } .key.setup { background: #f2c46d; } .key.run { background: #9cc3e0; }
.key.ok { background: #8fcf8f; } .key.failed { background: #ec8c84; } .key.idle { background: #e8e6df; }
.caption { margin: 14px 0 4px; font-size: 13px; color: var(--muted); }
table.example td { font: 12.5px ui-monospace, Consolas, monospace; }
"""

INTRO = """
<ul>
  <li><b>No snapshot.</b> Each query reads the Databricks <b>system tables</b> directly, every time the dashboard
    opens or a filter changes.</li>
  <li><b>Widgets share queries.</b> Each widget reads one query and adds up its rows its own way (the Widgets
    table says how); widgets that read the same query always agree.</li>
  <li><b>Each query reads like a recipe:</b> named steps (<code>WITH step AS (...)</code>, called CTEs), each building
    on the one before, then one final <code>SELECT</code>. A step named after a check (for example
    <code>lakeflow_job_reliability</code>) is a shared, tested check reused unchanged except for the dates; its own
    steps are listed under it.</li>
  <li><b>Time Window:</b> <code>:time_range.min</code> and <code>:time_range.max</code> are the dates picked in the
    Time Window filter (on Global Filters, or on the page). The end day counts in full.</li>
  <li><b>Workspace</b> and the other field filters need no SQL: Databricks keeps the rows whose column
    matches the pick.</li>
  <li><b>$</b> is list price (usage x the SKU's list price), not the invoice.</li>
  <li><b>Comments in the SQL</b> (green, starting <code>--</code>) are added on this page only.</li>
</ul>
"""


def output_columns(d) -> list[str]:
    """The final SELECT's column names."""
    import sqlglot

    sql = (d.sql().replace(":time_range.min", "DATE'2026-01-01'").replace(":time_range.max", "DATE'2026-01-30'")
           .replace(":month", "DATE'2026-01-15'"))
    return [e.alias_or_name for e in sqlglot.parse_one(sql, read="databricks").selects]


def _steps_graph(d) -> tuple[list[str], dict[str, set[str]]]:
    """Top-level steps in order (system tables first, final last) and what each reads."""
    bodies: dict[str, list[str]] = {}
    lines = _walk(d)
    last = max(i for i, (ln, *_r) in enumerate(lines) if ln.startswith("SELECT"))
    for i, (ln, top, _inner, _starts) in enumerate(lines):
        bodies.setdefault("final SELECT" if i >= last else top, []).append(ln)
    bodies.pop(None, None)
    names = [n for n in bodies if n != "final SELECT"]
    reads: dict[str, set[str]] = {}
    for n, body in bodies.items():
        text = "\n".join(body)
        reads[n] = {t for t in _tables(text)} | {m for m in names if m != n and re.search(rf"\b{m}\b", text)}
    tables = sorted({t for r in reads.values() for t in r if t.startswith("system.")})
    return tables + names + ["final SELECT"], reads


def _svg(columns: list[list[str]], edges: list[tuple[str, str]], kinds: dict[str, str],
         down: bool = False) -> str:
    """Boxes in columns left to right, or (down) in rows top to bottom, a row wrapping after 5 boxes;
    a line for each edge."""
    w, h = 172, 26
    gx, gy = (16, 34) if down else (28, 10)
    pos = {}
    if down:
        rows = [r[i:i + 5] for r in columns for i in range(0, len(r), 5)]
        for r, row in enumerate(rows):
            for c, n in enumerate(row):
                pos[n] = (c * (w + gx), r * (h + gy))
        width = max(len(r) for r in rows) * (w + gx) - gx
        height = len(rows) * (h + gy) - gy
    else:
        for c, col in enumerate(columns):
            for r, n in enumerate(col):
                pos[n] = (c * (w + gx), r * (h + gy))
        width = len(columns) * (w + gx) - gx
        height = max(len(c) for c in columns) * (h + gy) - gy
    out = [f'<svg class="flow" viewBox="-2 -2 {width + 4} {height + 4}" width="{width + 4}" '
           f'height="{height + 4}" role="img">']
    for a, b in edges:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        if down:
            x1, x2, y1 = x1 + w / 2, x2 + w / 2, y1 + h
            mid = (y1 + y2) / 2
            out.append(f'<path class="edge" d="M{x1},{y1} C{x1},{mid} {x2},{mid} {x2},{y2}"/>')
        else:
            x1, y1, y2 = x1 + w, y1 + h / 2, y2 + h / 2
            mid = (x1 + x2) / 2
            out.append(f'<path class="edge" d="M{x1},{y1} C{mid},{y1} {mid},{y2} {x2},{y2}"/>')
    for n, (x, y) in pos.items():
        short = n.removeprefix("system.")
        label = short if len(short) <= 24 else short[:23] + "…"
        out.append(f'<g class="node {kinds.get(n, "step")}"><title>{html.escape(n)}</title>'
                   f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5"/>'
                   f'<text x="{x + 8}" y="{y + 17}">{html.escape(label)}</text></g>')
    out.append("</svg>")
    return "<div class='flow-wrap'>" + "".join(out) + "</div>"


def flow_svg(d) -> str:
    """The query as a flow, top to bottom: system tables, then each step in the row after what it reads, then
    the final SELECT."""
    order, reads = _steps_graph(d)
    layer: dict[str, int] = {}
    for n in order:
        layer[n] = 0 if n.startswith("system.") else 1 + max((layer[r] for r in reads.get(n, ()) if r in layer),
                                                             default=0)
    columns = [[n for n in order if layer[n] == i] for i in range(max(layer.values()) + 1)]
    edges = [(r, n) for n in order for r in sorted(reads.get(n, ())) if r in layer]
    checks = dict(d.checks)
    kinds = {n: "table" if n.startswith("system.") else "final" if n == "final SELECT"
             else "check" if n in checks else "step" for n in order}
    return _svg(columns, edges, kinds, down=True) + (
        "<p class='muted'>Read top to bottom. Grey: system tables (hover for the full name). Blue: a shared check. "
        "White: a step of this query. Green: the rows the widgets read.</p>")


def map_svg(dash: dict, datasets: list) -> str:
    """System tables -> queries -> pages."""
    by_name = {d.name: d for d in datasets}
    pages: dict[str, set[str]] = {}
    for p in dash["pages"]:
        for item in p["layout"]:
            for q in item["widget"].get("queries", []):
                pages.setdefault(p["displayName"], set()).add(q["query"]["datasetName"])
    qlabel = {d.name: d.key for d in datasets}
    tables = sorted({t for d in datasets for t in _tables(d.sql())})
    edges = [(t, qlabel[d.name]) for d in datasets for t in _tables(d.sql())]
    edges += [(qlabel[n], page) for page, names in pages.items() for n in sorted(names) if n in by_name]
    kinds = {**{t: "table" for t in tables}, **{p: "final" for p in pages}}
    return _svg([tables, [qlabel[d.name] for d in datasets], list(pages)], edges, kinds)


def timeline_svg(t: dict) -> str:
    """Lanes of bars on a time axis: what happened when."""
    lanes, ticks = t["lanes"], t["ticks"]
    label_w, plot_w, lane_h, top = 200, 740, 30, 26
    end = max(b for _, bars in lanes for _a, b, *_r in bars)
    x = lambda m: label_w + m / end * plot_w  # noqa: E731
    height = top + len(lanes) * lane_h + 4
    out = [f'<svg class="flow timeline" viewBox="0 0 {label_w + plot_w + 30} {height}" '
           f'width="{label_w + plot_w + 30}" height="{height}" role="img">']
    for m, lab in ticks:
        out.append(f'<line class="tick" x1="{x(m)}" y1="{top - 6}" x2="{x(m)}" y2="{height}"/>'
                   f'<text class="axis" x="{x(m)}" y="{top - 10}" text-anchor="middle">{html.escape(lab)}</text>')
    for i, (label, bars) in enumerate(lanes):
        y = top + i * lane_h
        out.append(f'<text x="0" y="{y + 19}">{html.escape(label)}</text>')
        for a, b, text, kind in bars:
            w = max(x(b) - x(a), 2)
            fits = len(text) * 7 < w - 6
            out.append(f'<g class="bar {kind}"><title>{html.escape(text)}</title>'
                       f'<rect x="{x(a)}" y="{y + 4}" width="{w}" height="{lane_h - 10}" rx="3"/>'
                       + (f'<text x="{x(a) + 5}" y="{y + 19}">{html.escape(text)}</text>' if fits else "") + "</g>")
    out.append("</svg>")
    kinds = {k for _, bars in lanes for *_r, k in bars}
    legend = " ".join(f"<span class='key {k}'></span>{TIMELINE_KEYS[k]}" for k in TIMELINE_KEYS if k in kinds)
    return "<div class='flow-wrap'>" + "".join(out) + f"</div><p class='muted legend'>{legend}</p>"


TIMELINE_KEYS = {"queue": "queued", "setup": "cluster starting", "run": "running", "ok": "succeeded",
                 "failed": "failed", "idle": "nothing"}


def _example_table(caption: str, cols: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th><code>{html.escape(c)}</code></th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in r) + "</tr>" for r in rows)
    return (f"<p class='caption'>{html.escape(caption)}</p><div class='table-wrap'><table class='example'>"
            f"<tr>{head}</tr>{body}</table></div>")


def _story(sections: list[dict]) -> str:
    out = []
    for sec in sections:
        parts = [f"<h3>{html.escape(sec['title'])}</h3>"]
        parts += [f"<p>{html.escape(t)}</p>" for t in sec.get("text", [])]
        if sec.get("code"):
            parts.append(_pre(sec["code"]))
        if sec.get("timeline"):
            parts.append(timeline_svg(sec["timeline"]))
        if sec.get("flow"):
            parts.append(_svg(*sec["flow"]))
        parts += [_example_table(*tbl) for tbl in sec.get("tables", [])]
        if sec.get("after"):
            parts.append("<ul>" + "".join(f"<li>{html.escape(a)}</li>" for a in sec["after"]) + "</ul>")
        out.append("".join(parts))
    return "".join(out)


def _guide(dash: dict, datasets: list, g: dict, stem: str) -> str:
    from tools.direct_stories import STORIES

    basics = "".join(f"<tr><td><b>{html.escape(guides.BASICS[k][0])}</b></td><td>{html.escape(guides.BASICS[k][1])}"
                     f"</td></tr>" for k in g["basics"])
    rules = "".join(f"<tr><td><b>{html.escape(n)}</b></td><td>{html.escape(r)}</td><td>{html.escape(w)}</td></tr>"
                    for n, r, w in g["rules"])
    actions = ""
    if g.get("actions"):
        from tools.direct_governance import ADMIN_ACTIONS

        actions = ("<h3>The admin actions it lists</h3><div class='table-wrap'><table><tr><th>Kind</th><th>Service"
                   "</th><th>Action</th></tr>" + "".join(
                       f"<tr><td>{html.escape(c)}</td><td><code>{s}</code></td><td><code>{a}</code></td></tr>"
                       for s, a, c in ADMIN_ACTIONS) + "</table></div>")
    limits = "".join(f"<li>{html.escape(x)}</li>" for x in g["limits"])
    checks = "".join(f"<h4>{html.escape(t)}</h4>{_pre(sql)}" for t, sql in g["checks"])
    return f"""
<h2 id="purpose">What this dashboard is for</h2><p>{html.escape(g["purpose"])}</p>
<h2 id="joins">How the tables join</h2>
{er_svg(guides.ER[stem]) if stem in guides.ER else ""}
<h2 id="example">How it works, with an example</h2>
<p class="muted">What happens in Databricks, what the system tables record, and how the dashboard's queries read it.
Names and ids are made up; table rows are simplified.</p>
{_story(STORIES.get(stem, []))}
<h2 id="map">How it fits together</h2>
<p class="muted">Left: the system tables it reads. Middle: its queries. Right: the pages whose widgets read them.</p>
{map_svg(dash, datasets)}
<h2 id="basics">Databricks basics</h2>
<div class="table-wrap"><table><tr><th>Term</th><th>What it means</th></tr>{basics}</table></div>
<h2 id="rules">Rules and thresholds</h2>
<div class="table-wrap"><table><tr><th>Rule</th><th>Definition</th><th>Why</th></tr>{rules}</table></div>{actions}
<h2 id="limits">What it can miss</h2><ul>{limits}</ul>
<h2 id="checks">Check it against your data</h2>
<p class="muted">Run these in a SQL editor to see the raw rows the dashboard starts from.</p>{checks}"""


def build(dash: dict, datasets: list, title: str, stem: str | None = None) -> str:
    g = guides.GUIDES.get(stem or "")
    used = sorted({t for d in datasets for t in _tables(d.sql())})
    sys_rows = "".join(f'<tr id="{_anchor(t)}"><td><code>{t}</code></td><td>{html.escape(SYSTEM_NOTES.get(t, ""))}</td></tr>'
                       for t in used)
    nav = " ".join(f'<a href="#{d.name}">{html.escape(d.title)}</a>' for d in datasets)
    t = html.escape(title)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t} queries</title><style>{CSS}{FLOW_CSS}</style></head>
<body><main>
<h1>{t} (system tables, direct): the queries</h1>
<p class="muted">{len(datasets)} {"query" if len(datasets) == 1 else "queries"}, each reading the Databricks system tables live. Generated from the
dashboard's own SQL; regenerate rather than edit.</p>
<nav>{GUIDE_NAV if g else ""}<a href="#how">How to read</a> <a href="#tables">System tables</a> <a href="#widgets">Widgets</a> {nav}</nav>
{_guide(dash, datasets, g, stem) if g else ""}
<h2 id="how">How to read a query</h2>{INTRO}
<h2 id="tables">System tables</h2>
<p class="muted">Databricks fills them for the whole account.</p>
<div class="table-wrap"><table><tr><th>System table</th><th>What it holds</th></tr>{sys_rows}</table></div>
<h2 id="widgets">Widgets and the query each reads</h2>{_widgets(dash)}
<h2>The queries</h2>{''.join(_card(d, bool(g)) for d in datasets)}
</main></body></html>
"""


def er_svg(spec: dict) -> str:
    """Tables, steps and the result as boxes with their columns (keys highlighted), left to right; each line
    numbered, with what happens on it listed under the picture."""
    w, head, row, gx, gy = 200, 24, 17, 56, 22
    pos, size = {}, {}
    heights: dict[tuple[int, int], int] = {}
    nodes = [(n + (0,))[:6] for n in spec["nodes"]]
    for nid, _title, _kind, cols, col, band in nodes:
        h = head + len(cols) * row + 6
        size[nid] = h
        pos[nid] = (col, band, heights.get((band, col), 0))
        heights[(band, col)] = heights.get((band, col), 0) + h + gy
    tops, y = {}, 0
    for band in sorted({b for *_r, b in nodes}):
        tops[band] = y
        y += max(v for (b, _c), v in heights.items() if b == band) + 26
    band_of = {nid: band for nid, (_c, band, _y) in pos.items()}
    pos = {nid: (col * (w + gx), tops[band] + yy) for nid, (col, band, yy) in pos.items()}
    width = (max(n[4] for n in nodes) + 1) * (w + gx) - gx
    height = y - 26 - gy
    out = [f'<svg class="flow er" viewBox="-2 -2 {width + 4} {height + 4}" width="{width + 4}" '
           f'height="{height + 4}" role="img">']
    notes, placed = [], []
    for i, (a, b, label) in enumerate(spec["edges"], 1):
        (x1, y1), (x2, y2) = pos[a], pos[b]
        if band_of[a] != band_of[b]:  # down to the next row of boxes, through the gap between the rows
            gap = tops[band_of[b]] - 13
            x1, x2, y1 = x1 + w / 2, x2 + w / 2, y1 + size[a]
            d, mx, my = f"M{x1},{y1} V{gap} H{x2} V{y2}", (x1 + x2) / 2, gap
        elif x1 == x2 and y2 > y1:  # same column: bottom to top
            x1 = x2 = x1 + w / 2
            y1, y2 = y1 + size[a], y2
            d, mx, my = f"M{x1},{y1} L{x2},{y2}", x1, (y1 + y2) / 2
        else:
            x1, y1, y2 = x1 + w, y1 + min(size[a], 60) / 2, y2 + min(size[b], 60) / 2
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            d = f"M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}"
        out.append(f'<path class="edge" d="{d}"/>')
        if label:
            while any(abs(mx - px) < 17 and abs(my - py) < 17 for px, py in placed):
                my += 17  # keep the numbers apart where lines meet
            placed.append((mx, my))
            out.append(f'<g class="mark"><circle cx="{mx}" cy="{my}" r="8"/>'
                       f'<text x="{mx}" y="{my + 4}" text-anchor="middle">{len(notes) + 1}</text></g>')
            names = {n[0]: n[1] for n in spec["nodes"]}
            notes.append(f"<b>{html.escape(names[a].split(':')[0].removeprefix('system.'))}</b> → "
                         f"<b>{html.escape(names[b].split(':')[0])}</b>: "
                         f"{html.escape(label)}")
    for nid, title, kind, cols, *_r in spec["nodes"]:
        x, y = pos[nid]
        t = title.removeprefix("system.")
        t = t if len(t) <= 30 else t[:29] + "…"
        out.append(f'<g class="ent {kind}"><title>{html.escape(title)}</title>'
                   f'<rect x="{x}" y="{y}" width="{w}" height="{size[nid]}" rx="5"/>'
                   f'<rect class="hd" x="{x}" y="{y}" width="{w}" height="{head}" rx="5"/>'
                   f'<text class="t" x="{x + 8}" y="{y + 16}">{html.escape(t)}</text>')
        for j, c in enumerate(cols):
            key, note = c.startswith("*"), c.startswith("~")
            c = c.lstrip("*~")
            c = c if len(c) <= 30 or note else c[:27] + "…"
            out.append(f'<text class="{"k" if key else "n" if note else "c"}" x="{x + 8}" y="{y + head + 13 + j * row}">'
                       f'{"◆ " if key else ""}{html.escape(c)}</text>')
        out.append("</g>")
    out.append("</svg>")
    legend = ("<p class='muted'>Grey: system tables. Blue border: a shared check (its steps are listed under "
              "Steps). White: steps of the query. Green: the rows the dashboard shows. ◆ marks the columns the "
              "joins use.</p>")
    return ("<div class='flow-wrap'>" + "".join(out) + "</div>" + legend +
            "<ol class='er-notes'>" + "".join(f"<li>{n}</li>" for n in notes) + "</ol>")


_LOOKUP_KEYS = {"ws": ["workspace_id"], "jb": ["workspace_id", "job_id"], "pl": ["workspace_id", "pipeline_id"],
                "cl": ["cluster_id"], "wh": ["warehouse_id"]}
_LOOKUP_TABLE = {"ws": "system.access.workspaces_latest", "jb": "system.lakeflow.jobs",
                 "pl": "system.lakeflow.pipelines", "cl": "system.compute.clusters",
                 "wh": "system.compute.warehouses"}
_LOOKUP_NAME = {"ws": "workspace_name", "jb": "name", "pl": "name", "cl": "cluster_name", "wh": "warehouse_name"}


def query_er(d) -> dict:
    """A query's ER: its own spec when it has one, else system tables -> the shared check -> name lookups ->
    the query's own steps -> the rows."""
    if d.key in guides.DETAIL_ER:
        return guides.DETAIL_ER[d.key]
    order, reads = _steps_graph(d)
    checks = dict(d.checks)
    keys = sorted({k for n in d.names for k in _LOOKUP_KEYS[n]}) or ["workspace_id"]
    nodes, edges = [], []
    for alias, check in d.checks:
        for t in sorted(t for t in reads.get(alias, ()) if t.startswith("system.")):
            if ("t:" + t) not in [n[0] for n in nodes]:
                nodes.append(("t:" + t, t, "table", [], 0))
            edges.append(("t:" + t, alias, ""))
        nodes.append((alias, check, "check", [f"*{k}" for k in keys] + ["(and its other columns)"], 1))
    for n in d.names:
        nodes.append((n, f"{n}: {_LOOKUP_TABLE[n]}", "table", [f"*{k}" for k in _LOOKUP_KEYS[n]] + [_LOOKUP_NAME[n]], 1))
    own = [n for n in order if n not in checks and n not in d.names and not n.startswith("system.")
           and n != "final SELECT"]
    for n in own:
        nodes.append((n, n, "step", ["~" + ln for ln in textwrap.wrap(d.steps.get(n, ""), 30)], 2))
        for r in sorted(reads.get(n, ())):
            if r in checks:
                edges.append((r, n, "the check's rows"))
            elif r in d.names:
                edges.append((r, n, "LEFT JOIN ON " + ", ".join(_LOOKUP_KEYS[r]) + ": names"))
            elif r in own:
                edges.append((r, n, ""))
    nodes.append(("out", "dashboard rows", "result", output_columns(d), 3))
    for r in sorted(reads.get("final SELECT", ())):
        if r in own or r in checks:
            edges.append((r, "out", ""))
        elif r in d.names:
            edges.append((r, "out", "LEFT JOIN ON " + ", ".join(_LOOKUP_KEYS[r]) + ": names"))
    if own:
        edges.append((own[-1], "out", "")) if (own[-1], "out", "") not in edges else None
    return {"nodes": nodes, "edges": edges}


def _page_lines(dash: dict) -> list[tuple[str, str]]:
    """(page, its header line) for each canvas page."""
    out = []
    for page in dash["pages"]:
        if page.get("pageType") == "PAGE_TYPE_GLOBAL_FILTERS":
            continue
        lines = [w["widget"]["multilineTextboxSpec"]["lines"] for w in page["layout"]
                 if "multilineTextboxSpec" in w["widget"]]
        line = lines[0][3].strip().strip("*") if lines and len(lines[0]) > 3 else ""
        out.append((page["displayName"], line))
    return out


def build_short(dash: dict, datasets: list, title: str, stem: str) -> str:
    """The two-minute version: purpose, how the tables join, the tables, how it works, the query, pages, rules."""
    from tools.direct_stories import STORIES

    g, short = guides.GUIDES[stem], guides.SHORT[stem]
    first = STORIES.get(stem, [{}])[0]
    picture = (timeline_svg(first["timeline"]) if first.get("timeline")
               else _svg(*first["flow"]) if first.get("flow") else "")
    rules = {n: r for n, r, _w in g["rules"]}
    rule_rows = "".join(f"<tr><td><b>{html.escape(n)}</b></td><td>{html.escape(rules[n])}</td></tr>"
                        for n in short["rules"])
    page_rows = "".join(f"<tr><td><b>{html.escape(p)}</b></td><td>{html.escape(l)}</td></tr>"
                        for p, l in _page_lines(dash))
    used = sorted({t for d in datasets for t in _tables(d.sql())})
    table_rows = "".join(f"<tr><td><code>{t}</code></td><td>{html.escape(SYSTEM_NOTES.get(t, ''))}</td></tr>"
                         for t in used)
    t = html.escape(title)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t} in brief</title><style>{CSS}{FLOW_CSS}</style></head>
<body><main>
<h1>{t}, in brief</h1>
<p class="muted">A two-minute read. Every query, step and column: <a href="{stem}.html">the detailed guide</a>.</p>
<h2>What it is for</h2><p>{html.escape(g["purpose"])}</p>
<h2>How the tables join</h2>
{er_svg(guides.ER[stem])}
<h2>Each query's joins</h2>
<p class="muted">Every query the dashboard runs, with the page it feeds.</p>
{"".join(f"<h3>{html.escape(d.title)}</h3><p class='muted'>Used by: {html.escape(d.used_by)}</p>{er_svg(query_er(d))}" for d in datasets)}
<h2>The system tables</h2>
<div class="table-wrap"><table><tr><th>System table</th><th>What it holds</th></tr>{table_rows}</table></div>
<h2>How it works</h2>
<ul>{"".join(f"<li>{html.escape(x)}</li>" for x in short["how"])}</ul>
{f"<p class='muted'>Example: {html.escape(first.get('title', '').split('. ', 1)[-1].lower())}</p>{picture}" if picture else ""}
<h2>The query, simplified</h2>
<p class="muted">The core of it; runs as is in a SQL editor. The dashboard's full queries, step by step:
<a href="{stem}.html#widgets">the detailed guide</a>.</p>
{_pre(short["sql"])}
<h2>The pages</h2>
<div class="table-wrap"><table><tr><th>Page</th><th>What it shows</th></tr>{page_rows}</table></div>
<h2>The rules</h2>
<div class="table-wrap"><table><tr><th>Term</th><th>Means</th></tr>{rule_rows}</table></div>
<h2>Good to know</h2>
<ul>{"".join(f"<li>{html.escape(x.replace('Check with the query below.', 'The detailed guide has a query to check it.'))}</li>" for x in g["limits"][:3])}</ul>
</main></body></html>
"""
