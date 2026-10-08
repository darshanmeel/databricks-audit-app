-- query_id: access_data_classification_inventory
-- title: Auto-classified sensitive column inventory
-- domain: governance_access   tier: standard
-- reads: system.data_classification.results
-- requires: SELECT on system.data_classification; Public Preview, Unity Catalog required, needs the data-classification feature AND the system.data_classification schema both enabled
-- empty_if: schema_not_enabled, preview_unavailable, no_serverless
-- params: none - deduplicated current-state inventory, no window.
-- confidence: confirmed
-- confidence_note: Columns confirmed against Databricks documentation for system.data_classification.results.
-- read_this: One row = a column the auto-classifier tagged, deduplicated to one row per catalog/schema/table/column/class_tag/confidence/data_type. The columns that matter are class_tag (what kind of sensitive data) and max_frequency (how consistently it matched).
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: access_classified_unmasked (see which of these are masked), access_pii_propagation_untagged (see where this sensitive data propagates from here)
-- caveats: This deliberately DROPS the underlying samples array (up to 5 raw sample values) - collecting raw sensitive values would defeat the point of an audit tool, so never add it back. confidence is HIGH or LOW; frequency is a float from 0 to 1. It covers ENABLED CATALOGS ONLY - a catalog that never had classification turned on has no rows here, and that is not the same as "nothing sensitive." It requires BOTH the data-classification feature AND the separate system.data_classification schema to be enabled - enabling one does not enable the other, so if this query errors or returns nothing, check both settings before concluding you have no classified data. Public Preview. 13-month retention. Regional.
-- Scans run on serverless compute and complete asynchronously (new objects within ~24h), so a catalog with everything enabled but no serverless available, or only just enabled, can still show no rows yet.
SELECT catalog_name, schema_name, table_name, column_name, class_tag, confidence, data_type,
       MAX(frequency)            AS max_frequency,
       MAX(latest_detected_time) AS latest_detected_time,
       MIN(first_detected_time)  AS first_detected_time
FROM system.data_classification.results
WHERE class_tag IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY catalog_name, schema_name, table_name, column_name
