-- generated from dbt/models/databricks_direct/cost/d_overview_dbu_by_sku.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/overview_dbu_by_sku.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT
    workspace_id,
    sku_name,
    billing_origin_product,
    SUM(usage_quantity) AS net_dbus
FROM `system`.`billing`.`usage`
WHERE usage_unit = 'DBU'
  AND usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
GROUP BY workspace_id, sku_name, billing_origin_product
ORDER BY net_dbus DESC
) q
