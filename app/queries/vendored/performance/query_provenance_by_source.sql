-- query_id: query_provenance_by_source
-- title: Query volume and duration by source (job, dashboard, notebook, ad-hoc)
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap, privilege_scoped
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: The nested query_source struct's dotted-path access (query_source.job_info.job_id, query_source.dashboard_id, etc.) and the single-winner CASE precedence used to pick one source_kind per query are not verified verbatim against a live workspace. The subfield names themselves are confirmed, but Databricks' own documentation says multiple subfields can be populated on the same query simultaneously and are not execution-ordered, so this CASE is a heuristic, not authoritative attribution. Confirm the struct path resolves in your workspace, and consider emitting all non-null source flags instead of a single winner if you need exact attribution.
-- read_this: One row = a workspace + compute + identity + source_kind (job, dashboard, legacy_dashboard, notebook, alert, genie, sql_editor, or other) describing where queries came from. The columns that matter are source_kind (which surface issued the query) and query_count - a workspace dominated by "other" or split evenly between ad-hoc (sql_editor/notebook) and scheduled (job) sources tells you whether spend is coming from production pipelines or exploratory work.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: query_workload_mix_hours (for the same volume broken out by hour-of-day instead of source), query_per_query_estimate_lane (to turn this volume into a cost estimate)
-- caveats: Each query_source subfield is NULL when that entity was not involved in the query, so a row's source_kind reflects only the first non-null subfield in the CASE precedence order above (job, then dashboard, then legacy_dashboard, then notebook, then alert, then genie, then sql_editor) - not necessarily the only surface involved in that query. Provenance only covers Databricks-native entities (jobs, dashboards, notebooks, alerts, Genie spaces, the SQL editor); anything issuing SQL from outside these (a third-party BI tool, a raw API call) will show up as "other." If your workspace rejects grouping by a doubly-nested subfield like job_info.job_id, extract it into a scalar in a subquery before grouping. executed_by is masked in-SQL to DEC-66.3's identity format (executed_by_user_id, or when that is NULL the first 8 hex chars of sha2(lower(trim(executed_by)), 256), + a space + the first 2 chars of the raw identity + '***'; NULL/'__REDACTED__' and a service-principal GUID are kept as-is); identity_type is a coarse user/service_principal/unknown split derived from the raw identity, not a verified directory lookup. If you need exact run-as attribution for service principals, also check executed_as / executed_as_user_id, which this query does not select.
-- NEEDS CONFIRMATION: nested-struct dotted-path access + CASE attribution precedence are UNVERIFIED.
-- Confirm the struct path resolves and decide whether to emit ALL non-null source flags
-- rather than a single CASE winner (simultaneous population is documented).
-- system.query.history only captures queries run on SQL warehouses or serverless compute, so any job, notebook, or ad-hoc query executed on classic all-purpose or job clusters is entirely absent here, systematically undercounting those source_kinds.
SELECT workspace_id,
       compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       CASE WHEN executed_by IS NULL              THEN 'unknown'
            WHEN executed_by LIKE '%@%'           THEN 'user'
            ELSE 'service_principal' END AS identity_type,
       CASE WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
            WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
            ELSE concat(COALESCE(executed_by_user_id, substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***') END AS executed_by,
       -- same precedence as query_top_by_cost, so a statement with several origins (an alert run
       -- by a job) gets one label on every page
       CASE WHEN query_source.sql_query_id        IS NOT NULL THEN 'sql_editor'
            WHEN query_source.dashboard_id        IS NOT NULL THEN 'dashboard'
            WHEN query_source.legacy_dashboard_id IS NOT NULL THEN 'legacy_dashboard'
            WHEN query_source.genie_space_id      IS NOT NULL THEN 'genie'
            WHEN query_source.alert_id            IS NOT NULL THEN 'alert'
            WHEN query_source.job_info.job_id     IS NOT NULL THEN 'job'
            WHEN query_source.notebook_id         IS NOT NULL THEN 'notebook'
            ELSE 'other' END AS source_kind,
       query_source.job_info.job_id AS job_id,
       query_source.dashboard_id    AS dashboard_id,
       query_source.notebook_id     AS notebook_id,
       COUNT(*) AS query_count,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(read_bytes)            AS read_bytes_sum
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL :period_days DAYS
  AND start_time < current_date()
GROUP BY workspace_id, compute.type, compute.warehouse_id, source_kind,
         CASE WHEN executed_by IS NULL              THEN 'unknown'
              WHEN executed_by LIKE '%@%'           THEN 'user'
              ELSE 'service_principal' END,
         CASE WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
              WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
              ELSE concat(COALESCE(executed_by_user_id, substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***') END,
         query_source.job_info.job_id, query_source.dashboard_id, query_source.notebook_id
ORDER BY workspace_id, compute_type, source_kind
