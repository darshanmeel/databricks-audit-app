-- query_id: query_task_statement_breakdown
-- title: Inside a long job run, the SQL statements per task that burned the time
-- domain: performance   tier: standard
-- reads: system.query.history, system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline, system.lakeflow.jobs, system.compute.clusters
-- requires: SELECT on system.query, system.lakeflow AND system.compute; system.query.history is Public Preview per its doc page (records land within ~1 hour), the lakeflow and compute tables are GA. statement_text reads <REDACTED> unless you are an account admin or in the databricks_pii_access account group - the query still works, per statement instead of per shape (see caveats). SCOPE LIMIT: system.query.history records statements run on SQL warehouses and serverless compute ONLY - a task that ran on a classic all-purpose or job cluster has NO statements in it and comes back NOT_ASSESSED here, never OK (system.compute.clusters is read only to tell those tasks apart)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days; :warn_run_hours (default 2) run wall-clock hours at/above which a run is examined (the same bound as lakeflow_long_running_runs, so the two compose); :warn_stmt_share (default 0.3) fraction of a task's observed seconds one statement shape must consume to flag WARN; :crit_stmt_share (default 0.6) same for CRITICAL; :settle_hours (default 1) hours after a task's last observed row during which its statements may still be landing in query.history (the doc says ~1 hour) - a task younger than that is never banded OK; :top_n (default 10) statement shapes kept per task, worst-first; :min_stmt_ms (default 60000) a shape's own total_ms must reach this before it can band WARN/CRITICAL on share alone - below it the shape reads NOT_ASSESSED regardless of share
-- confidence: needs_confirmation
-- confidence_note: Every column and struct field used (query_source.job_info.job_id / job_run_id / job_task_run_id, compute.type / warehouse_id, job_task_run_timeline.compute_ids and its compute array, the *_duration_seconds columns) was checked against a live system-catalog schema dump of this account taken 2026-07-07, and the join keys, the <REDACTED> behaviour, the zero-valued run durations and the compute scope were checked against the Databricks doc pages on 2026-09-21 - but the query itself has NOT been executed on a workspace. Confirm on your account: 1) for a task you know ran SQL on serverless or a warehouse, its statements land on the task row (task_key set), not on an UNATTRIBUTED row - the doc's own example carries job_info.job_run_id NULL with job_task_run_id set, which is why the join is keyed on job_task_run_id = job_task_run_timeline.run_id; 2) a task you know ran on a classic job cluster shows task_compute_kind = CLASSIC_CLUSTER and status NOT_ASSESSED; 3) task_sql_share_pct is plausible for a pure-SQL task (near 100).
-- read_this: One row = one statement SHAPE inside one task run of one job run that lasted at least :warn_run_hours - plus one row per task run for which NO statement could be seen (status NOT_ASSESSED, not_assessed_reason says why). The columns that matter are task_key with share_of_task_pct (how much of that task's observed seconds this one shape consumed), stmt_rank_in_task (worst-first within the task) and task_sql_share_pct (how much of the task's time is explained by captured SQL at all - low means the time went to non-Spark Python, waiting, or compute this table cannot see). run_tasks_not_assessed / run_tasks_seen say, on every row of a run, how many of that run's task runs (a retried task counts twice) are invisible here, and run_unattributed_statements how many of the run's statements exist but could not be pinned to a task. A shape is the statement text with every string AND numeric literal replaced by ? (shape_fingerprint); statement_fingerprint is the string-literal-only recipe query_costly_statements uses, kept so a shape can be looked up in query_costly_statements_grouped. statement_text_devalued never carries a data value.
-- healthy: every task of a long run is covered (run_tasks_not_assessed = 0), nothing is still landing (sql_may_be_incomplete = false) and no single statement shape at/above :min_stmt_ms consumes :warn_stmt_share or more of its task's observed seconds - the time is spread over many shapes, so the fix is the task's design (volume, serial chaining), not one query - field heuristic.
-- investigate_if: share_of_task_pct at/above :warn_stmt_share (WARN) or :crit_stmt_share (CRITICAL), for a shape whose total_ms reaches :min_stmt_ms - one statement shape owns the task, so tune that statement first (field heuristic; tune the two shares and the floor for your account). NOT_ASSESSED rows are not a pass, read not_assessed_reason: classic_cluster_not_in_query_history means the task's SQL is structurally invisible here (use task_cluster_utilization for the shape of the problem and the Spark UI for the statement); statements_may_still_be_landing means the task is in flight or ended less than :settle_hours ago; task_seconds_unknown means the task's own duration could not be established; below_duration_floor means the shape's own total_ms is under :min_stmt_ms - too short in absolute terms to prioritize, whatever its share of a short task; no_statements_captured means the task ran on covered compute yet wrote nothing to query.history (non-Spark work such as pandas or model training, or a task whose SQL sits on this run's UNATTRIBUTED rows); task_not_executed means the task was SKIPPED or BLOCKED and never ran.
-- actions: 1) open the CRITICAL / WARN shape's sample_statement_id in the SQL query history UI (paste it into the Statement ID filter), read its query profile, and fix the usual suspects the counters point at - spilled_local_bytes (memory / sort), shuffle_read_bytes (join order / broadcast), read_bytes with no pruning (missing partition / clustering filter), waiting_for_compute_ms (warehouse start or serverless spin-up), total_task_ms far above total_ms (the statement is wide, not slow - fine unless it spills) (free); 2) if many shapes each take a small share but statement_count is high, cut the loop that issues them - batch it, or cache the CTE they share (free / config); 3) for classic_cluster_not_in_query_history tasks decide whether to move the task to serverless so its SQL lands in query.history next time, or turn on cluster log delivery to a Unity Catalog volume so the Spark event log outlives the cluster (config / spend).
-- next: task_cluster_utilization (for the NOT_ASSESSED classic-cluster tasks - CPU / memory shape of the cluster over the task's window), lakeflow_long_running_runs (for the run-level picture this query zooms into), query_costly_statements_grouped (if the same statement_fingerprint dominates several jobs), query_local_spillage (if spilled_local_bytes is high), query_pruning_effectiveness (if read_bytes is high and the statement should have pruned)
-- not_assessed_reasons: task_seconds_unknown: the task's own duration could not be established; statements_may_still_be_landing: the task is still running or ended too recently for its statements to have landed yet; task_not_executed: the task was skipped or blocked and never ran; classic_cluster_not_in_query_history: the task ran on a classic cluster, whose SQL is not captured here; no_statements_captured: the task ran on covered compute but wrote no captured SQL; below_duration_floor: the shape's own total_ms is under :min_stmt_ms, too short in absolute terms to band on share alone
-- caveats: SCOPE - system.query.history records statements run on SQL warehouses and serverless compute for notebooks and jobs; nothing run on a classic all-purpose or job cluster is in it, whatever the language. On serverless, Databricks documents that SQL AND Python (Spark DataFrame) queries are recorded, so a serverless task with no rows did non-Spark work (pandas, model training, pure Python) or is still landing; note that for Python cells the recorded statement_text is only the LAST line of the cell. A task with no statements is therefore NEVER reported as clean: it is a NOT_ASSESSED row, and task_compute_kind says which case it is. task_compute_kind is derived structurally, not from an enum: CLASSIC_CLUSTER when the task's first compute id exists in system.compute.clusters (all-purpose, job and pipeline clusters - never serverless or warehouses), SQL_WAREHOUSE when the task's compute struct carries a warehouse_id, SERVERLESS_OR_UNRECORDED when no compute id was recorded (serverless tasks record none, and neither do rows from before compute_ids was populated in early Dec 2025), UNRESOLVED when an id was recorded but is not in system.compute.clusters (a warehouse id on a pre-Dec-2025 row, a cluster deleted before Oct 2023, or a compute type this heuristic does not know); task_compute_type_raw carries the untouched compute[0].type value so you can see the real vocabulary. JOIN KEY - the doc's own example of a job-run statement has job_info.job_run_id NULL and job_info.job_task_run_id populated, so statements are pinned to a task run through job_task_run_id = job_task_run_timeline.run_id, and job_info.job_run_id is used only as the fallback for statements with no task run id; those become UNATTRIBUTED rows (task_key NULL) whose share is measured against the RUN's seconds, ranked and capped by :top_n in one bucket per run (or per stmt_task_run_id when the statement names a task run the timeline does not know), and kept so the SQL is not lost. The nested query_source.job_info.* path is extracted to plain scalar columns in the stmts subquery before any join or GROUP BY, because query_provenance_by_source reports that grouping by a doubly-nested field is not verified; the field names are confirmed by the schema dump and the doc. DURATIONS - the doc states that run_duration_seconds and the other phase columns on job_run_timeline are populated only for legacy single-task jobs and log 0 for every multi-task job, so a 0 is treated as "not reported" and the run falls back to wall clock between its first and last observed rows; task seconds are execution_duration_seconds where populated (end row only, since early Dec 2025, 0 treated the same way), else wall clock, measured to current_timestamp() for a task in flight. A run that started BEFORE the window is measured from its first in-window row and only its in-window statements are counted, so its seconds and shares are lower bounds - keep :period_days comfortably wider than your longest run. STATEMENTS - query.history holds only terminated statements (FINISHED / FAILED / CANCELED), so a statement still executing has no row yet and records land within about an hour: sql_may_be_incomplete marks every row of a task that is in flight or ended less than :settle_hours ago, and such a task is never banded OK - a shape already over the bar still flags. Statements inside one task can run concurrently, so shares within a task can add up past 100 and share_of_task_pct reads as "share of the task's wall clock this shape was busy", not a decomposition. total_ms is the statement's wall clock excluding result fetch (compile + waits + execution, per the doc) and is the numerator of every share; execution_ms is the engine time query_costly_statements ranks by; total_task_ms is CPU time summed across all cores. All execution statuses are kept - a statement that ran two hours and then FAILED still burned the time - and statements_not_finished counts the non-FINISHED runs of a shape; from_result_cache hits are kept too (they cost ~0 ms). SHAPES - shape_fingerprint masks emails, single-quoted literals AND bare numbers (so WHERE batch_id = 17 and = 18 are one shape, and lists of ? collapse to one ?); statement_fingerprint is the string-literal-only recipe shared with query_costly_statements, taken from the first statement of the shape, and literal_variants_in_shape says how many distinct such fingerprints the shape spans. When statement_text is <REDACTED> (the default for non-admins since Aug 2026) no shape can be built: text_redacted = true, every statement is its own row keyed by statement_id, and the share and band are per statement - still honest, just not grouped. Like lakeflow_long_running_runs this query keeps the current day so a run in flight is examined. Runs are bounded on RUN wall clock with :warn_run_hours exactly as lakeflow_long_running_runs does; set it to 0 to see every run. :top_n caps SHAPES PER TASK (or per UNATTRIBUTED bucket), not rows in total. query.history is scanned only for the long runs (a semi-join on their run and task-run ids), so the cost scales with the runs examined, not with the workspace. job_name comes from system.lakeflow.jobs (SCD2, latest row per job, deleted jobs kept) which one-time SUBMIT_RUN / WORKFLOW_RUN executions never write to - those runs show job_name NULL. PRIVACY: statement_text_devalued has emails replaced by <email> and every literal by ?, both fingerprints are hashes, and no executed_by / run_as identity is selected at all. There are no dollars here; price the run with cost_by_job. DURATION FLOOR - a shape below :min_stmt_ms total_ms cannot band WARN/CRITICAL even at 100% share_of_task_pct, because a short statement inside a slightly-longer task would otherwise read as the worst possible band purely from the task being short, not from the statement being slow; such a shape reads NOT_ASSESSED ('below_duration_floor') instead.
WITH run_agg AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)    AS run_start,
         MAX(period_end_time)      AS last_seen,
         MAX(result_state)         AS result_state,       -- NULL until the run's end row lands
         -- end row only; NULL before Dec 2025 and 0 for every multi-task job (doc), so 0 = not reported
         NULLIF(MAX(run_duration_seconds), 0) AS run_s_reported
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
  GROUP BY workspace_id, job_id, run_id
),
run_obs AS (
  SELECT workspace_id, job_id, run_id, run_start, result_state,
         (result_state IS NULL) AS in_flight,
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, run_start,
                                CASE WHEN result_state IS NULL THEN current_timestamp()
                                     ELSE last_seen END)) AS run_s
  FROM run_agg
),
long_runs AS (
  -- the same run bound as lakeflow_long_running_runs, so the two queries compose
  SELECT workspace_id, job_id, run_id, run_start, in_flight, run_s
  FROM run_obs
  WHERE run_s >= :warn_run_hours * 3600
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
  FROM system.lakeflow.job_task_run_timeline t
  JOIN long_runs r
    ON  r.workspace_id = t.workspace_id
    AND r.job_id       = t.job_id
    AND r.run_id       = t.job_run_id
  WHERE t.period_start_time >= dateadd(day, -:period_days, current_date())
  GROUP BY t.workspace_id, t.job_id, t.job_run_id, t.run_id, t.task_key
),
known_clusters AS (
  -- system.compute.clusters holds classic (all-purpose, job, pipeline) clusters only
  SELECT DISTINCT workspace_id, cluster_id
  FROM system.compute.clusters
),
task_obs AS (
  SELECT a.workspace_id, a.job_id, a.job_run_id, a.task_run_id, a.task_key,
         a.task_start, a.task_last_seen, a.task_result_state, a.task_cluster_id, a.task_compute_type_raw,
         COALESCE(a.task_exec_s,
                  timestampdiff(SECOND, a.task_start,
                                CASE WHEN a.task_result_state IS NULL THEN current_timestamp()
                                     ELSE a.task_last_seen END))     AS task_s,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (a.task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (a.task_result_state IS NOT NULL AND a.task_start = a.task_last_seen)) AS task_not_executed,
         -- in flight, or ended so recently that its statements may not have landed yet
         (a.task_result_state IS NULL
          OR a.task_last_seen >= dateadd(hour, -:settle_hours, current_timestamp())) AS sql_may_be_incomplete,
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
  FROM system.query.history
  WHERE start_time >= dateadd(day, -:period_days, current_date())
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
  FROM system.lakeflow.jobs
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
         WHEN k.shape_fingerprint IS NOT NULL AND k.total_ms < :min_stmt_ms
                                                             THEN 'below_duration_floor'
         WHEN k.shape_fingerprint IS NOT NULL
              AND k.share_of_task_pct_raw < :warn_stmt_share * 100
              AND k.sql_may_be_incomplete                    THEN 'statements_may_still_be_landing'
         WHEN k.shape_fingerprint IS NOT NULL                THEN NULL
         WHEN k.task_not_executed                            THEN 'task_not_executed'
         WHEN k.task_compute_kind = 'CLASSIC_CLUSTER'        THEN 'classic_cluster_not_in_query_history'
         WHEN k.sql_may_be_incomplete                        THEN 'statements_may_still_be_landing'
         ELSE 'no_statements_captured'
       END                                                            AS not_assessed_reason,
       -- status: worst-first band on the shape's share of its task's observed seconds (field
       -- heuristic; :warn_stmt_share / :crit_stmt_share), gated by :min_stmt_ms so a short statement
       -- inside a slightly-longer task cannot read CRITICAL on share alone. A task with no visible
       -- SQL, or whose SQL may still be landing, is NOT_ASSESSED - "could not look", never "nothing there".
       CASE
         WHEN k.shape_fingerprint IS NULL                              THEN 'NOT_ASSESSED'
         WHEN k.share_of_task_pct_raw IS NULL                          THEN 'NOT_ASSESSED'
         WHEN k.total_ms < :min_stmt_ms                                THEN 'NOT_ASSESSED'
         WHEN k.share_of_task_pct_raw >= :crit_stmt_share * 100        THEN 'CRITICAL'
         WHEN k.share_of_task_pct_raw >= :warn_stmt_share * 100        THEN 'WARN'
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
-- :top_n caps statement SHAPES per task (or per UNATTRIBUTED bucket); NOT_ASSESSED task rows always pass
WHERE k.stmt_rank_in_task <= :top_n
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         share_of_task_pct DESC,
         workspace_id, job_id, job_run_id, task_key, task_run_id, stmt_task_run_id, stmt_rank_in_task
