-- generated from dbt/models/databricks_direct/serving_ai/d_compute_ai_gateway_usage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/serving_ai/compute_ai_gateway_usage.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT
  CAST(g.event_time AS DATE)           AS usage_date,
  g.workspace_id                       AS workspace_id,
  g.endpoint_name AS endpoint_name,
  g.requester                                  AS requester,
  COUNT(*)                                                                          AS total_requests,
  -- 2xx == success; 429 == rate-limit/quota; other == error (unconfirmed mapping, see caveats).
  SUM(CASE WHEN g.status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END)                AS success_requests,
  SUM(CASE WHEN g.status_code = 429 THEN 1 ELSE 0 END)                              AS rate_limited_requests,
  SUM(CASE WHEN g.status_code IS NOT NULL
            AND NOT (g.status_code BETWEEN 200 AND 299)
            AND g.status_code <> 429 THEN 1 ELSE 0 END)                            AS error_requests,
  -- Token throughput is its own magnitude, never blended with request counts.
  SUM(COALESCE(g.input_tokens, 0))                                                  AS input_tokens,
  SUM(COALESCE(g.output_tokens, 0))                                                 AS output_tokens,
  -- Latency via percentile (exact) -- percentile_approx differs between the two build paths.
  percentile(g.latency_ms, 0.5)                                                    AS p50_latency_ms,
  percentile(g.latency_ms, 0.95)                                                   AS p95_latency_ms,
  MAX(g.latency_ms)                                                                AS max_latency_ms
FROM `system`.`ai_gateway`.`usage` g
WHERE g.event_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND g.event_time <  __AS_OF_DATE__
GROUP BY
  CAST(g.event_time AS DATE),
  g.workspace_id,
  g.endpoint_name,
  g.requester
ORDER BY usage_date DESC, total_requests DESC
) q
