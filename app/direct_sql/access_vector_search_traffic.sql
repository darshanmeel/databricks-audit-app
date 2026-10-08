-- generated from dbt/models/databricks_direct/governance_access/d_access_vector_search_traffic.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_vector_search_traffic.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
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
FROM `system`.`access`.`audit`
WHERE service_name = 'vectorSearch'
  AND action_name IN (
        'queryVectorIndex', 'queryVectorIndexNextPage',
        'queryVectorIndexRouteOptimized', 'scanVectorIndex',
        'scanVectorIndexRouteOptimized')
  AND event_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND event_date < __AS_OF_DATE__
GROUP BY event_date, workspace_id, action_name, COALESCE(request_params['index_name'], request_params['endpoint_name'])
ORDER BY event_date DESC, vector_search_key, action_name
) q
