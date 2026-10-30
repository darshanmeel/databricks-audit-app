{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:governance_access', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_data_classification_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT catalog_name, schema_name, table_name, column_name, class_tag, confidence, data_type,
       MAX(frequency)            AS max_frequency,
       MAX(latest_detected_time) AS latest_detected_time,
       MIN(first_detected_time)  AS first_detected_time
FROM {{ source('system_data_classification', 'results') }}
WHERE class_tag IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY catalog_name, schema_name, table_name, column_name
) q
