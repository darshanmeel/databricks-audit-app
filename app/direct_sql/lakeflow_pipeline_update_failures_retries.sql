-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_pipeline_update_failures_retries.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipeline_update_failures_retries.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, pipeline_id, update_id, request_id, update_type,
         trigger_type, result_state, period_start_time, period_end_time
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
    AND result_state IS NOT NULL          -- end row only for updates >1h
)
SELECT e.workspace_id, e.pipeline_id, e.update_type, e.trigger_type, e.result_state,
       COUNT(DISTINCT e.update_id) AS updates,
       SUM(CASE WHEN e.result_state = 'FAILED'             THEN 1 ELSE 0 END) AS failed_update_rows,
       SUM(CASE WHEN e.trigger_type = 'RETRY_ON_FAILURE'   THEN 1 ELSE 0 END) AS retry_triggered_rows,
       -- status: worst-first band on failed update rows per group (field heuristic; 3 / 10).
       CASE
         WHEN SUM(CASE WHEN e.result_state = 'FAILED' THEN 1 ELSE 0 END) >= 10 THEN 'CRITICAL'
         WHEN SUM(CASE WHEN e.result_state = 'FAILED' THEN 1 ELSE 0 END) >= 3 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM end_rows e
GROUP BY e.workspace_id, e.pipeline_id, e.update_type, e.trigger_type, e.result_state
ORDER BY failed_update_rows DESC
) q
