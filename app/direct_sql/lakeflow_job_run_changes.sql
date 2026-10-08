-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_run_changes.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_run_changes.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         NULLIF(queue_duration_seconds, 0) AS queue_s_reported,
         NULLIF(setup_duration_seconds, 0) AS setup_s_reported,
         NULLIF(run_duration_seconds, 0)   AS run_s_reported
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- end row only
),
run_span AS (
  -- the run's TRUE first observed slice, across every attempt's own rows
  SELECT workspace_id, job_id, run_id, MIN(period_start_time) AS run_start
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  GROUP BY workspace_id, job_id, run_id
),
run_final AS (
  -- one row per RUN, deduplicated to its final attempt (same convention as lakeflow_job_reliability)
  SELECT s.workspace_id, s.job_id, s.run_id, s.run_start, e.period_end_time AS run_end,
         e.result_state AS final_result_state, e.queue_s_reported, e.setup_s_reported,
         COALESCE(e.run_s_reported, timestampdiff(SECOND, s.run_start, e.period_end_time)) AS run_s
  FROM run_span s
  JOIN end_rows e
    ON  e.workspace_id = s.workspace_id AND e.job_id = s.job_id AND e.run_id = s.run_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY s.workspace_id, s.job_id, s.run_id ORDER BY e.period_end_time DESC
  ) = 1
),
run_cluster AS (
  -- first compute id of the run's latest-starting task (max_by picks it by period_start_time, not
  -- an arbitrary task's); NULL on serverless and pre-Dec-2025 rows
  SELECT workspace_id, job_id, job_run_id AS run_id,
         max_by(try_element_at(compute_ids, 1), period_start_time) AS cluster_id
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  GROUP BY workspace_id, job_id, job_run_id
),
ranked AS (
  SELECT f.*, c.cluster_id,
         ROW_NUMBER() OVER (
           PARTITION BY f.workspace_id, f.job_id ORDER BY f.run_start DESC, f.run_id DESC
         ) AS recency_rank
  FROM run_final f
  LEFT JOIN run_cluster c
    ON  c.workspace_id = f.workspace_id AND c.job_id = f.job_id AND c.run_id = f.run_id
),
job_p90 AS (
  SELECT workspace_id, job_id, percentile(run_s, 0.9) AS run_s_p90
  FROM ranked
  GROUP BY workspace_id, job_id
),
latest AS (
  SELECT * FROM ranked WHERE recency_rank = 1
),
candidate AS (
  -- the latest run either failed, or ran above the job's own p90 of every completed run
  SELECT l.workspace_id, l.job_id, l.run_id AS latest_run_id, l.run_start AS latest_start,
         l.cluster_id AS latest_cluster_id, l.recency_rank AS latest_rank,
         l.queue_s_reported AS latest_queue_s, l.setup_s_reported AS latest_setup_s,
         l.run_s AS latest_run_s
  FROM latest l
  JOIN job_p90 p ON p.workspace_id = l.workspace_id AND p.job_id = l.job_id
  WHERE l.final_result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
     OR (p.run_s_p90 IS NOT NULL AND l.run_s > p.run_s_p90)
),
baseline_pick AS (
  -- the nearest SUCCEEDED run strictly before the latest one; MIN(recency_rank) = most recent
  SELECT c.workspace_id, c.job_id, MIN(r.recency_rank) AS baseline_rank
  FROM candidate c
  JOIN ranked r
    ON  r.workspace_id = c.workspace_id AND r.job_id = c.job_id
    AND r.final_result_state = 'SUCCEEDED' AND r.recency_rank > c.latest_rank
  GROUP BY c.workspace_id, c.job_id
),
no_baseline AS (
  -- a candidate job with no SUCCEEDED run anywhere in the window: a marker row instead of no row
  -- at all, so the caller can tell "no successful run" apart from "not a candidate, nothing wrong"
  SELECT c.workspace_id, c.job_id, c.latest_run_id
  FROM candidate c
  LEFT JOIN baseline_pick bp ON bp.workspace_id = c.workspace_id AND bp.job_id = c.job_id
  WHERE bp.job_id IS NULL
),
baseline AS (
  SELECT r.workspace_id, r.job_id, r.run_id AS baseline_run_id, r.run_start AS baseline_start,
         r.cluster_id AS baseline_cluster_id
  FROM ranked r
  JOIN baseline_pick bp
    ON  bp.workspace_id = r.workspace_id AND bp.job_id = r.job_id AND bp.baseline_rank = r.recency_rank
),
good5 AS (
  -- the job's last 5 SUCCEEDED runs strictly before the latest run, for a robust duration baseline
  SELECT r.workspace_id, r.job_id, r.queue_s_reported, r.setup_s_reported, r.run_s,
         ROW_NUMBER() OVER (PARTITION BY r.workspace_id, r.job_id ORDER BY r.run_start DESC) AS good_rank
  FROM ranked r
  JOIN candidate c
    ON  c.workspace_id = r.workspace_id AND c.job_id = r.job_id
    AND r.final_result_state = 'SUCCEEDED' AND r.recency_rank > c.latest_rank
),
good5_median AS (
  SELECT workspace_id, job_id,
         percentile(queue_s_reported, 0.5) AS median_queue_s,
         percentile(setup_s_reported, 0.5) AS median_setup_s,
         percentile(run_s, 0.5)            AS median_run_s
  FROM good5
  WHERE good_rank <= 5
  GROUP BY workspace_id, job_id
),
pair AS (
  -- one row per candidate job that has a baseline (a job with none is left out - see caveats)
  SELECT c.workspace_id, c.job_id, c.latest_run_id, c.latest_start, c.latest_cluster_id,
         c.latest_queue_s, c.latest_setup_s, c.latest_run_s,
         b.baseline_run_id, b.baseline_start, b.baseline_cluster_id,
         g.median_queue_s, g.median_setup_s, g.median_run_s
  FROM candidate c
  JOIN baseline b ON b.workspace_id = c.workspace_id AND b.job_id = c.job_id
  LEFT JOIN good5_median g ON g.workspace_id = c.workspace_id AND g.job_id = c.job_id
),
run_points AS (
  -- the two points (baseline, latest) of every candidate job, unpivoted - every as-of join below
  -- runs once against this shared set instead of twice (once per point)
  SELECT workspace_id, job_id, 'baseline' AS which, baseline_run_id AS run_id,
         baseline_start AS run_start, baseline_cluster_id AS cluster_id
  FROM pair
  UNION ALL
  SELECT workspace_id, job_id, 'latest', latest_run_id, latest_start, latest_cluster_id
  FROM pair
),
cluster_asof AS (
  -- the system.compute.clusters row valid AT the run's own start (SCD2 as-of), not the cluster's
  -- current shape
  SELECT p.workspace_id, p.job_id, p.which, c.dbr_version, c.worker_node_type,
         CASE WHEN c.max_autoscale_workers IS NOT NULL
                   AND c.max_autoscale_workers <> COALESCE(c.min_autoscale_workers, c.max_autoscale_workers)
              THEN CONCAT(CAST(COALESCE(c.min_autoscale_workers, 0) AS STRING), '-', CAST(c.max_autoscale_workers AS STRING))
              WHEN c.worker_count IS NOT NULL THEN CAST(c.worker_count AS STRING)
              ELSE NULL END AS worker_shape
  FROM run_points p
  LEFT JOIN `system`.`compute`.`clusters` c
    ON  c.workspace_id = p.workspace_id AND c.cluster_id = p.cluster_id AND c.change_time <= p.run_start
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY p.workspace_id, p.job_id, p.which ORDER BY c.change_time DESC
  ) = 1
),
job_asof AS (
  -- the job definition's own SCD2 row valid AT the run's own start
  SELECT p.workspace_id, p.job_id, p.which, j.change_time AS job_version
  FROM run_points p
  LEFT JOIN `system`.`lakeflow`.`jobs` j
    ON  j.workspace_id = p.workspace_id AND j.job_id = p.job_id AND j.change_time <= p.run_start
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY p.workspace_id, p.job_id, p.which ORDER BY j.change_time DESC
  ) = 1
),
lineage_tables AS (
  -- this run's own distinct source tables (entity_run_id = the run's run_id); event_time bound to
  -- the check's own window so this scans recent lineage only, same as every other source here
  SELECT DISTINCT p.workspace_id, p.job_id, p.which,
         concat_ws('.', l.source_table_catalog, l.source_table_schema, l.source_table_name) AS source_table
  FROM run_points p
  JOIN `system`.`access`.`table_lineage` l
    ON  l.workspace_id = p.workspace_id AND l.entity_run_id = p.run_id
    AND l.event_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  WHERE l.source_table_full_name IS NOT NULL
),
lineage_asof AS (
  SELECT workspace_id, job_id, which,
         -- array_join (not concat_ws on an array -- DuckDB stringifies it as "[a, b]" instead of joining)
         array_join(array_sort(collect_set(source_table)), ', ') AS upstream
  FROM lineage_tables
  GROUP BY workspace_id, job_id, which
),
input_bytes_table AS (
  -- each source table's own latest snapshot at/before the run's date
  SELECT lt.workspace_id, lt.job_id, lt.which, m.active_bytes,
         ROW_NUMBER() OVER (
           PARTITION BY lt.workspace_id, lt.job_id, lt.which, lt.source_table ORDER BY m.snapshot_date DESC
         ) AS rn
  FROM lineage_tables lt
  JOIN run_points p ON p.workspace_id = lt.workspace_id AND p.job_id = lt.job_id AND p.which = lt.which
  JOIN `system`.`storage`.`table_metrics_history` m
    ON  m.catalog_name = split_part(lt.source_table, '.', 1)
    AND m.schema_name   = split_part(lt.source_table, '.', 2)
    AND m.table_name    = split_part(lt.source_table, '.', 3)
    AND m.snapshot_date <= date(p.run_start)
    AND m.snapshot_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
),
bytes_asof AS (
  SELECT workspace_id, job_id, which, SUM(active_bytes) AS input_bytes
  FROM input_bytes_table
  WHERE rn = 1
  GROUP BY workspace_id, job_id, which
),
cluster_wide AS (
  SELECT workspace_id, job_id,
         MAX(CASE WHEN which = 'baseline' THEN dbr_version END)     AS baseline_dbr_version,
         MAX(CASE WHEN which = 'latest'   THEN dbr_version END)     AS latest_dbr_version,
         MAX(CASE WHEN which = 'baseline' THEN worker_node_type END) AS baseline_worker_node_type,
         MAX(CASE WHEN which = 'latest'   THEN worker_node_type END) AS latest_worker_node_type,
         MAX(CASE WHEN which = 'baseline' THEN worker_shape END)     AS baseline_worker_shape,
         MAX(CASE WHEN which = 'latest'   THEN worker_shape END)     AS latest_worker_shape
  FROM cluster_asof
  GROUP BY workspace_id, job_id
),
job_wide AS (
  SELECT workspace_id, job_id,
         MAX(CASE WHEN which = 'baseline' THEN job_version END) AS baseline_job_version,
         MAX(CASE WHEN which = 'latest'   THEN job_version END) AS latest_job_version
  FROM job_asof
  GROUP BY workspace_id, job_id
),
lineage_wide AS (
  SELECT workspace_id, job_id,
         MAX(CASE WHEN which = 'baseline' THEN upstream END) AS baseline_upstream,
         MAX(CASE WHEN which = 'latest'   THEN upstream END) AS latest_upstream
  FROM lineage_asof
  GROUP BY workspace_id, job_id
),
bytes_wide AS (
  SELECT workspace_id, job_id,
         MAX(CASE WHEN which = 'baseline' THEN input_bytes END) AS baseline_input_bytes,
         MAX(CASE WHEN which = 'latest'   THEN input_bytes END) AS latest_input_bytes
  FROM bytes_asof
  GROUP BY workspace_id, job_id
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
base AS (
  SELECT p.workspace_id, p.job_id, jn.job_name, p.baseline_run_id, p.latest_run_id,
         cw.baseline_dbr_version, cw.latest_dbr_version,
         cw.baseline_worker_node_type, cw.latest_worker_node_type,
         cw.baseline_worker_shape, cw.latest_worker_shape,
         jw.baseline_job_version, jw.latest_job_version,
         lw.baseline_upstream, lw.latest_upstream,
         bw.baseline_input_bytes, bw.latest_input_bytes,
         p.median_queue_s, p.latest_queue_s, p.median_setup_s, p.latest_setup_s,
         p.median_run_s, p.latest_run_s
  FROM pair p
  LEFT JOIN cluster_wide cw ON cw.workspace_id = p.workspace_id AND cw.job_id = p.job_id
  LEFT JOIN job_wide jw     ON jw.workspace_id = p.workspace_id AND jw.job_id = p.job_id
  LEFT JOIN lineage_wide lw ON lw.workspace_id = p.workspace_id AND lw.job_id = p.job_id
  LEFT JOIN bytes_wide bw   ON bw.workspace_id = p.workspace_id AND bw.job_id = p.job_id
  LEFT JOIN latest_jobs jn  ON jn.workspace_id = p.workspace_id AND jn.job_id = p.job_id
),
changes AS (
SELECT workspace_id, job_id, job_name, 'job_definition' AS attribute, baseline_run_id, latest_run_id,
       'prior_success' AS baseline_kind,
       CAST(baseline_job_version AS STRING) AS baseline_value, CAST(latest_job_version AS STRING) AS latest_value,
       (baseline_job_version IS NOT NULL AND latest_job_version IS NOT NULL
        AND baseline_job_version <> latest_job_version) AS changed
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'runtime_version', baseline_run_id, latest_run_id, 'prior_success',
       baseline_dbr_version, latest_dbr_version,
       (baseline_dbr_version IS NOT NULL AND latest_dbr_version IS NOT NULL
        AND baseline_dbr_version <> latest_dbr_version)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'worker_node_type', baseline_run_id, latest_run_id, 'prior_success',
       baseline_worker_node_type, latest_worker_node_type,
       (baseline_worker_node_type IS NOT NULL AND latest_worker_node_type IS NOT NULL
        AND baseline_worker_node_type <> latest_worker_node_type)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'worker_count', baseline_run_id, latest_run_id, 'prior_success',
       baseline_worker_shape, latest_worker_shape,
       (baseline_worker_shape IS NOT NULL AND latest_worker_shape IS NOT NULL
        AND baseline_worker_shape <> latest_worker_shape)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'upstream_tables', baseline_run_id, latest_run_id, 'prior_success',
       baseline_upstream, latest_upstream,
       (baseline_upstream IS NOT NULL AND latest_upstream IS NOT NULL AND baseline_upstream <> latest_upstream)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'input_bytes', baseline_run_id, latest_run_id, 'prior_success',
       CAST(baseline_input_bytes AS STRING), CAST(latest_input_bytes AS STRING),
       (baseline_input_bytes IS NOT NULL AND latest_input_bytes IS NOT NULL
        AND baseline_input_bytes <> latest_input_bytes)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'queue_s', baseline_run_id, latest_run_id, 'prior_success',
       CAST(ROUND(median_queue_s, 0) AS STRING), CAST(latest_queue_s AS STRING),
       (median_queue_s IS NOT NULL AND latest_queue_s IS NOT NULL AND ROUND(median_queue_s, 0) <> latest_queue_s)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'setup_s', baseline_run_id, latest_run_id, 'prior_success',
       CAST(ROUND(median_setup_s, 0) AS STRING), CAST(latest_setup_s AS STRING),
       (median_setup_s IS NOT NULL AND latest_setup_s IS NOT NULL AND ROUND(median_setup_s, 0) <> latest_setup_s)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'run_s', baseline_run_id, latest_run_id, 'prior_success',
       CAST(ROUND(median_run_s, 0) AS STRING), CAST(latest_run_s AS STRING),
       (median_run_s IS NOT NULL AND latest_run_s IS NOT NULL AND ROUND(median_run_s, 0) <> latest_run_s)
FROM base
UNION ALL
SELECT nb.workspace_id, nb.job_id, jn.job_name, CAST(NULL AS STRING) AS attribute,
       CAST(NULL AS STRING) AS baseline_run_id, nb.latest_run_id,
       'no_success_in_window' AS baseline_kind,
       CAST(NULL AS STRING) AS baseline_value, CAST(NULL AS STRING) AS latest_value,
       CAST(NULL AS BOOLEAN) AS changed
FROM no_baseline nb
LEFT JOIN latest_jobs jn ON jn.workspace_id = nb.workspace_id AND jn.job_id = nb.job_id
)
SELECT * FROM changes
ORDER BY changed DESC, workspace_id, job_id,
         CASE attribute
           WHEN 'job_definition'    THEN 0
           WHEN 'runtime_version'   THEN 1
           WHEN 'worker_node_type'  THEN 2
           WHEN 'worker_count'      THEN 3
           WHEN 'upstream_tables'   THEN 4
           WHEN 'input_bytes'       THEN 5
           WHEN 'queue_s'           THEN 6
           WHEN 'setup_s'           THEN 7
           ELSE 8
         END
) q
