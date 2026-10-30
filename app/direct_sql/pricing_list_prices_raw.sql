-- generated from dbt/models/databricks_direct/cost/d_pricing_list_prices_raw.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/pricing_list_prices_raw.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT price_start_time, price_end_time, account_id, sku_name, cloud, currency_code, usage_unit,
       CAST(pricing.default       AS STRING) AS pricing_default,
       CAST(pricing.effective_list AS STRING) AS pricing_effective_list_json,
       CAST(pricing.promotional   AS STRING) AS pricing_promotional_json
FROM `system`.`billing`.`list_prices`
ORDER BY sku_name, cloud, currency_code, price_start_time
) q
