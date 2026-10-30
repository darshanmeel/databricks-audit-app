-- generated from dbt/models/databricks_direct/cost/d_cost_account_prices_raw.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_account_prices_raw.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT price_start_time, price_end_time, account_id, sku_name, cloud, currency_code, usage_unit,
       CAST(pricing.default AS STRING) AS pricing_default
FROM `system`.`billing`.`list_prices`
ORDER BY sku_name, cloud, currency_code, price_start_time
) q
