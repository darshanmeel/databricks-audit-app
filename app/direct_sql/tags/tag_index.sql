-- hand-written; not generated. Databricks twin of dbt/models/tags/tag_index.sql: one row per
-- (tag_key, tag_value, source) over every place a tag is seen. Whole snapshot, complete days only
-- -- no __WINDOW_DAYS__ marker. __SHARE_FLOOR__/__COVERAGE_FLOOR__ are filled in at export time
-- from config/settings.yml. tag_workspace's and tag_entity's own chains (minus tag_entity's
-- statement/billed_* parts, not needed here) are inlined below -- no ref() in direct mode.
WITH

-- =============================================================================================
-- 1. billing_custom_tags -- any tag on a DBU-billed usage row, complete days only.
-- =============================================================================================
billing_rows AS (
    SELECT monotonically_increasing_id() AS rid, workspace_id, usage_date, usage_quantity, custom_tags
    FROM `system`.`billing`.`usage`
    WHERE usage_unit = 'DBU' AND usage_date < __AS_OF_DATE__
      AND ingestion_date < __AS_OF_DATE__
),
billing_pairs AS (
    SELECT b.workspace_id, b.usage_date, b.usage_quantity, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM billing_rows b
    LATERAL VIEW explode(b.custom_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY b.rid, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
src_billing_custom_tags AS (
    SELECT tag_key, tag_value, 'billing_custom_tags' AS source,
           COUNT(*) AS object_count, COUNT(DISTINCT workspace_id) AS workspace_count,
           SUM(usage_quantity) AS dbus, MIN(usage_date) AS first_seen, MAX(usage_date) AS last_seen
    FROM billing_pairs
    GROUP BY tag_key, tag_value
),

-- =============================================================================================
-- 2. workspace_inferred -- dbt's tag_workspace.sql, inlined (allocating rows only).
-- =============================================================================================
ws_billed AS (
    SELECT monotonically_increasing_id() AS rid, u.workspace_id, u.usage_quantity, u.custom_tags
    FROM `system`.`billing`.`usage` u
    WHERE u.usage_unit = 'DBU' AND u.workspace_id IS NOT NULL AND u.usage_date < __AS_OF_DATE__
      AND u.ingestion_date < __AS_OF_DATE__
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
tag_workspace AS (
    SELECT r.workspace_id, r.tag_key, r.tag_value, d.display_key,
           (k.dbus_tagged / NULLIF(t.dbus_total, 0) >= __COVERAGE_FLOOR__ AND r.dbus / NULLIF(k.dbus_tagged, 0) >= __SHARE_FLOOR__) IS TRUE
               AS is_allocating,
           r.dbus / NULLIF(k.dbus_tagged, 0) AS share, k.dbus_tagged
    FROM ws_ranked r
    JOIN ws_per_key k ON k.workspace_id = r.workspace_id AND k.tag_key = r.tag_key
    JOIN ws_totals t ON t.workspace_id = r.workspace_id
    JOIN ws_display d ON d.tag_key = r.tag_key
    WHERE r.rn = 1
),
src_workspace_inferred AS (
    SELECT tag_key, tag_value, 'workspace_inferred' AS source,
           COUNT(DISTINCT workspace_id) AS object_count, COUNT(DISTINCT workspace_id) AS workspace_count,
           SUM(share * dbus_tagged) AS dbus, CAST(NULL AS DATE) AS first_seen, CAST(NULL AS DATE) AS last_seen
    FROM tag_workspace
    WHERE is_allocating
    GROUP BY tag_key, tag_value
),

-- =============================================================================================
-- 3. Resource / job / pipeline tags -- tag_entity's latest-SCD2 rows, inlined.
-- =============================================================================================
warehouse_latest AS (
    SELECT warehouse_id, workspace_id, tags,
           ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
    FROM __SRC_COMPUTE_WAREHOUSES__
),
warehouse_pairs AS (
    SELECT w.warehouse_id AS entity_id, w.workspace_id, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value,
           'warehouse_tags' AS source
    FROM warehouse_latest w
    LATERAL VIEW explode(w.tags) x AS raw_key, raw_value
    WHERE w.rn = 1 AND raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY w.warehouse_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
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
pool_entity_pairs AS (
    SELECT instance_pool_id AS entity_id, workspace_id, raw_key, tag_key, tag_value, 'pool_tags' AS source
    FROM pool_pairs
),
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
resource_entity AS (
    SELECT entity_id, workspace_id, raw_key, tag_key, tag_value, source FROM warehouse_pairs
    UNION ALL
    SELECT entity_id, workspace_id, raw_key, tag_key, tag_value, source FROM pool_entity_pairs
    UNION ALL
    SELECT cluster_id, workspace_id, raw_key, tag_key, tag_value, 'cluster_tags' FROM cluster_own_pairs
    UNION ALL
    SELECT cluster_id, workspace_id, raw_key, tag_key, tag_value, 'pool_tags' FROM cluster_pool_fallback_unclaimed
    UNION ALL
    SELECT job_id, workspace_id, raw_key, tag_key, tag_value, 'job_tags' FROM (
        SELECT j.job_id, j.workspace_id, raw_key,
               regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
        FROM (
            SELECT job_id, workspace_id, tags,
                   ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) AS rn
            FROM __SRC_LAKEFLOW_JOBS__
        ) j
        LATERAL VIEW explode(j.tags) x AS raw_key, raw_value
        WHERE j.rn = 1 AND raw_key IS NOT NULL AND raw_value IS NOT NULL
          AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY j.workspace_id, j.job_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
        ) = 1
    )
    UNION ALL
    SELECT pipeline_id, workspace_id, raw_key, tag_key, tag_value, 'pipeline_tags' FROM (
        SELECT p.pipeline_id, p.workspace_id, raw_key,
               regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
        FROM (
            SELECT pipeline_id, workspace_id, tags,
                   ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) AS rn
            FROM __SRC_LAKEFLOW_PIPELINES__
        ) p
        LATERAL VIEW explode(p.tags) x AS raw_key, raw_value
        WHERE p.rn = 1 AND raw_key IS NOT NULL AND raw_value IS NOT NULL
          AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY p.workspace_id, p.pipeline_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
        ) = 1
    )
),
src_resource_tags AS (
    SELECT tag_key, tag_value, source,
           COUNT(DISTINCT entity_id || '~' || COALESCE(workspace_id, '~')) AS object_count,
           COUNT(DISTINCT workspace_id) AS workspace_count,
           CAST(NULL AS DOUBLE) AS dbus, CAST(NULL AS DATE) AS first_seen, CAST(NULL AS DATE) AS last_seen
    FROM resource_entity
    GROUP BY tag_key, tag_value, source
),

-- =============================================================================================
-- 4a. query_tags -- system.query.history, whole snapshot.
-- =============================================================================================
query_pairs AS (
    SELECT q.workspace_id, q.statement_id, CAST(q.start_time AS DATE) AS stmt_date, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM `system`.`query`.`history` q
    LATERAL VIEW explode(q.query_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY q.statement_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
src_query_tags AS (
    SELECT tag_key, tag_value, 'query_tags' AS source,
           COUNT(DISTINCT statement_id || '~' || COALESCE(workspace_id, '~')) AS object_count,
           COUNT(DISTINCT workspace_id) AS workspace_count,
           CAST(NULL AS DOUBLE) AS dbus, MIN(stmt_date) AS first_seen, MAX(stmt_date) AS last_seen
    FROM query_pairs
    GROUP BY tag_key, tag_value
),

-- =============================================================================================
-- 4b. attributed_query_tags -- billing.attributed_usage.granular_tags.query_tags.
-- =============================================================================================
attributed_pairs AS (
    SELECT a.workspace_id, a.record_id, a.usage_date, a.active_usage_quantity, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM __SRC_BILLING_ATTRIBUTED_USAGE__ a
    LATERAL VIEW explode(a.granular_tags.query_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY a.record_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
src_attributed_query_tags AS (
    SELECT tag_key, tag_value, 'attributed_query_tags' AS source,
           COUNT(DISTINCT record_id) AS object_count, COUNT(DISTINCT workspace_id) AS workspace_count,
           SUM(active_usage_quantity) AS dbus, MIN(usage_date) AS first_seen, MAX(usage_date) AS last_seen
    FROM attributed_pairs
    GROUP BY tag_key, tag_value
),

-- =============================================================================================
-- 5. Unity Catalog tags -- already flat rows, no map to explode. workspace_count is always NULL.
-- =============================================================================================
uc_entity AS (
    SELECT lower(t.catalog_name) || '.' || lower(t.schema_name) || '.' || lower(t.table_name) AS entity_id,
           t.tag_name AS raw_key, regexp_replace(lower(t.tag_name), '[ _-]+', '') AS tag_key,
           trim(t.tag_value) AS tag_value, 'uc_table_tags' AS source
    FROM __SRC_UC_TABLE_TAGS__ t
    WHERE t.tag_name IS NOT NULL AND t.tag_value IS NOT NULL
      AND regexp_replace(lower(t.tag_name), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(t.catalog_name), lower(t.schema_name), lower(t.table_name),
                     regexp_replace(lower(t.tag_name), '[ _-]+', '')
        ORDER BY t.tag_name
    ) = 1

    UNION ALL

    SELECT lower(s.catalog_name) || '.' || lower(s.schema_name) AS entity_id,
           s.tag_name AS raw_key, regexp_replace(lower(s.tag_name), '[ _-]+', '') AS tag_key,
           trim(s.tag_value) AS tag_value, 'uc_schema_tags' AS source
    FROM __SRC_UC_SCHEMA_TAGS__ s
    WHERE s.tag_name IS NOT NULL AND s.tag_value IS NOT NULL
      AND regexp_replace(lower(s.tag_name), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(s.catalog_name), lower(s.schema_name), regexp_replace(lower(s.tag_name), '[ _-]+', '')
        ORDER BY s.tag_name
    ) = 1

    UNION ALL

    SELECT lower(c.catalog_name) || '.' || lower(c.schema_name) || '.' || lower(c.table_name) || '.'
               || lower(c.column_name) AS entity_id,
           c.tag_name AS raw_key, regexp_replace(lower(c.tag_name), '[ _-]+', '') AS tag_key,
           trim(c.tag_value) AS tag_value, 'uc_column_tags' AS source
    FROM __SRC_UC_COLUMN_TAGS__ c
    WHERE c.tag_name IS NOT NULL AND c.tag_value IS NOT NULL
      AND regexp_replace(lower(c.tag_name), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(c.catalog_name), lower(c.schema_name), lower(c.table_name), lower(c.column_name),
                     regexp_replace(lower(c.tag_name), '[ _-]+', '')
        ORDER BY c.tag_name
    ) = 1

    UNION ALL

    SELECT lower(v.catalog_name) || '.' || lower(v.schema_name) || '.' || lower(v.volume_name) AS entity_id,
           v.tag_name AS raw_key, regexp_replace(lower(v.tag_name), '[ _-]+', '') AS tag_key,
           trim(v.tag_value) AS tag_value, 'uc_volume_tags' AS source
    FROM __SRC_UC_VOLUME_TAGS__ v
    WHERE v.tag_name IS NOT NULL AND v.tag_value IS NOT NULL
      AND regexp_replace(lower(v.tag_name), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY lower(v.catalog_name), lower(v.schema_name), lower(v.volume_name),
                     regexp_replace(lower(v.tag_name), '[ _-]+', '')
        ORDER BY v.tag_name
    ) = 1
),
src_uc_tags AS (
    SELECT tag_key, tag_value, source,
           COUNT(DISTINCT entity_id) AS object_count, CAST(NULL AS BIGINT) AS workspace_count,
           CAST(NULL AS DOUBLE) AS dbus, CAST(NULL AS DATE) AS first_seen, CAST(NULL AS DATE) AS last_seen
    FROM uc_entity
    GROUP BY tag_key, tag_value, source
),

-- =============================================================================================
-- 6. ai_gateway_endpoint_tags -- system.ai_gateway.usage.endpoint_tags, per gateway request.
-- =============================================================================================
ai_gateway_pairs AS (
    SELECT g.workspace_id, g.request_id, COALESCE(g.endpoint_id, g.endpoint_name) AS ep_id,
           CAST(g.event_time AS DATE) AS ev_date, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM __SRC_AI_GATEWAY_USAGE__ g
    LATERAL VIEW explode(g.endpoint_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY g.request_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
src_ai_gateway_endpoint_tags AS (
    SELECT tag_key, tag_value, 'ai_gateway_endpoint_tags' AS source,
           COUNT(DISTINCT ep_id || '~' || COALESCE(workspace_id, '~')) AS object_count,
           COUNT(DISTINCT workspace_id) AS workspace_count,
           CAST(NULL AS DOUBLE) AS dbus, MIN(ev_date) AS first_seen, MAX(ev_date) AS last_seen
    FROM ai_gateway_pairs
    GROUP BY tag_key, tag_value
),

-- =============================================================================================
-- display_key / raw_keys: over every source's raw spellings (most object_count wins, ties
-- alphabetical); raw_keys is the sorted list, capped at 10.
-- =============================================================================================
all_raw_pairs AS (
    SELECT tag_key, raw_key FROM billing_pairs
    UNION ALL SELECT tag_key, display_key AS raw_key FROM tag_workspace WHERE is_allocating
    UNION ALL SELECT tag_key, raw_key FROM resource_entity
    UNION ALL SELECT tag_key, raw_key FROM query_pairs
    UNION ALL SELECT tag_key, raw_key FROM attributed_pairs
    UNION ALL SELECT tag_key, raw_key FROM uc_entity
    UNION ALL SELECT tag_key, raw_key FROM ai_gateway_pairs
),
raw_key_counts AS (
    SELECT tag_key, raw_key, COUNT(*) AS n
    FROM all_raw_pairs
    GROUP BY tag_key, raw_key
),
key_display AS (
    SELECT tag_key, raw_key AS display_key
    FROM (
        SELECT tag_key, raw_key, ROW_NUMBER() OVER (PARTITION BY tag_key ORDER BY n DESC, raw_key) AS rn
        FROM raw_key_counts
    ) ranked
    WHERE rn = 1
),
raw_key_distinct_ranked AS (
    SELECT tag_key, raw_key, ROW_NUMBER() OVER (PARTITION BY tag_key ORDER BY raw_key) AS rn
    FROM (SELECT DISTINCT tag_key, raw_key FROM all_raw_pairs)
),
key_raw_keys AS (
    SELECT tag_key, array_join(sort_array(collect_set(raw_key)), ',') AS raw_keys
    FROM raw_key_distinct_ranked
    WHERE rn <= 10
    GROUP BY tag_key
),

-- =============================================================================================
-- Every source's (tag_key, tag_value, source) rows, unioned (this is the model's own grain).
-- =============================================================================================
all_final AS (
    SELECT * FROM src_billing_custom_tags
    UNION ALL SELECT * FROM src_workspace_inferred
    UNION ALL SELECT * FROM src_resource_tags
    UNION ALL SELECT * FROM src_query_tags
    UNION ALL SELECT * FROM src_attributed_query_tags
    UNION ALL SELECT * FROM src_uc_tags
    UNION ALL SELECT * FROM src_ai_gateway_endpoint_tags
)

SELECT
    f.tag_key,
    kd.display_key,
    kr.raw_keys,
    f.tag_value,
    f.source,
    f.object_count,
    f.workspace_count,
    f.dbus,
    f.first_seen,
    f.last_seen
FROM all_final f
LEFT JOIN key_display kd ON kd.tag_key = f.tag_key
LEFT JOIN key_raw_keys kr ON kr.tag_key = f.tag_key
