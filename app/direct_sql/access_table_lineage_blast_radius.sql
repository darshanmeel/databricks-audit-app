-- generated from dbt/models/databricks_direct/governance_access/d_access_table_lineage_blast_radius.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_table_lineage_blast_radius.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH lineage_window AS (
  SELECT workspace_id, source_table_full_name, target_table_full_name, source_type, target_type,
         entity_type, direct_access, created_by, event_time
  FROM `system`.`access`.`table_lineage`
  WHERE event_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND event_date < __AS_OF_DATE__
    -- Exclude accesses this table cannot tie to a catalog table on EITHER side (path-based, JDBC,
    -- unresolved) rather than pooling every one of them into one fake NULL-name "edge" (see caveats).
    AND (source_table_full_name IS NOT NULL OR target_table_full_name IS NOT NULL)
),
source_reach AS (
  SELECT source_table_full_name, COUNT(DISTINCT created_by) AS distinct_principals
  FROM lineage_window
  WHERE source_table_full_name IS NOT NULL
  GROUP BY source_table_full_name
)
SELECT lw.workspace_id, lw.source_table_full_name, lw.target_table_full_name, lw.source_type, lw.target_type,
       lw.entity_type, lw.direct_access,
       CASE WHEN lw.source_type IS NOT NULL AND lw.target_type IS NULL THEN 'READ'
            WHEN lw.target_type IS NOT NULL AND lw.source_type IS NULL THEN 'WRITE'
            WHEN lw.source_type IS NOT NULL AND lw.target_type IS NOT NULL THEN 'READ_WRITE'
            ELSE 'UNKNOWN' END AS access_class,
       COUNT(*) AS event_count,
       COUNT(DISTINCT lw.created_by) AS distinct_principals,
       MAX(sr.distinct_principals) AS source_distinct_principals,
       MAX(lw.event_time) AS last_event_time,
       -- status: worst-first band on the source table's own total reach, this edge's own count
       -- for a WRITE row with no source to roll up to (field heuristic; 10 / 50).
       CASE
         WHEN COALESCE(MAX(sr.distinct_principals), COUNT(DISTINCT lw.created_by)) >= 50 THEN 'CRITICAL'
         WHEN COALESCE(MAX(sr.distinct_principals), COUNT(DISTINCT lw.created_by)) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM lineage_window lw
LEFT JOIN source_reach sr ON sr.source_table_full_name = lw.source_table_full_name
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
ORDER BY distinct_principals DESC
) q
