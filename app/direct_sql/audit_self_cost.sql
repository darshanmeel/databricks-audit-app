-- generated from dbt/models/databricks_direct/performance/d_audit_self_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/audit_self_cost.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT
    workspace_id,
    statement_type,
    COUNT(*)                                   AS query_count,
    SUM(total_duration_ms) / 1000.0            AS total_duration_secs,
    SUM(COALESCE(total_task_duration_ms, 0)) / 1000.0 AS total_task_secs,
    COUNT(DISTINCT executed_by)                AS distinct_principals,
    MIN(start_time)                            AS first_query_time,
    MAX(start_time)                            AS last_query_time
FROM `system`.`query`.`history`
WHERE start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  -- '_' is a LIKE/ILIKE wildcard (matches any single character) - escape it with ! via
  -- ESCAPE '!' (a backslash escape does not parse as Databricks SQL: Spark's string-literal
  -- lexer treats '\' inside a string literal as its own escape, so ESCAPE '\' is invalid there;
  -- '!' works identically on both DuckDB and Databricks SQL) so the marker only matches the
  -- literal text "databricks_audit", never "databricksXaudit" for any character X.
  AND statement_text ILIKE '%databricks!_audit%' ESCAPE '!'
GROUP BY workspace_id, statement_type
ORDER BY workspace_id, statement_type
) q
