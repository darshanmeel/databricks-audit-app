"""ML & AI tab (web/src/tabs/tab_mlai.tsx): Spend, Endpoints, AI Gateway."""
from tools.dashboard_kit import INT, MONEY, NUM, PCT, bar, kpi, table

# Endpoints dedupe: compute_serving_endpoint_cost_status fans out one row per served entity, with
# the endpoint-level $/requests repeated identically on each of an endpoint's rows (the check's own
# header note) -- MAX per (workspace, endpoint) collapses that back to one row per endpoint, the
# same thing the app's own dedupeByEndpoint does client-side. err is joined in by workspace+id only
# where used; a name-only endpoint (no id, e.g. Vector Search) just shows no error count.
_EP_CTE = """WITH raw AS (SELECT * FROM {named:compute_serving_endpoint_cost_status} WHERE {ws}),
base AS (
  SELECT workspace_id, COALESCE(endpoint_id, endpoint_name) AS ep_key,
         MAX(concat(COALESCE(endpoint_name, endpoint_id), ' . ', COALESCE(nm_workspace, workspace_id))) AS endpoint,
         MAX(endpoint_id) AS endpoint_id, MAX(status) AS status,
         MAX(endpoint_requests_window) AS requests, MAX(est_usd_list) AS usd,
         MAX(tracking_status) AS tracking_status
  FROM raw
  GROUP BY workspace_id, COALESCE(endpoint_id, endpoint_name)
),
err AS (
  SELECT workspace_id, endpoint_id, MAX(error_requests) AS errors
  FROM {S}.serving_endpoint_traffic_by_endpoint
  GROUP BY 1, 2
)
"""

# Endpoint name across workspaces: "Serving endpoint" exists in most workspaces, so a chart grouped
# on the plain name alone would merge them into one bar. This labels and groups by workspace too,
# the same disambiguation {named:...} gives lbl_<kind> for warehouses/jobs/clusters (no endpoint
# entry there, so it is built by hand); only when a check has no workspace_id does the plain name
# stand alone.
def _ep_label(name_expr: str) -> str:
    return f"CASE WHEN ws_name IS NULL THEN {name_expr} ELSE concat({name_expr}, ' · ', ws_name) END"


def build(d):
    spend(d)
    endpoints(d)
    gateway(d)


def spend(d):
    p = d.page("spend")
    p.kpis(
        kpi("ML & AI spend", """
WITH p AS (
  SELECT billing_origin_product, SUM(net_list_cost) AS usd
  FROM {named:cost_dollarized_by_sku_day}
  WHERE {ws} AND billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH')
  GROUP BY 1
)
SELECT ROUND(COALESCE((SELECT SUM(usd) FROM p), 0) * {disc}, 0) AS value,
       concat('Model serving $', CAST(ROUND(COALESCE((SELECT usd FROM p WHERE billing_origin_product = 'MODEL_SERVING'), 0) * {disc}, 0) AS STRING),
              '; Vector Search $', CAST(ROUND(COALESCE((SELECT usd FROM p WHERE billing_origin_product = 'VECTOR_SEARCH'), 0) * {disc}, 0) AS STRING)) AS target
""", target="target", fmt=MONEY,
            how="List $ for MODEL_SERVING + VECTOR_SEARCH SKUs this window, after your discount."),
        kpi("Scale-from-zero launches (%)", """
WITH m AS (
  SELECT is_launch_sku, SUM(net_list_cost) AS usd
  FROM {named:cost_serving_mode_by_endpoint}
  WHERE {ws}
  GROUP BY 1
)
SELECT ROUND(100.0 * COALESCE((SELECT usd FROM m WHERE is_launch_sku), 0) / NULLIF((SELECT SUM(usd) FROM m), 0), 0) AS value,
       'Model-serving $ on launch (cold-start) SKUs' AS target
""", target="target", fmt=PCT,
            how="Model-serving $ billed on scale-from-zero (launch) SKUs, as a share of all model-serving $."),
        kpi("Vector Search spend", """
SELECT ROUND(COALESCE(SUM(net_list_cost), 0) * {disc}, 0) AS value, 'List price' AS target
FROM {named:cost_vector_search_spend}
WHERE {ws}
""", target="target", fmt=MONEY,
            how="List $ for Vector Search SKUs this window, after your discount."),
    )
    p.charts(
        bar("Compute time by endpoint", """
SELECT """ + _ep_label("COALESCE(endpoint_name, endpoint_id)") + """ AS endpoint, SUM(net_usage_quantity) AS dbus
FROM {named:cost_by_serving_endpoint}
WHERE {ws} AND usage_type = 'COMPUTE_TIME'
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "endpoint", "dbus", fmt=NUM, category_title="Endpoint", value_title="DBUs",
            how="DBUs billed as compute time, per endpoint per workspace; same endpoint name in two workspaces stays two bars."),
    )
    p.charts(
        bar("Model-serving $ by endpoint", """
SELECT """ + _ep_label("COALESCE(endpoint_name, endpoint_id)") + """ AS endpoint, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:cost_serving_mode_by_endpoint}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "endpoint", "usd", fmt=MONEY, category_title="Endpoint", value_title="Spend",
            how="List $ per endpoint per workspace, after your discount; same endpoint name in two workspaces stays two bars."),
        bar("Vector Search $ by endpoint", """
SELECT """ + _ep_label("COALESCE(endpoint_name, endpoint_id)") + """ AS endpoint, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:cost_vector_search_spend}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "endpoint", "usd", fmt=MONEY, category_title="Endpoint", value_title="Spend",
            how="Vector Search list $ per endpoint per workspace, after your discount; same name in two workspaces stays two bars."),
    )
    p.text(["GenAI token & GPU spend has its own home: Cost > By resource."], height=1)
    p.checks()


def endpoints(d):
    p = d.page("endpoints")
    p.kpis(
        kpi("Endpoints with traffic", _EP_CTE + """
SELECT (SELECT count_if(requests > 0) FROM base) AS value,
       concat('of ', CAST((SELECT count(*) FROM base) AS STRING), ' endpoints') AS target
""", target="target",
            how="Endpoints with at least one request this window, one row per workspace + endpoint."),
        kpi("Requests", _EP_CTE + """
SELECT (SELECT COALESCE(SUM(requests), 0) FROM base) AS value, 'Across all endpoints' AS target
""", target="target",
            how="Sum of requests across all tracked endpoints, one row per workspace + endpoint."),
        kpi("Spend with no requests", _EP_CTE + """
SELECT (SELECT ROUND(COALESCE(SUM(CASE WHEN requests = 0 AND usd > 0 AND status <> 'NOT_ASSESSED' THEN usd END), 0) * {disc}, 0) FROM base) AS value,
       COALESCE((SELECT concat('Largest: ', endpoint) FROM base WHERE requests = 0 AND usd > 0 AND status <> 'NOT_ASSESSED' ORDER BY usd DESC LIMIT 1), 'None idle') AS target
""", target="target", fmt=MONEY, tone="warn",
            how="List $ billed this window on endpoints with zero requests and a tracked status; your discount applied."),
        kpi("Days over $50/day", """
SELECT COUNT(DISTINCT CASE WHEN status IN ('CRITICAL', 'WARN') THEN concat(workspace_id, '|', endpoint_id, '|', CAST(usage_date AS STRING)) END) AS value,
       'Endpoint-days over the warning line' AS target
FROM {named:cost_serving_mode_by_endpoint}
WHERE {ws}
""", target="target", tone="warn",
            how="Endpoint-days that hit the $50 warn or $250 critical daily list-price line (per workspace, endpoint, usage type)."),
    )
    p.charts(
        bar("Spend by endpoint", _EP_CTE + """
SELECT endpoint, ROUND(usd * {disc}, 0) AS usd
FROM base
ORDER BY usd DESC LIMIT 8
""", "endpoint", "usd", fmt=MONEY, category_title="Endpoint", value_title="Spend",
            how="List $ per endpoint this window, after your discount; one row per workspace + endpoint, labeled with its workspace."),
        bar("Requests by endpoint", _EP_CTE + """
SELECT endpoint, requests
FROM base
ORDER BY requests DESC LIMIT 8
""", "endpoint", "requests", fmt=INT, category_title="Endpoint", value_title="Requests",
            how="Requests per endpoint this window, one row per workspace + endpoint, labeled with its workspace."),
    )
    p.table(table("Endpoints, worst first", _EP_CTE + """
SELECT b.status, b.endpoint, b.requests, COALESCE(e.errors, 0) AS errors,
       ROUND(b.usd * {disc}, 0) AS usd, b.tracking_status
FROM base b
LEFT JOIN err e ON e.workspace_id = b.workspace_id AND e.endpoint_id = b.endpoint_id
ORDER BY CASE b.status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, b.usd DESC
LIMIT 100
""", [("status", "Status"), ("endpoint", "Endpoint"), ("requests", "Requests", INT), ("errors", "Errors", INT),
      ("usd", "$ billed", MONEY), ("tracking_status", "Tracking")],
        how="Each endpoint's billed $, requests and errors (deduped per workspace + endpoint); worst check status first."))
    p.checks()


def gateway(d):
    p = d.page("gateway")
    p.kpis(
        kpi("Requests", """
SELECT COALESCE(SUM(total_requests), 0) AS value, 'Across endpoints' AS target
FROM {named:compute_ai_gateway_usage}
WHERE {ws}
""", target="target",
            how="Sum of all requests routed through the AI Gateway this window."),
        kpi("Errored (%)", """
SELECT ROUND(100.0 * COALESCE(SUM(error_requests), 0) / NULLIF(SUM(total_requests), 0), 1) AS value,
       concat(CAST(COALESCE(SUM(error_requests), 0) AS STRING), ' errored requests') AS target
FROM {named:compute_ai_gateway_usage}
WHERE {ws}
""", target="target", fmt=PCT, tone="warn",
            how="Error requests as a share of all AI Gateway requests this window."),
        kpi("Rate-limited (%)", """
SELECT ROUND(100.0 * COALESCE(SUM(rate_limited_requests), 0) / NULLIF(SUM(total_requests), 0), 1) AS value,
       concat(CAST(COALESCE(SUM(rate_limited_requests), 0) AS STRING), ' rate-limited requests') AS target
FROM {named:compute_ai_gateway_usage}
WHERE {ws}
""", target="target", fmt=PCT,
            how="Rate-limited requests as a share of all AI Gateway requests this window."),
        kpi("Tokens", """
SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS value, 'Input + output' AS target
FROM {named:compute_ai_gateway_usage}
WHERE {ws}
""", target="target",
            how="Input plus output tokens logged by the AI Gateway this window."),
    )
    p.charts(
        bar("Requests by endpoint", """
SELECT """ + _ep_label("COALESCE(endpoint_name, '(no endpoint name)')") + """ AS endpoint, SUM(total_requests) AS requests
FROM {named:compute_ai_gateway_usage}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "endpoint", "requests", fmt=INT, category_title="Endpoint", value_title="Requests",
            how="Gateway requests per endpoint per workspace; same endpoint name in two workspaces stays two bars."),
    )
    p.checks()
