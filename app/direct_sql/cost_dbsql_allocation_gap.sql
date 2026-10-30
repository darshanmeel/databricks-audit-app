-- generated from dbt/models/databricks_direct/cost/d_cost_dbsql_allocation_gap.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_dbsql_allocation_gap.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH raw_side AS (
  SELECT 'raw' AS source_kind,
         u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         u.usage_date, u.cloud, u.billing_origin_product, u.usage_unit,
         SUM(u.usage_quantity) AS net_usage_quantity
  FROM `system`.`billing`.`usage` u
  WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date < __AS_OF_DATE__
    AND upper(u.usage_unit) = 'DBU'
    AND u.billing_origin_product = 'SQL'   -- DBSQL scope ONLY (attributed_usage doesn't cover jobs/DLT)
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id, u.usage_date, u.cloud, u.billing_origin_product, u.usage_unit
),
attributed_side AS (
  SELECT 'attributed' AS source_kind,
         a.workspace_id, a.usage_metadata.warehouse_id AS warehouse_id,
         a.usage_date, a.cloud, a.billing_origin_product, a.usage_unit,
         SUM(a.active_usage_quantity) AS net_usage_quantity
  FROM `system`.`billing`.`attributed_usage` a
  WHERE a.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND a.usage_date < __AS_OF_DATE__
    AND upper(a.usage_unit) = 'DBU'
    AND a.billing_origin_product = 'SQL'   -- same DBSQL scope on both sides
  GROUP BY a.workspace_id, a.usage_metadata.warehouse_id, a.usage_date, a.cloud, a.billing_origin_product, a.usage_unit
),
both_sides AS (
  SELECT * FROM raw_side
  UNION ALL
  SELECT * FROM attributed_side
)
SELECT source_kind, workspace_id, warehouse_id, usage_date, cloud, billing_origin_product, usage_unit, net_usage_quantity,
       -- coverage_status: window-wide flag, same value on every row (see caveats).
       CASE
         WHEN SUM(CASE WHEN source_kind = 'attributed' THEN 1 ELSE 0 END) OVER () = 0
              THEN 'NOT_ASSESSED'
         ELSE 'assessed'
       END AS coverage_status
FROM both_sides
ORDER BY usage_date DESC, cloud, billing_origin_product, source_kind
) q
