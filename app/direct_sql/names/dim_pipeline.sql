-- hand-written; not generated. Latest row per (workspace_id, pipeline_id), same rule as
-- dbt/models/dims/dim_pipeline.sql. Reads system.* directly, so it does not run against the
-- DuckDB fixtures (system is a reserved catalog there), unlike the dbt model it mirrors.
WITH ranked AS (
    SELECT
        workspace_id,
        pipeline_id,
        name AS pipeline_name,
        created_by,
        run_as,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, pipeline_id
            ORDER BY change_time DESC
        ) AS rn
    FROM system.lakeflow.pipelines
    WHERE workspace_id IS NOT NULL AND pipeline_id IS NOT NULL
)
SELECT workspace_id, pipeline_id, pipeline_name, created_by, run_as
FROM ranked
WHERE rn = 1
