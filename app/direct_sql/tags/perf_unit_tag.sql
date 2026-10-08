-- hand-written; not generated. Databricks twin of dbt/models/tags/perf_unit_tag.sql: one row per
-- (window_days, unit_id, level, tag_key, tag_value) -- tags.perf_unit's own REAL tag values.
-- __WINDOW_DAYS__ is filled in at export time.
--
-- perf_unit_local recomputes tags/perf_unit.sql's own history/hash/group pipeline (Databricks has
-- no `tags` schema to ref() at export time); it MUST hash identically to perf_unit.sql or a
-- statement's tag never finds its unit -- see that file's own header note for the same rule.
WITH history AS (
    SELECT __WINDOW_DAYS__ AS window_days, q.workspace_id,
           CASE WHEN q.compute.warehouse_id IS NOT NULL THEN 'warehouse'
                WHEN q.compute.cluster_id IS NOT NULL THEN 'cluster'
                ELSE 'serverless' END AS compute_kind,
           COALESCE(q.compute.warehouse_id, q.compute.cluster_id) AS compute_id,
           q.execution_status, q.total_duration_ms,
           COALESCE(q.waiting_at_capacity_duration_ms, 0) + COALESCE(q.waiting_for_compute_duration_ms, 0) AS queue_ms,
           q.spilled_local_bytes, q.query_tags,
           monotonically_increasing_id() AS srid
    FROM `system`.`query`.`history` q
    WHERE q.start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      AND q.start_time < __AS_OF_DATE__
),
hashed AS (
    SELECT *,
           md5(COALESCE(workspace_id, '~') || '|' || compute_kind || '|' || COALESCE(compute_id, '~')) AS unit_id
    FROM history
),
perf_unit_local AS (
    SELECT window_days, unit_id,
           ANY_VALUE(workspace_id) AS workspace_id, ANY_VALUE(compute_kind) AS compute_kind,
           ANY_VALUE(compute_id) AS compute_id,
           COUNT(*) AS statements,
           SUM(CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END) AS failed_statements,
           COALESCE(SUM(total_duration_ms), 0) AS duration_ms,
           COALESCE(SUM(queue_ms), 0) AS queue_ms,
           COALESCE(SUM(spilled_local_bytes), 0) AS spill_bytes
    FROM hashed
    GROUP BY window_days, unit_id
),

-- ---------------------------------------------------------------------------------------------
-- Compute level: the resource's own LATEST tags. Clusters fall back PER KEY to their instance
-- pool's value, worker pool first, then driver pool.
-- ---------------------------------------------------------------------------------------------
warehouse_tags_latest AS (
    SELECT workspace_id, warehouse_id, tags FROM (
        SELECT workspace_id, warehouse_id, tags,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn
        FROM __SRC_COMPUTE_WAREHOUSES__
    ) x WHERE rn = 1
),
pool_tags_latest AS (
    SELECT workspace_id, instance_pool_id, tags FROM (
        SELECT workspace_id, instance_pool_id, tags,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, instance_pool_id ORDER BY change_time DESC) AS rn
        FROM __SRC_COMPUTE_INSTANCE_POOLS__
    ) x WHERE rn = 1
),
cluster_tags_latest AS (
    SELECT workspace_id, cluster_id, tags, worker_instance_pool_id, driver_instance_pool_id FROM (
        SELECT workspace_id, cluster_id, tags, worker_instance_pool_id, driver_instance_pool_id,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) AS rn
        FROM __SRC_COMPUTE_CLUSTERS__
    ) x WHERE rn = 1
),
-- Per-key pool fallback (spec 3.3): cluster tags first, then worker-pool, then driver-pool; only
-- the highest-priority row survives per (workspace_id, cluster_id, tag_key).
cluster_tag_candidates AS (
    SELECT c.workspace_id, c.cluster_id, raw_key, raw_value, 'cluster_tags' AS source, 1 AS priority
    FROM cluster_tags_latest c
    LATERAL VIEW explode(c.tags) x AS raw_key, raw_value

    UNION ALL

    SELECT c.workspace_id, c.cluster_id, raw_key, raw_value, 'pool_tags' AS source, 2 AS priority
    FROM cluster_tags_latest c
    JOIN pool_tags_latest wp ON wp.workspace_id = c.workspace_id AND wp.instance_pool_id = c.worker_instance_pool_id
    LATERAL VIEW explode(wp.tags) x AS raw_key, raw_value

    UNION ALL

    SELECT c.workspace_id, c.cluster_id, raw_key, raw_value, 'pool_tags' AS source, 3 AS priority
    FROM cluster_tags_latest c
    JOIN pool_tags_latest dp ON dp.workspace_id = c.workspace_id AND dp.instance_pool_id = c.driver_instance_pool_id
    LATERAL VIEW explode(dp.tags) x AS raw_key, raw_value
),
cluster_tag_pairs AS (
    SELECT workspace_id, cluster_id, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value, source
    FROM cluster_tag_candidates
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY workspace_id, cluster_id, regexp_replace(lower(raw_key), '[ _-]+', '')
        ORDER BY priority, raw_key
    ) = 1
),
warehouse_pairs AS (
    SELECT u.window_days, u.unit_id, u.statements, u.failed_statements, u.duration_ms, u.queue_ms,
           u.spill_bytes, 'warehouse_tags' AS source, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM perf_unit_local u
    JOIN warehouse_tags_latest wt
      ON wt.workspace_id = u.workspace_id AND u.compute_kind = 'warehouse' AND wt.warehouse_id = u.compute_id
    LATERAL VIEW explode(wt.tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY u.window_days, u.unit_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
cluster_pairs AS (
    SELECT u.window_days, u.unit_id, u.statements, u.failed_statements, u.duration_ms, u.queue_ms,
           u.spill_bytes, ctp.source, ctp.raw_key, ctp.tag_key, ctp.tag_value
    FROM perf_unit_local u
    JOIN cluster_tag_pairs ctp
      ON ctp.workspace_id = u.workspace_id AND u.compute_kind = 'cluster' AND ctp.cluster_id = u.compute_id
),
compute_pairs AS (
    SELECT * FROM warehouse_pairs
    UNION ALL
    SELECT * FROM cluster_pairs
),
compute_level AS (
    SELECT window_days, unit_id, 'compute' AS level, tag_key, raw_key, tag_value, source,
           statements, failed_statements, duration_ms, queue_ms, spill_bytes
    FROM compute_pairs
),

-- ---------------------------------------------------------------------------------------------
-- Work level: query.history.query_tags, per statement, summed per (unit, key, value).
-- ---------------------------------------------------------------------------------------------
stmt_pairs AS (
    SELECT h.window_days, h.unit_id, h.srid, h.execution_status, h.total_duration_ms, h.queue_ms,
           h.spilled_local_bytes, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM hashed h
    LATERAL VIEW explode(h.query_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY h.window_days, h.srid, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
work_level AS (
    SELECT window_days, unit_id, 'work' AS level, tag_key, ANY_VALUE(raw_key) AS raw_key, tag_value,
           'query_tags' AS source,
           COUNT(*) AS statements,
           SUM(CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END) AS failed_statements,
           COALESCE(SUM(total_duration_ms), 0) AS duration_ms,
           COALESCE(SUM(queue_ms), 0) AS queue_ms,
           COALESCE(SUM(spilled_local_bytes), 0) AS spill_bytes
    FROM stmt_pairs
    GROUP BY window_days, unit_id, tag_key, tag_value
)

SELECT window_days, unit_id, level, tag_key, raw_key, tag_value, source,
       statements, failed_statements, duration_ms, queue_ms, spill_bytes
FROM compute_level

UNION ALL

SELECT window_days, unit_id, level, tag_key, raw_key, tag_value, source,
       statements, failed_statements, duration_ms, queue_ms, spill_bytes
FROM work_level
