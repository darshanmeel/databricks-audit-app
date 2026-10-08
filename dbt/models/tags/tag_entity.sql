{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- P4-T-IDX (tasks/P4-T-SPEC.md, section 4.2 and 5.2). One row per (entity_type, workspace_id,
-- entity_id, tag_key) for every place a Databricks object carries a tag: latest-SCD2 resource and
-- job/pipeline tags ("who ran it"), query tags, Unity Catalog object tags, the inferred workspace
-- level (tags.tag_workspace, reused verbatim), and the DBU-weighted dominant billed value per
-- entity ("who pays", section 3.3) for the ids the bill itself carries.
--
--   entity_type    workspace | warehouse | cluster | pool | job | pipeline | statement |
--                  billed_warehouse | billed_cluster | billed_pool | billed_endpoint |
--                  billed_endpoint_name | billed_job | billed_pipeline |
--                  uc_table | uc_schema | uc_column | uc_volume  (section 4.2's table)
--   workspace_id   NULL for the four uc_* entity types (a UC object has no workspace)
--   entity_id      the id named in section 4.2's table (a UC id is catalog.schema[.table[.column
--                  | volume]], lower-cased)
--   tag_key        normalised (dbt/macros/norm_tag_key.sql / app/core/tag_keys.normalize_tag_key)
--   raw_key        the raw spelling that won for this row (alphabetically-first when one resource's
--                  own tag map holds two spellings of one key; DBU-dominant for the inferred rows)
--   is_allocating  TRUE for every direct resource/job/query/UC tag row (there is nothing to infer);
--                  for `workspace` and the `billed_*` rows, TRUE only for a real inferred value
--                  ('__mixed__'/'__untagged__' never allocate -- same rule as tag_workspace.sql)
--   source         app/core/tag_keys.SOURCE_LABELS' code (section 4.3)
--   share/coverage/reason   NULL for a direct tag row; set for `workspace` and `billed_*` rows
--                  (section 3.2's inference, reused here -- appendix B / tag_entity_billed_inference)
--
-- A cluster with no direct value for a key takes its instance pool's value, worker pool first
-- (section 3.3): `cluster` rows below are the union of the cluster's own tags and, only for a key
-- the cluster does not itself carry, its pool's tag for that key.
--
-- DuckDB only for now (P4-T todo: a Databricks dialect branch, same as tag_workspace.sql).
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.tag_entity is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}

WITH

-- =============================================================================================
-- workspace: tags.tag_workspace's own rows, reused verbatim (ALL rows, including non-allocating
-- __mixed__/__untagged__ ones -- section 4.2's table says so explicitly).
-- =============================================================================================
workspace_entity AS (
    SELECT
        'workspace' AS entity_type, workspace_id, workspace_id AS entity_id, tag_key, display_key AS raw_key,
        tag_value, is_allocating, 'workspace_inferred' AS source, share, coverage, reason
    FROM {{ ref('tag_workspace') }}
),

-- =============================================================================================
-- warehouse: latest compute.warehouses.tags (SCD2, latest row by change_time; deleted warehouses
-- keep their last known tags).
-- =============================================================================================
warehouse_latest AS (
    SELECT warehouse_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
    FROM {{ source('system_compute', 'warehouses') }}
),
warehouse_pairs AS (
    SELECT w.warehouse_id, w.workspace_id, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM warehouse_latest w, unnest(map_entries(w.tags)) AS x(e)
    WHERE w.rn = 1 AND e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY w.warehouse_id, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
),
warehouse_entity AS (
    SELECT
        'warehouse' AS entity_type, workspace_id, warehouse_id AS entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'warehouse_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM warehouse_pairs
),

-- =============================================================================================
-- pool: latest compute.instance_pools.tags. Shared with the cluster fallback below.
-- =============================================================================================
pool_latest AS (
    SELECT instance_pool_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY instance_pool_id ORDER BY change_time DESC) AS rn
    FROM {{ source('system_compute', 'instance_pools') }}
),
pool_pairs AS (
    SELECT p.instance_pool_id, p.workspace_id, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM pool_latest p, unnest(map_entries(p.tags)) AS x(e)
    WHERE p.rn = 1 AND e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY p.instance_pool_id, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
),
pool_entity AS (
    SELECT
        'pool' AS entity_type, workspace_id, instance_pool_id AS entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'pool_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM pool_pairs
),

-- =============================================================================================
-- cluster: its own tags, PLUS (for a key it does not itself carry) its pool's tag for that key,
-- worker pool first, then driver pool (section 3.3).
-- =============================================================================================
cluster_latest AS (
    SELECT cluster_id, workspace_id, tags, worker_instance_pool_id, driver_instance_pool_id,
           ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
    FROM {{ source('system_compute', 'clusters') }}
),
cluster_own_pairs AS (
    SELECT c.cluster_id, c.workspace_id, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM cluster_latest c, unnest(map_entries(c.tags)) AS x(e)
    WHERE c.rn = 1 AND e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY c.cluster_id, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
),
cluster_pool_fallback AS (
    SELECT c.cluster_id, c.workspace_id, wp.raw_key, wp.tag_key, wp.tag_value
    FROM cluster_latest c
    JOIN pool_pairs wp ON wp.instance_pool_id = c.worker_instance_pool_id
    WHERE c.rn = 1 AND c.worker_instance_pool_id IS NOT NULL

    UNION ALL

    SELECT c.cluster_id, c.workspace_id, dp.raw_key, dp.tag_key, dp.tag_value
    FROM cluster_latest c
    JOIN pool_pairs dp ON dp.instance_pool_id = c.driver_instance_pool_id
    WHERE c.rn = 1 AND c.driver_instance_pool_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM pool_pairs wp2
          WHERE wp2.instance_pool_id = c.worker_instance_pool_id AND wp2.tag_key = dp.tag_key
      )
),
cluster_pool_fallback_unclaimed AS (
    -- Only for a (cluster_id, tag_key) the cluster does not directly carry.
    SELECT f.cluster_id, f.workspace_id, f.raw_key, f.tag_key, f.tag_value
    FROM cluster_pool_fallback f
    WHERE NOT EXISTS (
        SELECT 1 FROM cluster_own_pairs o
        WHERE o.cluster_id = f.cluster_id AND o.tag_key = f.tag_key
    )
),
cluster_entity AS (
    SELECT
        'cluster' AS entity_type, workspace_id, cluster_id AS entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'cluster_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM cluster_own_pairs

    UNION ALL

    SELECT
        'cluster' AS entity_type, workspace_id, cluster_id AS entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'pool_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM cluster_pool_fallback_unclaimed
),

-- =============================================================================================
-- job / pipeline: latest lakeflow.jobs.tags / lakeflow.pipelines.tags.
-- =============================================================================================
job_latest AS (
    SELECT job_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) AS rn
    FROM {{ source('system_lakeflow', 'jobs') }}
),
job_pairs AS (
    SELECT j.job_id, j.workspace_id, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM job_latest j, unnest(map_entries(j.tags)) AS x(e)
    WHERE j.rn = 1 AND e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY j.workspace_id, j.job_id, {{ norm_tag_key('e.key') }} ORDER BY e.key
    ) = 1
),
job_entity AS (
    SELECT
        'job' AS entity_type, workspace_id, job_id AS entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'job_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM job_pairs
),
pipeline_latest AS (
    SELECT pipeline_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) AS rn
    FROM {{ source('system_lakeflow', 'pipelines') }}
),
pipeline_pairs AS (
    SELECT p.pipeline_id, p.workspace_id, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM pipeline_latest p, unnest(map_entries(p.tags)) AS x(e)
    WHERE p.rn = 1 AND e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY p.workspace_id, p.pipeline_id, {{ norm_tag_key('e.key') }} ORDER BY e.key
    ) = 1
),
pipeline_entity AS (
    SELECT
        'pipeline' AS entity_type, workspace_id, pipeline_id AS entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'pipeline_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM pipeline_pairs
),

-- =============================================================================================
-- statement: query.history.query_tags -- statements that carry a tag only, whole snapshot (not
-- windowed: tags.tag_entity is a lookup table, not a per-window rollup).
-- =============================================================================================
statement_pairs AS (
    SELECT q.statement_id, q.workspace_id, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM {{ source('system_query', 'history') }} q, unnest(map_entries(q.query_tags)) AS x(e)
    WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY q.statement_id, {{ norm_tag_key('e.key') }} ORDER BY e.key
    ) = 1
),
statement_entity AS (
    SELECT
        'statement' AS entity_type, workspace_id, statement_id AS entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'query_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM statement_pairs
),

-- =============================================================================================
-- Unity Catalog object tags: flat rows already, one (object, tag_name) pair per row -- dedup
-- defensively the same alphabetical-first-spelling way, in case two raw spellings ever collide.
-- workspace_id is NULL for every uc_* entity type (a UC object has no workspace, section 4.2).
-- =============================================================================================
uc_table_pairs AS (
    SELECT lower(t.catalog_name) || '.' || lower(t.schema_name) || '.' || lower(t.table_name) AS entity_id,
           t.tag_name AS raw_key, {{ norm_tag_key('t.tag_name') }} AS tag_key, trim(t.tag_value) AS tag_value
    FROM {{ source('system_information_schema', 'table_tags') }} t
    WHERE t.tag_name IS NOT NULL AND t.tag_value IS NOT NULL AND {{ norm_tag_key('t.tag_name') }} <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(t.catalog_name), lower(t.schema_name), lower(t.table_name), {{ norm_tag_key('t.tag_name') }}
        ORDER BY t.tag_name
    ) = 1
),
uc_table_entity AS (
    SELECT
        'uc_table' AS entity_type, CAST(NULL AS VARCHAR) AS workspace_id, entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'uc_table_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM uc_table_pairs
),
uc_schema_pairs AS (
    SELECT lower(s.catalog_name) || '.' || lower(s.schema_name) AS entity_id,
           s.tag_name AS raw_key, {{ norm_tag_key('s.tag_name') }} AS tag_key, trim(s.tag_value) AS tag_value
    FROM {{ source('system_information_schema', 'schema_tags') }} s
    WHERE s.tag_name IS NOT NULL AND s.tag_value IS NOT NULL AND {{ norm_tag_key('s.tag_name') }} <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(s.catalog_name), lower(s.schema_name), {{ norm_tag_key('s.tag_name') }}
        ORDER BY s.tag_name
    ) = 1
),
uc_schema_entity AS (
    SELECT
        'uc_schema' AS entity_type, CAST(NULL AS VARCHAR) AS workspace_id, entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'uc_schema_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM uc_schema_pairs
),
uc_column_pairs AS (
    SELECT lower(c.catalog_name) || '.' || lower(c.schema_name) || '.' || lower(c.table_name) || '.'
               || lower(c.column_name) AS entity_id,
           c.tag_name AS raw_key, {{ norm_tag_key('c.tag_name') }} AS tag_key, trim(c.tag_value) AS tag_value
    FROM {{ source('system_information_schema', 'column_tags') }} c
    WHERE c.tag_name IS NOT NULL AND c.tag_value IS NOT NULL AND {{ norm_tag_key('c.tag_name') }} <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(c.catalog_name), lower(c.schema_name), lower(c.table_name), lower(c.column_name),
                     {{ norm_tag_key('c.tag_name') }}
        ORDER BY c.tag_name
    ) = 1
),
uc_column_entity AS (
    SELECT
        'uc_column' AS entity_type, CAST(NULL AS VARCHAR) AS workspace_id, entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'uc_column_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM uc_column_pairs
),
uc_volume_pairs AS (
    SELECT lower(v.catalog_name) || '.' || lower(v.schema_name) || '.' || lower(v.volume_name) AS entity_id,
           v.tag_name AS raw_key, {{ norm_tag_key('v.tag_name') }} AS tag_key, trim(v.tag_value) AS tag_value
    FROM {{ source('system_information_schema', 'volume_tags') }} v
    WHERE v.tag_name IS NOT NULL AND v.tag_value IS NOT NULL AND {{ norm_tag_key('v.tag_name') }} <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(v.catalog_name), lower(v.schema_name), lower(v.volume_name), {{ norm_tag_key('v.tag_name') }}
        ORDER BY v.tag_name
    ) = 1
),
uc_volume_entity AS (
    SELECT
        'uc_volume' AS entity_type, CAST(NULL AS VARCHAR) AS workspace_id, entity_id, tag_key, raw_key, tag_value,
        TRUE AS is_allocating, 'uc_volume_tags' AS source,
        CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS VARCHAR) AS reason
    FROM uc_volume_pairs
),

-- =============================================================================================
-- billed_*: the dominant billing.usage.custom_tags value per billed entity id (section 3.3 /
-- 4.2 / 5.2). One shared per-row classification (compute_kind, source), then one filtered base
-- CTE per billed_* type, each fed into tag_entity_billed_inference (dbt/macros/, reusing appendix
-- B's share/coverage inference partitioned by entity id).
-- =============================================================================================
billed_usage AS (
    SELECT
        ROW_NUMBER() OVER () AS rid,
        workspace_id,
        usage_quantity,
        custom_tags,
        usage_metadata.warehouse_id      AS warehouse_id,
        usage_metadata.cluster_id        AS cluster_id,
        usage_metadata.instance_pool_id  AS instance_pool_id,
        usage_metadata.endpoint_id       AS endpoint_id,
        usage_metadata.endpoint_name     AS endpoint_name,
        usage_metadata.job_id            AS job_id,
        usage_metadata.dlt_pipeline_id   AS dlt_pipeline_id,
        CASE
            WHEN usage_metadata.warehouse_id IS NOT NULL THEN 'billing:warehouse'
            WHEN usage_metadata.cluster_id IS NOT NULL THEN 'billing:cluster'
            WHEN usage_metadata.endpoint_id IS NOT NULL OR usage_metadata.endpoint_name IS NOT NULL
                THEN 'billing:endpoint'
            WHEN usage_metadata.app_id IS NOT NULL THEN 'billing:app'
            WHEN usage_metadata.job_id IS NOT NULL OR usage_metadata.dlt_pipeline_id IS NOT NULL
                 OR usage_metadata.notebook_id IS NOT NULL OR usage_metadata.budget_policy_id IS NOT NULL
                 OR usage_metadata.usage_policy_id IS NOT NULL OR product_features.is_serverless THEN
                CASE
                    WHEN usage_metadata.budget_policy_id IS NOT NULL OR usage_metadata.usage_policy_id IS NOT NULL
                        THEN 'billing:budget_policy'
                    ELSE 'billing:serverless'
                END
            ELSE 'billing:other'
        END AS source
    FROM {{ source('system_billing', 'usage') }}
    WHERE usage_unit = 'DBU' AND workspace_id IS NOT NULL AND usage_date < {{ audit_today() }}
),
billed_warehouse_base AS (
    SELECT rid, workspace_id, warehouse_id AS entity_id, usage_quantity, custom_tags, source
    FROM billed_usage WHERE warehouse_id IS NOT NULL
),
billed_cluster_base AS (
    SELECT rid, workspace_id, cluster_id AS entity_id, usage_quantity, custom_tags, source
    FROM billed_usage WHERE cluster_id IS NOT NULL
),
billed_pool_base AS (
    SELECT rid, workspace_id, instance_pool_id AS entity_id, usage_quantity, custom_tags, source
    FROM billed_usage WHERE instance_pool_id IS NOT NULL
),
billed_endpoint_base AS (
    SELECT rid, workspace_id, endpoint_id AS entity_id, usage_quantity, custom_tags, source
    FROM billed_usage WHERE endpoint_id IS NOT NULL
),
billed_endpoint_name_base AS (
    -- Only when endpoint_id is absent (same "first non-NULL of ..." precedence as cost_unit's
    -- compute_id, section 4.2) -- never double-counts a row under both billed_endpoint and
    -- billed_endpoint_name.
    SELECT rid, workspace_id, endpoint_name AS entity_id, usage_quantity, custom_tags, source
    FROM billed_usage WHERE endpoint_id IS NULL AND endpoint_name IS NOT NULL
),
billed_job_base AS (
    SELECT rid, workspace_id, job_id AS entity_id, usage_quantity, custom_tags, source
    FROM billed_usage WHERE job_id IS NOT NULL
),
billed_pipeline_base AS (
    SELECT rid, workspace_id, dlt_pipeline_id AS entity_id, usage_quantity, custom_tags, source
    FROM billed_usage WHERE dlt_pipeline_id IS NOT NULL
)

SELECT * FROM workspace_entity
UNION ALL SELECT * FROM warehouse_entity
UNION ALL SELECT * FROM pool_entity
UNION ALL SELECT * FROM cluster_entity
UNION ALL SELECT * FROM job_entity
UNION ALL SELECT * FROM pipeline_entity
UNION ALL SELECT * FROM statement_entity
UNION ALL SELECT * FROM uc_table_entity
UNION ALL SELECT * FROM uc_schema_entity
UNION ALL SELECT * FROM uc_column_entity
UNION ALL SELECT * FROM uc_volume_entity
UNION ALL SELECT * FROM {{ tag_entity_billed_inference('billed_warehouse_base', 'billed_warehouse') }}
UNION ALL SELECT * FROM {{ tag_entity_billed_inference('billed_cluster_base', 'billed_cluster') }}
UNION ALL SELECT * FROM {{ tag_entity_billed_inference('billed_pool_base', 'billed_pool') }}
UNION ALL SELECT * FROM {{ tag_entity_billed_inference('billed_endpoint_base', 'billed_endpoint') }}
UNION ALL SELECT * FROM {{ tag_entity_billed_inference('billed_endpoint_name_base', 'billed_endpoint_name') }}
UNION ALL SELECT * FROM {{ tag_entity_billed_inference('billed_job_base', 'billed_job') }}
UNION ALL SELECT * FROM {{ tag_entity_billed_inference('billed_pipeline_base', 'billed_pipeline') }}
