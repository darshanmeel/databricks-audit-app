# dims.dim_pipeline

Latest known name and owners per Lakeflow (DLT) pipeline, from `system.lakeflow.pipelines` (SCD2, latest row by change_time) (`dbt/models/dims/dim_pipeline.sql:1-10`).

- Schema/materialization: `dims`, table (`dbt/models/dims/dim_pipeline.sql:1`, `dbt/dbt_project.yml:63-65`).
- Grain / key: (workspace_id, pipeline_id); pipeline_id unique only within a workspace (`dbt/models/dims/dim_pipeline.sql:7-10`, `dbt/models/dims/_dims.yml:72-89`).
- Versioning: `ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC)`, rn = 1 (`dbt/models/dims/dim_pipeline.sql:29-32,43`).
- Direct twin: `app/direct_sql/names/dim_pipeline.sql`; stand-in columns `tools/load_direct_results.py:59-62`.

```mermaid
flowchart LR
  P[system.lakeflow.pipelines SCD2] -->|rn = 1| DP[(dims.dim_pipeline)]
  DP --> API[GET /api/dims, names for pipeline_id]
```

## Columns

`workspace_id, pipeline_id, pipeline_name (= name), created_by (PII), run_as (PII)` (`dbt/models/dims/dim_pipeline.sql:24-28,36-41`).

- No delete_time here, unlike the other three dims (`dbt/models/dims/dim_pipeline.sql:36-43`).
- created_by / run_as masked on read (`app/core/data.py:3219-3235`).

## Read by

- `app/core/data.py:3230` (`read_dim_pipeline`).
