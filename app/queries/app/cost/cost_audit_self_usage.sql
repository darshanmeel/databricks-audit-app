-- query_id: cost_audit_self_usage
-- title: Cost of running this audit itself, by warehouse
-- domain: cost   tier: lite
-- reads: system.query.history, system.billing.usage, system.billing.list_prices, system.compute.warehouses
-- requires: SELECT on system.query, system.billing and system.compute; GA
-- empty_if: schema_not_enabled, compute_scope_gap, no_activity
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: The shipped library query audit_self_cost always returns empty - it matches
--   statement_text for a marker this app's own export never writes (config/library_corrections.yml,
--   audit_self_cost-undocumented-marker-and-wildcard-escape). This query matches this export's own
--   statements by client_application containing the settings query_source tag (default: the brand name), the user_agent_entry
--   tools/snapshot.py's dbsql.connect() sets on every connection this app opens - a marker unique
--   to this app's own export, unlike matching on the bare connector name (databricks-sql-connector),
--   which also catches dbt-databricks and any other Python-connector tool running under the same
--   credentials. Confirm on a live account that SELECT DISTINCT client_application FROM
--   system.query.history WHERE client_application ILIKE '%__QUERY_SOURCE__%' returns this export's
--   own statements; matched_client_application in the output shows what actually matched.
-- read_this: One row = one (workspace_id, warehouse_id) this export's own statements ran on in
--   :period_days. matched_statement_count/matched_duration_secs are this export's own statements and
--   their runtime; warehouse_all_duration_secs is every statement's runtime on that warehouse in the
--   same window, so matched_share_pct shows how much of the warehouse's traffic was this audit.
--   est_audit_usd_list apportions the warehouse's whole list-price spend by that share - a runtime-
--   share proxy, not a per-statement DBU reading (Databricks exposes no such column).
-- healthy: n/a - coverage/inventory, not a WARN/CRITICAL finding.
-- investigate_if: n/a - coverage/inventory. A large est_audit_usd_list on a warehouse used only for
--   this audit is worth knowing about, but this query does not band it.
-- actions: n/a - inventory (reference for cost_chargeback_by_allocation_tag and overview_dbu_by_sku).
-- next: cost_chargeback_by_allocation_tag (see the workspace/tag this warehouse's spend rolls into), overview_dbu_by_sku (the same warehouse's SKU-level DBUs)
-- caveats: est_audit_usd_list apportions each warehouse's WHOLE list-price spend
--   (system.billing.usage x system.billing.list_prices.pricing.effective_list.default) by the
--   matched statements' share of that warehouse's total statement duration in the window - a warehouse
--   shared with other tools is only ever apportioned, never billed in full, to this audit. price_basis
--   is 'unpriced' when some of the warehouse's billed usage had no matching list_prices row and was
--   not FREE_USAGE (est costs then understated), 'free' when every matched SKU is FREE_USAGE, 'priced'
--   otherwise. A workspace whose export ran entirely on a classic/job cluster (no SQL warehouse) is
--   invisible here, the same gap the shipped library query has. This is coverage-only: it never emits
--   a status column, so it never appears in the WARN/CRITICAL count on any tab.
WITH matched AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id, statement_id,
         client_application, total_duration_ms, start_time
  FROM system.query.history
  WHERE start_time >= date_sub(current_date(), :period_days)
    AND start_time < current_date()
    AND compute.warehouse_id IS NOT NULL
    -- the export's query tag (fixed key audit_app), or its client-application name
    AND (client_application ILIKE '%__QUERY_SOURCE__%' OR array_contains(map_keys(query_tags), 'audit_app'))
),
warehouse_all AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id,
         SUM(total_duration_ms) AS all_duration_ms
  FROM system.query.history
  WHERE start_time >= date_sub(current_date(), :period_days)
    AND start_time < current_date()
    AND compute.warehouse_id IS NOT NULL
  GROUP BY workspace_id, compute.warehouse_id
),
matched_agg AS (
  SELECT workspace_id, warehouse_id,
         COUNT(*) AS matched_statement_count,
         SUM(total_duration_ms) AS matched_duration_ms,
         MIN(start_time) AS first_query_time,
         MAX(start_time) AS last_query_time,
         MAX(client_application) AS matched_client_application
  FROM matched
  GROUP BY workspace_id, warehouse_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
wh_usd AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS warehouse_usd_list,
         CASE WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                            THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
              WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
              ELSE 'priced' END AS price_basis
  FROM system.billing.usage u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= date_sub(current_date(), :period_days)
    AND u.usage_date < current_date()
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id
),
wh_name AS (
  SELECT warehouse_id, warehouse_name
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
)
SELECT m.workspace_id,
       m.warehouse_id,
       n.warehouse_name,
       m.matched_client_application,
       m.matched_statement_count,
       ROUND(m.matched_duration_ms / 1000.0, 1) AS matched_duration_secs,
       ROUND(a.all_duration_ms / 1000.0, 1) AS warehouse_all_duration_secs,
       ROUND(m.matched_duration_ms * 100.0 / NULLIF(a.all_duration_ms, 0), 1) AS matched_share_pct,
       ROUND(w.warehouse_usd_list, 2) AS est_usd_list,
       CASE WHEN w.price_basis IN ('priced', 'free') AND a.all_duration_ms > 0
            THEN ROUND(w.warehouse_usd_list * m.matched_duration_ms / a.all_duration_ms, 2) END AS est_audit_usd_list,
       w.price_basis,
       m.first_query_time, m.last_query_time
FROM matched_agg m
LEFT JOIN warehouse_all a ON a.workspace_id = m.workspace_id AND a.warehouse_id = m.warehouse_id
LEFT JOIN wh_usd w        ON w.workspace_id = m.workspace_id AND w.warehouse_id = m.warehouse_id
LEFT JOIN wh_name n       ON n.warehouse_id = m.warehouse_id
ORDER BY est_audit_usd_list DESC NULLS LAST, m.workspace_id, m.warehouse_id
