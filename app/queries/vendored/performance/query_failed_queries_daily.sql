-- query_id: query_failed_queries_daily
-- title: Failed and canceled queries by day
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days; :warn_failed_count (default 5) failed/canceled query count per day+warehouse+statement_type that flags WARN; :crit_failed_count (default 20) ... that flags CRITICAL
-- confidence: confirmed
-- confidence_note: The execution_status enum (FINISHED/FAILED/CANCELED) and error_message behavior under customer-managed keys were verified against system.query.history in a live workspace.
-- read_this: One row = a day + warehouse + status + statement_type of failed or canceled queries, broken out by who ran them. The columns that matter are query_count (how many failed or were canceled that day) and error_message_sample (a de-identified shape of the most recent error) - a repeated high count on the same warehouse/statement_type points to a systemic issue (bad credentials, a broken job, schema drift), not one-off flakiness.
-- healthy: query_count below :warn_failed_count per day+warehouse+statement_type (field heuristic - tune :warn_failed_count for your account).
-- investigate_if: query_count at/above :warn_failed_count (WARN) or :crit_failed_count (CRITICAL) - field heuristic; a spike concentrated on one warehouse or statement_type is the real signal.
-- actions: 1) read error_message_sample for the shape of the failure and check the source job/notebook logs for the un-redacted detail (free); 2) fix the query, permission, or schema issue it points to, or add a retry/backoff on the calling job (config); 3) if failures cluster around an unstable warehouse (capacity errors), resize it or move the workload to a more resilient one (spend).
-- next: query_costly_statements (if you want the individual failed statements), query_queuing_waits (if failures cluster with queuing or cold-start)
-- caveats: execution_status here is FINISHED/FAILED/CANCELED - only FAILED and CANCELED rows are included, so a spike in CANCELED does not necessarily mean anything broke, it may mean users are cancelling long-running queries. error_message is empty under customer-managed keys (CMK), so MAX() returns blank for those rows even though a failure happened - read a blank error_message_sample as "redacted by your key policy," not "no detail available." Some environments still need the query-history schema explicitly enabled by an account admin before rows appear here - if this returns zero rows and you know queries have failed, confirm system.query.history is enabled for your metastore before concluding there were no failures. This table is regional. Classic/all-purpose clusters are not captured here - only SQL-warehouse and serverless statements are.
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       execution_status, statement_type,
       CASE
         WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
         WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
         ELSE concat(COALESCE(MAX(executed_by_user_id), substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***')
       END AS executed_by,
       COUNT(*) AS query_count,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       -- error text de-valued at source: strip emails, then single-quoted string literals (chr(39) is
       -- the single quote) - keeps the error SHAPE, drops literal data values.
       regexp_replace(
         regexp_replace(MAX(error_message), '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
         concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
       )                          AS error_message_sample,
       -- status: worst-first band on daily failed/canceled query count (field heuristic; :warn_failed_count / :crit_failed_count).
       CASE
         WHEN COUNT(*) >= :crit_failed_count THEN 'CRITICAL'
         WHEN COUNT(*) >= :warn_failed_count THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL :period_days DAYS
  AND start_time < current_date()
  AND execution_status IN ('FAILED','CANCELED')
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, execution_status, statement_type, executed_by
ORDER BY query_count DESC
