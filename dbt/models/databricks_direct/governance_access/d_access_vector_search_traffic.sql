{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:governance_access', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_vector_search_traffic.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT event_date,
       workspace_id,
       action_name,
       -- index_name first (see caveats): queryVectorIndex/scanVectorIndex key on the index, not the endpoint.
       COALESCE(request_params['index_name'], request_params['endpoint_name']) AS vector_search_key,
       -- endpoint_name kept, unchanged, for compatibility -- no longer the group key (MAX per group).
       MAX(request_params['endpoint_name']) AS endpoint_name,
       COUNT(*) AS event_count,
       COUNT(DISTINCT request_params['index_name']) AS distinct_index_names
FROM {{ source('system_access', 'audit') }}
WHERE service_name = 'vectorSearch'
  AND action_name IN (
        'queryVectorIndex', 'queryVectorIndexNextPage',
        'queryVectorIndexRouteOptimized', 'scanVectorIndex',
        'scanVectorIndexRouteOptimized')
  AND event_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND event_date < {{ audit_today() }}
GROUP BY event_date, workspace_id, action_name, COALESCE(request_params['index_name'], request_params['endpoint_name'])
ORDER BY event_date DESC, vector_search_key, action_name
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
