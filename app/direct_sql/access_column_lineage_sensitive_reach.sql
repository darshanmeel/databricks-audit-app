-- generated from dbt/models/databricks_direct/governance_access/d_access_column_lineage_sensitive_reach.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_column_lineage_sensitive_reach.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH sensitive_tags AS (
  -- One row per SOURCE column (GROUP BY, not the raw per-tag rows): a column carrying more than
  -- one matching tag collapses here first, so the join below cannot fan the same edge out twice.
  SELECT catalog_name, schema_name, table_name, column_name,
         array_join(collect_set(concat(tag_name, '=', tag_value)), ', ') AS source_sensitivity_tags
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
SELECT lw.workspace_id, lw.source_table_full_name, lw.source_column_name, st.source_sensitivity_tags,
       lw.target_table_full_name, lw.target_column_name, lw.entity_type, lw.direct_access,
       COUNT(*) AS event_count,
       COUNT(DISTINCT lw.created_by) AS distinct_principals,
       MAX(lw.event_time) AS last_event_time,
       -- status: worst-first band on how many distinct principals drove this sensitive-source
       -- column-to-column edge within this workspace (field heuristic; 10 /
       -- 50).
       CASE
         WHEN COUNT(DISTINCT lw.created_by) >= 50 THEN 'CRITICAL'
         WHEN COUNT(DISTINCT lw.created_by) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`access`.`column_lineage` lw
JOIN sensitive_tags st
  ON  lw.source_table_catalog = st.catalog_name
  AND lw.source_table_schema  = st.schema_name
  AND lw.source_table_name    = st.table_name
  AND lw.source_column_name   = st.column_name
WHERE lw.source_table_full_name IS NOT NULL
  AND lw.event_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND lw.event_date < __AS_OF_DATE__
GROUP BY lw.workspace_id, lw.source_table_full_name, lw.source_column_name, st.source_sensitivity_tags,
         lw.target_table_full_name, lw.target_column_name, lw.entity_type, lw.direct_access
ORDER BY distinct_principals DESC
) q
