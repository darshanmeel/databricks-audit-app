{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- Statements per (window_days, unit_id, own_keys, origin, job_id, pipeline_id, notebook_id):
-- own_keys is the sorted, comma-joined set of the query's OWN query_tags keys (normalised), so the
-- Tags page can tell which mandatory tags a query carried together, where it came from, and which
-- job or pipeline ran it. notebook_id is kept for serverless queries only, to find their usage
-- policy's bill. unit_id hashes exactly like tags.perf_unit. Twin of
-- app/direct_sql/tags/query_tag_keys.sql.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.query_tag_keys is DuckDB-only; the direct export has its own copy") }}
{% endif %}

WITH
{% for w in var('windows') %}
history_{{ w }} AS (
    SELECT {{ w }} AS window_days, q.workspace_id,
           CASE WHEN q.compute.warehouse_id IS NOT NULL THEN 'warehouse'
                WHEN q.compute.cluster_id IS NOT NULL THEN 'cluster'
                ELSE 'serverless' END AS compute_kind,
           COALESCE(q.compute.warehouse_id, q.compute.cluster_id) AS compute_id,
           CASE WHEN q.query_tags IS NULL THEN ''
                ELSE array_to_string(list_sort(list_distinct(list_filter(
                         list_transform(list_filter(map_entries(q.query_tags), e -> e.value IS NOT NULL),
                                        e -> regexp_replace(lower(e.key), '[ _-]+', '', 'g')),
                         k -> k <> ''))), ',')
           END AS own_keys,
           CASE WHEN q.query_source.job_info.job_id IS NOT NULL THEN 'job'
                WHEN q.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
                WHEN q.query_source.dashboard_id IS NOT NULL OR q.query_source.legacy_dashboard_id IS NOT NULL THEN 'dashboard'
                WHEN q.query_source.genie_space_id IS NOT NULL THEN 'genie'
                WHEN q.query_source.alert_id IS NOT NULL THEN 'alert'
                WHEN q.query_source.notebook_id IS NOT NULL THEN 'notebook'
                WHEN q.query_source.sql_query_id IS NOT NULL OR q.client_application ILIKE '%sql editor%' THEN 'sql_editor'
                WHEN q.client_application IS NOT NULL THEN 'tool'
                ELSE 'other' END AS origin,
           q.query_source.job_info.job_id AS job_id,
           q.query_source.pipeline_info.pipeline_id AS pipeline_id,
           CASE WHEN q.compute.warehouse_id IS NULL AND q.compute.cluster_id IS NULL
                THEN q.query_source.notebook_id END AS notebook_id
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
)
SELECT
    window_days,
    md5(COALESCE(workspace_id, '~') || '|' || compute_kind || '|' || COALESCE(compute_id, '~')) AS unit_id,
    own_keys,
    origin,
    job_id,
    pipeline_id,
    notebook_id,
    count(*) AS statements
FROM history
GROUP BY 1, 2, 3, 4, 5, 6, 7
