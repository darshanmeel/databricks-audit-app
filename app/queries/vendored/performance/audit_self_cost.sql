-- query_id: audit_self_cost
-- title: Cost of running this audit itself
-- domain: performance   tier: lite
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: Measured magnitude - query count and runtime are directly observable columns in system.query.history; Databricks exposes no per-query DBU column, so the dollar cost of running this audit stays "not assessed" by design, not by omission.
-- read_this: One row = a workspace + statement type describing how many queries this audit itself issued and how long they ran. The columns that matter are query_count and total_duration_secs - how much of your own query-history footprint running this audit adds.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: query_costly_statements (if you want to see the audit's own queries ranked individually), query_workload_mix_hours (to see how the audit's load compares to the rest of your workload)
-- caveats: This is the INVERSE of self-exclusion - it deliberately INCLUDES this audit's own queries so you can see what running it cost. Queries are matched by a text marker embedded in every query this audit issues, so this only catches queries whose statement_text contains that marker; anything that strips or rewrites statement_text before it reaches system tables will fall out of this count. The marker is matched literally: '_' is a LIKE/ILIKE wildcard (matches any single character), so the '_' inside "databricks_audit" is escaped with ! via ESCAPE '!' below (a backslash escape does NOT parse on Databricks SQL - Spark's string-literal lexer treats a backslash inside a string literal as its own escape, so ESCAPE '\' is an unterminated literal there; '!' is an ordinary character on both engines, so it works identically on DuckDB and Databricks SQL) - without it, statement_text could match on an unrelated character in that position and over-count (a coincidental match). This app issues no such marker itself (see tools/snapshot.py) - a marker-embedding convention is the caller's own contract to fulfil if they want this query populated. system.query.history has short ingest latency, so the most recent minutes of a run still in flight may not be reflected yet - the totals here are cumulative across every audit run inside :period_days, not just the latest one. There is no upper bound on start_time, so today's already-landed queries are included even while the run continues. Databricks does not expose a per-query DBU column, so this reports COUNT and runtime (magnitude) only - the dollar cost is "not assessed" unless you separately supply a DBU rate.
-- system.query.history only captures queries run on SQL warehouses or serverless compute; if this audit executes on a classic all-purpose or job cluster its queries are never recorded here, so the count would understate or entirely miss the audit's own footprint.
SELECT
    workspace_id,
    statement_type,
    COUNT(*)                                   AS query_count,
    SUM(total_duration_ms) / 1000.0            AS total_duration_secs,
    SUM(COALESCE(total_task_duration_ms, 0)) / 1000.0 AS total_task_secs,
    COUNT(DISTINCT executed_by)                AS distinct_principals,
    MIN(start_time)                            AS first_query_time,
    MAX(start_time)                            AS last_query_time
FROM system.query.history
WHERE start_time >= dateadd(day, -:period_days, current_date())
  -- '_' is a LIKE/ILIKE wildcard (matches any single character) - escape it with ! via
  -- ESCAPE '!' (a backslash escape does not parse as Databricks SQL: Spark's string-literal
  -- lexer treats '\' inside a string literal as its own escape, so ESCAPE '\' is invalid there;
  -- '!' works identically on both DuckDB and Databricks SQL) so the marker only matches the
  -- literal text "databricks_audit", never "databricksXaudit" for any character X.
  AND statement_text ILIKE '%databricks!_audit%' ESCAPE '!'
GROUP BY workspace_id, statement_type
ORDER BY workspace_id, statement_type
