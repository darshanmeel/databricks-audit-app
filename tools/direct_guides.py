"""tools/direct_guides.py

The explanations in the HTML guides of the Jobs health and governance dashboards: Databricks basics
for a newcomer, the rules and thresholds with their reasons, limits and how to check them, why each
query is built the way it is, and every column it returns. tools/direct_dashboard_doc.py renders them.
"""
from __future__ import annotations

from tools import direct_governance as gov
from tools import direct_jobs_health as jh

# ------------------------------------------------------------------------------------------------
# Databricks basics, picked per dashboard
# ------------------------------------------------------------------------------------------------

BASICS = {
    "workspace": ("Workspace", "One Databricks environment (its own URL, users, jobs, clusters). An account "
                  "holds many workspaces; the dashboards read all of them at once."),
    "system_tables": ("System tables", "Read-only tables Databricks fills for the whole account under the "
                      "catalog system (system.billing, system.lakeflow, system.compute, system.access). The "
                      "dashboards read them live; there are no tables of our own."),
    "sql": ("Query, CTE", "A query is SQL. Each one here is a chain of named steps written WITH name AS (...), "
            "called CTEs; each step reads the ones before it, and one last SELECT gives the rows."),
    "job": ("Job, run, task", "A job is a scheduled or triggered workflow. Each time it starts that is a run; a "
            "run has one or more tasks (a notebook, a SQL file, a pipeline, ...). A failed run can be repaired: "
            "re-run in place, which adds an attempt to the same run."),
    "cluster": ("Cluster, driver, worker", "Classic compute: machines (nodes) Databricks starts for you. The "
                "driver coordinates, workers do the work. A job cluster lives for one run; an all-purpose "
                "cluster is shared and costs more per DBU. Serverless compute has no cluster you manage."),
    "cold_start": ("Cluster start (setup)", "Before a task can run on a classic cluster the machines must start "
                   "and install libraries: that wait is the setup time, also called a cold start."),
    "dbu": ("DBU and $", "Databricks bills in DBUs (Databricks Units). DBUs x the list price of the product (SKU) "
            "gives $ at list price; the invoice is usually lower after discounts."),
    "pipeline": ("Pipeline", "A Lakeflow Declarative Pipeline (formerly Delta Live Tables): tables kept up to date "
                 "by updates, either triggered or running continuously."),
    "median": ("Median, p95", "The median is the middle value: half the runs took less, half more; one very long "
               "run doesn't move it. p95: 95% of values are at or below it, so it shows the slow tail."),
    "audit": ("Audit log", "system.access.audit: one row per action someone took (log in, grant, change a group, "
              "...), with who did it, when, in which workspace, the request and whether it succeeded. Databricks "
              "keeps it for 365 days."),
    "uc": ("Unity Catalog, grant", "Unity Catalog governs data: catalogs hold schemas, schemas hold tables. A "
           "grant gives a principal a privilege on one of them (SELECT, MODIFY, ALL PRIVILEGES, ...); a grant on a "
           "catalog or schema reaches everything inside it."),
    "principal": ("User, group, service principal", "A user is a person (an email). A group bundles users. A "
                  "service principal is a non-human identity for automation (CI/CD, Terraform); in the audit log it "
                  "appears as its application id, a UUID like 1b2c3d4e-...."),
    "account_users": ("account users", "A built-in group holding every user of the account. Granting it "
                      "something gives that to everyone."),
}

# ------------------------------------------------------------------------------------------------
# Column meanings: shared ones, then per query
# ------------------------------------------------------------------------------------------------

COMMON = {
    "job": "The job's name and id, as name (id). One-off submitted runs have no name and show the id.",
    "workspace": "The workspace's name (its id when the name is unknown).",
    "status": "CRITICAL, WARN, OK or NOT_ASSESSED (not enough data to judge), from the rule in Rules.",
    "flagged": "1 when status is CRITICAL or WARN, else 0; tiles add it up to count flagged rows.",
    "jobs": "Always 1: charts add it up to count jobs.",
}

COLUMNS = {
    "job_days": {
        "run_date": "The day the run started.",
        "top_failing_job": "The job's name for the 10 jobs with the most failed runs in the window; All others for the rest.",
        "runs": "Runs that ended that day (one per run, repairs not counted twice).",
        "failed_runs": "Runs whose last attempt ended FAILED, ERROR or TIMED_OUT.",
        "slow_runs": "Successful runs that were slow by the slow-run rule.",
    },
    "jobs": {
        "reliability": "Always fails, Flaky, Some failures or No failures (see Rules).",
        "runs": "Runs in the window (one per run).",
        "failed_runs": "Runs whose last attempt failed, errored or timed out.",
        "failure_pct": "failed_runs / runs x 100.",
        "days_failed": "Days with at least one failed run.",
        "days_slow": "Days with at least one slow run.",
        "slow_runs": "Successful runs that took over 1.5x the job's usual time and 5+ minutes longer.",
        "median_minutes": "The middle run time of all its runs, in minutes.",
        "avg_minutes": "The average run time, in minutes.",
        "max_minutes": "The longest run, in minutes.",
        "total_hours": "All its run time added up, in hours.",
        "last_failure": "The error code of its latest failed run (empty when none failed).",
        "failing": "1 when the job had any failed run.",
        "flaky": "1 when reliability is Flaky.",
        "always_fails": "1 when reliability is Always fails.",
        "top_by_time": "The job's name for the 10 jobs with the most run time; All others for the rest.",
    },
    "failed_jobs": {
        "top_job": "The job's name for the 10 with the most wasted $; All others for the rest.",
        "runs": "Distinct runs in the window.",
        "failed_runs": "Runs that failed.",
        "repaired_runs": "Runs that were repaired (re-run after a failure).",
        "failure_rate_pct": "failed_runs / runs x 100.",
        "wasted_usd": "List-price $ of the failed runs and the failed attempts of repaired runs.",
        "last_failure": "The error code of the latest failed run.",
    },
    "slower_than_usual": {
        "recent_runs": "Successful runs in the recent days (the last up to 7 days of the window).",
        "baseline_runs": "Successful runs in the rest of the window.",
        "recent_median_minutes": "Median run time of the recent runs, in minutes.",
        "baseline_median_minutes": "Median run time of the earlier runs, in minutes.",
        "slowdown_ratio": "recent median / earlier median; 1.5 means 50% slower.",
        "what_grew": "Which part grew more: Waiting or cluster start (capacity), or Run time (code or data).",
    },
    "cold_starts": {
        "runs": "Runs in the window.",
        "setup_p95_s": "p95 of the cluster start time per run, in seconds.",
        "setup_hours": "All setup time added up, in hours.",
        "queue_p95_s": "p95 of the time runs waited in the queue before starting, in seconds.",
        "top_job": "The job's name for the 10 slowest p95 starts.",
    },
    "failed_cluster_starts": {
        "error_code": "The termination code Databricks recorded (for example CLOUD_FAILURE, DRIVER_UNREACHABLE).",
        "failed_starts": "Task runs that failed in setup and never ran.",
        "setup_minutes": "Minutes spent in those failed setups.",
        "last_failed": "When the last one ended.",
    },
    "jobs_no_timeout": {
        "duration_warning": "yes when the job has a run-time warning (a health rule); it only notifies, it never stops a run.",
        "spend_usd": "The job's list-price $ in the window.",
    },
    "tasks_no_timeout": {
        "task": "The task's key inside the job.",
        "job_spend_usd": "The job's list-price $ in the window.",
    },
    "ownership_risk": {
        "risk": "Scheduled, runs as a person; Runs as a person; or Owner or run-as missing.",
        "runs_as": "The identity the job runs as.",
        "created_by": "Who created the job.",
        "runs_as_person": "1 when it runs as a person (an email), not a service principal.",
    },
    "oversized_jobs": {
        "node_type": "The worker machine type.",
        "max_workers": "Workers configured (autoscale maximum).",
        "workers_used": "The most workers that ran at once.",
        "cpu_p90_pct": "Worker CPU p90 over its runs, %.",
        "memory_peak_pct": "Worker memory at its highest, %.",
        "spend_usd": "The job's list-price $ on those clusters.",
        "saving_usd": "The estimated saving (see Rules).",
        "suggested_action": "fewer workers / lower autoscale max, or one node size down.",
    },
    "compute_pressure": {
        "pressure": "What limits the job: Memory: too little, CPU, Skew, I/O wait, Driver, Idle: too big, Balanced, Not judged.",
        "hint": "The change to try for that pressure.",
        "why": "The numbers behind it, with the threshold crossed.",
        "task_hours": "Task run time added up, in hours.",
        "node_type": "The worker machine type.",
        "memory_p90_pct": "Worker memory p90, %.",
        "cpu_p90_pct": "Worker CPU p90, %.",
        "memory_bound": "1 when the pressure is memory.",
    },
    "pipeline_cost": {
        "pipeline": "The pipeline's name and id.",
        "pipelines": "Always 1: tiles add it up to count pipelines.",
        "mode": "Continuous or Triggered.",
        "top_pipeline": "The pipeline's name for the 10 costliest; All others for the rest.",
        "spend_usd": "List-price $ in the window.",
        "dbus": "DBUs billed.",
        "updates": "Updates that ended in the window.",
        "failed_updates": "Updates that failed or were canceled.",
        "nothing_delivered": "yes when no update completed in the window although it was billed.",
    },
    "monthly_active_users": {
        "workspace_class": "The workspace's class from its name pattern, or Not classified.",
        "user_name": "The person's email, lower case. Widgets count it distinct.",
    },
    "workspace_activity": {
        "workspace_id": "The workspace's id.",
        "workspace_class": "The workspace's class from its name pattern, or Not classified.",
        "workspaces": "Always 1: tiles add it up to count workspaces.",
        "last_activity": "The last day with any audit event, or none in 365 days.",
        "days_since": "Days from that day to today (empty when there is none).",
        "inactive": "1 when days_since is 90+ or there is no event.",
        "no_events": "1 when there is no audit event in 365 days.",
    },
    "grant_events": {
        "granted_at": "When the grant happened (UTC).",
        "day": "The day of it, for the chart.",
        "object_type": "CATALOG, SCHEMA, TABLE, ...",
        "object": "The full name, for example main.sales.orders.",
        "privilege": "The privilege granted, for example SELECT or ALL_PRIVILEGES.",
        "grantee": "Who received it: a user, group (account users included) or service principal.",
        "actor": "Who made the grant.",
        "actor_type": "Service principal (an application id), User (an email), or Other (system).",
        "grants": "Always 1: tiles add it up to count grants.",
        "why": "All privileges, To account users, both, or Not broad.",
        "critical": "1 when the grant is broad (see Rules).",
    },
    "admin_changes": {
        "changed_at": "When the action happened (UTC).",
        "day": "The day of it, for the chart.",
        "category": "Group membership, Groups, Admin role, Workspace access, Grants (Unity Catalog), "
                    "Permissions (ACL), Cluster policies or Settings.",
        "action": "The audit log's action name, for example addPrincipalToGroup.",
        "target": "What the action touched: the object, or user -> group; - when the event doesn't say.",
        "change": "For grants: the change itself (who got or lost what), first 300 characters.",
        "actor": "Who did it.",
        "actor_type": "Service principal (an application id), User (an email), or Other (system).",
        "changes_count": "Always 1: tiles add it up to count changes.",
        "outcome": "Done (accepted), Failed (refused), or Unknown.",
        "by_user": "1 when a person did it.",
    },
}

# ------------------------------------------------------------------------------------------------
# Why each query is built the way it is
# ------------------------------------------------------------------------------------------------

WHY = {
    "job_days": "Counting rows in job_run_timeline would count a long run several times (it writes one row per "
                "hour) and a repaired run once per attempt. So the query keeps one row per run, takes the result "
                "of its last attempt, and only then counts per day. Slow runs are judged against the job's own "
                "usual time, because 20 minutes is normal for one job and alarming for another.",
    "jobs": "One row per job lets every tile, chart and table on two pages read the same numbers, so they always "
            "agree. Reliability needs a minimum number of runs: one failure in two runs is not a pattern.",
    "top_failed_jobs": "The same per-job numbers as the jobs query, cut to the 20 jobs with the most failed runs, "
                       "so the details table is short and ordered by what needs attention.",
    "failed_jobs": "Failures cost money: the compute of a failed run is paid but delivers nothing. This puts a $ on "
                   "it from billing, at list price.",
    "slower_than_usual": "A job that got slower is often a sign of growing data or a code change. Comparing the "
                         "last days with the rest of the window, on medians, catches a trend rather than one bad run.",
    "cold_starts": "Cluster start time is paid compute that does no work and delays every run. p95 shows the bad "
                   "starts that averages hide.",
    "failed_cluster_starts": "When a cluster can't start (no capacity, network, permissions) the run fails before "
                             "any code ran; the error code points at the platform, not the job.",
    "jobs_no_timeout": "Without a timeout a hung run keeps its cluster, and the bill, running until someone notices.",
    "tasks_no_timeout": "A task can hang even when the job has other tasks with timeouts; the task's own timeout "
                        "or the job's must stop it.",
    "ownership_risk": "A job that runs as a person stops working when that person leaves, and it runs with their "
                      "rights. Scheduled jobs should run as a service principal.",
    "oversized_jobs": "Paying for workers that sit idle is the easiest saving: the job keeps working on smaller "
                      "or fewer machines.",
    "compute_pressure": "Each limit has a different fix: more memory, more workers, fixing skewed data, moving work "
                        "off the driver. The query reads per-minute machine use (node_timeline) to tell them apart.",
    "pipeline_cost": "Pipelines bill continuously or per update; spend per pipeline shows where to look first, and "
                     "nothing delivered marks spend with no completed update.",
    "monthly_active_users": "A login in the audit log is the clearest sign someone used a workspace. Counting "
                            "distinct people (not logins) shows reach; service principals are left out because they "
                            "are automation, not users.",
    "workspace_activity": "A workspace with no audit event at all for 90 days has no users and no jobs: a candidate "
                          "to retire. Starting from the list of all workspaces makes ones with no events show up too.",
    "grant_events": "information_schema shows today's grants but not when they were made or by whom. The audit "
                    "log records each GRANT as it happens, with the actor, so a broad grant can be caught and "
                    "traced.",
    "admin_changes": "In a platform run by Terraform or CI/CD, changes come from service principals. A person "
                     "changing groups, grants or settings by hand is drift; the actor type makes that visible.",
}

# ------------------------------------------------------------------------------------------------
# Per dashboard: purpose, basics, rules, limits, checks
# ------------------------------------------------------------------------------------------------

_LATENCY = ("System tables are not real time: job and billing rows can arrive hours late, so today and yesterday "
            "read low.")
_REGION = ("Some system tables only hold workspaces in the same cloud region as the metastore; if a workspace is "
           "missing, check its region.")

GUIDES = {
    "jobs_health": {
        "purpose": "Shows which jobs fail, fail now and then (flaky), run slower than usual or wait long for their "
                   "cluster; which have no timeout or run as a person; which use the wrong size of compute; and "
                   "what each pipeline costs. Each page answers one of those questions.",
        "basics": ["workspace", "system_tables", "job", "cluster", "cold_start", "dbu", "pipeline", "median", "sql"],
        "rules": [
            ("Failed run", "Its last attempt ended FAILED, ERROR or TIMED_OUT.",
             "A repaired run that finally succeeded is not a failure."),
            ("Always fails", "3+ runs and every one failed.", "Broken, not flaky: fix it or switch it off."),
            ("Flaky", f"{jh.FLAKY_MIN_RUNS}+ runs and {jh.FLAKY_FAILED_PCT}%+ of them failed, but not all.",
             "Fails often enough to matter, with enough runs to be sure."),
            ("Usual run time", f"The median of the job's successful runs, once it has {jh.USUAL_MIN_RUNS}+.",
             "The median ignores the odd very long or short run."),
            ("Slow run", f"Succeeded, over {jh.SLOW_FACTOR}x the usual time and {jh.SLOW_EXTRA_MIN}+ minutes longer.",
             "The extra minutes stop 2-minute jobs being called slow over seconds."),
            ("Slower than usual", "Median of the last days (up to 7, at most half the window) vs the rest: 1.5x is "
             "Warn, 2x Critical; needs 3+ runs on each side.", "A lasting slowdown, not one bad run."),
            ("Slow cold start", "p95 cluster start 60 s+ is Warn, 300 s+ Critical.",
             "A minute of start time on every run adds up."),
            ("Failed cluster start", "A task run that failed in setup with no run time.",
             "The cluster never came up; the code never ran."),
            ("No timeout", "Timeout empty or 0. Warn; Critical when the job spent $200+ in the window.",
             "The cost decides how urgent it is."),
            ("Ownership risk", "Runs as a person and is scheduled: Critical. Runs as a person, or creator or run-as "
             "missing: Warn.", "Scheduled work should not depend on one person."),
            ("Oversized", "Worker CPU p90 under 30%, memory peak under 40%, no swap, over 3+ measured runs. Saving: "
             "half the $ when under half those limits, else a quarter; $25+ Warn, $200+ Critical.",
             "Peak memory, not average, so a smaller node still holds the busiest minute."),
            ("Compute pressure", "Memory p90 85%+ or swap 10%+; skew 40%+ CPU gap between workers; CPU p50 70%+; "
             "I/O wait 20%+; driver busy 70%+ while workers idle; idle under 20% CPU.",
             "Checked in that order: the first match is the pressure."),
            ("Pipeline status", "2,000+ DBUs and nothing delivered: Critical; 2,000+ DBUs, or 500+ and nothing "
             "delivered: Warn.", "Spend with no completed update is the waste signal."),
        ],
        "limits": [
            _LATENCY, _REGION,
            "Only runs that started in the Time Window count; a run that started before it is cut at its start.",
            "Job size and compute pressure need node telemetry (system.compute.node_timeline): jobs on serverless "
            "or SQL warehouses read Not judged.",
            "$ is list price from system.billing.list_prices, not the invoice.",
            "A query used on two pages is tagged with the first page's tab in system.query.history.",
        ],
        "checks": [
            ("A job's runs match the Jobs UI",
             "SELECT run_id, result_state, period_start_time, period_end_time\n"
             "FROM system.lakeflow.job_run_timeline\n"
             "WHERE job_id = '<job id>' AND result_state IS NOT NULL\n"
             "ORDER BY period_end_time DESC LIMIT 20;"),
            ("Node telemetry exists for a job's cluster",
             "SELECT cluster_id, COUNT(*) AS minutes\nFROM system.compute.node_timeline\n"
             "WHERE start_time >= current_date() - 7\nGROUP BY cluster_id ORDER BY minutes DESC LIMIT 20;"),
        ],
    },
    "workspace_usage": {
        "purpose": "Shows how many people log in per workspace class and per workspace in a month, and which "
                   "workspaces have had no activity for 90 days or more, as candidates to retire.",
        "basics": ["workspace", "system_tables", "audit", "principal", "sql"],
        "rules": [
            ("Active user", "A person (an email) with at least one successful login (accounts service, a login "
             "action, status 200) in the picked month.", "People, not logins; service principals left out."),
            ("Month", "Any day in the month picks the whole month; default this month.",
             "Monthly active users is a month figure."),
            ("Class", "The first LIKE pattern the lower-case workspace name matches; else Not classified.",
             "Set with --workspace-class when the dashboard is built."),
            ("Inactive", f"No audit event for {gov.INACTIVE_DAYS}+ days, or none in the last {gov.LOOKBACK_DAYS} days.",
             "Any event counts, scheduled jobs too: a workspace that only runs jobs is in use."),
        ],
        "limits": [
            _REGION,
            "The audit log keeps 365 days, so last activity can't be older than that.",
            "Account-console logins (workspace 0) are left out: they are not a workspace.",
            "Reading a year of audit log for last activity is the slowest part.",
        ],
        "checks": [
            ("Which login actions your account records",
             "SELECT action_name, COUNT(*)\nFROM system.access.audit\n"
             "WHERE service_name = 'accounts' AND action_name RLIKE '(?i)login'\n"
             "  AND event_date >= current_date() - 30\nGROUP BY action_name;"),
        ],
    },
    "broad_grants": {
        "purpose": "Lists every privilege granted in Unity Catalog, with who granted it and when, and flags the broad "
                   "ones: ALL PRIVILEGES on a catalog, schema or table, and anything granted to account users.",
        "basics": ["system_tables", "audit", "uc", "principal", "account_users", "sql"],
        "rules": [
            ("Grant", "A successful updatePermissions event in Unity Catalog; one row per object, grantee and privilege "
             "added.", "One GRANT statement can give several privileges to several principals."),
            ("Critical", "ALL_PRIVILEGES on a CATALOG, SCHEMA or TABLE, or any privilege to account users.",
             "Both break least privilege: everything to someone, or something to everyone."),
            ("Actor type", "Service principal when the actor is a UUID (application id), User when it is an email, "
             "else Other (system).", "Automation uses service principals; people use emails."),
        ],
        "limits": [
            "Only grants still in the audit log (365 days).",
            "Revokes are left out; Admin changes shows them.",
            "The object and grantee come from the event's request_params (securable_type, securable_full_name, "
            "changes); if Databricks changes that layout the table goes empty. Check with the query below.",
            _REGION,
        ],
        "checks": [
            ("What a grant event looks like",
             "SELECT event_time, user_identity.email, request_params\nFROM system.access.audit\n"
             "WHERE service_name = 'unityCatalog' AND action_name = 'updatePermissions'\n"
             "  AND event_date >= current_date() - 30\nLIMIT 20;"),
        ],
    },
    "admin_changes": {
        "purpose": "Lists admin actions (group membership, groups, admin roles, workspace access, grants and revokes, "
                   "permissions, cluster policies, settings) with who did them, and whether that was a person or a "
                   "service principal, to spot manual changes outside automation.",
        "basics": ["workspace", "system_tables", "audit", "uc", "principal", "sql"],
        "rules": [
            ("Admin action", "One of the service and action names in the list below the Rules.",
             "Reads and logins are not changes and are left out."),
            ("Actor type", "Service principal when the actor is a UUID (application id), User when it is an email, "
             "else Other (system).", "Changes by a User are manual."),
            ("Outcome", "Done when the response status is 200, Failed for another status, Unknown when none.",
             "A refused attempt is still worth seeing."),
        ],
        "limits": [
            "Only actions still in the audit log (365 days).",
            "The action list is from Databricks' audit log reference; an action it doesn't name is not shown.",
            "Object or principal reads request_params keys that differ per action; - when none of them is set.",
            "Account-level actions show workspace Account.",
            _REGION,
        ],
        "checks": [
            ("Which admin actions happened, and their parameters",
             "SELECT service_name, action_name, request_params\nFROM system.access.audit\n"
             "WHERE action_name IN ('addPrincipalToGroup', 'updatePermissions', 'setAdmin')\n"
             "  AND event_date >= current_date() - 30\nLIMIT 20;"),
        ],
        "actions": True,
    },
}


COLUMNS["top_failed_jobs"] = COLUMNS["jobs"]  # the same per-job columns, cut to the top 20


def columns(key: str) -> dict:
    return {**COMMON, **COLUMNS.get(key, {})}

# ------------------------------------------------------------------------------------------------
# The two-minute summaries: how it works in three lines, and the rules that matter
# ------------------------------------------------------------------------------------------------

SHORT = {
    "jobs_health": {
        "how": [
            "Every job run leaves rows in the system tables: its result per attempt (job_run_timeline), each "
            "task's setup and run time (job_task_run_timeline), machine use per minute (node_timeline) and its "
            "DBUs (billing.usage).",
            "The dashboard keeps one row per run and judges its last attempt, so a repaired run that succeeded is "
            "not a failure, but the failed attempt's cost still counts.",
            "Each job is compared with itself: slow means slow for that job, not slower than other jobs.",
        ],
        "rules": ["Failed run", "Flaky", "Always fails", "Slow run", "Slower than usual", "Slow cold start",
                  "No timeout", "Ownership risk", "Oversized"],
    },
    "workspace_usage": {
        "how": [
            "Every login and every action in a workspace writes a row to system.access.audit.",
            "Active users counts each person (an email) with a successful login in the month, once; service "
            "principals are left out.",
            "A workspace with no audit row at all for 90 days, people or jobs, is inactive.",
        ],
        "rules": ["Active user", "Class", "Inactive"],
    },
    "broad_grants": {
        "how": [
            "Every GRANT, from SQL, the UI or the API, writes an updatePermissions row to system.access.audit with "
            "the object, the grantee and the privileges.",
            "The dashboard splits it into one row per privilege and flags the broad ones.",
            "It shows who granted it, a person or a service principal, and when.",
        ],
        "rules": ["Critical", "Actor type"],
    },
    "admin_changes": {
        "how": [
            "Group, role, access, grant, permission, policy and setting changes each write a row to "
            "system.access.audit, with who made them.",
            "A service principal (automation, Terraform) appears as an application id; a person as an email.",
            "Rows by a User are changes made by hand: the ones to review.",
        ],
        "rules": ["Admin action", "Actor type", "Outcome"],
    },
}

# The core of each dashboard in a few lines of SQL: simplified (no names, no edge cases), runs as is.
SHORT["jobs_health"]["sql"] = """-- One row per run, judged on its last attempt; then per job: runs, failed runs, slow runs.
WITH runs AS (
  SELECT job_id, run_id,
         MAX_BY(result_state, period_end_time) AS result_state,   -- the last row carries the result
         timestampdiff(MINUTE, MIN(period_start_time), MAX(period_end_time)) AS minutes
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= current_date() - 30
  GROUP BY job_id, run_id
),
usual AS (  -- the job's usual time: the median of its successful runs
  SELECT job_id, percentile(minutes, 0.5) AS usual_minutes
  FROM runs WHERE result_state = 'SUCCEEDED'
  GROUP BY job_id HAVING COUNT(*) >= 5
)
SELECT r.job_id,
       COUNT(*) AS runs,
       SUM(CASE WHEN result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END) AS failed_runs,
       SUM(CASE WHEN result_state = 'SUCCEEDED' AND minutes > 1.5 * usual_minutes
                 AND minutes - usual_minutes >= 5 THEN 1 ELSE 0 END) AS slow_runs
FROM runs r LEFT JOIN usual u ON u.job_id = r.job_id
GROUP BY r.job_id
ORDER BY failed_runs DESC;"""

SHORT["workspace_usage"]["sql"] = """-- Active users this month, per workspace: people with a successful login.
SELECT ws.workspace_name, COUNT(DISTINCT lower(a.user_identity.email)) AS active_users
FROM system.access.audit a
JOIN system.access.workspaces_latest ws ON ws.workspace_id = a.workspace_id
WHERE a.service_name = 'accounts' AND a.action_name RLIKE '(?i)login'
  AND a.response.status_code = 200
  AND a.user_identity.email LIKE '%@%'                 -- people, not service principals
  AND a.event_date >= date_trunc('MONTH', current_date())
GROUP BY ws.workspace_name;

-- Inactive: every workspace whose last audit event is 90+ days ago, or that has none.
SELECT ws.workspace_name, MAX(a.event_date) AS last_activity
FROM system.access.workspaces_latest ws
LEFT JOIN system.access.audit a
  ON a.workspace_id = ws.workspace_id AND a.event_date >= current_date() - 365
GROUP BY ws.workspace_name
HAVING MAX(a.event_date) IS NULL OR MAX(a.event_date) < current_date() - 90;"""

SHORT["broad_grants"]["sql"] = """-- Every privilege granted, one row each; broad ones flagged.
SELECT a.event_time, a.user_identity.email AS granted_by,
       a.request_params['securable_full_name'] AS object,
       g.principal AS grantee, p AS privilege,
       p = 'ALL_PRIVILEGES' OR lower(g.principal) = 'account users' AS critical
FROM system.access.audit a
LATERAL VIEW explode(from_json(a.request_params['changes'],
                               'ARRAY<STRUCT<principal: STRING, add: ARRAY<STRING>>>')) x AS g
LATERAL VIEW explode(g.add) y AS p
WHERE a.service_name = 'unityCatalog' AND a.action_name = 'updatePermissions'
  AND a.response.status_code = 200
  AND a.event_date >= current_date() - 30
ORDER BY critical DESC, a.event_time DESC;"""

SHORT["admin_changes"]["sql"] = """-- Admin actions and who did them: a service principal (an application id) or a person (an email).
SELECT event_time, workspace_id, action_name, user_identity.email AS actor,
       CASE WHEN user_identity.email RLIKE '^[0-9a-fA-F-]{36}$' THEN 'Service principal'
            WHEN user_identity.email LIKE '%@%' THEN 'User' ELSE 'Other' END AS actor_type
FROM system.access.audit
WHERE action_name IN ('addPrincipalToGroup', 'removePrincipalFromGroup', 'setAdmin', 'removeAdmin',
                      'updatePermissions', 'changeWorkspaceAcl', 'workspaceConfEdit')   -- part of the list
  AND event_date >= current_date() - 30
ORDER BY event_time DESC;"""

# ------------------------------------------------------------------------------------------------
# How the tables join: an ER-style picture per dashboard. A node: (id, title, kind, columns, column
# index); kind is table (a system table), step (a CTE that filters or aggregates) or result. Key
# columns start with *. An edge: (from, to, what happens on the way).
# ------------------------------------------------------------------------------------------------

def _ws(col: int) -> tuple:
    return ("ws", "system.access.workspaces_latest", "table", ["*workspace_id", "workspace_name"], col)

ER = {
    "jobs_health": {
        "nodes": [
            ("jrt", "system.lakeflow.job_run_timeline", "table",
             ["*workspace_id", "*job_id", "*run_id", "period_start_time", "period_end_time", "result_state"], 0),
            ("runs", "runs: one row per run", "step",
             ["*job_id", "*run_id", "result_state (last attempt)", "minutes"], 1),
            ("usual", "usual: one row per job", "step", ["*job_id", "usual_minutes (median)"], 1),
            ("per_job", "per job", "step", ["*job_id", "runs", "failed_runs", "slow_runs"], 2),
            ("jobs", "system.lakeflow.jobs", "table", ["*workspace_id", "*job_id", "name", "change_time"], 2),
            _ws(2),
            ("out", "dashboard rows", "result",
             ["job (name)", "workspace (name)", "runs", "failed_runs", "failure_pct", "slow_runs"], 3),
        ],
        "edges": [
            ("jrt", "runs", "GROUP BY job_id, run_id"),
            ("runs", "usual", "successful runs: median"),
            ("runs", "per_job", "GROUP BY job_id"),
            ("usual", "per_job", "JOIN ON job_id"),
            ("per_job", "out", ""),
            ("jobs", "out", "JOIN ON job_id (latest row)"),
            ("ws", "out", "JOIN ON workspace_id"),
        ],
    },
    "workspace_usage": {
        "nodes": [
            ("audit", "system.access.audit", "table",
             ["*workspace_id", "event_date", "service_name", "action_name", "user_identity.email",
              "response.status_code"], 0),
            ("logins", "logins: one row per workspace and person", "step", ["*workspace_id", "user_name"], 1),
            ("seen", "seen: one row per workspace", "step", ["*workspace_id", "last_activity"], 1),
            _ws(1),
            ("mau", "active users", "result", ["workspace", "active_users"], 2),
            ("inactive", "inactive workspaces", "result", ["workspace", "last_activity", "days_since"], 2),
        ],
        "edges": [
            ("audit", "logins", "logins, status 200, emails"),
            ("audit", "seen", "MAX(event_date)"),
            ("logins", "mau", "COUNT(DISTINCT user)"),
            ("ws", "mau", "JOIN ON workspace_id"),
            ("seen", "inactive", "90+ days or none"),
            ("ws", "inactive", "LEFT JOIN: all workspaces"),
        ],
    },
    "broad_grants": {
        "nodes": [
            ("audit", "system.access.audit", "table",
             ["*workspace_id", "event_time", "action_name", "user_identity.email", "request_params"], 0),
            ("ev", "ev: one row per GRANT event", "step", ["*workspace_id", "actor", "object", "changes (JSON)"], 1),
            ("granted", "granted: one row per privilege", "step",
             ["*workspace_id", "object", "grantee", "privilege"], 2),
            _ws(2),
            ("out", "dashboard rows", "result", ["object", "privilege", "grantee", "actor", "actor_type",
                                                  "critical"], 3),
        ],
        "edges": [
            ("audit", "ev", "updatePermissions, status 200"),
            ("ev", "granted", "explode changes, then add"),
            ("granted", "out", "flag broad"),
            ("ws", "out", "JOIN ON workspace_id"),
        ],
    },
    "admin_changes": {
        "nodes": [
            ("audit", "system.access.audit", "table",
             ["*service_name", "*action_name", "workspace_id", "event_time", "user_identity.email",
              "request_params"], 0),
            ("actions", "actions (a list in the query)", "table", ["*service_name", "*action_name", "category"], 0),
            ("ev", "ev: one row per admin action", "step",
             ["*workspace_id", "action", "category", "actor", "target"], 1),
            _ws(1),
            ("out", "dashboard rows", "result", ["workspace", "action", "target", "actor", "actor_type",
                                                  "outcome"], 2),
        ],
        "edges": [
            ("audit", "ev", "JOIN ON service, action"),
            ("actions", "ev", "JOIN ON service, action"),
            ("ev", "out", "actor type from the actor"),
            ("ws", "out", "JOIN ON workspace_id"),
        ],
    },
}

# ------------------------------------------------------------------------------------------------
# Per query, in the detailed guide: every step with its columns. A node may carry a 6th value, the
# band (0 = top row of boxes, 1 = the row under it). Queries on a shared check are drawn automatically.
# ------------------------------------------------------------------------------------------------

_K3 = ["*workspace_id", "*job_id", "*run_id"]
_RUN_NODES = [
    ("jrt", "system.lakeflow.job_run_timeline", "table",
     _K3 + ["period_start_time", "period_end_time", "result_state", "termination_code", "run_duration_seconds"], 0),
    ("ends", "ends: the last row of a run", "step", _K3 + ["run_end", "result_state", "termination_code",
                                                            "reported_s"], 1),
    ("starts", "starts: the first row of a run", "step", _K3 + ["run_start"], 1),
    ("runs", "runs: one row per run", "step", _K3 + ["run_date", "minutes", "failed"], 2),
    ("usual", "usual: one row per job", "step", ["*workspace_id", "*job_id", "usual_minutes"], 3),
    ("judged", "judged: one row per run", "step", _K3 + ["minutes", "usual_minutes", "slow"], 3),
]
_RUN_EDGES = [
    ("jrt", "ends", "rows with a result; the last per run (QUALIFY ROW_NUMBER)"),
    ("jrt", "starts", "MIN(period_start_time) per run"),
    ("ends", "runs", "JOIN ON workspace_id, job_id, run_id"),
    ("starts", "runs", "JOIN ON workspace_id, job_id, run_id"),
    ("runs", "usual", "successful runs: median per job, 5+ runs"),
    ("runs", "judged", "every run"),
    ("usual", "judged", "LEFT JOIN ON workspace_id, job_id; slow = 1.5x and 5+ min"),
]
_JB = ("jb", "jb: system.lakeflow.jobs", "table", ["*workspace_id", "*job_id", "name"], 0, 1)
_WSB = ("ws", "ws: system.access.workspaces_latest", "table", ["*workspace_id", "workspace_name"], 0, 1)


def _per_job_er(last: str) -> dict:
    return {
        "nodes": _RUN_NODES + [
            ("per_job", "per_job: one row per job", "step",
             ["*workspace_id", "*job_id", "runs", "failed_runs", "slow_runs", "days_failed", "days_slow",
              "median / avg / max_minutes", "total_hours", "last_code"], 0, 1),
            _JB, _WSB,
            ("named", "named", "step", ["*workspace_id", "*job_id", "job_name", "workspace", "(per_job columns)"],
             1, 1),
            ("ranked", "ranked", "step", ["reliability", "fail_rank", "time_rank"], 2, 1),
            ("out", "dashboard rows", "result",
             ["job", "workspace", "reliability", "runs", "failed_runs", "failure_pct", "days_failed", "days_slow",
              "median / avg / max_minutes", "total_hours", "last_failure", "flaky, always_fails", "top_by_time"],
             3, 1),
        ],
        "edges": _RUN_EDGES + [
            ("judged", "per_job", "GROUP BY workspace_id, job_id"),
            ("per_job", "named", ""),
            ("jb", "named", "LEFT JOIN ON workspace_id, job_id (latest row)"),
            ("ws", "named", "LEFT JOIN ON workspace_id"),
            ("named", "ranked", "reliability rule; ROW_NUMBER by failed runs and by hours"),
            ("ranked", "out", last),
        ],
    }


_CLASSES = ("classes", "classes: a list in the query", "table", ["workspace_class", "name_pattern"], 0)
_WS0 = ("ws", "ws: system.access.workspaces_latest", "table", ["*workspace_id", "workspace_name"], 0)
_WS_CLASS = ("ws_class", "ws_class: one row per workspace", "step",
             ["*workspace_id", "workspace_name", "workspace_class"], 1)
_CLASS_EDGES = [("ws", "ws_class", "every workspace"),
                ("classes", "ws_class", "LEFT JOIN ON lower(name) LIKE pattern; first match")]

DETAIL_ER = {
    "jobs": _per_job_er("all jobs, always failing and flaky first"),
    "top_failed_jobs": _per_job_er("WHERE failed_runs > 0 AND fail_rank <= 20"),
    "job_days": {
        "nodes": _RUN_NODES + [
            _JB, _WSB,
            ("named", "named: one row per run", "step",
             _K3 + ["run_date", "job_name", "workspace", "failed", "slow"], 1, 1),
            ("ranked", "ranked", "step", ["job_failed (per job)", "rk"], 2, 1),
            ("out", "dashboard rows", "result",
             ["run_date", "workspace", "job", "top_failing_job", "runs", "failed_runs", "slow_runs"], 3, 1),
        ],
        "edges": _RUN_EDGES + [
            ("judged", "named", "every run"),
            ("jb", "named", "LEFT JOIN ON workspace_id, job_id"),
            ("ws", "named", "LEFT JOIN ON workspace_id"),
            ("named", "ranked", "SUM(failed) per job; DENSE_RANK"),
            ("ranked", "out", "top 10 keep their name; GROUP BY day and job"),
        ],
    },
    "monthly_active_users": {
        "nodes": [
            ("audit", "system.access.audit", "table",
             ["*workspace_id", "event_date", "service_name", "action_name", "user_identity.email",
              "response.status_code"], 0),
            _WS0, _CLASSES, _WS_CLASS,
            ("logins", "logins: workspace and person", "step", ["*workspace_id", "user_name"], 1),
            ("out", "dashboard rows", "result", ["workspace", "workspace_class", "user_name"], 2),
        ],
        "edges": _CLASS_EDGES + [
            ("audit", "logins", "accounts, login, status 200, the month; DISTINCT"),
            ("logins", "out", "people only (an email)"),
            ("ws_class", "out", "LEFT JOIN ON workspace_id"),
        ],
    },
    "workspace_activity": {
        "nodes": [
            ("audit", "system.access.audit", "table", ["*workspace_id", "event_date"], 0),
            _WS0, _CLASSES, _WS_CLASS,
            ("seen", "seen: one row per workspace", "step", ["*workspace_id", "last_activity"], 1),
            ("w", "w: every workspace", "step",
             ["*workspace_id", "workspace_name", "workspace_class", "last_activity", "days_since"], 2),
            ("out", "dashboard rows", "result",
             ["workspace", "workspace_class", "last_activity", "days_since", "inactive", "no_events"], 3),
        ],
        "edges": _CLASS_EDGES + [
            ("audit", "seen", "last 365 days: MAX(event_date) per workspace"),
            ("ws_class", "w", "every workspace"),
            ("seen", "w", "LEFT JOIN ON workspace_id"),
            ("w", "out", "inactive: 90+ days or none"),
        ],
    },
    "grant_events": {
        "nodes": [
            ("audit", "system.access.audit", "table",
             ["*event_id", "*workspace_id", "event_time", "service_name", "action_name", "user_identity.email",
              "request_params", "response.status_code"], 0),
            ("ev", "ev: one row per GRANT event", "step",
             ["*workspace_id", "event_time", "actor", "object_type", "object", "changes (JSON)"], 1),
            ("per_grantee", "per_grantee", "step", ["*workspace_id", "object", "c.principal", "c.add"], 1),
            ("granted", "granted: one per privilege", "step",
             ["*workspace_id", "object_type", "object", "grantee", "privilege", "actor"], 2),
            ("judged", "judged", "step", ["all_privileges", "to_account_users"], 2),
            ("ws", "ws: system.access.workspaces_latest", "table", ["*workspace_id", "workspace_name"], 2),
            ("out", "dashboard rows", "result",
             ["granted_at", "workspace", "object_type", "object", "privilege", "grantee", "actor", "actor_type",
              "why", "critical"], 3),
        ],
        "edges": [
            ("audit", "ev", "unityCatalog updatePermissions, 200, window; one per event_id"),
            ("ev", "per_grantee", "from_json(changes); explode"),
            ("per_grantee", "granted", "explode(c.add); ALL PRIVILEGES = ALL_PRIVILEGES"),
            ("granted", "judged", "ALL_PRIVILEGES on catalog/schema/table; account users"),
            ("judged", "out", "actor type from the actor"),
            ("ws", "out", "LEFT JOIN ON workspace_id"),
        ],
    },
    "admin_changes": {
        "nodes": [
            ("audit", "system.access.audit", "table",
             ["*event_id", "*service_name", "*action_name", "workspace_id", "event_time", "user_identity.email",
              "request_params", "response.status_code"], 0),
            ("actions", "actions: a list in the query", "table", ["*service_name", "*action_name", "category"], 0),
            ("ev", "ev: one row per admin action", "step",
             ["*workspace_id", "event_time", "action_name", "category", "actor", "status_code", "target", "changes"],
             1),
            ("ws", "ws: system.access.workspaces_latest", "table", ["*workspace_id", "workspace_name"], 1),
            ("out", "dashboard rows", "result",
             ["changed_at", "workspace", "category", "action", "target", "change", "actor", "actor_type",
              "outcome", "by_user"], 2),
        ],
        "edges": [
            ("audit", "ev", "JOIN ON service_name, action_name; window; one per event_id"),
            ("actions", "ev", "JOIN ON service_name, action_name"),
            ("ev", "out", "actor type; outcome from the status"),
            ("ws", "out", "LEFT JOIN ON workspace_id (0 = Account)"),
        ],
    },
}



# ------------------------------------------------------------------------------------------------
# Waste & savings
# ------------------------------------------------------------------------------------------------

BASICS["warehouse"] = ("SQL warehouse", "Compute for SQL: dashboards, BI tools and the SQL editor run their queries on "
                       "it. It is billed while it runs, whether or not a query is running, and stops after its "
                       "auto-stop setting with no query. Serverless warehouses start in seconds; pro and classic "
                       "take minutes.")
BASICS["idle"] = ("Idle time", "Time a warehouse or cluster is running and billed but doing no work: between "
                  "queries, before the first one, or waiting for auto-stop after the last one.")
BASICS["serving"] = ("Model serving endpoint", "An always-on API that serves a model; it is billed while it is "
                     "up, even with no requests.")
BASICS["pool"] = ("Instance pool", "Machines kept ready so clusters start faster; min idle instances are kept "
                  "running (and paid for the cloud machine) even when no cluster uses them.")

COLUMNS.update({
    "waste": {
        "source": "Which check flagged it: warehouse idle time, idle cluster nodes, failed job runs, failed SQL "
                  "statements, or serving endpoints with no requests.",
        "kind": "SQL warehouse, Cluster, Job or Serving endpoint.",
        "resource": "Its name and id, as name (id).",
        "top_resource": "The name for the 10 with the most possible waste; All others for the rest.",
        "possible_usd": "The possible waste in $ at list price, from that check.",
        "top3_usd": "possible_usd for the three biggest only; a tile adds it up.",
    },
    "warehouses": {
        "warehouse": "The warehouse's name and workspace.",
        "kind": "serverless, pro or classic.",
        "warehouses": "Always 1: charts add it up to count warehouses.",
        "auto_stop_now": "Its auto-stop setting in minutes; off when it never stops.",
        "autostop_band": "The setting grouped for the charts (01 min, 02 min, 03-05 min, ...).",
        "running_hours": "Hours it was running (billed).",
        "busy_hours": "Hours at least one query ran.",
        "idle_hours": "Hours running with no query, short pauses of a minute or less left out.",
        "waiting_hours": "Idle hours after its last query, waiting for auto-stop.",
        "idle_share_pct": "idle_hours / running_hours x 100.",
        "idle_usd": "The idle time's $ at list price.",
        "total_usd": "All its $ at list price.",
        "top_warehouse": "The name for the 10 with the most idle time.",
        "save_1": "$ saved at 1 minute auto-stop (empty when not allowed or not shorter).",
        "starts_1": "Extra cold starts at 1 minute.",
        "save_2": "$ saved at 2 minutes.", "starts_2": "Extra cold starts at 2 minutes.",
        "save_5": "$ saved at 5 minutes.", "starts_5": "Extra cold starts at 5 minutes.",
        "save_10": "$ saved at 10 minutes.", "starts_10": "Extra cold starts at 10 minutes.",
        "saving_lowest_usd": "The saving at the shortest setting it can take (1 min serverless, 10 min otherwise).",
        "the_fix": "One line: what to change, from the biggest kind of idle time.",
    },
    "warehouse_parts": {
        "top_warehouse": "The name for the 10 that ran longest.",
        "warehouse": "The warehouse's name and workspace.",
        "kind": "serverless, pro or classic.",
        "part": "Busy, Idle: before first query, Idle: between queries, Idle: waiting for auto-stop, Idle: no query "
                "at all, or Short pauses (1 min or less).",
        "hours": "Hours in that part.",
    },
    "whatif": {
        "warehouse": "The warehouse's name and workspace.",
        "kind": "serverless, pro or classic.",
        "setting": "The shorter auto-stop setting, in minutes.",
        "step": "Down to NN min: the label of one step down.",
        "step_saving": "The extra $ of this step over the next longer setting; a warehouse's steps add up.",
        "setting_label": "The setting as text for the charts.",
        "saving": "$ saved at this setting against today's.",
        "hours_saved": "Idle hours saved.",
        "extra_starts": "Extra cold starts at this setting.",
        "top_warehouse": "The name for the 10 with the biggest saving.",
        "saving_serverless_1": "saving for serverless at 1 min, for the tiles; the same pattern for 2 and 5.",
        "saving_serverless_2": "saving for serverless at 2 min.",
        "saving_serverless_5": "saving for serverless at 5 min.",
        "saving_classic_10": "saving for pro and classic at 10 min.",
        "starts_serverless_1": "extra_starts for serverless at 1 min, for the tiles.",
        "starts_serverless_2": "extra_starts for serverless at 2 min.",
        "starts_serverless_5": "extra_starts for serverless at 5 min.",
        "starts_classic_10": "extra_starts for pro and classic at 10 min.",
    },
    "clusters": {
        "cluster": "The cluster's name and id.",
        "kind": "All-purpose, or another source (job clusters are left out).",
        "clusters": "Always 1: charts add it up to count clusters.",
        "ran": "Ran, or Did not run in the window.",
        "auto_termination": "Its auto-termination in minutes; off when it never stops.",
        "autoterm_band": "The setting grouped for the charts.",
        "no_autoterm": "1 for an all-purpose cluster with auto-termination off.",
        "idle_pct": "Share of its node time with CPU near 0%.",
        "avg_cpu_pct": "Average CPU over its nodes.",
        "idle_usd": "The idle node time's $ at list price.",
        "flagged_idle_usd": "idle_usd when the cluster is Critical or Warn.",
        "total_usd": "All its $ at list price.",
        "top_cluster": "The name for the 10 with the highest idle share and enough data.",
    },
    "pools": {
        "pool": "The pool's name and id.",
        "node_type": "The machine type it keeps.",
        "min_idle_instances": "Machines kept running with no cluster on them.",
        "idle_autotermination_minutes": "How long an extra idle machine stays before it is released.",
        "total_usd": "Databricks $ of clusters from the pool (the cloud machine cost is billed by the cloud).",
    },
    "failed_statements": {
        "warehouse": "The warehouse's name and id.",
        "top_warehouse": "The name for the 10 with the most failed $.",
        "statements": "Statements in the window.",
        "failed_statements": "Statements that failed.",
        "canceled_statements": "Statements canceled.",
        "failed_usd": "The warehouse $ attributed to the failed statements.",
        "failed_cost_share_pct": "failed_usd as a share of the warehouse's $.",
        "cost_basis": "How the $ was split: attributed usage, or by run time when that is missing.",
    },
    "serving": {
        "endpoint": "The endpoint's name and id.",
        "tracking": "Whether request tracking is on (without it requests can't be counted).",
        "top_endpoint": "The name for the 10 with the most $ and no requests.",
        "traffic": "Has requests, or No requests.",
        "with_traffic": "1 when it had requests.",
        "requests": "Requests in the window.",
        "last_request": "The day of its last request.",
        "total_usd": "Its $ at list price.",
        "wasted_usd": "$ counted as waste (billed with tracking on and no requests).",
        "no_request_usd": "total_usd when it had no requests.",
        "billed_no_requests": "1 when billed with no requests.",
    },
    "dormant_endpoints": {
        "endpoint": "The endpoint's name and id.",
        "requests": "Requests in the history kept (90 days).",
        "last_request": "The day of its last request.",
        "billed_dbus": "DBUs billed.",
        "tracking": "on or off.",
        "not_assessed_reason": "Why it wasn't judged (for example tracking off).",
    },
    "stale_jobs": {
        "last_run": "The day of its last run.",
        "created": "The day it was created.",
        "total_usd": "Its $ in the window.",
    },
    "jobs_on_all_purpose": {
        "cluster": "The all-purpose cluster's name and id.",
        "cluster_source": "UI or API: made by hand, not a job cluster.",
        "task_runs": "Task runs on it.",
        "total_usd": "The job's $ on it at list price.",
        "jobs_sharing_cluster": "How many jobs use the same cluster.",
    },
    "pipeline_idle_tail": {
        "pipeline": "The pipeline's name and id.",
        "updates": "Updates in the window.",
        "idle_dbus": "DBUs billed in hours with no update running.",
        "idle_pct": "idle_dbus as a share of its DBUs.",
        "total_usd": "Its $ at list price.",
    },
})

WHY.update({
    "waste": "One list across five checks lets the biggest fixes rise to the top whatever their kind. A floor of $50 "
             "per 30 days keeps small items out; failed runs have none, because a job that keeps failing matters "
             "even when it is cheap.",
    "warehouses": "A warehouse is billed while it runs, not while it queries. The check replays its start and stop "
                  "events against the queries that ran, so the time with no query shows up, split by why it was "
                  "idle; the fix follows from the biggest part.",
    "warehouse_parts": "Six rows per warehouse let one stacked bar show where its running time went.",
    "whatif": "Shorter auto-stop saves idle time but adds cold starts. Replaying the window's real gaps between "
              "queries at each setting gives both numbers, per warehouse, instead of a guess.",
    "clusters": "An all-purpose cluster with machines at near 0% CPU is paying for nothing; per-minute node use "
                "(node_timeline) shows how much, and the auto-termination setting shows why it stayed up.",
    "pools": "Min idle instances are machines kept running for faster starts; they cost cloud money all the time.",
    "failed_jobs": "The compute of a failed run is paid and delivers nothing; billing per run gives its $.",
    "failed_statements": "Failed SQL still used the warehouse; attributed usage splits the warehouse $ by statement.",
    "serving": "An endpoint is billed while it is up; with tracking on and no requests, that spend is waste.",
    "dormant_endpoints": "Endpoints with no traffic at all over the request history are candidates to remove.",
    "stale_jobs": "A job that hasn't run for months is clutter at best, and may still hold clusters or permissions.",
    "jobs_on_all_purpose": "Scheduled work on an all-purpose cluster pays the all-purpose rate, "
                           "higher per DBU than a job cluster's.",
    "pipeline_idle_tail": "A pipeline's compute billed when no update runs is idle tail: the cluster stays up after "
                          "the work is done.",
})

GUIDES["waste_savings"] = {
    "purpose": "Shows what we pay for that does nothing, and what to change: idle SQL warehouses and cluster nodes, "
               "failed job runs and SQL statements, serving endpoints with no requests, and smaller flags without a "
               "clean $ figure. Auto-stop what-if shows what a shorter auto-stop would save and cost in cold starts.",
    "basics": ["workspace", "system_tables", "warehouse", "idle", "cluster", "job", "serving", "pool", "dbu", "sql"],
    "rules": [
        ("Possible waste", "Critical or Warn in one of the five priced checks, $ above 0, and at least $50 per 30 "
         "days (failed runs: no floor).", "Small items would bury the big ones."),
        ("Warehouse idle", "Running with no query; pauses of a minute or less don't count. Critical: 30+ idle "
         "minutes and 60%+ idle, or $200+; Warn: 30%+ idle, or $20+.", "Billing runs whether a query runs or not."),
        ("Auto-stop what-if", "The window's gaps replayed at 1, 2, 5 min (serverless) and 10 min (pro, classic); "
         "only settings shorter than today's.", "Pro and classic can't go below 10 minutes."),
        ("Idle cluster nodes", "Node time near 0% CPU: 80%+ is Critical, 50%+ Warn.", "The machines were up but idle."),
        ("Failed job runs", "$ of failed runs and failed repair attempts. Critical: 50%+ failed (3+ runs) or $200+; "
         "Warn: 10%+ or $20+.", "A failed run delivers nothing."),
        ("Failed SQL statements", "$ of failed statements per warehouse: $50+ Critical, $5+ Warn.",
         "Failed queries still used the warehouse."),
        ("Serving with no requests", "Tracked, billed and no requests: Critical; tracking off: Warn (turn it on).",
         "Without tracking, no requests can't be told apart from unknown."),
        ("Stale job", "No run for 30+ days is Warn, 90+ Critical.", "Old jobs clutter and may keep access."),
        ("Job on all-purpose", "Its tasks ran on a UI or API cluster: Warn; $100+ Critical.",
         "Interactive pricing for scheduled work."),
        ("Pipeline idle tail", "DBUs billed with no update running: 100+ Warn, 500+ Critical.",
         "Compute kept up after the work."),
        ("Pool idle floor", "Min idle instances 5+ is Warn, 20+ Critical.", "Machines paid for while waiting."),
    ],
    "limits": [
        _LATENCY,
        "$ is list price, not the invoice; idle $ is the idle share of the billed $.",
        "Warehouse idle needs query history and warehouse events; a warehouse with neither reads Not assessed.",
        _REGION,
        "Pool machines are billed by the cloud, not in system.billing; the pool $ is the Databricks part only.",
    ],
    "checks": [
        ("A warehouse's start and stop events",
         "SELECT warehouse_id, event_type, event_time, cluster_count\nFROM system.compute.warehouse_events\n"
         "WHERE event_time >= current_date() - 1\nORDER BY warehouse_id, event_time;"),
        ("The queries on that warehouse",
         "SELECT statement_id, start_time, end_time, execution_status\nFROM system.query.history\n"
         "WHERE compute.warehouse_id = '<warehouse id>' AND start_time >= current_date() - 1\n"
         "ORDER BY start_time;"),
    ],
}

SHORT["waste_savings"] = {
    "how": [
        "A warehouse or cluster is billed while it runs. The checks put its start and stop events (warehouse_events) "
        "or its per-minute machine use (node_timeline) next to the work that ran (query.history, job runs), so the "
        "running time with no work shows up as idle.",
        "Idle time, failed runs and statements, and endpoints with no requests get a $ from billing.usage x list "
        "price, and the five checks are stacked into one list of possible waste.",
        "Auto-stop what-if replays the real gaps between queries at shorter settings: the saving, and the extra cold "
        "starts it would cost.",
    ],
    "rules": ["Possible waste", "Warehouse idle", "Auto-stop what-if", "Idle cluster nodes", "Failed job runs",
              "Serving with no requests"],
    "sql": """-- The $ of failed job runs in the last 30 days, per job, at list price (simplified: every DBU of a
-- run that ended failed).
WITH failed AS (
  SELECT DISTINCT workspace_id, job_id, run_id
  FROM system.lakeflow.job_run_timeline
  WHERE result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') AND period_start_time >= current_date() - 30
)
SELECT u.usage_metadata.job_id AS job_id,
       ROUND(SUM(u.usage_quantity * p.pricing.default), 2) AS failed_usd
FROM system.billing.usage u
JOIN failed f
  ON f.workspace_id = u.workspace_id AND f.job_id = u.usage_metadata.job_id
 AND f.run_id = u.usage_metadata.job_run_id
JOIN system.billing.list_prices p
  ON p.sku_name = u.sku_name AND p.cloud = u.cloud AND p.usage_unit = u.usage_unit
 AND u.usage_start_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
WHERE u.usage_date >= current_date() - 30
GROUP BY u.usage_metadata.job_id
ORDER BY failed_usd DESC;""",
}

ER["waste_savings"] = {
    "nodes": [
        ("events", "system.compute.warehouse_events", "table",
         ["*warehouse_id", "event_time", "event_type", "cluster_count"], 0),
        ("qh", "system.query.history", "table",
         ["*compute.warehouse_id", "start_time", "end_time", "execution_status"], 0),
        ("nt", "system.compute.node_timeline", "table", ["*cluster_id", "*instance_id", "start_time",
                                                         "cpu_user_percent"], 0),
        ("jrt", "system.lakeflow.job_run_timeline", "table", ["*job_id", "*run_id", "result_state"], 0),
        ("se", "system.serving.endpoint_usage", "table", ["*served_entity_id", "request_time"], 0),
        ("usage", "system.billing.usage", "table",
         ["*usage_metadata (warehouse, cluster, job, run, endpoint ids)", "*sku_name", "usage_quantity"], 0),
        ("lp", "system.billing.list_prices", "table", ["*sku_name", "pricing.default", "price_start_time"], 0),
        ("idle", "warehouse idle", "check", ["*warehouse_id", "running / busy / idle min", "idle $"], 1),
        ("nodes", "idle cluster nodes", "check", ["*cluster_id", "idle_ratio", "idle $"], 1),
        ("fj", "failed job runs", "check", ["*job_id", "failed_runs", "wasted $"], 1),
        ("fs", "failed SQL statements", "check", ["*warehouse_id", "failed $"], 1),
        ("sv", "serving endpoints", "check", ["*endpoint_id", "requests", "$ with none"], 1),
        ("src", "src: the five stacked", "step", ["source", "kind", "*entity_id", "status", "$"], 2),
        ("names", "names: warehouses, clusters, jobs", "table", ["*entity_id", "name", "workspace"], 2),
        ("out", "possible waste rows", "result", ["source", "resource", "workspace", "status", "possible_usd"], 3),
    ],
    "edges": [
        ("events", "idle", "running periods"),
        ("qh", "idle", "busy stretches; the gaps are idle"),
        ("nt", "nodes", "minutes near 0% CPU"),
        ("jrt", "fj", "runs that failed"),
        ("qh", "fs", "statements that failed"),
        ("se", "sv", "requests per endpoint"),
        ("usage", "idle", "every check: DBUs x list price = $"),
        ("lp", "idle", ""),
        ("idle", "src", ""), ("nodes", "src", ""), ("fj", "src", ""), ("fs", "src", ""), ("sv", "src", ""),
        ("src", "out", "Critical or Warn, $50+ per 30 days"),
        ("names", "out", "LEFT JOIN ON the id: names"),
    ],
}

# ------------------------------------------------------------------------------------------------
# Data usage
# ------------------------------------------------------------------------------------------------

from app.core import usage as _usage  # noqa: E402
from tools import direct_data_usage as wd  # noqa: E402

BASICS["volume"] = ("Volume", "A Unity Catalog folder for files (CSV, images, exports) inside a schema, under "
                    "/Volumes/<catalog>/<schema>/<volume>. Its files are not tables, so no system table counts them.")
BASICS["home_workspace"] = ("Home workspace", "The workspace that reads and writes a catalog most in lineage over "
                            f"{wd.HOME_DAYS} days. A catalog's tables and volumes count for that workspace.")
BASICS["usage_class"] = ("Usage class", "Every table, view and volume falls in exactly one: " + "; ".join(
    f"{label} ({meaning.split('.')[0].lower()})" for _k, label, meaning in _usage.CLASSES) + ".")

_WS = {"workspace": "The home workspace of the catalog, by name; No home workspace when lineage has none."}
_CLS = {"usage_class": "Written, never read; Idle 90d+; Cooling; or Active (app/core/usage.py).",
        "table_name": "catalog.schema.table.", "table_link": "The table in Catalog Explorer of its home workspace."}

COLUMNS.update({
    "wd_workspaces": {
        "workspace": "The workspace, by name.", "workspace_id": "Its id.", "workspace_link": "The workspace URL.",
        "tables_written_never_read": "Its tables written in the window but never read.",
        "tables_idle": "Its tables with no read or write in 90 days.", "tables_cooling": "Its tables not read in the window.",
        "tables_active": "Its tables read in the window.", "data_gb": "Table bytes plus volume files, in GB.",
        "unread_gb": "GB in written-never-read and idle tables.", "sensitive_tables": "Tables with a sensitive column.",
        "catalogs": "Catalogs it is home to.", "tables": "Tables in those catalogs.", "views": "Views in those catalogs.",
        "table_columns": "Columns of those tables.", "view_columns": "Columns of those views.",
        "volumes": "Volumes in those catalogs.", "volume_files": "Files in those volumes, from the sizes job.",
        "active_people": "People (emails) with an audit event or a query in the window.",
        "active_service_principals": "Service principals (UUIDs) the same way.", "queries": "Queries in the window.",
        "active_days": "Days in the window with any activity.", "last_query": "The latest query.",
        "complexity_index": "0-100: views 20, view columns 15, tables 20, table columns 15, volume files 10, size 20; "
                            "each on a log scale against the largest workspace.",
        "usage_index": "0-100: people 35, service principals 15, queries 30 (log scale), active days / window days 20.",
        "signal": "Retire candidate, Review, Light or Healthy.", "flagged": "1 when Retire candidate or Review.",
        "workspaces": "Always 1: counts workspaces.",
    },
    "wd_tables": {**_WS, **_CLS,
                  "kind": "Table or View.", "size_gb": "From DESCRIBE DETAIL in the sizes job.", "num_files": "Data files.",
                  "reads": "Lineage reads in the window.", "writes": "Lineage writes in the window.",
                  "readers": "Distinct identities reading it.", "writers": "Distinct identities writing it.",
                  "last_read": f"The latest read over {wd.READ_LOOKBACK_DAYS} days.",
                  "last_write": "The latest write, lineage or DESCRIBE HISTORY.",
                  "sensitive_types": "Types of its sensitive columns.", "unmasked_columns": "Sensitive columns without a mask.",
                  "unread_gb": "Its GB when written never read or idle.", "objects": "Always 1: counts tables and views."},
    "wd_volumes": {**_WS, "volume": "catalog.schema.volume.", "usage_class": _CLS["usage_class"],
                   "files": "Files listed by the sizes job.", "size_gb": "Their size in GB.",
                   "reads": "File reads in the window.", "writes": "File writes in the window.",
                   "unread_gb": "Its GB when written never read or idle."},
    "wd_people": {"workspace": "The workspace, by name.", "identity": "The email, or a service principal's id.",
                  "kind": "Person or Service principal.", "tables_read": "Tables read in the window.",
                  "tables_written": "Tables written in the window.", "writes_nobody_reads": "Tables it wrote that nobody read.",
                  "sensitive_tables": "Tables with sensitive columns it read or wrote.",
                  "direct_grants": "Tables it can read through a direct grant.", "last_seen": "The latest audit event or query.",
                  "inactive": f"1 when not seen for {wd.INACTIVE_DAYS}+ days.", "active": "1 when seen in the window."},
    "wd_daily": {**_WS, "day": "The day.", "table_name": "catalog.schema.table.", "direction": "Reads or Writes.",
                 "events": "Lineage events."},
    "wd_access": {"identity": "Who.", "table_name": "catalog.schema.table.", "access": "Reads, Writes, or both.",
                  "last_access": "The latest of them."},
    "wd_edges": {"direction": "Upstream (it reads from) or Downstream (it feeds).", "other_table": "The other table.",
                 "other_class": "The other table's usage class."},
    "wd_grants": {"grantee": "Who holds the grant.", "used": "Whether the grantee read the table in the window."},
})

_DU = {
    "workspace": "The workspace, by name: for a table or volume, the home workspace of its catalog.",
    "catalog": "The catalog.", "schema_name": "The schema.", "schema_full": "catalog.schema.",
    "table_name": "catalog.schema.table.", "volume": "catalog.schema.volume.", "identity": "The email, or a service principal's id.",
    "kind": "Person or Service principal; for a table, Table or View.", "usage_class": _CLS["usage_class"],
    "class_rank": "1 written never read, 2 idle, 3 cooling, 4 active: the clean-up order.",
    "size_in_bytes": "Bytes, from DESCRIBE DETAIL or the volume listing (the sizes job).", "size_gb": "The same in GB.",
    "gb_written_never_read": "size_gb when written never read, else 0.", "gb_idle": "size_gb when idle 90d+, else 0.",
    "gb_cooling": "size_gb when cooling, else 0.", "gb_active": "size_gb when active, else 0.",
    "unread_gb": "GB when written never read or idle 90d+.",
    "reads": "Reads in the window (lineage; file reads for a volume).", "writes": "Writes in the window.",
    "readers": "Distinct identities reading.", "writers": "Distinct identities writing.",
    "last_read": f"The latest read over {wd.READ_LOOKBACK_DAYS} days.", "last_write": "The latest write, lineage or DESCRIBE HISTORY.",
    "days_since_read": "Days from the end of the window to the last read.",
    "last_change_op": "The last data change's operation, from DESCRIBE HISTORY.",
    "last_maintenance": "The last OPTIMIZE, VACUUM or predictive optimization run.",
    "rows_changed": "Rows changed in the window, from DESCRIBE HISTORY.", "gb_written": "GB written, from DESCRIBE HISTORY.",
    "sensitive_columns": "Its sensitive columns.", "sensitive_types": "Their types.", "unmasked_columns": "Of them without a mask.",
    "sensitive": "1 with a sensitive column.", "sensitive_unread": "1 when sensitive and unread.",
    "has_unmasked": "1 with an unmasked sensitive column.", "owner": "The table owner.", "created": "When it was created.",
    "volume_type": "managed or external.", "files": "Files, from the volume listing.", "volumes": "Volumes; 1 per volume row.",
    "tables": "Tables.", "views": "Views.", "table_columns": "Columns of the tables.", "view_columns": "Columns of the views.",
    "volume_files": "Files in its volumes.", "sensitive_tables": "Tables with a sensitive column.",
    "tables_written_never_read": "Tables written in the window, not read.", "tables_idle": "Tables idle 90d+.",
    "tables_cooling": "Tables cooling.", "tables_active": "Tables active.",
    "logins": "Successful logins in the window.", "queries": "Queries in the window.",
    "tables_read": "Tables read in the window.", "tables_written": "Tables written in the window.",
    "writes_nobody_reads": "Tables it wrote that nobody read.", "direct_grants": "Tables it can read through a direct grant.",
    "sensitive_grants": "Of them with sensitive columns.", "last_seen": "The latest audit event or query.",
    "days_since": "Days since last seen.", "last_seen_band": "Under 30, 30-90, 90-180 or 180+ days.",
    "inactive": f"1 when not seen for {wd.INACTIVE_DAYS}+ days.", "active": "1 when seen in the window.",
    "touched_sensitive": "1 when it read or wrote a sensitive table.", "active_person": "1 for an active person.",
    "active_sp": "1 for an active service principal.", "identities": "Always 1: counts people and service principals.",
    "access": "Reads, Writes, or both.", "last_access": "The latest of them.", "pairs": "Always 1: counts identity-table pairs.",
    "direction": "Reads or Writes; on lineage, Upstream or Downstream.", "events": "Lineage events.", "day": "The day.",
    "other_table": "The table on the other end.", "other_class": "Its usage class.",
    "grantee": "Who holds the grant.", "privilege": "SELECT or ALL PRIVILEGES.", "used": "Whether the grantee read it in the window.",
    "unused": "1 when never read in the window.", "grants": "Always 1: counts grants.",
    "version": "The table version.", "changed_at": "When.", "operation": "What changed.", "user_name": "Who.",
    "column_name": "The column.", "pii_type": "What it holds: email, phone, IBAN, ...", "mask": "masked or unmasked.",
    "unmasked": "1 when unmasked.", "columns": "Always 1: counts columns.",
    "name": "The workspace, table, person or volume ranked.",
    "source": "Delta (DESCRIBE HISTORY) or Predictive optimization.", "who": "Who ran it.", "status": "Predictive optimization's status.",
    "files_added": "Files written.", "files_removed": "Files removed (merged by OPTIMIZE, deleted by VACUUM).",
    "gb_added": "GB written.", "gb_removed": "GB removed from the table.", "gb_freed": "GB VACUUM deleted from storage.",
    "dbu": "Predictive optimization's estimated DBUs.", "compactions": "1 for OPTIMIZE, compaction or clustering.",
    "files_merged": "Files a compaction merged.", "files_after": "Files it wrote.",
    "started": "When the query started.", "statement_type": "SELECT, MERGE, INSERT...", "seconds": "Total time.",
    "gb_read": "GB the whole query read.", "files_read": "Files the whole query read.", "files_pruned": "Files skipped.",
    "scan": "Full scan when 50+ files were read and none pruned.", "warehouse_id": "The warehouse.", "query": "The first 300 characters.",
    "avg_seconds": "Average time per read.", "full_scans": "Reads that pruned no file.", "pruned_pct": "Share of files skipped.",
    "via": "JOB, NOTEBOOK, PIPELINE...", "events": "Read or write events.", "last_event": "The last one.",
    "time_bytes_files": "Always not available: not in query history.",
    "compacted_at": "When the compaction ran.", "reads_before": "Reads in the 7 days before.", "reads_after": "Reads in the 7 days after.",
    "seconds_before": "Average seconds per read before.", "seconds_after": "After.", "time_change_pct": "Change in time per read.",
    "files_per_read_before": "Average files per read before.", "files_per_read_after": "After.",
}
for _key in ("wd_workspaces", "wd_tables", "wd_cleanup", "wd_largest", "wd_volumes", "wd_people", "wd_inactive", "wd_daily",
             "wd_schemas", "wd_access", "wd_edges", "wd_changes", "wd_columns", "wd_grants", "wd_top_workspaces",
             "wd_top_tables", "wd_top_tables_ws", "wd_top_people", "wd_top_people_ws", "wd_top_volumes_ws", "wd_daily_top",
             "wd_table_queries", "wd_table_scans", "wd_table_other", "wd_compaction_effect", "wd_table_people", "wd_person_tables"):
    COLUMNS[_key] = {**_DU, **COLUMNS.get(_key, {})}

WHY.update({
    "wd_top_workspaces": "Which workspaces carry the work, and which barely read or write anything.",
    "wd_top_tables": "The tables the platform runs on: break one of these and many people notice.",
    "wd_top_tables_ws": "The same inside one workspace: what its owners should protect first.",
    "wd_top_people": "Who reads and writes most: the people to ask before changing shared data.",
    "wd_top_people_ws": "The same inside one workspace.",
    "wd_top_volumes_ws": "The volumes in daily use inside one workspace; the rest are clean-up candidates.",
    "wd_daily_top": "Whether a workspace's main tables are used every day or in bursts.",
    "wd_cleanup": "The short list to act on: unread tables, sensitive ones first.",
    "wd_largest": "Size is where the money is; reads say whether it earns it.",
    "wd_inactive": "Access nobody uses is the easiest access to remove.",
    "wd_schemas": "Whole schemas to clean up, not table by table.",
    "wd_changes": "What changed a table, when and who, and whether its upkeep runs: OPTIMIZE merges small files, VACUUM frees storage.",
    "wd_table_queries": "The queries that cost the most time on a table; a full scan is the one to fix first.",
    "wd_table_scans": "How often a table is read, how long it takes and how many reads skip nothing.",
    "wd_table_other": "Jobs and notebooks on classic compute use the table too: who and how often, even without query details.",
    "wd_compaction_effect": "Whether compacting a table made its reads faster.",
    "wd_table_people": "Who to ask before you change or drop a table.",
    "wd_person_tables": "What one person depends on.",
    "wd_columns": "Which columns need a mask.",
})

WHY.update({
    "wd_workspaces": "One row per workspace: how its data is used, what it holds and how much work happens there.",
    "wd_tables": "Size alone doesn't say if a table matters; reads do. The class says what to do with it.",
    "wd_volumes": "Volume files are invisible to SQL, so the job lists them; the audit log's file events show use.",
    "wd_people": "Who reads and writes, and who stopped coming but can still read.",
    "wd_daily": "A trend shows a table going quiet before it is idle.",
    "wd_access": "Who to ask before you drop or change a table.",
    "wd_edges": "A downstream table nobody reads means the job that builds it can stop.",
    "wd_grants": "Grants nobody uses are the easiest access to remove.",
})

GUIDES["data_usage"] = {
    "purpose": "The app's Data usage section as a dashboard: an overview, workspaces, tables and volumes in one usage "
               "class each, people, and detail pages for one workspace, table or person. For every workspace.",
    "basics": ["workspace", "home_workspace", "usage_class", "uc", "volume", "system_tables", "audit", "principal", "sql"],
    "rules": [
        ("Home workspace", f"The workspace with the most lineage reads and writes of a catalog over {wd.HOME_DAYS} days.",
         "A catalog shared by many workspaces counts once."),
        ("Usage class", "; ".join(f"{label}: {meaning}" for _k, label, meaning in _usage.CLASSES),
         "Exactly one per table; the first rule that holds wins."),
        ("Complexity index", "Views 20, view columns 15, tables 20, table columns 15, volume files 10, table size 20; each "
         "on a log scale against the largest workspace shown.", "A rough size of what is held, not a quality score."),
        ("Usage index", "People 35, service principals 15, queries 30 (log scale); active days / window days 20.",
         "Steady use beats a burst."),
        ("Signal", "Usage under 10: Retire candidate. Complexity 50+ and usage under 50: Review. Complexity under 50: "
         "Light. Else Healthy.", "Bottom right on the scatter: built a lot, used little."),
        ("Sensitive column", "A column tag starting pii, sensitive, gdpr or class., else a column name such as email, "
         "phone, iban, birth.", "Values are never read."),
        ("Inactive", f"Not seen in the workspace for {wd.INACTIVE_DAYS}+ days within the last year.",
         "With a direct grant: easy access to remove."),
    ],
    "limits": [
        "Sizes and volume files come from the sizes job; tables it can't read show no size.",
        f"A catalog with no lineage in {wd.HOME_DAYS} days has no home workspace: No home workspace.",
        "Direct table grants only; schema, catalog and group grants aren't expanded. A group's grant reads never used.",
        "Data classification results aren't used, so sensitive columns come from tags and names only.",
        _REGION,
    ],
    "checks": [
        ("Which file-system actions and path keys your audit log has",
         "SELECT action_name, map_keys(request_params) AS keys, COUNT(*)\nFROM system.access.audit\n"
         "WHERE service_name = 'filesystem' AND event_date >= current_date() - 7\nGROUP BY 1, 2 ORDER BY 3 DESC;"),
        ("Tables the sizes job could not read",
         f"SELECT catalog, schema, table_name, detail\nFROM {wd._sized('table_sizes')}\nWHERE detail <> 'described';"),
    ],
}

SHORT["data_usage"] = {
    "how": [
        "Lineage gives each catalog its home workspace, and each table its reads, writes and last use.",
        "The class rules put every table and volume in one class; information_schema adds columns, tags and grants.",
        "Audit and query history give the people; the sizes job adds bytes and files.",
    ],
    "rules": ["Home workspace", "Usage class", "Complexity index", "Usage index", "Signal", "Sensitive column", "Inactive"],
    "sql": """-- Each table's usage class in the last 30 days, simplified.
WITH used AS (
  SELECT full_name, SUM(is_read) AS reads, SUM(is_write) AS writes FROM (
    SELECT source_table_full_name AS full_name, 1 AS is_read, 0 AS is_write FROM system.access.table_lineage
    WHERE event_date >= current_date() - 30
    UNION ALL
    SELECT target_table_full_name, 0, 1 FROM system.access.table_lineage WHERE event_date >= current_date() - 30
  ) GROUP BY 1
)
SELECT full_name,
       CASE WHEN writes > 0 AND reads = 0 THEN 'Written, never read'
            WHEN reads = 0 THEN 'Cooling or idle' ELSE 'Active' END AS usage_class
FROM used;""",
}

ER["data_usage"] = {
    "nodes": [
        ("lin", "system.access.table_lineage", "table", ["*workspace_id", "source / target table", "event_time"], 0),
        ("is", "system.information_schema", "table", ["*table_catalog", "tables, columns, tags, grants"], 0),
        ("sz", "table_sizes / volume_sizes (job)", "table", ["*catalog", "schema", "size_in_bytes", "files"], 0),
        ("aud", "system.access.audit", "table", ["*workspace_id", "user_identity", "event_time"], 0),
        ("qh", "system.query.history", "table", ["*workspace_id", "executed_by", "start_time"], 0),
        ("home", "home: catalog to workspace", "step", ["*catalog", "workspace_id"], 1),
        ("c", "c: every table, classed", "step", ["*full_name", "usage_class", "size", "sensitive"], 1),
        ("who", "people per workspace", "step", ["*workspace_id", "who", "kind", "last_seen"], 1),
        ("out", "workspaces, tables, volumes, people, details", "result",
         ["workspace", "usage_class", "unread_gb", "complexity_index", "usage_index", "signal"], 2),
    ],
    "edges": [
        ("lin", "home", "most reads and writes"), ("lin", "c", "reads, writes, last use"), ("home", "c", "ON catalog"),
        ("is", "c", "tables, tags"), ("sz", "c", "bytes, files"),
        ("aud", "who", "events"), ("qh", "who", "queries"),
        ("c", "out", "per workspace, per table"), ("who", "out", "LEFT JOIN ON workspace_id"),
    ],
}

# ------------------------------------------------------------------------------------------------
# Dashboard load
# ------------------------------------------------------------------------------------------------

BASICS["query_tags"] = ("Query tags", "Key-value labels set with SET QUERY_TAGS at the top of a query and stored "
                        "with it in system.query.history (query_tags). Every dataset here sets dashboard and tab.")

COLUMNS["dashboard_load"] = {
    "dashboard": "The dashboard, by title: query_tags['dashboard'] matched to the dashboards of this set.",
    "tab": "The page the query belongs to: query_tags['tab'].",
    "day": "The day the queries started (UTC).",
    "run_as": "Who ran them: the dashboard's owner when credentials are embedded, else the viewer.",
    "warehouse": "The SQL warehouse, by name.",
    "queries": "Queries in the row.",
    "failed": "Of them, failed.",
    "cached": "Of them, answered from the result cache: no compute.",
    "hours": "Their run time, summed, in hours.",
    "queued_hours": "Time they waited for a free slot on the warehouse, summed, in hours.",
    "slowest_s": "The longest single query in the row, in seconds.",
    "gb_read": "Data they read, in GB.",
}

WHY["dashboard_load"] = ("A dashboard's query text reads <REDACTED> in the query history, so only its tags tell its "
                         "queries apart. Per dashboard and tab it shows which page is slow, fails or queues, and "
                         "which warehouse carries the load.")

GUIDES["dashboard_load"] = {
    "purpose": "Shows what the dashboards of this set cost to run: their queries per dashboard, tab, day and "
               "warehouse, with failures, cache hits, run time and queue time, found by their query tags.",
    "basics": ["system_tables", "warehouse", "query_tags", "principal", "sql"],
    "rules": [
        ("Dashboard query", "A query-history row whose dashboard tag is one of this set's dashboards and that has a "
         "tab tag; SET statements left out.", "The SET QUERY_TAGS lines run as statements of their own but do no "
         "work."),
        ("Queued", "waiting_at_capacity_duration_ms: the query waited because the warehouse was full.",
         "Queue time on a dashboard means a bigger or a scaling warehouse, or fewer widgets per page."),
        ("From cache", "from_result_cache is true.", "A repeat view answered without compute."),
    ],
    "limits": [
        "Run time, not $: the warehouse's bill covers every query on it, and idle time too.",
        "Only queries since the dashboards carry tags; an older copy without SET QUERY_TAGS doesn't show.",
        "A dashboard renamed or added later shows only after this dashboard is rebuilt.",
        _REGION,
    ],
    "checks": [
        ("Which dashboard tags your query history has",
         "SELECT query_tags['dashboard'] AS dashboard, query_tags['tab'] AS tab, COUNT(*) AS queries\n"
         "FROM system.query.history\n"
         "WHERE query_tags['dashboard'] IS NOT NULL AND statement_type <> 'SET'\n"
         "  AND start_time >= current_date() - 7\nGROUP BY 1, 2 ORDER BY 3 DESC;"),
    ],
}

SHORT["dashboard_load"] = {
    "how": [
        "Every dataset of these dashboards starts with SET QUERY_TAGS: dashboard and tab.",
        "system.query.history stores the tags with each query, so they can be counted per dashboard and tab.",
        "Run time, queue time and failures per tab show which page to fix and which warehouse to size.",
    ],
    "rules": ["Dashboard query", "Queued", "From cache"],
    "sql": """-- Per dashboard and tab, last 7 days: queries, hours, queued hours.
SELECT query_tags['dashboard'] AS dashboard, query_tags['tab'] AS tab, COUNT(*) AS queries,
       ROUND(SUM(total_duration_ms) / 3600000, 2) AS hours,
       ROUND(SUM(waiting_at_capacity_duration_ms) / 3600000, 2) AS queued_hours
FROM system.query.history
WHERE query_tags['dashboard'] IS NOT NULL AND query_tags['tab'] IS NOT NULL
  AND statement_type <> 'SET' AND start_time >= current_date() - 7
GROUP BY 1, 2 ORDER BY hours DESC;""",
}

ER["dashboard_load"] = {
    "nodes": [
        ("qh", "system.query.history", "table",
         ["*compute.warehouse_id", "query_tags", "start_time", "total_duration_ms", "from_result_cache"], 0),
        ("boards", "boards: this set's dashboards", "table", ["*slug", "dashboard"], 0),
        ("q", "q: tagged queries in the window", "step", ["*slug", "tab", "day", "warehouse_id", "duration_ms"], 1),
        ("wh", "system.compute.warehouses", "table", ["*warehouse_id", "warehouse_name"], 1),
        ("out", "dashboard rows", "result", ["dashboard", "tab", "day", "warehouse", "queries", "hours",
                                              "queued_hours"], 2),
    ],
    "edges": [
        ("qh", "q", "dashboard and tab tags; not SET"),
        ("q", "out", "JOIN boards ON slug"),
        ("boards", "out", "the title"),
        ("wh", "out", "LEFT JOIN ON warehouse_id"),
    ],
}
