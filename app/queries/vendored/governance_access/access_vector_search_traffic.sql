-- query_id: access_vector_search_traffic
-- title: Vector Search query and scan traffic by endpoint
-- domain: governance_access   tier: standard
-- reads: system.access.audit
-- requires: SELECT on system.access; Public Preview
-- empty_if: schema_not_enabled, preview_unavailable, verbose_audit_required, privilege_scoped
-- params: :period_days (default 30) rolling window in days.
-- confidence: needs_confirmation
-- confidence_note: service_name='vectorSearch' and the query/scan action_name set (queryVectorIndex, queryVectorIndexNextPage, queryVectorIndexRouteOptimized, scanVectorIndex, scanVectorIndexRouteOptimized) are confirmed against Databricks' documentation on unused Vector Search endpoints. request_params['index_name'] is NOT confirmed against a live account - it is inferred from the Query/Scan Index REST API's own path parameter, not read off a real event. Before trusting vector_search_key, run `SELECT DISTINCT map_keys(request_params) FROM system.access.audit WHERE service_name = 'vectorSearch'` on your own account and confirm which key is actually populated.
-- read_this: One row = a workspace + index (or endpoint, if that is the only key populated - see caveats) x action x day, with how many query/scan events it received. The columns that matter are vector_search_key (the grouping key) and event_count - join this against your own billing data to tell a provisioned-but-unqueried endpoint (bills spend, zero rows here) from a genuinely idle one.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_vector_search_spend (join this against spend to find endpoints that bill but never show up here), compute_serving_endpoint_cost_status (the general serving-endpoint idle-detection sibling)
-- caveats: service_name='vectorSearch' and the query/scan action_name set (queryVectorIndex, queryVectorIndexNextPage, queryVectorIndexRouteOptimized, scanVectorIndex, scanVectorIndexRouteOptimized) are confirmed against Databricks' documentation on unused Vector Search endpoints. GRAIN (fixes access_vector_search_traffic-endpoint-name-always-null): this query used to key rows on request_params['endpoint_name'] alone, but the real vectorSearch query/scan audit events we have reviewed do not carry that key at all - it read NULL on every row, since queryVectorIndex/scanVectorIndex operate on a named INDEX, not directly on an endpoint (the endpoint is implicit in the index). vector_search_key now reads request_params['index_name'] first, falling back to request_params['endpoint_name'] if some event shape carries that instead - see confidence_note before trusting either key on your account; if NEITHER key is populated, this query still collapses to one NULL group per day/action, exactly as before the fix. endpoint_name is kept, meaning unchanged (a straight read of request_params['endpoint_name'], MAX'd per group since it is no longer the group key itself), so nothing that already reads this column by name breaks; it will still read NULL on the events this fix was written against. distinct_index_names counts distinct request_params['index_name'] values folded into each vector_search_key group (0 when index_name is never populated). An endpoint/index that bills spend but has no row here is provisioned-but-unqueried - a retire candidate - but you need to join this against your own billing data; this query alone only tells you what was queried, not what was billed. If this source has no rows at all, idle status cannot be assessed from it - treat that as visibility-only, never assume idle. workspace_id is added to the SELECT and GROUP BY so the app's workspace filter can narrow this check; it does not change any other column's meaning.
SELECT event_date,
       workspace_id,
       action_name,
       -- index_name first (see caveats): queryVectorIndex/scanVectorIndex key on the index, not the endpoint.
       COALESCE(request_params['index_name'], request_params['endpoint_name']) AS vector_search_key,
       -- endpoint_name kept, unchanged, for compatibility -- no longer the group key (MAX per group).
       MAX(request_params['endpoint_name']) AS endpoint_name,
       COUNT(*) AS event_count,
       COUNT(DISTINCT request_params['index_name']) AS distinct_index_names
FROM system.access.audit
WHERE service_name = 'vectorSearch'
  AND action_name IN (
        'queryVectorIndex', 'queryVectorIndexNextPage',
        'queryVectorIndexRouteOptimized', 'scanVectorIndex',
        'scanVectorIndexRouteOptimized')
  AND event_date >= dateadd(day, -:period_days, current_date())
  AND event_date < current_date()
GROUP BY event_date, workspace_id, action_name, COALESCE(request_params['index_name'], request_params['endpoint_name'])
ORDER BY event_date DESC, vector_search_key, action_name
