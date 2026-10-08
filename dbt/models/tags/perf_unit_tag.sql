{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.1). One row per (window_days, unit_id, level, tag_key,
-- tag_value): tags.perf_unit's own REAL tag values, at the compute level (the warehouse's or
-- cluster's own LATEST tags -- per KEY, a cluster with no value of its own for that key falls
-- back to its instance pool's value, worker pool first, then driver pool, section 3.3) and the
-- work level (query.history.query_tags per statement,
-- summed to the unit -- section 6.1). No dollars anywhere (DEC-61.1); the five performance metrics
-- (statements, failed_statements, duration_ms, queue_ms, spill_bytes) are summed per value exactly
-- like usd is for cost_unit_tag. No "untagged" row here either -- app/core/rollup.py computes the
-- untagged remainder per unit at request time, from tags.perf_unit's own totals.
--
-- Compute-level joins tags.perf_unit by (workspace_id, compute_kind, compute_id) -- its hash has
-- no per-row tag component (unlike cost_unit's), so it is safe to ref() it directly here rather
-- than recompute the grouping. Work-level needs PER-STATEMENT detail query.history alone has, so
-- that half recomputes the same (workspace_id, compute_kind, compute_id) classification and unit_id
-- formula from tags.perf_unit.sql, duplicated on purpose (same reasoning as cost_unit_tag.sql's
-- header note) -- the two MUST hash identically or a statement's tag never finds its unit.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.perf_unit_tag is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}

WITH
-- ---------------------------------------------------------------------------------------------
-- Compute level: the resource's own LATEST tags. Warehouses stay simple (their own map only).
-- Clusters fall back PER KEY to their instance pool's value (worker pool first, then driver
-- pool) -- see cluster_tag_candidates/cluster_tag_pairs below.
-- ---------------------------------------------------------------------------------------------
warehouse_tags_latest AS (
    SELECT workspace_id, warehouse_id, tags FROM (
        SELECT workspace_id, warehouse_id, tags,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn
        FROM {{ source('system_compute', 'warehouses') }}
    ) WHERE rn = 1
),
pool_tags_latest AS (
    SELECT workspace_id, instance_pool_id, tags FROM (
        SELECT workspace_id, instance_pool_id, tags,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, instance_pool_id ORDER BY change_time DESC) AS rn
        FROM {{ source('system_compute', 'instance_pools') }}
    ) WHERE rn = 1
),
cluster_tags_latest AS (
    SELECT workspace_id, cluster_id, tags, worker_instance_pool_id, driver_instance_pool_id FROM (
        SELECT workspace_id, cluster_id, tags, worker_instance_pool_id, driver_instance_pool_id,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) AS rn
        FROM {{ source('system_compute', 'clusters') }}
    ) WHERE rn = 1
),
-- Per-key pool fallback (spec 3.3: "A cluster with no value for K takes its instance pool's
-- value, worker pool first, then driver pool"). This is per KEY, not per map: a cluster tagged
-- {team: x} on a pool tagged {cc: poolteam} still reads poolteam for cc, even though the cluster
-- carries its own (unrelated) tags. So each source's raw key/value pairs are exploded separately,
-- given a priority (cluster tags first, then worker-pool, then driver-pool), and only the
-- highest-priority row survives per (workspace_id, cluster_id, tag_key).
cluster_tag_candidates AS (
    SELECT c.workspace_id, c.cluster_id, x.raw_key, x.raw_value, 'cluster_tags' AS source, 1 AS priority
    FROM cluster_tags_latest c, LATERAL (
        SELECT e.key AS raw_key, e.value AS raw_value FROM unnest(map_entries(c.tags)) AS y(e)
    ) x
    UNION ALL
    SELECT c.workspace_id, c.cluster_id, x.raw_key, x.raw_value, 'pool_tags' AS source, 2 AS priority
    FROM cluster_tags_latest c
    JOIN pool_tags_latest wp ON wp.workspace_id = c.workspace_id AND wp.instance_pool_id = c.worker_instance_pool_id
    , LATERAL (SELECT e.key AS raw_key, e.value AS raw_value FROM unnest(map_entries(wp.tags)) AS y(e)) x
    UNION ALL
    SELECT c.workspace_id, c.cluster_id, x.raw_key, x.raw_value, 'pool_tags' AS source, 3 AS priority
    FROM cluster_tags_latest c
    JOIN pool_tags_latest dp ON dp.workspace_id = c.workspace_id AND dp.instance_pool_id = c.driver_instance_pool_id
    , LATERAL (SELECT e.key AS raw_key, e.value AS raw_value FROM unnest(map_entries(dp.tags)) AS y(e)) x
),
cluster_tag_pairs AS (
    SELECT workspace_id, cluster_id, raw_key,
           {{ norm_tag_key('raw_key') }} AS tag_key, trim(raw_value) AS tag_value, source
    FROM cluster_tag_candidates
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL AND {{ norm_tag_key('raw_key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id, {{ norm_tag_key('raw_key') }} ORDER BY priority, raw_key) = 1
),
warehouse_pairs AS (
    SELECT u.window_days, u.unit_id, u.statements, u.failed_statements, u.duration_ms, u.queue_ms,
           u.spill_bytes, 'warehouse_tags' AS source, x.raw_key,
           {{ norm_tag_key('x.raw_key') }} AS tag_key, trim(x.raw_value) AS tag_value
    FROM {{ ref('perf_unit') }} u
    JOIN warehouse_tags_latest wt
      ON wt.workspace_id = u.workspace_id AND u.compute_kind = 'warehouse' AND wt.warehouse_id = u.compute_id
    , LATERAL (SELECT e.key AS raw_key, e.value AS raw_value FROM unnest(map_entries(wt.tags)) AS y(e)) x
    WHERE x.raw_key IS NOT NULL AND x.raw_value IS NOT NULL AND {{ norm_tag_key('x.raw_key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.window_days, u.unit_id, {{ norm_tag_key('x.raw_key') }} ORDER BY x.raw_key) = 1
),
cluster_pairs AS (
    SELECT u.window_days, u.unit_id, u.statements, u.failed_statements, u.duration_ms, u.queue_ms,
           u.spill_bytes, ctp.source, ctp.raw_key, ctp.tag_key, ctp.tag_value
    FROM {{ ref('perf_unit') }} u
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
-- Work level: query.history.query_tags, per statement, summed per (unit, key, value). Recomputes
-- the SAME classification/hash as tags.perf_unit.sql -- see header note.
-- ---------------------------------------------------------------------------------------------
{% for w in var('windows') %}
history_{{ w }} AS (
    SELECT {{ w }} AS window_days, q.workspace_id,
           CASE WHEN q.compute.warehouse_id IS NOT NULL THEN 'warehouse'
                WHEN q.compute.cluster_id IS NOT NULL THEN 'cluster'
                ELSE 'serverless' END AS compute_kind,
           COALESCE(q.compute.warehouse_id, q.compute.cluster_id) AS compute_id,
           q.execution_status, q.total_duration_ms,
           COALESCE(q.waiting_at_capacity_duration_ms, 0) + COALESCE(q.waiting_for_compute_duration_ms, 0) AS queue_ms,
           q.spilled_local_bytes, q.query_tags,
           ROW_NUMBER() OVER () AS srid
    FROM {{ source('system_query', 'history') }} q
    WHERE q.start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAY
      AND q.start_time < {{ audit_today() }}
),
{% endfor %}
history AS (
    {% for w in var('windows') %}
    SELECT * FROM history_{{ w }}
    {%- if not loop.last %}
    UNION ALL
    {% endif %}
    {%- endfor %}
),
hashed AS (
    SELECT *,
           md5(COALESCE(workspace_id, '~') || '|' || compute_kind || '|' || COALESCE(compute_id, '~')) AS unit_id
    FROM history
),
stmt_pairs AS (
    SELECT h.window_days, h.unit_id, h.srid, h.execution_status, h.total_duration_ms, h.queue_ms,
           h.spilled_local_bytes, x.raw_key,
           {{ norm_tag_key('x.raw_key') }} AS tag_key, trim(x.raw_value) AS tag_value
    FROM hashed h, LATERAL (
        SELECT e.key AS raw_key, e.value AS raw_value FROM unnest(map_entries(h.query_tags)) AS y(e)
    ) x
    WHERE x.raw_key IS NOT NULL AND x.raw_value IS NOT NULL AND {{ norm_tag_key('x.raw_key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY h.window_days, h.srid, {{ norm_tag_key('x.raw_key') }} ORDER BY x.raw_key) = 1
),
work_level AS (
    SELECT window_days, unit_id, 'work' AS level, tag_key, ANY_VALUE(raw_key) AS raw_key, tag_value,
           'query_tags' AS source,
           COUNT(*) AS statements,
           COUNT(*) FILTER (WHERE execution_status = 'FAILED') AS failed_statements,
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
