-- query_id: query_failed_statements_grouped
-- title: Failed and canceled statements grouped by error and source
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days; :warn_statements (default 10) FAILED
--   statements in one group at/above which it reads WARN; :crit_statements (default 50) the same
--   that reads CRITICAL
-- confidence: needs_confirmation
-- confidence_note: execution_status and error_message are already read this way by
--   query_failed_queries_daily; the leading [ERROR_CLASS] extraction and the source_kind/source_id
--   grouping (query_top_by_cost's own query_source precedence) are new. Confirm on your account:
--   1) a warehouse you know has failing jobs shows a matching statements count and a readable
--   error_class; 2) a canceled statement never counts toward status.
-- read_this: One row = one warehouse's FAILED or CANCELED statements in the window that share an
--   error_class and a source. statements is how many; first_seen/last_seen bound them;
--   sample_statement_id is the group's most recent statement; error_message_sample is a
--   de-identified shape of that sample's error. source_kind/source_id name the dashboard, Genie
--   space, alert, job, notebook, saved query or pipeline the statements came from, NULL for
--   ad-hoc SQL with no captured source.
-- healthy: status = OK - a CANCELED group (a cancel is not a failure), or a FAILED group under
--   :warn_statements.
-- investigate_if: status = WARN or CRITICAL - a FAILED group at/above :warn_statements (WARN) or
--   :crit_statements (CRITICAL) - field heuristic; a high count sharing one error_class and source
--   points at one systemic cause, not scattered flakiness.
-- actions: 1) read error_message_sample for the shape of the failure and check the source job,
--   notebook or dashboard for the un-redacted detail (free); 2) fix the query, permission or
--   schema problem error_class points to, or add a retry/backoff on the calling job (config);
--   3) if failures cluster on one warehouse, check whether it is undersized or out of capacity
--   for the source's load (spend).
-- next: query_failed_queries_daily (the same failures by day, user and error), query_top_by_cost
--   (the same source's cost, if it also runs successfully elsewhere)
-- caveats: FAILED and CANCELED only - a CANCELED group always reads OK (canceling a runaway query
--   is often the fix, not the problem) but is still shown, so a "cancel storm" stays visible even
--   though it never flags. error_class is the leading [ERROR_CLASS] token of error_message
--   (`^\[([A-Za-z0-9_.]+)\]`, built via concat/chr so Java regex on Databricks does not read the
--   literal `[` as a nested character class); a message with no such prefix, or a blank error_message (redacted
--   under customer-managed keys - see query_failed_queries_daily), reads 'unclassified'.
--   source_kind/source_id follow query_top_by_cost's own query_source precedence (saved query,
--   dashboard, legacy dashboard, Genie space, alert, job, notebook, pipeline), NULL for ad-hoc SQL
--   with no captured source. error_message_sample is de-valued the same way as
--   query_failed_queries_daily (emails and single-quoted literals stripped) and taken from the
--   group's most recent statement (sample_statement_id). Regional (query.history). No identities
--   are emitted.
WITH stmts AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, q.statement_id,
         q.execution_status, q.error_message, q.query_source, q.start_time
  FROM system.query.history q
  WHERE q.start_time >= current_date() - INTERVAL :period_days DAYS
    AND q.start_time < current_date()
    AND q.execution_status IN ('FAILED', 'CANCELED')
),
classed AS (
  SELECT s.*,
         -- leading [ERROR_CLASS] token: pattern built via chr(92) (backslash), not a literal
         -- '[[]...[]]' char-class - Java regex (Databricks) reads a literal '[' inside a class as
         -- opening a nested class and throws "Unclosed character class".
         CASE
           WHEN s.error_message IS NULL OR s.error_message = '' THEN 'unclassified'
           WHEN regexp_extract(s.error_message, concat('^', chr(92), '[([A-Za-z0-9_.]+)', chr(92), ']'), 1) = '' THEN 'unclassified'
           ELSE regexp_extract(s.error_message, concat('^', chr(92), '[([A-Za-z0-9_.]+)', chr(92), ']'), 1)
         END AS error_class,
         CASE
           WHEN s.query_source.sql_query_id IS NOT NULL              THEN 'saved query'
           WHEN s.query_source.dashboard_id IS NOT NULL              THEN 'dashboard'
           WHEN s.query_source.legacy_dashboard_id IS NOT NULL       THEN 'dashboard'
           WHEN s.query_source.genie_space_id IS NOT NULL            THEN 'Genie space'
           WHEN s.query_source.alert_id IS NOT NULL                  THEN 'alert'
           WHEN s.query_source.job_info.job_id IS NOT NULL           THEN 'job'
           WHEN s.query_source.notebook_id IS NOT NULL               THEN 'notebook'
           WHEN s.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
         END AS source_kind,
         CASE
           WHEN s.query_source.sql_query_id IS NOT NULL              THEN s.query_source.sql_query_id
           WHEN s.query_source.dashboard_id IS NOT NULL              THEN s.query_source.dashboard_id
           WHEN s.query_source.legacy_dashboard_id IS NOT NULL       THEN s.query_source.legacy_dashboard_id
           WHEN s.query_source.genie_space_id IS NOT NULL            THEN s.query_source.genie_space_id
           WHEN s.query_source.alert_id IS NOT NULL                  THEN s.query_source.alert_id
           WHEN s.query_source.job_info.job_id IS NOT NULL           THEN s.query_source.job_info.job_id
           WHEN s.query_source.notebook_id IS NOT NULL               THEN s.query_source.notebook_id
           WHEN s.query_source.pipeline_info.pipeline_id IS NOT NULL THEN s.query_source.pipeline_info.pipeline_id
         END AS source_id
  FROM stmts s
),
sample AS (
  -- one representative statement per group: its most recent
  SELECT workspace_id, warehouse_id, execution_status, error_class, source_kind, source_id,
         statement_id AS sample_statement_id,
         regexp_replace(
           regexp_replace(error_message, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
           concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
         ) AS error_message_sample
  FROM classed
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, execution_status, error_class,
                                          source_kind, source_id
                             ORDER BY start_time DESC, statement_id DESC) = 1
)
SELECT c.workspace_id, c.warehouse_id, c.execution_status, c.error_class, c.source_kind, c.source_id,
       COUNT(*) AS statements,
       MIN(c.start_time) AS first_seen,
       MAX(c.start_time) AS last_seen,
       sm.sample_statement_id,
       sm.error_message_sample,
       CASE
         WHEN c.execution_status = 'CANCELED'  THEN 'OK'
         WHEN COUNT(*) >= :crit_statements     THEN 'CRITICAL'
         WHEN COUNT(*) >= :warn_statements     THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM classed c
JOIN sample sm
  ON  sm.workspace_id = c.workspace_id AND sm.warehouse_id IS NOT DISTINCT FROM c.warehouse_id
  AND sm.execution_status = c.execution_status AND sm.error_class = c.error_class
  AND sm.source_kind IS NOT DISTINCT FROM c.source_kind
  AND sm.source_id   IS NOT DISTINCT FROM c.source_id
GROUP BY c.workspace_id, c.warehouse_id, c.execution_status, c.error_class, c.source_kind, c.source_id,
         sm.sample_statement_id, sm.error_message_sample
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, statements DESC
