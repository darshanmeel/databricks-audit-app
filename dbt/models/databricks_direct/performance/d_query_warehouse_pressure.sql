{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/performance/query_warehouse_pressure.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
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
  FROM {{ source('system_query', 'history') }}
  WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND start_time < {{ audit_today() }}
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
         -- a spilling statement is one that reached {{ param('query_warehouse_pressure', 'min_stmt_spill_mb', 100) }} MB (10^6 bytes) of spill
         SUM(CASE WHEN spilled_local_bytes > 0
                   AND spilled_local_bytes >= {{ param('query_warehouse_pressure', 'min_stmt_spill_mb', 100) }} * 1e6
                  THEN 1 ELSE 0 END)                                         AS spilling_query_count,
         SUM(spilled_local_bytes)                                             AS spilled_local_bytes_sum,
         -- execution time of the spilling statements: the memory verdict is time-weighted
         SUM(CASE WHEN spilled_local_bytes > 0
                   AND spilled_local_bytes >= {{ param('query_warehouse_pressure', 'min_stmt_spill_mb', 100) }} * 1e6
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
  FROM {{ source('system_compute', 'warehouses') }}
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY warehouse_id ORDER BY change_time DESC
  ) = 1
),
events AS (
  -- the most clusters the warehouse ran at once in the window, and how often it scaled
  SELECT warehouse_id,
         MAX(cluster_count)                                                          AS max_cluster_count_seen,
         SUM(CASE WHEN event_type IN ('SCALED_UP', 'SCALED_DOWN') THEN 1 ELSE 0 END) AS scaling_events
  FROM {{ source('system_compute', 'warehouse_events') }}
  WHERE event_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND event_time < {{ audit_today() }}
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
           WHEN m.spill_time_raw >= {{ param('query_warehouse_pressure', 'crit_spill_time_pct', 25) }}
             OR m.spilled_local_bytes_max_day >= {{ param('query_warehouse_pressure', 'crit_spill_gb', 10) }} * 1e9 THEN 'CRITICAL'
           WHEN m.spill_time_raw >= {{ param('query_warehouse_pressure', 'warn_spill_time_pct', 10) }}
             OR m.spilled_local_bytes_max_day >= {{ param('query_warehouse_pressure', 'warn_spill_gb', 1) }} * 1e9 THEN 'WARN'
           ELSE 'OK'
         END AS mem_band,
         -- concurrency: the share of statement time queued at capacity
         CASE
           WHEN m.capacity_wait_raw >= {{ param('query_warehouse_pressure', 'crit_queue_time_pct', 20) }} THEN 'CRITICAL'
           WHEN m.capacity_wait_raw >= {{ param('query_warehouse_pressure', 'warn_queue_time_pct', 5) }} THEN 'WARN'
           ELSE 'OK'
         END AS cap_band
  FROM measured m
),
judged AS (
  SELECT b.*,
         -- the verdict (field heuristic); NULL = too few statements to judge
         CASE
           WHEN b.query_count < {{ param('query_warehouse_pressure', 'min_queries', 20) }}                                               THEN NULL
           WHEN b.mem_band IN ('WARN', 'CRITICAL') AND b.cap_band IN ('WARN', 'CRITICAL') THEN 'MEMORY_AND_CAPACITY'
           WHEN b.mem_band IN ('WARN', 'CRITICAL')                                         THEN 'MEMORY'
           WHEN b.cap_band IN ('WARN', 'CRITICAL')                                         THEN 'CAPACITY'
           ELSE 'NONE'
         END AS pressure,
         -- the WHY, from the row's own numbers: whole percentages, GB to one decimal, and the
         -- threshold of the band the row actually reached (CRITICAL quotes the critical values)
         CONCAT(COALESCE(CAST(ROUND(b.spill_time_raw, 0) AS BIGINT), 0),
                '% of execution time was in statements that spilled to local disk (threshold ',
                CASE WHEN b.mem_band = 'CRITICAL' THEN {{ param('query_warehouse_pressure', 'crit_spill_time_pct', 25) }} ELSE {{ param('query_warehouse_pressure', 'warn_spill_time_pct', 10) }} END,
                '%); worst day spilled ',
                ROUND(b.spilled_local_bytes_max_day / 1e9, 1), ' GB (threshold ',
                CASE WHEN b.mem_band = 'CRITICAL' THEN {{ param('query_warehouse_pressure', 'crit_spill_gb', 10) }} ELSE {{ param('query_warehouse_pressure', 'warn_spill_gb', 1) }} END,
                ' GB)') AS mem_clause,
         CONCAT(COALESCE(CAST(ROUND(b.capacity_wait_raw, 0) AS BIGINT), 0),
                '% of statement time was spent queued at capacity (threshold ',
                CASE WHEN b.cap_band = 'CRITICAL' THEN {{ param('query_warehouse_pressure', 'crit_queue_time_pct', 20) }} ELSE {{ param('query_warehouse_pressure', 'warn_queue_time_pct', 5) }} END,
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
                CASE WHEN ROUND(COALESCE(b.spill_time_raw, 0), 0) >= {{ param('query_warehouse_pressure', 'warn_spill_time_pct', 10) }}
                     THEN CONCAT('just under ', {{ param('query_warehouse_pressure', 'warn_spill_time_pct', 10) }})
                     ELSE CAST(CAST(ROUND(COALESCE(b.spill_time_raw, 0), 0) AS BIGINT) AS STRING)
                END,
                '% of execution time spilled, ',
                CASE WHEN ROUND(COALESCE(b.capacity_wait_raw, 0), 0) >= {{ param('query_warehouse_pressure', 'warn_queue_time_pct', 5) }}
                     THEN CONCAT('just under ', {{ param('query_warehouse_pressure', 'warn_queue_time_pct', 5) }})
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
LIMIT {{ param('query_warehouse_pressure', 'top_n', 100000) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
