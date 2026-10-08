-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_identity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_chargeback_by_identity.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       CASE
         WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN 'unknown'
         WHEN identity_metadata.run_as LIKE '%@%' THEN 'user'
         ELSE 'service_principal'
       END                          AS identity_type,
       identity_metadata.run_as                          AS identity_run_as,
       identity_metadata.owned_by                          AS identity_owned_by,
       identity_metadata.created_by                          AS identity_created_by,
       SUM(usage_quantity) AS net_usage_quantity
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         CASE
           WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN 'unknown'
           WHEN identity_metadata.run_as LIKE '%@%' THEN 'user'
           ELSE 'service_principal'
         END,
         identity_metadata.run_as, identity_metadata.owned_by, identity_metadata.created_by
ORDER BY usage_date DESC, workspace_id, identity_type
) q
