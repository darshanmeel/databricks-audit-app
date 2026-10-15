-- hand-written; not generated. Latest row per (workspace_id, job_id), same rule as
-- dbt/models/dims/dim_job.sql. Reads system.* directly, so it does not run against the DuckDB
-- fixtures (system is a reserved catalog there), unlike the dbt model it mirrors.
WITH ranked AS (
    SELECT
        workspace_id,
        job_id,
        name,
        run_as,
        run_as_user_name,
        creator_user_name,
        delete_time,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, job_id
            ORDER BY change_time DESC
        ) AS rn
    FROM system.lakeflow.jobs
    WHERE workspace_id IS NOT NULL AND job_id IS NOT NULL
)
SELECT workspace_id, job_id, name, run_as, run_as_user_name, creator_user_name, delete_time
FROM ranked
WHERE rn = 1
