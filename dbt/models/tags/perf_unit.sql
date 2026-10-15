{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.1). The performance rollup's own unit: one row per
-- (window_days, unit_id), unit_id = md5(workspace_id|compute_kind|compute_id) (stable across
-- windows, same convention as tags.cost_unit -- '~' for NULL), grouped from system.query.history
-- by (workspace_id, compute_kind, compute_id) over the window (by start_time, the vendored
-- query.history convention every other performance query already uses).
--
-- compute_kind: 'warehouse' when compute.warehouse_id is set, 'cluster' when compute.cluster_id is
-- set, else 'serverless' (no warehouse, no cluster -- section 6.1). queue_ms sums
-- waiting_at_capacity_duration_ms + waiting_for_compute_duration_ms (NULLs as 0); duration_ms is
-- total_duration_ms; failed_statements counts execution_status = 'FAILED'. DEC-61.1: no dollars
-- here at all, ever.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.perf_unit is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}

WITH
{% for w in var('windows') %}
history_{{ w }} AS (
    SELECT {{ w }} AS window_days, q.workspace_id,
           CASE WHEN q.compute.warehouse_id IS NOT NULL THEN 'warehouse'
                WHEN q.compute.cluster_id IS NOT NULL THEN 'cluster'
                ELSE 'serverless' END AS compute_kind,
           COALESCE(q.compute.warehouse_id, q.compute.cluster_id) AS compute_id,
           q.execution_status, q.total_duration_ms,
           COALESCE(q.waiting_at_capacity_duration_ms, 0) + COALESCE(q.waiting_for_compute_duration_ms, 0) AS queue_ms,
           q.spilled_local_bytes
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
)
SELECT
    window_days,
    unit_id,
    ANY_VALUE(workspace_id) AS workspace_id,
    ANY_VALUE(compute_kind) AS compute_kind,
    ANY_VALUE(compute_id) AS compute_id,
    COUNT(*) AS statements,
    COUNT(*) FILTER (WHERE execution_status = 'FAILED') AS failed_statements,
    COALESCE(SUM(total_duration_ms), 0) AS duration_ms,
    COALESCE(SUM(queue_ms), 0) AS queue_ms,
    COALESCE(SUM(spilled_local_bytes), 0) AS spill_bytes
FROM hashed
GROUP BY window_days, unit_id
