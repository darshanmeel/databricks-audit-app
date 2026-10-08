-- hand-written; not generated. Latest row per warehouse_id (a globally-unique GUID), same rule
-- as dbt/models/dims/dim_warehouse.sql. Reads system.* directly, so it does not run against the
-- DuckDB fixtures (system is a reserved catalog there), unlike the dbt model it mirrors.
WITH ranked AS (
    SELECT
        workspace_id,
        warehouse_id,
        warehouse_name,
        created_by,
        delete_time,
        ROW_NUMBER() OVER (
            PARTITION BY warehouse_id
            ORDER BY change_time DESC
        ) AS rn
    FROM system.compute.warehouses
    WHERE warehouse_id IS NOT NULL
)
SELECT workspace_id, warehouse_id, warehouse_name, created_by, delete_time
FROM ranked
WHERE rn = 1
