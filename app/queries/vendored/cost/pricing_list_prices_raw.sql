-- query_id: pricing_list_prices_raw
-- title: Raw Databricks list price history
-- domain: cost   tier: lite
-- reads: system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.list_prices is generally available)
-- params: none - this collects full price history with no date filter (price_end_time NULL marks the currently active row)
-- confidence: confirmed
-- confidence_note: pricing.default, pricing.effective_list, and pricing.promotional are documented system.billing.list_prices columns.
-- read_this: One row = a SKU's price at a point in time, with the full pricing struct preserved as JSON. pricing_default is the pre-promotion (sticker) list rate; pricing_effective_list_json parses to the effective (post-promotion) list rate that DEC-66.1 uses as this app's one dollar basis. price_end_time NULL means this is the currently active price. pricing_promotional_json carries the raw promotional struct for you to parse yourself. Neither price is negotiated or account-specific - no negotiated-rate source exists anywhere in system.billing.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_actual_vs_list_by_sku (for a ready-made actual-vs-list comparison built from this same table), cost_dollarized_by_sku_day (for a ready-made dollarized cost series, though its list_rate path is less confirmed than dollarizing from this raw table yourself)
-- caveats: This is a small table (one row per SKU price change) - it is collected raw with no aggregation so you can pick the right [price_start_time, price_end_time) window per usage row yourself, and so this collection survives any drift in the pricing struct's internal shape. effective_list and promotional are serialized to JSON strings precisely so collection never breaks if their internal shape changes - parse them defensively. price_end_time NULL means the currently active price. pricing.default is typed STRING in the documentation - confirm it is numerically castable before using it in arithmetic.
SELECT price_start_time, price_end_time, account_id, sku_name, cloud, currency_code, usage_unit,
       CAST(pricing.default       AS STRING) AS pricing_default,
       CAST(pricing.effective_list AS STRING) AS pricing_effective_list_json,
       CAST(pricing.promotional   AS STRING) AS pricing_promotional_json
FROM system.billing.list_prices
ORDER BY sku_name, cloud, currency_code, price_start_time
