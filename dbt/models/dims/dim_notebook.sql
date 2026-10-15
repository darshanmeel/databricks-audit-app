{{ config(materialized='table', tags=['dims']) }}
-- Latest path per (workspace_id, notebook_id), from the billing rows that carry one: notebooks have
-- no system table of their own. Twin of app/direct_sql/names/dim_notebook.sql.
WITH ranked AS (
    SELECT
        workspace_id,
        usage_metadata.notebook_id   AS notebook_id,
        usage_metadata.notebook_path AS notebook_path,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, usage_metadata.notebook_id
            ORDER BY usage_end_time DESC
        ) AS rn
    FROM {{ source('system_billing', 'usage') }}
    WHERE usage_metadata.notebook_id IS NOT NULL
      AND usage_metadata.notebook_path IS NOT NULL
      AND usage_date < {{ audit_today() }}
)
SELECT workspace_id, notebook_id, notebook_path
FROM ranked
WHERE rn = 1
