-- generated from dbt/models/databricks_direct/governance_access/d_access_data_classification_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_data_classification_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT catalog_name, schema_name, table_name, column_name, class_tag, confidence, data_type,
       MAX(frequency)            AS max_frequency,
       MAX(latest_detected_time) AS latest_detected_time,
       MIN(first_detected_time)  AS first_detected_time
FROM `system`.`data_classification`.`results`
WHERE class_tag IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY catalog_name, schema_name, table_name, column_name
) q
