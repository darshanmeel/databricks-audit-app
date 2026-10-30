"""tools/direct_stories.py

"How it works, with an example" in the detailed HTML guides: what happens in Databricks (a job run, a
login, a GRANT, an admin change), which system table records each part, example rows, and how the
dashboard's query reads them. Made-up names and ids; tools/direct_dashboard_doc.py renders it.

A section: title, text (paragraphs), optional code, optional timeline {ticks: [(min, label)], lanes: [(label, [(from_min, to_min, text,
kind)])]}, optional flow (columns of boxes, edges, kinds), tables [(caption, columns, rows)], after (bullets).
"""
from __future__ import annotations

SP = "7f3c2a10-5b4e-4d2a-9c11-2e8f6a0b4d93"  # a service principal's application id

STORIES = {
    "jobs_health": [
        {
            "title": "1. One job run, minute by minute",
            "text": [
                "The job nightly_sales_load (id 1234) runs at 02:00 on a job cluster. It has two tasks: extract, "
                "then load. The load task fails; someone repairs the run, which starts a new cluster and re-runs only "
                "the failed task, and this time it succeeds.",
            ],
            "timeline": {"ticks": [(0, "02:00"), (30, "02:30"), (60, "03:00"), (90, "03:30"), (110, "03:50")], "lanes": [
                ("Run 4711 (the whole run)", [(0, 1, "queued", "queue"), (1, 75, "attempt 1", "failed"),
                                              (80, 110, "repair", "ok")]),
                ("Job cluster", [(1, 5, "starting", "setup"), (5, 75, "running", "run"),
                                 (80, 83, "starting", "setup"), (83, 110, "running", "run")]),
                ("Task extract", [(5, 40, "SUCCEEDED", "ok")]),
                ("Task load, attempt 1", [(40, 75, "FAILED", "failed")]),
                ("Task load, repair", [(83, 110, "SUCCEEDED", "ok")]),
            ]},
            "after": [
                "Queued: waiting for a free slot (the job's concurrency limit). Starting: the cluster's machines boot "
                "and install libraries; this is the cold start. Running: the tasks do their work.",
                "The run took 110 minutes in total, of which 7 were cluster starts and 5 were idle between the failure "
                "and the repair.",
            ],
        },
        {
            "title": "2. What each system table records about it",
            "text": [
                "Databricks writes the run into several system tables, each from its own angle. The rows below are "
                "simplified (fewer columns); the real tables have more.",
            ],
            "tables": [
                ("system.lakeflow.job_run_timeline: one row per run per hour; only an attempt's last row has a result",
                 ["job_id", "run_id", "period_start_time", "period_end_time", "result_state", "termination_code"],
                 [["1234", "4711", "02:00", "03:00", "", ""],
                  ["1234", "4711", "03:00", "03:15", "FAILED", "RUN_EXECUTION_ERROR"],
                  ["1234", "4711", "03:20", "03:50", "SUCCEEDED", "SUCCESS"]]),
                ("system.lakeflow.job_task_run_timeline: one row per task run, with its setup and run seconds",
                 ["task_key", "result_state", "setup_duration_seconds", "execution_duration_seconds", "compute_ids"],
                 [["extract", "SUCCEEDED", "240", "2100", "[0420-cluster-a]"],
                  ["load", "FAILED", "0", "2100", "[0420-cluster-a]"],
                  ["load", "SUCCEEDED", "180", "1620", "[0420-cluster-b]"]]),
                ("system.compute.node_timeline: one row per machine per minute, with CPU and memory",
                 ["cluster_id", "instance_id", "driver", "start_time", "cpu_user_percent", "mem_used_percent"],
                 [["0420-cluster-a", "i-01", "true", "02:06", "12", "35"],
                  ["0420-cluster-a", "i-02", "false", "02:06", "71", "62"],
                  ["...", "...", "...", "...", "...", "..."]]),
                ("system.billing.usage: DBUs per hour, tagged with the job and the run",
                 ["usage_start_time", "sku_name", "usage_quantity (DBUs)", "usage_metadata.job_id",
                  "usage_metadata.job_run_id"],
                 [["02:00", "JOBS_COMPUTE", "14.2", "1234", "4711"],
                  ["03:00", "JOBS_COMPUTE", "9.8", "1234", "4711"]]),
                ("system.lakeflow.jobs: the job's settings, a new row each time they change",
                 ["job_id", "name", "timeout_seconds", "run_as_user_name", "change_time"],
                 [["1234", "nightly_sales_load", "0", "alice@example.com", "2026-09-01"]]),
            ],
        },
        {
            "title": "3. How the dashboard reads it",
            "flow": ([["job_run_timeline: 3 rows"], ["last row per attempt"], ["one run, last attempt"],
                      ["4711: SUCCEEDED, 110 min"]],
                     [("job_run_timeline: 3 rows", "last row per attempt"),
                      ("last row per attempt", "one run, last attempt"),
                      ("one run, last attempt", "4711: SUCCEEDED, 110 min")],
                     {"job_run_timeline: 3 rows": "table", "4711: SUCCEEDED, 110 min": "final"}),
            "after": [
                "Failed jobs: run 4711 is not a failed run, because its last attempt succeeded. It is a repaired run, "
                "and the $ of the failed attempt (from billing.usage) counts in Cost of failed runs.",
                "Slow jobs: 110 minutes is compared with the job's usual (median) time. If it usually takes 60, this "
                "run is slow (over 1.5x and 5+ minutes longer).",
                "Cold starts: the setup seconds (240 and 180) feed the p95 cluster start.",
                "Hygiene: timeout_seconds is 0, so the job has no timeout; it runs as alice@example.com, a person, "
                "and it is scheduled: an ownership risk.",
                "Job size: node_timeline shows how busy the machines were; a worker at 15% CPU the whole run would "
                "make the job oversized.",
            ],
        },
    ],
    "workspace_usage": [
        {
            "title": "1. A login becomes an audit row",
            "text": ["alice@example.com signs in to the workspace analytics-prod in the browser, 30 times in October. A "
                     "deployment pipeline also signs in, as a service principal, with a token."],
            "flow": ([["alice signs in (browser)", "pipeline (token login)"], ["system.access.audit"],
                      ["counted once: alice"]],
                     [("alice signs in (browser)", "system.access.audit"),
                      ("pipeline (token login)", "system.access.audit"),
                      ("system.access.audit", "counted once: alice")],
                     {"system.access.audit": "table", "counted once: alice": "final"}),
            "tables": [
                ("system.access.audit (simplified)",
                 ["event_date", "workspace", "service_name", "action_name", "user_identity.email", "status_code"],
                 [["2026-10-01", "analytics-prod", "accounts", "aadBrowserLogin", "alice@example.com", "200"],
                  ["2026-10-02", "analytics-prod", "accounts", "aadBrowserLogin", "alice@example.com", "200"],
                  ["2026-10-02", "analytics-prod", "accounts", "tokenLogin", SP, "200"],
                  ["2026-10-03", "analytics-prod", "accounts", "aadBrowserLogin", "bob@example.com", "401"]]),
            ],
            "after": [
                "alice counts once for October however often she signs in.",
                "The pipeline is left out: its identity is an application id, not an email.",
                "bob's attempt failed (status 401), so it isn't a login.",
            ],
        },
        {
            "title": "2. When a workspace counts as inactive",
            "text": ["Every action leaves an audit row: logins, queries, job runs, changes. The query takes each "
                     "workspace's last audit row in the past year and counts the days since."],
            "timeline": {"ticks": [(0, "1 Jun"), (30, "1 Jul"), (61, "1 Aug"), (92, "1 Sep"), (122, "1 Oct"), (147, "26 Oct")], "lanes": [
                ("analytics-prod", [(0, 147, "events every day", "run")]),
                ("sandbox-old", [(0, 32, "events", "run"), (32, 147, "nothing since 2 Jul", "idle")]),
                ("test-2023", [(0, 147, "no events this year", "idle")]),
            ]},
            "tables": [
                ("What the dashboard shows (today 2026-10-26)",
                 ["workspace", "last activity", "days since", "inactive"],
                 [["test-2023", "none in 365 days", "", "yes"],
                  ["sandbox-old", "2026-07-02", "116", "yes"],
                  ["analytics-prod", "2026-10-25", "1", "no"]]),
            ],
        },
    ],
    "broad_grants": [
        {
            "title": "1. A GRANT becomes an audit row",
            "text": ["alice@example.com runs this in a SQL editor:"],
            "code": "GRANT ALL PRIVILEGES ON SCHEMA main.sales TO `account users`;\n"
                    "GRANT SELECT, MODIFY ON TABLE main.sales.orders TO `data-engineers`;",
            "flow": ([["GRANT in SQL, UI or API"], ["audit: updatePermissions"],
                      ["one row per privilege"], ["critical if broad"]],
                     [("GRANT in SQL, UI or API", "audit: updatePermissions"),
                      ("audit: updatePermissions", "one row per privilege"),
                      ("one row per privilege", "critical if broad")],
                     {"audit: updatePermissions": "table",
                      "critical if broad": "final"}),
            "tables": [
                ("system.access.audit: each GRANT is an updatePermissions event (request_params simplified)",
                 ["event_time", "action_name", "user_identity.email", "securable_type", "securable_full_name",
                  "changes"],
                 [["09:14", "updatePermissions", "alice@example.com", "schema", "main.sales",
                   '[{"principal":"account users","add":["ALL_PRIVILEGES"]}]'],
                  ["09:15", "updatePermissions", "alice@example.com", "table", "main.sales.orders",
                   '[{"principal":"data-engineers","add":["SELECT","MODIFY"]}]']]),
                ("What the dashboard shows",
                 ["object", "privilege", "grantee", "granted by", "actor type", "why critical"],
                 [["main.sales", "ALL_PRIVILEGES", "account users", "alice@example.com", "User",
                   "All privileges to account users"],
                  ["main.sales.orders", "SELECT", "data-engineers", "alice@example.com", "User", "Not broad"],
                  ["main.sales.orders", "MODIFY", "data-engineers", "alice@example.com", "User", "Not broad"]]),
            ],
            "after": [
                "The second GRANT gave two privileges, so it becomes two rows.",
                "ALL PRIVILEGES on a schema reaches every table in it, now and later; to account users it reaches "
                "everyone in the account.",
            ],
        },
    ],
    "admin_changes": [
        {
            "title": "1. The same change, by automation or by hand",
            "text": ["bob@example.com joins the group data-engineers twice: once through Terraform (run by a service "
                     "principal), once by alice clicking in the admin settings. Both land in the audit log; only the "
                     "actor differs."],
            "flow": ([["Terraform (SP)", "alice in the admin UI"], ["system.access.audit"],
                      ["actor type: SP or User"]],
                     [("Terraform (SP)", "system.access.audit"),
                      ("alice in the admin UI", "system.access.audit"),
                      ("system.access.audit", "actor type: SP or User")],
                     {"system.access.audit": "table", "actor type: SP or User": "final"}),
            "tables": [
                ("system.access.audit (request_params simplified)",
                 ["event_time", "service_name", "action_name", "user_identity.email", "targetUserName",
                  "targetGroupName"],
                 [["08:00", "accounts", "addPrincipalToGroup", SP, "bob@example.com", "data-engineers"],
                  ["14:32", "accounts", "addPrincipalToGroup", "alice@example.com", "bob@example.com",
                   "data-engineers"]]),
                ("What the dashboard shows",
                 ["when", "kind", "action", "object or principal", "done by", "actor type"],
                 [["14:32", "Group membership", "addPrincipalToGroup", "bob@example.com -> data-engineers",
                   "alice@example.com", "User"],
                  ["08:00", "Group membership", "addPrincipalToGroup", "bob@example.com -> data-engineers", SP,
                   "Service principal"]]),
            ],
            "after": [
                "The User row is the one to look at: a change made by hand, which Terraform may undo or "
                "which drifts from the code.",
                "Filter Actor type = User to see only manual changes, or pick one Actor to see everything one "
                "person did.",
            ],
        },
    ],
}

# ------------------------------------------------------------------------------------------------
# What each non-trivial step of a query brings, on the same example. (columns, rows) per step.
# ------------------------------------------------------------------------------------------------

_RUN_STEPS = {
    "slices": (["job_id", "run_id", "period_start_time", "period_end_time", "result_state"],
               [["1234", "4711", "02:00", "03:00", "(empty)"], ["1234", "4711", "03:00", "03:15", "FAILED"],
                ["1234", "4711", "03:20", "03:50", "SUCCEEDED"]]),
    "ends": (["job_id", "run_id", "run_end", "result_state", "termination_code"],
             [["1234", "4711", "03:50", "SUCCEEDED", "SUCCESS"]]),
    "starts": (["job_id", "run_id", "run_start"], [["1234", "4711", "02:00"]]),
    "runs": (["job_id", "run_id", "run_date", "minutes", "failed"],
             [["1234", "4711", "2026-10-20", "110", "0"], ["1234", "4690", "2026-10-19", "58", "0"],
              ["1234", "4655", "2026-10-17", "41", "1"]]),
    "usual": (["job_id", "usual_minutes"], [["1234", "60"]]),
    "judged": (["run_id", "minutes", "usual_minutes", "slow"],
               [["4711", "110", "60", "1  (110 > 1.5 x 60 and 50 min longer)"], ["4690", "58", "60", "0"],
                ["4655", "41", "60", "0  (failed runs are never slow)"]]),
}
_PER_JOB = {
    **_RUN_STEPS,
    "per_job": (["job_id", "runs", "failed_runs", "slow_runs", "days_failed", "median_minutes", "max_minutes",
                 "last_code"],
                [["1234", "30", "2", "3", "2", "61", "110", "RUN_EXECUTION_ERROR"]]),
    "named": (["job_id", "job_name", "workspace"], [["1234", "nightly_sales_load", "analytics-prod"]]),
    "ranked": (["job_name", "reliability", "fail_rank", "time_rank"],
               [["nightly_sales_load", "Some failures  (2 of 30 = 7%, under 20%)", "5", "2"]]),
}

STEP_EXAMPLES = {
    "job_days": {
        **_RUN_STEPS,
        "named": (["run_id", "job_name", "workspace"], [["4711", "nightly_sales_load", "analytics-prod"]]),
        "ranked": (["job_name", "job_failed", "rk"], [["nightly_sales_load", "2", "5"]]),
        "labelled": (["run_date", "job", "top_failing_job", "failed", "slow"],
                     [["2026-10-20", "nightly_sales_load (1234)", "nightly_sales_load · analytics-prod", "0", "1"]]),
    },
    "jobs": _PER_JOB,
    "top_failed_jobs": _PER_JOB,
    "monthly_active_users": {
        "ws_class": (["workspace_id", "workspace_name", "workspace_class"],
                     [["101", "analytics-prod", "Not classified"]]),
        "logins": (["workspace_id", "user_name"],
                   [["101", "alice@example.com"], ["101", SP]]),
    },
    "workspace_activity": {
        "seen": (["workspace_id", "last_activity"], [["101", "2026-10-25"], ["102", "2026-07-02"]]),
        "w": (["workspace_name", "last_activity", "days_since"],
              [["analytics-prod", "2026-10-25", "1"], ["sandbox-old", "2026-07-02", "116"],
               ["test-2023", "(none)", "(empty)"]]),
    },
    "grant_events": {
        "ev": (["event_time", "actor", "object_type", "object", "changes"],
               [["09:14", "alice@example.com", "SCHEMA", "main.sales",
                 '[{"principal":"account users","add":["ALL_PRIVILEGES"]}]'],
                ["09:15", "alice@example.com", "TABLE", "main.sales.orders",
                 '[{"principal":"data-engineers","add":["SELECT","MODIFY"]}]']]),
        "per_grantee": (["object", "c.principal", "c.add"],
                        [["main.sales", "account users", "[ALL_PRIVILEGES]"],
                         ["main.sales.orders", "data-engineers", "[SELECT, MODIFY]"]]),
        "granted": (["object", "grantee", "privilege"],
                    [["main.sales", "account users", "ALL_PRIVILEGES"],
                     ["main.sales.orders", "data-engineers", "SELECT"],
                     ["main.sales.orders", "data-engineers", "MODIFY"]]),
        "judged": (["object", "privilege", "grantee", "all_privileges", "to_account_users"],
                   [["main.sales", "ALL_PRIVILEGES", "account users", "true", "true"],
                    ["main.sales.orders", "SELECT", "data-engineers", "false", "false"]]),
    },
    "admin_changes": {
        "ev": (["event_time", "action_name", "category", "actor", "target"],
               [["14:32", "addPrincipalToGroup", "Group membership", "alice@example.com",
                 "bob@example.com -> data-engineers"],
                ["08:00", "addPrincipalToGroup", "Group membership", SP, "bob@example.com -> data-engineers"]]),
    },
}


STORIES["waste_savings"] = [
    {
        "title": "1. One warehouse's day",
        "text": [
            "The serverless warehouse bi-warehouse (auto-stop 30 minutes) starts at 08:55 for a dashboard. Queries "
            "come in bursts until 17:00; then nothing, and it stops at 17:30.",
        ],
        "timeline": {"ticks": [(0, "08:00"), (120, "10:00"), (240, "12:00"), (360, "14:00"), (480, "16:00"),
                               (600, "18:00")], "lanes": [
            ("Warehouse", [(55, 60, "starting", "setup"), (60, 570, "running (billed)", "run")]),
            ("Queries (busy)", [(60, 120, "busy", "ok"), (180, 200, "", "ok"), (300, 420, "busy", "ok"),
                                (500, 540, "busy", "ok")]),
            ("Idle between queries", [(120, 180, "idle", "idle"), (200, 300, "idle", "idle"),
                                      (420, 500, "idle", "idle")]),
            ("Waiting for auto-stop", [(540, 570, "30 min", "failed")]),
        ]},
        "after": [
            "Running 8.5 hours, busy 4 hours: 4.5 hours idle, billed all the same. 4 hours were between queries and "
            "30 minutes waiting for auto-stop after the last query.",
        ],
    },
    {
        "title": "2. What the system tables record",
        "tables": [
            ("system.compute.warehouse_events: each start, stop and scale",
             ["warehouse_id", "event_type", "event_time", "cluster_count"],
             [["a1b2c3", "STARTING", "08:55", "1"], ["a1b2c3", "RUNNING", "09:00", "1"],
              ["a1b2c3", "STOPPING", "17:30", "0"], ["a1b2c3", "STOPPED", "17:31", "0"]]),
            ("system.query.history: each query, when it ran",
             ["statement_id", "compute.warehouse_id", "start_time", "end_time", "execution_status"],
             [["q-001", "a1b2c3", "09:00", "09:04", "FINISHED"], ["q-002", "a1b2c3", "09:05", "10:00", "FINISHED"],
              ["...", "...", "...", "...", "..."], ["q-118", "a1b2c3", "16:20", "17:00", "FINISHED"]]),
            ("system.billing.usage: DBUs per hour for the warehouse",
             ["usage_start_time", "sku_name", "usage_quantity (DBUs)", "usage_metadata.warehouse_id"],
             [["09:00", "SERVERLESS_SQL_COMPUTE", "24.0", "a1b2c3"], ["...", "...", "...", "..."]]),
        ],
    },
    {
        "title": "3. How the dashboard reads it",
        "flow": ([["warehouse_events", "query.history"], ["running vs busy"], ["idle: 4.5 h, $"],
                  ["the fix + what-if"]],
                 [("warehouse_events", "running vs busy"), ("query.history", "running vs busy"),
                  ("running vs busy", "idle: 4.5 h, $"), ("idle: 4.5 h, $", "the fix + what-if")],
                 {"warehouse_events": "table", "query.history": "table", "the fix + what-if": "final"}),
        "after": [
            "Warehouses: idle 4.5 of 8.5 hours (53%), its idle $ from billing.usage. The biggest part is between "
            "queries, so the fix reads: run its work together so it runs in fewer bursts.",
            "Auto-stop what-if at 10 minutes: each gap over 10 minutes now stops the warehouse after 10, so about "
            "230 idle minutes fewer, and 3 extra cold starts (seconds on serverless).",
            "Possible waste: the idle $ counts if the warehouse is Critical or Warn and over $50 per 30 days.",
        ],
    },
]

STORIES["workspace_data"] = [
    {
        "title": "1. One workspace, from lineage to the dashboard",
        "text": ["Lineage shows workspace analytics-lab reads and writes catalog sales_lab most, so the 40 tables "
                 "and 12 views of sales_lab count for analytics-lab."],
        "flow": ([["lineage"], ["information_schema", "sizes job"], ["audit + query history"],
                  ["one row: analytics-lab"]],
                 [("lineage", "information_schema"), ("lineage", "sizes job"),
                  ("information_schema", "one row: analytics-lab"), ("sizes job", "one row: analytics-lab"),
                  ("audit + query history", "one row: analytics-lab")],
                 {"lineage": "table", "one row: analytics-lab": "final"}),
        "tables": [
            ("What the dashboard shows",
             ["workspace", "tables", "views", "GB", "active people", "queries", "complexity", "usage"],
             [["analytics-lab", "40", "12", "85.2", "6", "1,240", "71.0", "64.5"],
              ["hr-sandbox", "2", "0", "0.1", "0", "0", "8.4", "0.0"]]),
        ],
        "after": [
            "hr-sandbox: almost empty and nobody used it in the window; a candidate to retire.",
            "In analytics-lab, a 30 GB table with no read in 30 days shows as Written, never read.",
        ],
    },
]
