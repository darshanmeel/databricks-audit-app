{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:performance', 'tier:standard', 'drilldown', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_task_statement_breakdown.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH run_agg AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)    AS run_start,
         MAX(period_end_time)      AS last_seen,
         MAX(result_state)         AS result_state,       -- NULL until the run's end row lands
         -- end row only; NULL before Dec 2025 and 0 for every multi-task job (doc), so 0 = not reported
         NULLIF(MAX(run_duration_seconds), 0) AS run_s_reported
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
  GROUP BY workspace_id, job_id, run_id
),
run_obs AS (
  SELECT workspace_id, job_id, run_id, run_start, result_state,
         (result_state IS NULL) AS in_flight,
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, run_start,
                                CASE WHEN result_state IS NULL THEN {{ audit_now() }}
                                     ELSE last_seen END)) AS run_s
  FROM run_agg
),
long_runs AS (
  -- the same run bound as lakeflow_long_running_runs, so the two queries compose
  SELECT workspace_id, job_id, run_id, run_start, in_flight, run_s
  FROM run_obs
  WHERE run_s >= {{ param('query_task_statement_breakdown', 'warn_run_hours', 2) }} * 3600
     OR run_s IS NULL
),
task_agg AS (
  -- one row per TASK RUN (run_id here is the task run id; job_run_id is the parent run)
  SELECT t.workspace_id, t.job_id, t.job_run_id,
         t.run_id                                          AS task_run_id,
         t.task_key,
         MIN(t.period_start_time)                          AS task_start,
         MAX(t.period_end_time)                            AS task_last_seen,
         MAX(t.result_state)                               AS task_result_state,
         NULLIF(MAX(t.execution_duration_seconds), 0)      AS task_exec_s,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(t.compute_ids, 1))             AS task_cluster_id,
         MAX(try_element_at(t.compute, 1).type)            AS task_compute_type_raw,
         MAX(try_element_at(t.compute, 1).warehouse_id)    AS task_warehouse_id
  FROM {{ source('system_lakeflow', 'job_task_run_timeline') }} t
  JOIN long_runs r
    ON  r.workspace_id = t.workspace_id
    AND r.job_id       = t.job_id
    AND r.run_id       = t.job_run_id
  WHERE t.period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
  GROUP BY t.workspace_id, t.job_id, t.job_run_id, t.run_id, t.task_key
),
known_clusters AS (
  -- system.compute.clusters holds classic (all-purpose, job, pipeline) clusters only
  SELECT DISTINCT workspace_id, cluster_id
  FROM {{ source('system_compute', 'clusters') }}
),
task_obs AS (
  SELECT a.workspace_id, a.job_id, a.job_run_id, a.task_run_id, a.task_key,
         a.task_start, a.task_last_seen, a.task_result_state, a.task_cluster_id, a.task_compute_type_raw,
         COALESCE(a.task_exec_s,
                  timestampdiff(SECOND, a.task_start,
                                CASE WHEN a.task_result_state IS NULL THEN {{ audit_now() }}
                                     ELSE a.task_last_seen END))     AS task_s,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (a.task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (a.task_result_state IS NOT NULL AND a.task_start = a.task_last_seen)) AS task_not_executed,
         -- in flight, or ended so recently that its statements may not have landed yet
         (a.task_result_state IS NULL
          OR a.task_last_seen >= dateadd(hour, -{{ param('query_task_statement_breakdown', 'settle_hours', 1) }}, {{ audit_now() }})) AS sql_may_be_incomplete,
         -- structural compute classification; see caveats
         CASE
           WHEN a.task_cluster_id IS NOT NULL AND k.cluster_id IS NOT NULL THEN 'CLASSIC_CLUSTER'
           WHEN a.task_warehouse_id IS NOT NULL                             THEN 'SQL_WAREHOUSE'
           WHEN a.task_cluster_id IS NULL                                   THEN 'SERVERLESS_OR_UNRECORDED'
           ELSE 'UNRESOLVED'
         END                                                          AS task_compute_kind
  FROM task_agg a
  LEFT JOIN known_clusters k
    ON  a.workspace_id   = k.workspace_id
    AND a.task_cluster_id = k.cluster_id
),
stmts AS (
  -- the nested query_source path is extracted to scalars HERE, before any join or GROUP BY;
  -- the semi-joins keep the scan to the long runs' own statements
  SELECT workspace_id, statement_id, statement_type, execution_status,
         query_source.job_info.job_id          AS src_job_id,
         query_source.job_info.job_run_id      AS src_job_run_id,
         query_source.job_info.job_task_run_id AS src_task_run_id,
         compute.type                          AS stmt_compute_type,
         start_time, end_time,
         total_duration_ms, execution_duration_ms, compilation_duration_ms,
         waiting_for_compute_duration_ms, waiting_at_capacity_duration_ms, total_task_duration_ms,
         read_bytes, spilled_local_bytes, shuffle_read_bytes,
         (statement_text IS NULL OR statement_text = '<REDACTED>') AS text_redacted,
         -- statement_text de-valued exactly as query_costly_statements does: strip emails, then
         -- replace every single-quoted literal with '?' (chr(39) is the single quote)
         regexp_replace(
           regexp_replace(statement_text, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
           concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
         )                                     AS statement_text_devalued
  FROM {{ source('system_query', 'history') }}
  WHERE start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND (query_source.job_info.job_task_run_id IN (SELECT task_run_id FROM task_obs)
         OR query_source.job_info.job_run_id   IN (SELECT run_id FROM long_runs))
),
stmt_fp AS (
  SELECT s.*,
         -- the recipe shared with query_costly_statements (string literals only): for lookups
         sha2(s.statement_text_devalued, 256) AS statement_fingerprint,
         -- the grouping key: bare numbers masked too, and runs of ?, ?, ? collapsed to one ?;
         -- with <REDACTED> text no shape exists, so each statement is its own key
         CASE WHEN s.text_redacted THEN concat('statement:', s.statement_id)
              ELSE sha2(
                     regexp_replace(
                       regexp_replace(s.statement_text_devalued,
                                      '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '$1?'),
                       '[?]( *, *[?])+', '?'),
                     256)
         END                                   AS shape_fingerprint,
         -- statements with no task run id are bucketed per run
         CASE WHEN s.src_task_run_id IS NULL THEN s.src_job_run_id END AS orphan_job_run_id
  FROM stmts s
),
stmt_shape AS (
  -- one row per (task run OR run bucket, shape)
  SELECT workspace_id, src_task_run_id, orphan_job_run_id, shape_fingerprint,
         MAX(src_job_id)                                             AS src_job_id,
         MAX(src_job_run_id)                                         AS src_job_run_id,
         MAX(text_redacted)                                          AS text_redacted,
         COUNT(*)                                                    AS statement_count,
         SUM(CASE WHEN execution_status <> 'FINISHED' THEN 1 ELSE 0 END) AS statements_not_finished,
         COUNT(DISTINCT statement_fingerprint)                       AS literal_variants_in_shape,
         MIN(statement_fingerprint)                                  AS statement_fingerprint,
         MIN(statement_type)                                         AS statement_type,
         MIN(statement_id)                                           AS sample_statement_id,
         MIN(stmt_compute_type)                                      AS stmt_compute_type,
         MIN(start_time)                                             AS first_start_time,
         MAX(end_time)                                               AS last_end_time,
         SUM(total_duration_ms)                                      AS total_ms,
         SUM(execution_duration_ms)                                  AS execution_ms,
         SUM(compilation_duration_ms)                                AS compilation_ms,
         SUM(waiting_for_compute_duration_ms)                        AS waiting_for_compute_ms,
         SUM(waiting_at_capacity_duration_ms)                        AS waiting_at_capacity_ms,
         SUM(total_task_duration_ms)                                 AS total_task_ms,
         SUM(read_bytes)                                             AS read_bytes,
         SUM(spilled_local_bytes)                                    AS spilled_local_bytes,
         SUM(shuffle_read_bytes)                                     AS shuffle_read_bytes,
         MIN(statement_text_devalued)                                AS statement_text_devalued
  FROM stmt_fp
  GROUP BY workspace_id, src_task_run_id, orphan_job_run_id, shape_fingerprint
),
task_sql AS (
  -- per task run: how much SQL was captured at all
  SELECT workspace_id, src_task_run_id,
         SUM(statement_count) AS task_statement_count,
         SUM(total_ms)        AS task_sql_ms
  FROM stmt_shape
  WHERE src_task_run_id IS NOT NULL
  GROUP BY workspace_id, src_task_run_id
),
joined AS (
  -- tasks with their statement shapes; tasks with none survive (NOT_ASSESSED); statements that
  -- matched no task run of a long run survive too (UNATTRIBUTED, pinned to the run)
  SELECT COALESCE(t.workspace_id, s.workspace_id) AS workspace_id,
         COALESCE(t.job_id, s.src_job_id)         AS job_id,
         COALESCE(t.job_run_id, s.src_job_run_id) AS job_run_id,
         t.task_run_id, t.task_key, t.task_start, t.task_result_state, t.task_s,
         t.task_not_executed, t.sql_may_be_incomplete,
         t.task_cluster_id, t.task_compute_type_raw,
         CASE WHEN t.task_run_id IS NULL THEN 'UNATTRIBUTED' ELSE t.task_compute_kind END AS task_compute_kind,
         s.src_task_run_id AS stmt_task_run_id,
         s.shape_fingerprint, s.statement_fingerprint, s.literal_variants_in_shape, s.text_redacted,
         s.statement_count, s.statements_not_finished, s.statement_type,
         s.sample_statement_id, s.stmt_compute_type, s.first_start_time, s.last_end_time,
         s.total_ms, s.execution_ms, s.compilation_ms, s.waiting_for_compute_ms,
         s.waiting_at_capacity_ms, s.total_task_ms, s.read_bytes, s.spilled_local_bytes,
         s.shuffle_read_bytes, s.statement_text_devalued
  FROM task_obs t
  FULL OUTER JOIN stmt_shape s
    ON  t.workspace_id = s.workspace_id
    AND t.task_run_id  = s.src_task_run_id
),
scoped AS (
  -- keep only the long runs (drops statements whose fallback run id is unknown or not long)
  SELECT j.*, r.run_s, r.in_flight,
         -- share denominator: the task's observed seconds, or the run's for UNATTRIBUTED rows
         COALESCE(j.task_s, r.run_s) AS share_denominator_s
  FROM joined j
  JOIN long_runs r
    ON  r.workspace_id = j.workspace_id
    AND r.run_id       = j.job_run_id
),
run_cov AS (
  -- per run: task runs seen, task runs with no captured SQL (never-run tasks excluded), and
  -- statements that exist but could not be pinned to a task
  SELECT workspace_id, job_run_id,
         COUNT(DISTINCT task_run_id)                                   AS run_tasks_seen,
         COUNT(DISTINCT CASE WHEN shape_fingerprint IS NULL AND NOT task_not_executed
                             THEN task_run_id END)                     AS run_tasks_not_assessed,
         SUM(CASE WHEN task_run_id IS NULL THEN statement_count ELSE 0 END) AS run_unattributed_statements
  FROM scoped
  GROUP BY workspace_id, job_run_id
),
ranked AS (
  SELECT sc.*,
         100.0 * sc.total_ms / 1000.0 / NULLIF(sc.share_denominator_s, 0) AS share_of_task_pct_raw,
         ROW_NUMBER() OVER (PARTITION BY sc.workspace_id, sc.job_run_id,
                                         COALESCE(sc.task_run_id, sc.stmt_task_run_id)
                            ORDER BY sc.total_ms DESC, sc.shape_fingerprint) AS stmt_rank_in_task
  FROM scoped sc
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM {{ source('system_lakeflow', 'jobs') }}
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT k.workspace_id,
       k.job_id,
       j.job_name,                                                    -- NULL for submit/workflow runs
       k.job_run_id,
       ROUND(k.run_s / 3600.0, 2)                                     AS run_hours,
       k.in_flight,
       c.run_tasks_seen,
       c.run_tasks_not_assessed,
       c.run_unattributed_statements,
       k.task_key,                                                    -- NULL = UNATTRIBUTED
       k.task_run_id,
       k.stmt_task_run_id,                                            -- what the statement itself claims
       k.task_start,
       COALESCE(k.task_result_state, CASE WHEN k.task_run_id IS NOT NULL THEN 'RUNNING' END) AS task_state,
       ROUND(k.task_s / 3600.0, 2)                                    AS task_hours,
       k.task_compute_kind,
       k.task_compute_type_raw,
       k.task_cluster_id,
       CASE WHEN k.task_run_id IS NULL THEN NULL ELSE COALESCE(q.task_statement_count, 0) END AS task_statement_count,
       ROUND(100.0 * q.task_sql_ms / 1000.0 / NULLIF(k.task_s, 0), 1) AS task_sql_share_pct,
       k.sql_may_be_incomplete,
       CASE WHEN k.shape_fingerprint IS NULL THEN NULL ELSE k.stmt_rank_in_task END AS stmt_rank_in_task,
       k.shape_fingerprint,
       k.statement_fingerprint,
       k.literal_variants_in_shape,
       k.text_redacted,
       k.statement_count,
       k.statements_not_finished,
       k.statement_type,
       k.sample_statement_id,
       k.stmt_compute_type,
       k.first_start_time,
       k.last_end_time,
       k.total_ms,
       k.execution_ms,
       k.compilation_ms,
       k.waiting_for_compute_ms,
       k.waiting_at_capacity_ms,
       k.total_task_ms,
       k.read_bytes,
       k.spilled_local_bytes,
       k.shuffle_read_bytes,
       ROUND(k.share_of_task_pct_raw, 1)                              AS share_of_task_pct,
       k.statement_text_devalued,
       -- why a row could not be judged (NULL when it could)
       CASE
         WHEN k.shape_fingerprint IS NOT NULL AND k.share_of_task_pct_raw IS NULL
                                                             THEN 'task_seconds_unknown'
         WHEN k.shape_fingerprint IS NOT NULL AND k.total_ms < {{ param('query_task_statement_breakdown', 'min_stmt_ms', 60000) }}
                                                             THEN 'below_duration_floor'
         WHEN k.shape_fingerprint IS NOT NULL
              AND k.share_of_task_pct_raw < {{ param('query_task_statement_breakdown', 'warn_stmt_share', 0.3) }} * 100
              AND k.sql_may_be_incomplete                    THEN 'statements_may_still_be_landing'
         WHEN k.shape_fingerprint IS NOT NULL                THEN NULL
         WHEN k.task_not_executed                            THEN 'task_not_executed'
         WHEN k.task_compute_kind = 'CLASSIC_CLUSTER'        THEN 'classic_cluster_not_in_query_history'
         WHEN k.sql_may_be_incomplete                        THEN 'statements_may_still_be_landing'
         ELSE 'no_statements_captured'
       END                                                            AS not_assessed_reason,
       -- status: worst-first band on the shape's share of its task's observed seconds (field
       -- heuristic; {{ param('query_task_statement_breakdown', 'warn_stmt_share', 0.3) }} / {{ param('query_task_statement_breakdown', 'crit_stmt_share', 0.6) }}), gated by {{ param('query_task_statement_breakdown', 'min_stmt_ms', 60000) }} so a short statement
       -- inside a slightly-longer task cannot read CRITICAL on share alone. A task with no visible
       -- SQL, or whose SQL may still be landing, is NOT_ASSESSED - "could not look", never "nothing there".
       CASE
         WHEN k.shape_fingerprint IS NULL                              THEN 'NOT_ASSESSED'
         WHEN k.share_of_task_pct_raw IS NULL                          THEN 'NOT_ASSESSED'
         WHEN k.total_ms < {{ param('query_task_statement_breakdown', 'min_stmt_ms', 60000) }}                                THEN 'NOT_ASSESSED'
         WHEN k.share_of_task_pct_raw >= {{ param('query_task_statement_breakdown', 'crit_stmt_share', 0.6) }} * 100        THEN 'CRITICAL'
         WHEN k.share_of_task_pct_raw >= {{ param('query_task_statement_breakdown', 'warn_stmt_share', 0.3) }} * 100        THEN 'WARN'
         WHEN k.sql_may_be_incomplete                                  THEN 'NOT_ASSESSED'
         ELSE 'OK'
       END AS status
FROM ranked k
LEFT JOIN run_cov c
  ON  k.workspace_id = c.workspace_id
  AND k.job_run_id   = c.job_run_id
LEFT JOIN task_sql q
  ON  k.workspace_id = q.workspace_id
  AND k.task_run_id  = q.src_task_run_id
LEFT JOIN latest_jobs j
  ON  k.workspace_id = j.workspace_id
  AND k.job_id       = j.job_id
WHERE k.stmt_rank_in_task <= {{ param('query_task_statement_breakdown', 'top_n', 10) }}
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         share_of_task_pct DESC,
         workspace_id, job_id, job_run_id, task_key, task_run_id, stmt_task_run_id, stmt_rank_in_task
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
