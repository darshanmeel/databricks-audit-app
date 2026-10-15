-- query_id: compute_ai_gateway_usage
-- title: AI Gateway usage, tokens, and latency by endpoint
-- domain: serving_ai   tier: standard
-- reads: system.ai_gateway.usage
-- requires: SELECT on system.ai_gateway; must be enabled per-metastore (empty until AI Gateway is enabled on an endpoint)
-- empty_if: usage_tracking_off, account_admin_only, schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: Column names (event_time, workspace_id, endpoint_name, requester, status_code, latency_ms, input_tokens, output_tokens) and the 2xx/429/error status-code grouping are verified against a live workspace system-schema dump but not against published Databricks documentation, so treat the request classification as a working assumption until you confirm it against your own workspace.
-- read_this: One row = a day + workspace + endpoint + requester's AI Gateway traffic. The columns that matter are total_requests, rate_limited_requests, and error_requests - a rise in rate_limited_requests means you are hitting Gateway or provider rate limits, and a rise in error_requests means calls are failing outright; both mean tokens and latency spent on calls that did not succeed.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: compute_serving_endpoint_usage (if you want the underlying serving-endpoint request/error/token detail), cost_by_compute_resource (if you want this endpoint's DBU cost)
-- caveats: system.ai_gateway.usage is empty unless AI Gateway is enabled on an endpoint - read zero rows as "AI Gateway not in use / not measured", never as a true zero. Column names are verified against a live workspace system-schema dump: event_time (timestamp), workspace_id, endpoint_name, requester (the calling principal), status_code (HTTP), latency_ms (end-to-end latency), input_tokens, output_tokens. There is no request_count column - each row is a single request, so requests are tallied with COUNT(*). status_code 429 is assumed to be the rate-limit/quota literal; this mapping is unconfirmed. Latency is summarized with percentile(latency_ms, fraction), exact on both build engines (translate.py maps it to DuckDB's quantile_cont), so a build never disagrees with itself over a sample small enough for an approximation to matter. Any metric whose source column is absent should read as "unknown", not as a finding. requester is an individual user or service principal, not a team - do not relabel it as one. A 429 or error row is a rate-limit/abuse SIGNAL, not proof of misuse - confirm before acting on it.
-- Reads of system.ai_gateway.usage are account-admin-only, so a non-account-admin (even one nominally granted SELECT) sees zero rows regardless of actual traffic - confirm you are querying as an account admin before reading empty results as "not in use".
SELECT
  CAST(g.event_time AS DATE)           AS usage_date,
  g.workspace_id                       AS workspace_id,
  g.endpoint_name AS endpoint_name,
  CASE
    WHEN g.requester IS NULL OR g.requester = '__REDACTED__' THEN g.requester
    WHEN g.requester RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN g.requester
    ELSE concat(substr(sha2(lower(trim(g.requester)), 256), 1, 8), ' ', substr(g.requester, 1, 2), '***')
  END                                  AS requester,
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
FROM system.ai_gateway.usage g
WHERE g.event_time >= dateadd(day, -:period_days, current_date())
  AND g.event_time <  current_date()
GROUP BY
  CAST(g.event_time AS DATE),
  g.workspace_id,
  g.endpoint_name,
  g.requester
ORDER BY usage_date DESC, total_requests DESC
