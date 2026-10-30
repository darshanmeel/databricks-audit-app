{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:compute', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/compute/compute_last_activity.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH warehouses AS (
  SELECT workspace_id, warehouse_id, warehouse_name, created_at
  FROM (
    SELECT workspace_id, warehouse_id, warehouse_name, delete_time,
           MIN(change_time) OVER (PARTITION BY workspace_id, warehouse_id) AS created_at,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn
    FROM {{ source('system_compute', 'warehouses') }}
  ) w
  WHERE rn = 1 AND delete_time IS NULL
),
clusters AS (
  SELECT workspace_id, cluster_id, cluster_name, create_time
  FROM (
    SELECT workspace_id, cluster_id, cluster_name, create_time, delete_time, cluster_source,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) AS rn
    FROM {{ source('system_compute', 'clusters') }}
  ) c
  WHERE rn = 1 AND delete_time IS NULL
    AND COALESCE(cluster_source, '') NOT IN ('JOB', 'PIPELINE', 'PIPELINE_MAINTENANCE', 'SQL', 'MODELS')
),
billed AS (
  SELECT workspace_id,
         usage_metadata.warehouse_id AS warehouse_id,
         usage_metadata.cluster_id   AS cluster_id,
         MAX(usage_date)             AS last_billed_date
  FROM {{ source('system_billing', 'usage') }}
  WHERE usage_quantity > 0
  GROUP BY workspace_id, usage_metadata.warehouse_id, usage_metadata.cluster_id
),
queried AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id,
         MAX(CAST(start_time AS DATE)) AS last_query_date
  FROM {{ source('system_query', 'history') }}
  GROUP BY workspace_id, compute.warehouse_id
),
started AS (
  SELECT workspace_id, warehouse_id, MAX(CAST(event_time AS DATE)) AS last_start_date
  FROM {{ source('system_compute', 'warehouse_events') }}
  WHERE event_type IN ('STARTING', 'RUNNING')
  GROUP BY workspace_id, warehouse_id
),
job_runs AS (
  SELECT workspace_id, MAX(CAST(period_start_time AS DATE)) AS last_job_run_date
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  GROUP BY workspace_id
),
audited AS (
  SELECT workspace_id,
         MAX(CASE WHEN user_identity.email LIKE '%@%' THEN event_date END) AS last_user_event_date
  FROM {{ source('system_access', 'audit') }}
  GROUP BY workspace_id
),
workspace_keys AS (
  SELECT workspace_id FROM {{ source('system_access', 'workspaces_latest') }} WHERE workspace_id IS NOT NULL
  UNION
  -- a cancelled workspace leaves the workspace list but not the bill: billing ids only stand in
  -- when the list itself is empty (not exported)
  SELECT workspace_id FROM billed
  WHERE workspace_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM {{ source('system_access', 'workspaces_latest') }})
),
all_rows AS (
  SELECT 'warehouse' AS object_type, w.workspace_id, w.warehouse_id, CAST(NULL AS STRING) AS cluster_id,
         w.warehouse_name AS name, CAST(w.created_at AS DATE) AS created_date,
         q.last_query_date, s.last_start_date, CAST(NULL AS DATE) AS last_job_run_date,
         CAST(NULL AS DATE) AS last_user_event_date, b.last_billed_date,
         CAST(NULL AS BOOLEAN) AS audit_seen
  FROM warehouses w
  LEFT JOIN queried q ON q.workspace_id = w.workspace_id AND q.warehouse_id = w.warehouse_id
  LEFT JOIN started s ON s.workspace_id = w.workspace_id AND s.warehouse_id = w.warehouse_id
  LEFT JOIN (
    SELECT workspace_id, warehouse_id, MAX(last_billed_date) AS last_billed_date
    FROM billed WHERE warehouse_id IS NOT NULL GROUP BY workspace_id, warehouse_id
  ) b ON b.workspace_id = w.workspace_id AND b.warehouse_id = w.warehouse_id
  UNION ALL
  SELECT 'cluster', c.workspace_id, CAST(NULL AS STRING), c.cluster_id,
         c.cluster_name, CAST(c.create_time AS DATE),
         CAST(NULL AS DATE), CAST(NULL AS DATE), CAST(NULL AS DATE), CAST(NULL AS DATE), b.last_billed_date,
         CAST(NULL AS BOOLEAN)
  FROM clusters c
  LEFT JOIN (
    SELECT workspace_id, cluster_id, MAX(last_billed_date) AS last_billed_date
    FROM billed WHERE cluster_id IS NOT NULL GROUP BY workspace_id, cluster_id
  ) b ON b.workspace_id = c.workspace_id AND b.cluster_id = c.cluster_id
  UNION ALL
  SELECT 'workspace', k.workspace_id, CAST(NULL AS STRING), CAST(NULL AS STRING),
         n.workspace_name, CAST(n.create_time AS DATE),
         q.last_query_date, CAST(NULL AS DATE), j.last_job_run_date, a.last_user_event_date, b.last_billed_date,
         a.workspace_id IS NOT NULL
  FROM workspace_keys k
  LEFT JOIN {{ source('system_access', 'workspaces_latest') }} n ON n.workspace_id = k.workspace_id
  LEFT JOIN (
    SELECT workspace_id, MAX(last_query_date) AS last_query_date FROM queried GROUP BY workspace_id
  ) q ON q.workspace_id = k.workspace_id
  LEFT JOIN job_runs j ON j.workspace_id = k.workspace_id
  LEFT JOIN audited a ON a.workspace_id = k.workspace_id
  LEFT JOIN (
    SELECT workspace_id, MAX(last_billed_date) AS last_billed_date FROM billed GROUP BY workspace_id
  ) b ON b.workspace_id = k.workspace_id
),
dated AS (
  SELECT *,
         -- a warehouse is in use when it runs queries or starts; one billed with neither is not
         CASE WHEN object_type = 'warehouse' THEN GREATEST(last_query_date, last_start_date)
              ELSE GREATEST(last_query_date, last_start_date, last_job_run_date, last_user_event_date,
                            last_billed_date)
         END AS last_activity_date
  FROM all_rows
)
SELECT object_type, workspace_id, warehouse_id, cluster_id, name, created_date,
       last_query_date, last_start_date, last_job_run_date, last_user_event_date, last_billed_date,
       audit_seen, last_activity_date,
       datediff({{ audit_today() }}, last_activity_date) AS days_since_activity,
       CASE
         WHEN last_activity_date IS NULL AND created_date >= dateadd(day, -{{ param('compute_last_activity', 'idle_days', 90) }}, {{ audit_today() }})
              THEN 'NOT_ASSESSED'
         WHEN last_activity_date IS NULL THEN 'WARN'
         WHEN datediff({{ audit_today() }}, last_activity_date) >= {{ param('compute_last_activity', 'idle_days', 90) }} THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN last_activity_date IS NULL AND created_date >= dateadd(day, -{{ param('compute_last_activity', 'idle_days', 90) }}, {{ audit_today() }})
              THEN 'created_recently'
       END AS not_assessed_reason
FROM dated
ORDER BY CASE status WHEN 'WARN' THEN 0 WHEN 'OK' THEN 1 ELSE 2 END,
         last_activity_date ASC NULLS FIRST, object_type, workspace_id, name
) q
