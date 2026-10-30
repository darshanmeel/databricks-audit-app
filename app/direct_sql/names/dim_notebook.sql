-- hand-written; not generated. Notebooks have no system table of their own, so the latest path
-- per (workspace_id, notebook_id) comes from the billing rows that carry it, over the export window.
WITH ranked AS (
    SELECT
        workspace_id,
        usage_metadata.notebook_id   AS notebook_id,
        usage_metadata.notebook_path AS notebook_path,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, usage_metadata.notebook_id
            ORDER BY usage_end_time DESC
        ) AS rn
    FROM system.billing.usage
    WHERE usage_metadata.notebook_id IS NOT NULL
      AND usage_metadata.notebook_path IS NOT NULL
      AND usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
)
SELECT workspace_id, notebook_id, notebook_path
FROM ranked
WHERE rn = 1
