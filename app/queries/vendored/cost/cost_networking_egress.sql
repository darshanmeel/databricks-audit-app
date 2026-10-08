-- query_id: cost_networking_egress
-- title: Networking / egress usage
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- empty_if: ingestion_lag
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: usage_metadata.{source_region, destination_region, networking_client, recipient_id} are documented system.billing.usage columns.
-- read_this: One row = a workspace + day + cloud + SKU + usage type's billed networking usage. The columns that matter are usage_type (NETWORK_BYTE vs NETWORK_HOUR can be different units) and net_usage_quantity, priced at usd_list - this is the closest billed-egress signal available, not a full network cost reconciliation. price_basis (free/priced/unpriced) discloses whether usd_list is a real $0 or a pricing-coverage gap.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_cloud_infra (for the broader DBU-derived cloud cost estimate), cost_by_billing_origin_product (for total usage by product line in the same window)
-- caveats: source_region / destination_region are always NULL on GCP - do not read a GCP null as "no egress." These are the closest billed-egress signal, but egress is largely cloud-side; a real reconciliation needs your cloud provider's own cost export. usage_unit is not reliably bytes/hours-only or never-DBU - NETWORK_BYTE usage on the serverless-inference SKU we reviewed is actually priced in DBU - so usd_list is priced by joining system.billing.list_prices on each row's OWN (sku_name, cloud, usage_unit) rather than assuming a unit; any further rollup should still group by usage_unit, as this query already does. workspace_id is added to the SELECT and GROUP BY so the app's workspace filter can narrow this check; it does not change any other column's meaning.
SELECT u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
       u.usage_metadata.source_region      AS source_region,
       u.usage_metadata.destination_region AS destination_region,
       u.usage_metadata.networking_client  AS networking_client,
       u.usage_metadata.recipient_id       AS recipient_id,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(p.list_rate, 0)), 2) AS usd_list,
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
) p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
  AND u.usage_type IN ('NETWORK_BYTE', 'NETWORK_HOUR')
GROUP BY u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
         u.usage_metadata.source_region, u.usage_metadata.destination_region,
         u.usage_metadata.networking_client, u.usage_metadata.recipient_id
ORDER BY usage_date DESC, cloud, usage_type
