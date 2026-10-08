-- hand-written; not generated. Latest row per cluster billed or changed in the export window.
-- Job and pipeline clusters are skipped: one per run, auto-named; the app names them by their job
-- or pipeline instead.
WITH billed AS (
    SELECT DISTINCT usage_metadata.cluster_id AS cluster_id
    FROM system.billing.usage
    WHERE usage_metadata.cluster_id IS NOT NULL
      AND usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
),
ranked AS (
    SELECT
        workspace_id,
        cluster_id,
        cluster_name,
        cluster_source,
        owned_by,
        change_time,
        delete_time,
        ROW_NUMBER() OVER (
            PARTITION BY cluster_id
            ORDER BY change_time DESC
        ) AS rn
    FROM system.compute.clusters
    WHERE cluster_id IS NOT NULL
      AND COALESCE(cluster_source, '') NOT IN ('JOB', 'PIPELINE', 'PIPELINE_MAINTENANCE')
)
SELECT workspace_id, cluster_id, cluster_name, cluster_source, owned_by, delete_time
FROM ranked
WHERE rn = 1
  AND (
      cluster_id IN (SELECT cluster_id FROM billed)
      OR change_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      OR delete_time IS NULL  -- every live cluster, so the Tags page sees unused ones too
  )
