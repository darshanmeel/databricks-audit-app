-- generated from dbt/models/databricks_direct/governance_access/d_access_dead_table_candidates.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_dead_table_candidates.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH lineage_window AS (
  SELECT *
  FROM `system`.`access`.`table_lineage`
  WHERE event_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND event_date < __AS_OF_DATE__
),
source_tables AS (
  SELECT DISTINCT
         source_table_catalog AS catalog,
         source_table_schema  AS schema,
         source_table_name    AS name
  FROM lineage_window
  WHERE source_table_name IS NOT NULL
),
inventory AS (
  SELECT table_catalog,
         table_schema,
         table_name,
         table_type,
         table_owner,
         created,
         last_altered
  FROM `system`.`information_schema`.`tables`
  WHERE table_catalog <> 'system'
    AND table_schema <> 'information_schema'
    AND table_type IN ('MANAGED', 'EXTERNAL')
)
SELECT inv.table_catalog,
       inv.table_schema,
       inv.table_name,
       inv.table_type,
       inv.table_owner AS table_owner,
       inv.created,
       inv.last_altered,
       -- Age in whole days since the object was last altered (NULL -> age unknown).
       datediff(__AS_OF_DATE__, DATE(inv.last_altered)) AS days_since_altered,
       -- status: worst-first band on staleness (field heuristic; 90 / 365). NULL age -> NOT_ASSESSED, never a finding.
       CASE
         WHEN datediff(__AS_OF_DATE__, DATE(inv.last_altered)) IS NULL THEN 'NOT_ASSESSED'
         WHEN datediff(__AS_OF_DATE__, DATE(inv.last_altered)) >= 365 THEN 'CRITICAL'
         WHEN datediff(__AS_OF_DATE__, DATE(inv.last_altered)) >= 90 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM inventory inv
LEFT JOIN source_tables src
  ON  inv.table_catalog = src.catalog
  AND inv.table_schema  = src.schema
  AND inv.table_name    = src.name
WHERE src.name IS NULL          -- never appeared as a lineage source in the window
ORDER BY days_since_altered DESC NULLS LAST
) q
