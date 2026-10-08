-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_tag.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_chargeback_by_tag.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product, tag_key, tag_value,
       SUM(usage_quantity) AS net_usage_quantity,
       COUNT(*) AS record_count
FROM `system`.`billing`.`usage`
     LATERAL VIEW OUTER explode(custom_tags) t AS tag_key, tag_value
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
GROUP BY usage_date, cloud, workspace_id, billing_origin_product, tag_key, tag_value
ORDER BY usage_date DESC, workspace_id, tag_key, tag_value
) q
