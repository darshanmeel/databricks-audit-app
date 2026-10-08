-- query_id: query_warehouse_pressure
-- title: SQL warehouse pressure - memory-bound (statements spill) or concurrency-bound (statements
--   queue at capacity), with the scaling lever for each
-- domain: performance   tier: standard
-- reads: system.query.history, system.compute.warehouses, system.compute.warehouse_events
-- requires: SELECT on system.query and system.compute; GA. SCOPE LIMIT: system.query.history holds
--   SQL-warehouse, serverless and classic Lakeflow pipeline statements only, and only SQL-warehouse
--   statements carry a warehouse_id, so this query judges SQL warehouses and nothing else
-- empty_if: schema_not_enabled, compute_scope_gap, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days; :min_queries (default 20) statements in
--   the window below which a warehouse is NOT_ASSESSED; :min_stmt_spill_mb (default 100) the local
--   spill, in MB (10^6 bytes), one statement must reach to count as a spilling statement in
--   spill_time_pct and spilling_query_count (the GB columns and the worst day still add every
--   spilled byte); :warn_spill_time_pct (default 10) percent of the warehouse's execution time spent
--   in statements that spilled at least :min_stmt_spill_mb MB to local disk that flags WARN
--   (the 10 percent band query_costly_statements uses for one statement's share of its warehouse);
--   :crit_spill_time_pct (default 25) the same share that flags CRITICAL (its 25 percent band);
--   :warn_spill_gb (default 1) the worst single day's local spill in GB for the warehouse that flags
--   WARN (the query_local_spillage thresholds, applied to the whole warehouse's day); :crit_spill_gb
--   (default 10) the same that flags CRITICAL; :warn_queue_time_pct (default 5) percent of total
--   statement time spent queued at capacity that flags WARN; :crit_queue_time_pct (default 20) the
--   same that flags CRITICAL; :top_n (default 100000) row cap - far above any account's warehouse
--   count, because this is a verdict per warehouse
-- confidence: needs_confirmation
-- confidence_note: Every column read here is one a vendored query already reads on a live workspace
--   (query_local_spillage and query_queuing_waits on system.query.history, sql_warehouse_config_current
--   on system.compute.warehouses, compute_warehouse_autoscale_churn on system.compute.warehouse_events),
--   and the compute.type values, the warehouse_id scope and the size-versus-clusters sizing advice were
--   checked against the Databricks doc pages on 2026-09-23 - but the verdict bands and the
--   time-weighting are new and the query has NOT been executed on a workspace. Confirm on your
--   account: 1) for a warehouse you know queues, capacity_wait_pct is non-zero and
--   max_cluster_count_seen matches the peak cluster count the warehouse monitoring page shows;
--   2) max_clusters on the latest system.compute.warehouses row is the autoscale ceiling the
--   warehouse runs with today; 3) execution, compilation and the two waits roughly add up to
--   total_duration_ms for a sample of statements (that split is not documented).
-- read_this: One row = one SQL warehouse (workspace_id, warehouse_id) with at least one statement in
--   the window. The columns that matter are pressure (MEMORY, CAPACITY, MEMORY_AND_CAPACITY or NONE),
--   scaling_hint (the lever: SCALE_UP, SCALE_OUT, KEEP_WARM, SCALE_UP_THEN_OUT, SCALE_UP_THEN_WARM
--   or NONE) and pressure_reason (the row's own numbers behind the verdict). Memory pressure is
--   spill_time_pct - the share of the warehouse's execution time spent in statements that spilled
--   at least :min_stmt_spill_mb MB to local disk - or
--   spilled_local_gb_max_day, the worst single day's spill; concurrency pressure is
--   capacity_wait_pct, the share of total statement time spent queued because the warehouse was at
--   capacity. at_ceiling says whether "more clusters" is even the knob: TRUE means the warehouse has
--   no autoscale range (min_clusters >= max_clusters, so it always runs at its maximum) or some
--   warehouse_events row in the window showed max_clusters running, FALSE means no event in the
--   window showed max_clusters running (read as autoscale lag - keep a cluster warm), NULL means
--   unseen. cold_start_wait_pct is context only; query_cache_coldstart is the cold-start finding.
-- healthy: pressure = NONE - spill_time_pct below :warn_spill_time_pct, the worst day's local spill
--   below :warn_spill_gb GB and capacity_wait_pct below :warn_queue_time_pct (field heuristic; every
--   threshold is a header param).
-- investigate_if: MEMORY - spill_time_pct at/above :warn_spill_time_pct (WARN) or
--   :crit_spill_time_pct (CRITICAL), or the worst day's local spill at/above :warn_spill_gb GB (WARN)
--   or :crit_spill_gb GB (CRITICAL): statements on this warehouse do not fit in its memory, so the
--   lever is size; CAPACITY - capacity_wait_pct at/above :warn_queue_time_pct (WARN) or
--   :crit_queue_time_pct (CRITICAL): statements queue because every cluster is busy, so the lever is
--   clusters at the ceiling or a warm cluster below it; MEMORY_AND_CAPACITY - both. status is the
--   worse of the two bands (field heuristic). NOT_ASSESSED (fewer than :min_queries statements) is
--   not a pass: read not_assessed_reason.
-- actions: 1) MEMORY (SCALE_UP) - find the spilling statements in query_local_spillage and cut their
--   shuffle: broadcast the small side, filter earlier, avoid exploding joins; CAPACITY - check with
--   query_workload_mix_hours whether the queueing clusters at a few hours before changing the
--   warehouse (free); 2) CAPACITY below the ceiling (KEEP_WARM) - raise min_clusters so a second
--   cluster is already running when the load arrives, or route the heaviest recurring shapes to
--   their own warehouse (config); 3) MEMORY - scale up: a larger warehouse_size gives every cluster
--   more memory per statement; CAPACITY at the ceiling (SCALE_OUT) - scale out: raise max_clusters so
--   concurrent statements stop queueing, or a larger size if the queued statements are themselves
--   heavy; MEMORY_AND_CAPACITY at the ceiling or with the ceiling unseen (SCALE_UP_THEN_OUT) - size
--   first, then clusters; MEMORY_AND_CAPACITY below the ceiling (SCALE_UP_THEN_WARM) - size first,
--   then raise min_clusters so a second cluster is already running when the load arrives (spend).
-- next: query_local_spillage (if MEMORY - the spilling days and users), query_queuing_waits (if
--   CAPACITY - the day-by-day queue), query_costly_statements (for the statements that eat the
--   warehouse's execution time), sql_warehouse_config_current (for warehouse_size, min_clusters and
--   max_clusters - not for a deleted warehouse, which it drops), compute_warehouse_autoscale_churn
--   (if at_ceiling is FALSE - how the warehouse scales), query_cache_coldstart (if
--   cold_start_wait_pct is high), query_workload_mix_hours (to see whether the queueing clusters at
--   a few hours)
-- not_assessed_reasons: too_few_statements: too few statements ran on this warehouse in the
--   window to judge
-- caveats: SCOPE - system.query.history records statements run on SQL warehouses (compute.type
--   WAREHOUSE), serverless compute (SERVERLESS_COMPUTE) and classic Lakeflow pipelines
--   (CLASSIC_COMPUTE) only, and compute.warehouse_id is populated only for WAREHOUSE (cluster_id is
--   never populated). Serverless and classic-pipeline statements are therefore EXCLUDED here - no
--   size or cluster knob exists for them, and query_task_statement_breakdown covers serverless job
--   statements - while statements on classic all-purpose or job clusters are never recorded at all,
--   so a workload that runs on classic clusters is invisible here; its machine-level pressure is
--   lakeflow_job_compute_pressure's (compute_scope_gap). LOCAL spill ONLY, as in
--   query_local_spillage: system tables expose no spilled_remote_bytes column, so remote spill is
--   NOT assessed - read a warehouse without MEMORY as "no local spill measured", never as "no
--   spill". There are only two queue buckets, as in query_queuing_waits:
--   waiting_at_capacity_duration_ms (queued because the warehouse was at capacity) and
--   waiting_for_compute_duration_ms (compute still provisioning - cold start). NULL is treated as 0
--   (both mean "no wait"), an assumption rather than a documented guarantee, and capacity_wait_pct
--   and cold_start_wait_pct divide by total_duration_ms on the assumption that it adds up the two
--   waits, compilation and execution - that additive split is not documented, so read the shares as
--   close, not exact. The memory verdict is TIME-WEIGHTED: spill_time_pct weighs every statement by
--   its execution time, so a warehouse where few statements spill can still be memory-bound when
--   those statements are the long ones (spill_query_pct, the count share, is context only), and the
--   worst single day's local spill (the query_local_spillage thresholds) catches a rare but huge
--   spill that a share would dilute. A statement counts as spilling only once its local spill
--   reaches :min_stmt_spill_mb MB: below that floor its execution time stays out of spill_time_pct
--   (a long statement that spilled a few MB is not a warehouse short of memory), while
--   spilled_local_gb_sum and the worst day still add every spilled byte. Above the floor the
--   statement's WHOLE execution time counts, so read spill_time_pct beside spilled_local_gb_sum.
--   The floor is a field default, not calibrated per warehouse size. The worst day is summed over
--   every user of the warehouse, while query_local_spillage bands one user's day, so its rows can
--   all sit below the band for a warehouse flagged here. A warehouse under :min_queries statements
--   is NOT_ASSESSED even when its worst day reaches the spill bands - read spilled_local_gb_max_day
--   on those rows.
--   Every band is judged on the unrounded share; the percentages are rounded only for display, and
--   pressure_reason quotes the threshold of the band the row reached (a CRITICAL memory clause
--   names :crit_spill_time_pct and :crit_spill_gb, a WARN one the warn values; likewise for the
--   queue) and, on a NONE row, writes a share that rounds up to its threshold as "just under" it.
--   MEMORY_AND_CAPACITY reads SCALE_UP_THEN_WARM when at_ceiling is FALSE - the warehouse queued
--   below its maximum, so the step after size is a warm cluster (raise min_clusters), not a higher
--   max_clusters - and SCALE_UP_THEN_OUT otherwise, at_ceiling NULL included, as CAPACITY reads
--   SCALE_OUT. cold_start_wait_pct is context, never a verdict - query_cache_coldstart is the
--   cold-start finding. at_ceiling compares the most clusters seen running at any
--   warehouse_events event in the window (max_cluster_count_seen; cluster_count is clusters running
--   at event time) against the latest max_clusters, except that a warehouse with no autoscale range
--   (min_clusters >= max_clusters) is TRUE from its configuration alone - it cannot add a cluster,
--   so KEEP_WARM (raise min_clusters) is impossible and its lever is max_clusters, and its reason
--   names the range instead of an event count; system.compute.warehouse_events may be absent or
--   silent for a warehouse, so at_ceiling NULL means unseen, not below the ceiling, and a CAPACITY
--   warehouse with at_ceiling NULL reads as SCALE_OUT. at_ceiling is WINDOW-WIDE: a ceiling reached
--   on one day labels queueing on any other day of the window, and a cluster count carried in from
--   before the window with no new event in it is not seen (a warehouse that sat at max_clusters from
--   before the window and only scaled down during it reads FALSE) - read FALSE as "no event showed
--   the ceiling", and check compute_warehouse_autoscale_churn before raising min_clusters. Config (warehouse_type, warehouse_size,
--   min_clusters, max_clusters, auto_stop_minutes) comes from system.compute.warehouses, SCD2 - the
--   latest row per warehouse_id, so a warehouse resized during the window shows its last shape, and
--   a deleted warehouse is KEPT (warehouse_deleted TRUE) because its statements are still in the
--   window; a warehouse with no config row keeps NULL config columns. The statement window excludes
--   the current day (start_time < current_date()), like every vendored performance query, and the
--   warehouse_events window covers the same days, so an event from today is not counted. history is per-region - run per metastore
--   region. No identities are emitted, so nothing is masked, and there is no dollar figure here:
--   pressure is time and bytes, never cost. Scale up here means a larger warehouse_size / more
--   memory per worker and scale out means more clusters / more workers; Databricks' own
--   warehouse_events logs SCALED_UP when a warehouse ADDS A CLUSTER, so do not read that event name
--   as this query's scale-up. Thresholds and caveats are inherited from query_local_spillage,
--   query_queuing_waits and query_costly_statements.
WITH stmts AS (
  -- SQL-warehouse statements in the window; serverless and classic-pipeline statements carry no
  -- warehouse_id and are excluded. A NULL duration or byte count reads as 0 (query_queuing_waits).
  SELECT workspace_id,
         compute.warehouse_id                         AS warehouse_id,
         compute.type                                 AS compute_type,
         execution_status,
         date(start_time)                             AS stmt_day,
         COALESCE(spilled_local_bytes, 0)             AS spilled_local_bytes,
         COALESCE(execution_duration_ms, 0)           AS execution_ms,
         COALESCE(total_duration_ms, 0)               AS total_ms,
         COALESCE(read_bytes, 0)                      AS read_bytes,
         COALESCE(shuffle_read_bytes, 0)              AS shuffle_read_bytes,
         COALESCE(waiting_at_capacity_duration_ms, 0) AS waiting_at_capacity_ms,
         COALESCE(waiting_for_compute_duration_ms, 0) AS waiting_for_compute_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL :period_days DAYS
    AND start_time < current_date()
    AND compute.warehouse_id IS NOT NULL
),
daily AS (
  -- local spill per warehouse per calendar day, for the worst-day volume band
  SELECT workspace_id, warehouse_id, stmt_day,
         SUM(spilled_local_bytes) AS spilled_day_bytes
  FROM stmts
  GROUP BY workspace_id, warehouse_id, stmt_day
),
worst_day AS (
  SELECT workspace_id, warehouse_id,
         MAX(spilled_day_bytes) AS spilled_local_bytes_max_day
  FROM daily
  GROUP BY workspace_id, warehouse_id
),
per_wh AS (
  SELECT workspace_id, warehouse_id,
         MIN(compute_type)                                                    AS compute_type,
         COUNT(*)                                                             AS query_count,
         SUM(CASE WHEN execution_status = 'FINISHED' THEN 1 ELSE 0 END)       AS finished_count,
         -- a spilling statement is one that reached :min_stmt_spill_mb MB (10^6 bytes) of spill
         SUM(CASE WHEN spilled_local_bytes > 0
                   AND spilled_local_bytes >= :min_stmt_spill_mb * 1e6
                  THEN 1 ELSE 0 END)                                         AS spilling_query_count,
         SUM(spilled_local_bytes)                                             AS spilled_local_bytes_sum,
         -- execution time of the spilling statements: the memory verdict is time-weighted
         SUM(CASE WHEN spilled_local_bytes > 0
                   AND spilled_local_bytes >= :min_stmt_spill_mb * 1e6
                  THEN execution_ms ELSE 0 END)                              AS spill_exec_ms,
         SUM(execution_ms)                                                    AS execution_ms_sum,
         SUM(total_ms)                                                        AS total_duration_ms_sum,
         SUM(read_bytes)                                                      AS read_bytes_sum,
         SUM(shuffle_read_bytes)                                              AS shuffle_read_bytes_sum,
         SUM(CASE WHEN waiting_at_capacity_ms > 0 THEN 1 ELSE 0 END)          AS queued_at_capacity_count,
         SUM(waiting_at_capacity_ms)                                          AS waiting_at_capacity_ms_sum,
         SUM(CASE WHEN waiting_for_compute_ms > 0 THEN 1 ELSE 0 END)          AS waited_for_compute_count,
         SUM(waiting_for_compute_ms)                                          AS waiting_for_compute_ms_sum
  FROM stmts
  GROUP BY workspace_id, warehouse_id
),
latest_wh AS (
  -- system.compute.warehouses is SCD2: the newest row per warehouse, deleted warehouses KEPT
  -- (a warehouse deleted since still has statements in the window)
  SELECT warehouse_id, warehouse_type, warehouse_size, min_clusters, max_clusters,
         auto_stop_minutes,
         (delete_time IS NOT NULL) AS warehouse_deleted
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY warehouse_id ORDER BY change_time DESC
  ) = 1
),
events AS (
  -- the most clusters the warehouse ran at once in the window, and how often it scaled
  SELECT warehouse_id,
         MAX(cluster_count)                                                          AS max_cluster_count_seen,
         SUM(CASE WHEN event_type IN ('SCALED_UP', 'SCALED_DOWN') THEN 1 ELSE 0 END) AS scaling_events
  FROM system.compute.warehouse_events
  WHERE event_time >= current_date() - INTERVAL :period_days DAYS
    AND event_time < current_date()
  GROUP BY warehouse_id
),
measured AS (
  SELECT p.*,
         d.spilled_local_bytes_max_day,
         w.warehouse_type, w.warehouse_size, w.min_clusters, w.max_clusters, w.auto_stop_minutes,
         w.warehouse_deleted,
         e.max_cluster_count_seen, e.scaling_events,
         -- unrounded shares: every band below is judged on these, and they are rounded (1 dp)
         -- only in the final SELECT, so a true 9.96% never crosses a 10% threshold
         p.spill_exec_ms / NULLIF(p.execution_ms_sum, 0) * 100                   AS spill_time_raw,
         p.waiting_at_capacity_ms_sum / NULLIF(p.total_duration_ms_sum, 0) * 100 AS capacity_wait_raw,
         -- context only, never a verdict (query_cache_coldstart is the cold-start finding)
         p.waiting_for_compute_ms_sum / NULLIF(p.total_duration_ms_sum, 0) * 100 AS cold_start_wait_raw,
         p.spilling_query_count * 100.0 / p.query_count                         AS spill_query_raw,
         -- did the warehouse reach its maximum clusters? A warehouse with no autoscale range
         -- (min_clusters >= max_clusters) always runs at its maximum; otherwise the events decide,
         -- and NULL = not seen (no event, or no config)
         CASE WHEN w.max_clusters IS NOT NULL AND w.min_clusters >= w.max_clusters THEN TRUE
              WHEN e.max_cluster_count_seen IS NULL OR w.max_clusters IS NULL THEN NULL
              ELSE e.max_cluster_count_seen >= w.max_clusters END                        AS at_ceiling
  FROM per_wh p
  LEFT JOIN worst_day d
    ON  d.workspace_id = p.workspace_id
    AND d.warehouse_id = p.warehouse_id
  LEFT JOIN latest_wh w
    ON  w.warehouse_id = p.warehouse_id
  LEFT JOIN events e
    ON  e.warehouse_id = p.warehouse_id
),
banded AS (
  SELECT m.*,
         -- memory: the time-weighted spill share, or the worst single day's spill volume
         CASE
           WHEN m.spill_time_raw >= :crit_spill_time_pct
             OR m.spilled_local_bytes_max_day >= :crit_spill_gb * 1e9 THEN 'CRITICAL'
           WHEN m.spill_time_raw >= :warn_spill_time_pct
             OR m.spilled_local_bytes_max_day >= :warn_spill_gb * 1e9 THEN 'WARN'
           ELSE 'OK'
         END AS mem_band,
         -- concurrency: the share of statement time queued at capacity
         CASE
           WHEN m.capacity_wait_raw >= :crit_queue_time_pct THEN 'CRITICAL'
           WHEN m.capacity_wait_raw >= :warn_queue_time_pct THEN 'WARN'
           ELSE 'OK'
         END AS cap_band
  FROM measured m
),
judged AS (
  SELECT b.*,
         -- the verdict (field heuristic); NULL = too few statements to judge
         CASE
           WHEN b.query_count < :min_queries                                               THEN NULL
           WHEN b.mem_band IN ('WARN', 'CRITICAL') AND b.cap_band IN ('WARN', 'CRITICAL') THEN 'MEMORY_AND_CAPACITY'
           WHEN b.mem_band IN ('WARN', 'CRITICAL')                                         THEN 'MEMORY'
           WHEN b.cap_band IN ('WARN', 'CRITICAL')                                         THEN 'CAPACITY'
           ELSE 'NONE'
         END AS pressure,
         -- the WHY, from the row's own numbers: whole percentages, GB to one decimal, and the
         -- threshold of the band the row actually reached (CRITICAL quotes the critical values)
         CONCAT(COALESCE(CAST(ROUND(b.spill_time_raw, 0) AS BIGINT), 0),
                '% of execution time was in statements that spilled to local disk (threshold ',
                CASE WHEN b.mem_band = 'CRITICAL' THEN :crit_spill_time_pct ELSE :warn_spill_time_pct END,
                '%); worst day spilled ',
                ROUND(b.spilled_local_bytes_max_day / 1e9, 1), ' GB (threshold ',
                CASE WHEN b.mem_band = 'CRITICAL' THEN :crit_spill_gb ELSE :warn_spill_gb END,
                ' GB)') AS mem_clause,
         CONCAT(COALESCE(CAST(ROUND(b.capacity_wait_raw, 0) AS BIGINT), 0),
                '% of statement time was spent queued at capacity (threshold ',
                CASE WHEN b.cap_band = 'CRITICAL' THEN :crit_queue_time_pct ELSE :warn_queue_time_pct END,
                '%); ',
                CASE WHEN b.max_clusters IS NOT NULL AND b.min_clusters >= b.max_clusters
                     THEN CONCAT('no autoscale range (min_clusters ', b.min_clusters,
                                 ', max_clusters ', b.max_clusters, ')')
                     WHEN b.max_cluster_count_seen IS NULL OR b.max_clusters IS NULL
                     THEN 'clusters seen unknown'
                     ELSE CONCAT('up to ', b.max_cluster_count_seen, ' of ', b.max_clusters,
                                 ' clusters seen')
                END) AS cap_clause,
         -- a NONE row's shares are below their WARN thresholds; one that rounds up to its threshold
         -- is written "just under <threshold>", so the reason never prints the threshold itself
         CONCAT('no threshold crossed: ',
                CASE WHEN ROUND(COALESCE(b.spill_time_raw, 0), 0) >= :warn_spill_time_pct
                     THEN CONCAT('just under ', :warn_spill_time_pct)
                     ELSE CAST(CAST(ROUND(COALESCE(b.spill_time_raw, 0), 0) AS BIGINT) AS STRING)
                END,
                '% of execution time spilled, ',
                CASE WHEN ROUND(COALESCE(b.capacity_wait_raw, 0), 0) >= :warn_queue_time_pct
                     THEN CONCAT('just under ', :warn_queue_time_pct)
                     ELSE CAST(CAST(ROUND(COALESCE(b.capacity_wait_raw, 0), 0) AS BIGINT) AS STRING)
                END,
                '% of statement time queued') AS none_clause
  FROM banded b
)
SELECT j.workspace_id,
       j.warehouse_id,
       j.compute_type,
       j.warehouse_type,
       j.warehouse_size,
       j.min_clusters,
       j.max_clusters,
       j.auto_stop_minutes,
       j.warehouse_deleted,
       j.query_count,
       j.finished_count,
       j.spilling_query_count,
       ROUND(j.spill_query_raw, 1)                     AS spill_query_pct,
       ROUND(j.spill_time_raw, 1)                      AS spill_time_pct,
       ROUND(j.spilled_local_bytes_sum / 1e9, 2)       AS spilled_local_gb_sum,
       ROUND(j.spilled_local_bytes_max_day / 1e9, 2)   AS spilled_local_gb_max_day,
       ROUND(j.read_bytes_sum / 1e9, 2)                AS read_gb_sum,
       ROUND(j.shuffle_read_bytes_sum / 1e9, 2)        AS shuffle_read_gb_sum,
       j.queued_at_capacity_count,
       ROUND(j.capacity_wait_raw, 1)                   AS capacity_wait_pct,
       ROUND(j.waiting_at_capacity_ms_sum / 1000.0, 0) AS waiting_at_capacity_s_sum,
       j.waited_for_compute_count,
       ROUND(j.cold_start_wait_raw, 1)                 AS cold_start_wait_pct,
       ROUND(j.waiting_for_compute_ms_sum / 1000.0, 0) AS waiting_for_compute_s_sum,
       ROUND(j.execution_ms_sum / 1000.0, 0)           AS execution_s_sum,
       ROUND(j.total_duration_ms_sum / 1000.0, 0)      AS total_duration_s_sum,
       j.max_cluster_count_seen,
       j.scaling_events,
       j.at_ceiling,
       j.pressure,
       -- the lever (see actions): size for memory; clusters at the ceiling, a warm cluster below it
       CASE
         WHEN j.pressure = 'MEMORY'                        THEN 'SCALE_UP'
         WHEN j.pressure = 'MEMORY_AND_CAPACITY' AND NOT j.at_ceiling THEN 'SCALE_UP_THEN_WARM'
         WHEN j.pressure = 'MEMORY_AND_CAPACITY'           THEN 'SCALE_UP_THEN_OUT'
         WHEN j.pressure = 'CAPACITY' AND NOT j.at_ceiling THEN 'KEEP_WARM'
         WHEN j.pressure = 'CAPACITY'                      THEN 'SCALE_OUT'
         WHEN j.pressure = 'NONE'                          THEN 'NONE'
         ELSE NULL
       END AS scaling_hint,
       CASE
         WHEN j.pressure = 'MEMORY'              THEN j.mem_clause
         WHEN j.pressure = 'CAPACITY'            THEN j.cap_clause
         WHEN j.pressure = 'MEMORY_AND_CAPACITY' THEN CONCAT(j.mem_clause, '; ', j.cap_clause)
         WHEN j.pressure = 'NONE'                THEN j.none_clause
         ELSE NULL
       END AS pressure_reason,
       -- why a warehouse could not be judged (NULL when it could)
       CASE WHEN j.pressure IS NULL THEN 'too_few_statements' ELSE NULL END AS not_assessed_reason,
       -- status: the worse of the memory and concurrency bands; too few statements is NOT_ASSESSED
       CASE
         WHEN j.pressure IS NULL                                 THEN 'NOT_ASSESSED'
         WHEN j.mem_band = 'CRITICAL' OR j.cap_band = 'CRITICAL' THEN 'CRITICAL'
         WHEN j.mem_band = 'WARN' OR j.cap_band = 'WARN'         THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM judged j
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         spill_time_pct DESC,
         capacity_wait_pct DESC,
         workspace_id, warehouse_id
LIMIT :top_n
