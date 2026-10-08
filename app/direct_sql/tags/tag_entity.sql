-- hand-written; not generated. Databricks twin of dbt/models/tags/tag_entity.sql: one row per
-- (entity_type, workspace_id, entity_id, tag_key) for every place a Databricks object carries a
-- tag. Whole snapshot, complete days only -- no __WINDOW_DAYS__ marker. __SHARE_FLOOR__/
-- __COVERAGE_FLOOR__ (and their _PCT display twins) are filled in at export time from
-- config/settings.yml, same as __WINDOW_DAYS__.
-- The 7 billed_* entity types (tags.tag_entity_billed_inference in the dbt macro) are combined into
-- one shared pipeline below (billed_all onward), partitioned by entity_type as well as entity_id,
-- rather than one hand-copied block per type -- same result, far less repetition.
WITH

-- =============================================================================================
-- workspace: dbt's tag_workspace.sql, inlined verbatim (no ref() in direct mode).
-- =============================================================================================
ws_billed AS (
    SELECT monotonically_increasing_id() AS rid, u.workspace_id, u.usage_quantity, u.custom_tags
    FROM `system`.`billing`.`usage` u
    WHERE u.usage_unit = 'DBU' AND u.workspace_id IS NOT NULL AND u.usage_date < __AS_OF_DATE__
      AND u.ingestion_date < __AS_OF_DATE__
      -- every billed row without a usage policy: an Azure workspace tag reaches serverless usage as
      -- well as classic compute, and a policy's tags are the policy's, not the workspace's
      AND u.usage_metadata.budget_policy_id IS NULL AND u.usage_metadata.usage_policy_id IS NULL
),
ws_totals AS (
    SELECT workspace_id, SUM(usage_quantity) AS dbus_total FROM ws_billed GROUP BY workspace_id
),
ws_pairs AS (
    SELECT b.workspace_id, b.usage_quantity, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM ws_billed b
    LATERAL VIEW explode(b.custom_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY b.rid, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
ws_per_value AS (
    SELECT workspace_id, tag_key, tag_value, SUM(usage_quantity) AS dbus
    FROM ws_pairs GROUP BY workspace_id, tag_key, tag_value
),
ws_per_key AS (
    SELECT workspace_id, tag_key, SUM(dbus) AS dbus_tagged FROM ws_per_value GROUP BY workspace_id, tag_key
),
ws_ranked AS (
    SELECT workspace_id, tag_key, tag_value, dbus,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key ORDER BY dbus DESC, tag_value) AS rn
    FROM ws_per_value
),
ws_display AS (
    SELECT tag_key, raw_key AS display_key FROM (
        SELECT tag_key, raw_key,
               ROW_NUMBER() OVER (PARTITION BY tag_key ORDER BY SUM(usage_quantity) DESC, raw_key) AS rn
        FROM ws_pairs GROUP BY tag_key, raw_key
    ) d WHERE rn = 1
),
ws_measured AS (
    SELECT r.workspace_id, r.tag_key, d.display_key, r.tag_value AS top_value,
           r.dbus / NULLIF(k.dbus_tagged, 0) AS share,
           k.dbus_tagged / NULLIF(t.dbus_total, 0) AS coverage
    FROM ws_ranked r
    JOIN ws_per_key k ON k.workspace_id = r.workspace_id AND k.tag_key = r.tag_key
    JOIN ws_totals t ON t.workspace_id = r.workspace_id
    JOIN ws_display d ON d.tag_key = r.tag_key
    WHERE r.rn = 1
),
workspace_entity AS (
    SELECT
        'workspace' AS entity_type, workspace_id, workspace_id AS entity_id, tag_key, display_key AS raw_key,
        CASE WHEN coverage IS NULL OR coverage < __COVERAGE_FLOOR__ THEN '__untagged__'
             WHEN share >= __SHARE_FLOOR__ THEN top_value ELSE '__mixed__' END AS tag_value,
        (coverage >= __COVERAGE_FLOOR__ AND share >= __SHARE_FLOOR__) IS TRUE AS is_allocating,
        'workspace_inferred' AS source,
        share, coverage,
        CASE
            WHEN coverage IS NULL OR coverage < __COVERAGE_FLOOR__ THEN
                'only ' || CAST(ROUND(COALESCE(coverage, 0) * 100, 1) AS STRING) || '% of this workspace\'s billed DBUs carry '
                || display_key || ' (top value "' || top_value || '"); at least __COVERAGE_FLOOR_PCT__% is needed to infer a workspace tag'
            WHEN share >= __SHARE_FLOOR__ THEN
                display_key || ' "' || top_value || '" carries ' || CAST(ROUND(share * 100, 1) AS STRING)
                || '% of the ' || display_key || '-tagged DBUs, which are '
                || CAST(ROUND(coverage * 100, 1) AS STRING) || '% of this workspace\'s billed DBUs'
            ELSE
                'no single ' || display_key || ' value reaches __SHARE_FLOOR_PCT__% of the ' || display_key
                || '-tagged DBUs (top "' || top_value || '" at ' || CAST(ROUND(share * 100, 1) AS STRING)
                || '%); tagged DBUs are ' || CAST(ROUND(coverage * 100, 1) AS STRING) || '% of this workspace\'s billed DBUs'
        END AS reason
    FROM ws_measured
),

-- =============================================================================================
-- warehouse: latest compute.warehouses.tags.
-- =============================================================================================
warehouse_latest AS (
    SELECT warehouse_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
    FROM __SRC_COMPUTE_WAREHOUSES__
),
warehouse_pairs AS (
    SELECT w.warehouse_id, w.workspace_id, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM warehouse_latest w
    LATERAL VIEW explode(w.tags) x AS raw_key, raw_value
    WHERE w.rn = 1 AND raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY w.warehouse_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
warehouse_entity AS (
    SELECT 'warehouse' AS entity_type, workspace_id, warehouse_id AS entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'warehouse_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM warehouse_pairs
),

-- =============================================================================================
-- pool: latest compute.instance_pools.tags. Shared with the cluster fallback below.
-- =============================================================================================
pool_latest AS (
    SELECT instance_pool_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY instance_pool_id ORDER BY change_time DESC) AS rn
    FROM __SRC_COMPUTE_INSTANCE_POOLS__
),
pool_pairs AS (
    SELECT p.instance_pool_id, p.workspace_id, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM pool_latest p
    LATERAL VIEW explode(p.tags) x AS raw_key, raw_value
    WHERE p.rn = 1 AND raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY p.instance_pool_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
pool_entity AS (
    SELECT 'pool' AS entity_type, workspace_id, instance_pool_id AS entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'pool_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM pool_pairs
),

-- =============================================================================================
-- cluster: its own tags, plus (for a key it does not carry) its pool's tag, worker pool first.
-- =============================================================================================
cluster_latest AS (
    SELECT cluster_id, workspace_id, tags, worker_instance_pool_id, driver_instance_pool_id,
           ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
    FROM __SRC_COMPUTE_CLUSTERS__
),
cluster_own_pairs AS (
    SELECT c.cluster_id, c.workspace_id, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM cluster_latest c
    LATERAL VIEW explode(c.tags) x AS raw_key, raw_value
    WHERE c.rn = 1 AND raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY c.cluster_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
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
    SELECT f.cluster_id, f.workspace_id, f.raw_key, f.tag_key, f.tag_value
    FROM cluster_pool_fallback f
    WHERE NOT EXISTS (
        SELECT 1 FROM cluster_own_pairs o WHERE o.cluster_id = f.cluster_id AND o.tag_key = f.tag_key
    )
),
cluster_entity AS (
    SELECT 'cluster' AS entity_type, workspace_id, cluster_id AS entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'cluster_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM cluster_own_pairs

    UNION ALL

    SELECT 'cluster' AS entity_type, workspace_id, cluster_id AS entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'pool_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM cluster_pool_fallback_unclaimed
),

-- =============================================================================================
-- job / pipeline: latest lakeflow.jobs.tags / lakeflow.pipelines.tags.
-- =============================================================================================
job_latest AS (
    SELECT job_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) AS rn
    FROM __SRC_LAKEFLOW_JOBS__
),
job_pairs AS (
    SELECT j.job_id, j.workspace_id, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM job_latest j
    LATERAL VIEW explode(j.tags) x AS raw_key, raw_value
    WHERE j.rn = 1 AND raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY j.workspace_id, j.job_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
job_entity AS (
    SELECT 'job' AS entity_type, workspace_id, job_id AS entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'job_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM job_pairs
),
pipeline_latest AS (
    SELECT pipeline_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) AS rn
    FROM __SRC_LAKEFLOW_PIPELINES__
),
pipeline_pairs AS (
    SELECT p.pipeline_id, p.workspace_id, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM pipeline_latest p
    LATERAL VIEW explode(p.tags) x AS raw_key, raw_value
    WHERE p.rn = 1 AND raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY p.workspace_id, p.pipeline_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
pipeline_entity AS (
    SELECT 'pipeline' AS entity_type, workspace_id, pipeline_id AS entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'pipeline_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM pipeline_pairs
),

-- =============================================================================================
-- statement: query.history.query_tags, whole snapshot.
-- =============================================================================================
statement_pairs AS (
    SELECT q.statement_id, q.workspace_id, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM `system`.`query`.`history` q
    LATERAL VIEW explode(q.query_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY q.statement_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
statement_entity AS (
    SELECT 'statement' AS entity_type, workspace_id, statement_id AS entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'query_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM statement_pairs
),

-- =============================================================================================
-- Unity Catalog object tags: already flat (catalog/schema[/table[/column|volume]], tag_name,
-- tag_value) -- no map to explode. workspace_id is NULL: a UC object has no workspace.
-- =============================================================================================
uc_table_pairs AS (
    SELECT lower(t.catalog_name) || '.' || lower(t.schema_name) || '.' || lower(t.table_name) AS entity_id,
           t.tag_name AS raw_key, regexp_replace(lower(t.tag_name), '[ _-]+', '') AS tag_key,
           trim(t.tag_value) AS tag_value
    FROM __SRC_UC_TABLE_TAGS__ t
    WHERE t.tag_name IS NOT NULL AND t.tag_value IS NOT NULL
      AND regexp_replace(lower(t.tag_name), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(t.catalog_name), lower(t.schema_name), lower(t.table_name),
                     regexp_replace(lower(t.tag_name), '[ _-]+', '')
        ORDER BY t.tag_name
    ) = 1
),
uc_table_entity AS (
    SELECT 'uc_table' AS entity_type, CAST(NULL AS STRING) AS workspace_id, entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'uc_table_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM uc_table_pairs
),
uc_schema_pairs AS (
    SELECT lower(s.catalog_name) || '.' || lower(s.schema_name) AS entity_id,
           s.tag_name AS raw_key, regexp_replace(lower(s.tag_name), '[ _-]+', '') AS tag_key,
           trim(s.tag_value) AS tag_value
    FROM __SRC_UC_SCHEMA_TAGS__ s
    WHERE s.tag_name IS NOT NULL AND s.tag_value IS NOT NULL
      AND regexp_replace(lower(s.tag_name), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(s.catalog_name), lower(s.schema_name), regexp_replace(lower(s.tag_name), '[ _-]+', '')
        ORDER BY s.tag_name
    ) = 1
),
uc_schema_entity AS (
    SELECT 'uc_schema' AS entity_type, CAST(NULL AS STRING) AS workspace_id, entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'uc_schema_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM uc_schema_pairs
),
uc_column_pairs AS (
    SELECT lower(c.catalog_name) || '.' || lower(c.schema_name) || '.' || lower(c.table_name) || '.'
               || lower(c.column_name) AS entity_id,
           c.tag_name AS raw_key, regexp_replace(lower(c.tag_name), '[ _-]+', '') AS tag_key,
           trim(c.tag_value) AS tag_value
    FROM __SRC_UC_COLUMN_TAGS__ c
    WHERE c.tag_name IS NOT NULL AND c.tag_value IS NOT NULL
      AND regexp_replace(lower(c.tag_name), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(c.catalog_name), lower(c.schema_name), lower(c.table_name), lower(c.column_name),
                     regexp_replace(lower(c.tag_name), '[ _-]+', '')
        ORDER BY c.tag_name
    ) = 1
),
uc_column_entity AS (
    SELECT 'uc_column' AS entity_type, CAST(NULL AS STRING) AS workspace_id, entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'uc_column_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM uc_column_pairs
),
uc_volume_pairs AS (
    SELECT lower(v.catalog_name) || '.' || lower(v.schema_name) || '.' || lower(v.volume_name) AS entity_id,
           v.tag_name AS raw_key, regexp_replace(lower(v.tag_name), '[ _-]+', '') AS tag_key,
           trim(v.tag_value) AS tag_value
    FROM __SRC_UC_VOLUME_TAGS__ v
    WHERE v.tag_name IS NOT NULL AND v.tag_value IS NOT NULL
      AND regexp_replace(lower(v.tag_name), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(v.catalog_name), lower(v.schema_name), lower(v.volume_name),
                     regexp_replace(lower(v.tag_name), '[ _-]+', '')
        ORDER BY v.tag_name
    ) = 1
),
uc_volume_entity AS (
    SELECT 'uc_volume' AS entity_type, CAST(NULL AS STRING) AS workspace_id, entity_id, tag_key, raw_key, tag_value,
           TRUE AS is_allocating, 'uc_volume_tags' AS source,
           CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage, CAST(NULL AS STRING) AS reason
    FROM uc_volume_pairs
),

-- =============================================================================================
-- billed_*: the dominant billing.usage.custom_tags value per billed entity id (section 3.3). The
-- 7 macro calls in dbt/models/tags/tag_entity.sql are one shared pipeline here, partitioned by
-- entity_type as well as entity_id so the 7 types never mix.
-- =============================================================================================
billed_usage AS (
    SELECT
        monotonically_increasing_id() AS rid,
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
        END AS row_source
    FROM `system`.`billing`.`usage`
    WHERE usage_unit = 'DBU' AND workspace_id IS NOT NULL AND usage_date < __AS_OF_DATE__
      AND ingestion_date < __AS_OF_DATE__
),
billed_all AS (
    SELECT rid, workspace_id, 'billed_warehouse' AS entity_type, warehouse_id AS entity_id,
           usage_quantity, custom_tags, row_source AS source
    FROM billed_usage WHERE warehouse_id IS NOT NULL
    UNION ALL
    SELECT rid, workspace_id, 'billed_cluster', cluster_id, usage_quantity, custom_tags, row_source
    FROM billed_usage WHERE cluster_id IS NOT NULL
    UNION ALL
    SELECT rid, workspace_id, 'billed_pool', instance_pool_id, usage_quantity, custom_tags, row_source
    FROM billed_usage WHERE instance_pool_id IS NOT NULL
    UNION ALL
    SELECT rid, workspace_id, 'billed_endpoint', endpoint_id, usage_quantity, custom_tags, row_source
    FROM billed_usage WHERE endpoint_id IS NOT NULL
    UNION ALL
    -- only when endpoint_id is absent: never double-counts a row under both billed_endpoint types.
    SELECT rid, workspace_id, 'billed_endpoint_name', endpoint_name, usage_quantity, custom_tags, row_source
    FROM billed_usage WHERE endpoint_id IS NULL AND endpoint_name IS NOT NULL
    UNION ALL
    SELECT rid, workspace_id, 'billed_job', job_id, usage_quantity, custom_tags, row_source
    FROM billed_usage WHERE job_id IS NOT NULL
    UNION ALL
    SELECT rid, workspace_id, 'billed_pipeline', dlt_pipeline_id, usage_quantity, custom_tags, row_source
    FROM billed_usage WHERE dlt_pipeline_id IS NOT NULL
),
billed_pairs AS (
    SELECT ba.entity_type, ba.workspace_id, ba.entity_id, ba.rid, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value,
           ba.usage_quantity, ba.source
    FROM billed_all ba
    LATERAL VIEW explode(ba.custom_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ba.entity_type, ba.rid, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
billed_per_value AS (
    SELECT entity_type, workspace_id, entity_id, tag_key, tag_value, SUM(usage_quantity) AS dbus,
           max_by(source, usage_quantity) AS src
    FROM billed_pairs
    GROUP BY entity_type, workspace_id, entity_id, tag_key, tag_value
),
billed_per_key AS (
    SELECT entity_type, workspace_id, entity_id, tag_key, SUM(dbus) AS dbus_tagged
    FROM billed_per_value
    GROUP BY entity_type, workspace_id, entity_id, tag_key
),
billed_totals AS (
    SELECT entity_type, workspace_id, entity_id, SUM(usage_quantity) AS dbus_total
    FROM billed_all
    GROUP BY entity_type, workspace_id, entity_id
),
billed_ranked AS (
    SELECT entity_type, workspace_id, entity_id, tag_key, tag_value, dbus, src,
           ROW_NUMBER() OVER (
               PARTITION BY entity_type, workspace_id, entity_id, tag_key ORDER BY dbus DESC, tag_value
           ) AS rn
    FROM billed_per_value
),
billed_display AS (
    SELECT entity_type, workspace_id, entity_id, tag_key, raw_key,
           ROW_NUMBER() OVER (
               PARTITION BY entity_type, workspace_id, entity_id, tag_key ORDER BY SUM(usage_quantity) DESC, raw_key
           ) AS rn
    FROM billed_pairs
    GROUP BY entity_type, workspace_id, entity_id, tag_key, raw_key
),
billed_measured AS (
    SELECT r.entity_type, r.workspace_id, r.entity_id, r.tag_key, d.raw_key, r.tag_value AS top_value,
           r.src AS source,
           r.dbus / NULLIF(k.dbus_tagged, 0) AS share,
           k.dbus_tagged / NULLIF(t.dbus_total, 0) AS coverage
    FROM billed_ranked r
    JOIN billed_per_key k
        ON k.entity_type = r.entity_type AND k.workspace_id = r.workspace_id
       AND k.entity_id = r.entity_id AND k.tag_key = r.tag_key
    JOIN billed_totals t
        ON t.entity_type = r.entity_type AND t.workspace_id = r.workspace_id AND t.entity_id = r.entity_id
    JOIN billed_display d
        ON d.entity_type = r.entity_type AND d.workspace_id = r.workspace_id
       AND d.entity_id = r.entity_id AND d.tag_key = r.tag_key AND d.rn = 1
    WHERE r.rn = 1
),
billed_entity AS (
    SELECT
        entity_type, workspace_id, entity_id, tag_key, raw_key,
        CASE WHEN coverage IS NULL OR coverage < __COVERAGE_FLOOR__ THEN '__untagged__'
             WHEN share >= __SHARE_FLOOR__ THEN top_value ELSE '__mixed__' END AS tag_value,
        (coverage >= __COVERAGE_FLOOR__ AND share >= __SHARE_FLOOR__) IS TRUE AS is_allocating,
        source, share, coverage,
        CASE
            WHEN coverage IS NULL OR coverage < __COVERAGE_FLOOR__ THEN
                'only ' || CAST(ROUND(COALESCE(coverage, 0) * 100, 1) AS STRING)
                || '% of this ' || entity_type || '\'s billed DBUs carry ' || raw_key || ' (top value "'
                || top_value || '"); at least __COVERAGE_FLOOR_PCT__% is needed to infer a tag'
            WHEN share >= __SHARE_FLOOR__ THEN
                raw_key || ' "' || top_value || '" carries ' || CAST(ROUND(share * 100, 1) AS STRING)
                || '% of the ' || raw_key || '-tagged DBUs, which are '
                || CAST(ROUND(coverage * 100, 1) AS STRING) || '% of this ' || entity_type || '\'s billed DBUs'
            ELSE
                'no single ' || raw_key || ' value reaches __SHARE_FLOOR_PCT__% of the ' || raw_key
                || '-tagged DBUs (top "' || top_value || '" at ' || CAST(ROUND(share * 100, 1) AS STRING)
                || '%); tagged DBUs are ' || CAST(ROUND(coverage * 100, 1) AS STRING)
                || '% of this ' || entity_type || '\'s billed DBUs'
        END AS reason
    FROM billed_measured
)

SELECT * FROM (
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
UNION ALL SELECT * FROM billed_entity
) all_entities
-- settings export_tag_keys, filled in at export time: TRUE keeps every tag
WHERE __TAG_KEY_FILTER__
