{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_dead_table_candidates.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH lineage_window AS (
  SELECT *
  FROM {{ source('system_access', 'table_lineage') }}
  WHERE event_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND event_date < {{ audit_today() }}
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
  FROM {{ source('system_information_schema', 'tables') }}
  WHERE table_catalog <> 'system'
    AND table_schema <> 'information_schema'
    AND table_type IN ('MANAGED', 'EXTERNAL')
)
SELECT inv.table_catalog,
       inv.table_schema,
       inv.table_name,
       inv.table_type,
       {{ mask_user('inv.table_owner') }} AS table_owner,
       inv.created,
       inv.last_altered,
       -- Age in whole days since the object was last altered (NULL -> age unknown).
       datediff({{ audit_today() }}, DATE(inv.last_altered)) AS days_since_altered,
       -- status: worst-first band on staleness (field heuristic; {{ param('access_dead_table_candidates', 'warn_dead_days', 90) }} / {{ param('access_dead_table_candidates', 'crit_dead_days', 365) }}). NULL age -> NOT_ASSESSED, never a finding.
       CASE
         WHEN datediff({{ audit_today() }}, DATE(inv.last_altered)) IS NULL THEN 'NOT_ASSESSED'
         WHEN datediff({{ audit_today() }}, DATE(inv.last_altered)) >= {{ param('access_dead_table_candidates', 'crit_dead_days', 365) }} THEN 'CRITICAL'
         WHEN datediff({{ audit_today() }}, DATE(inv.last_altered)) >= {{ param('access_dead_table_candidates', 'warn_dead_days', 90) }} THEN 'WARN'
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
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
