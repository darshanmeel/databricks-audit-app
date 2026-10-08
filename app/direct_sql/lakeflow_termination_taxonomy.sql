-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_termination_taxonomy.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_termination_taxonomy.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id, job_id, termination_code,
       COUNT(*)              AS run_rows,
       COUNT(DISTINCT run_id) AS distinct_runs
FROM `system`.`lakeflow`.`job_run_timeline`
WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
  AND result_state IS NOT NULL          -- end row only
  AND termination_code IS NOT NULL
GROUP BY workspace_id, job_id, termination_code
ORDER BY workspace_id, run_rows DESC
) q
