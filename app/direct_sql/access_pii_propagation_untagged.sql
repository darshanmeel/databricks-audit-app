-- generated from dbt/models/databricks_direct/governance_access/d_access_pii_propagation_untagged.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_pii_propagation_untagged.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH lineage_window AS (
  SELECT workspace_id, source_table_catalog, source_table_schema, source_table_name, source_column_name,
         target_table_catalog, target_table_schema, target_table_name, target_column_name,
         created_by
  FROM `system`.`access`.`column_lineage`
  WHERE direct_access = true                       -- direct flows only; excludes view expansion
    AND source_column_name IS NOT NULL
    AND target_column_name IS NOT NULL
    AND source_table_catalog <> 'system'
    AND event_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND event_date < __AS_OF_DATE__
),
sensitive_tags AS (
  SELECT catalog_name, schema_name, table_name, column_name,
         array_join(sort_array(collect_set(concat(tag_name, '=', tag_value))), ', ') AS tags
  FROM `system`.`information_schema`.`column_tags`
  WHERE lower(tag_name) IN (
          'pii', 'sensitivity', 'sensitive', 'data_classification', 'classification',
          'confidentiality', 'phi', 'pci'
        )
     OR lower(tag_value) IN (
          'pii', 'sensitive', 'confidential', 'restricted', 'phi', 'pci'
        )
  GROUP BY catalog_name, schema_name, table_name, column_name
)
SELECT lw.workspace_id,
       lw.source_table_catalog,
       lw.source_table_schema,
       lw.source_table_name,
       lw.source_column_name,
       st.tags AS source_tags,
       lw.target_table_catalog,
       lw.target_table_schema,
       lw.target_table_name,
       lw.target_column_name,
       COUNT(DISTINCT lw.created_by) AS distinct_creators,
       COUNT(*) AS event_count,
       -- status: worst-first band on how often this untagged-PII-propagation edge fired (field heuristic; 5 / 50).
       CASE
         WHEN COUNT(*) >= 50 THEN 'CRITICAL'
         WHEN COUNT(*) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM lineage_window lw
JOIN sensitive_tags st
  ON  lw.source_table_catalog = st.catalog_name
  AND lw.source_table_schema  = st.schema_name
  AND lw.source_table_name    = st.table_name
  AND lw.source_column_name   = st.column_name
LEFT JOIN sensitive_tags tt
  ON  lw.target_table_catalog = tt.catalog_name
  AND lw.target_table_schema  = tt.schema_name
  AND lw.target_table_name    = tt.table_name
  AND lw.target_column_name   = tt.column_name
WHERE tt.column_name IS NULL          -- target carries no SENSITIVITY tag -> propagation gap
GROUP BY lw.workspace_id, lw.source_table_catalog, lw.source_table_schema, lw.source_table_name,
         lw.source_column_name, st.tags,
         lw.target_table_catalog, lw.target_table_schema, lw.target_table_name,
         lw.target_column_name
ORDER BY event_count DESC
) q
