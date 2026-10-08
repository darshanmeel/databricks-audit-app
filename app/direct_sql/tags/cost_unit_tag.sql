-- hand-written; not generated. Databricks twin of dbt/models/tags/cost_unit_tag.sql: one row per
-- (window_days, unit_id, level, tag_key, tag_value) -- the REAL tag values a tags.cost_unit row
-- carries. __WINDOW_DAYS__ is filled in at export time.
--
-- The base pipeline (priced/classified/wh_days/attr_days/split_factor/alloc_rows/keyed/hashed) is
-- the SAME logic as tags/cost_unit.sql, duplicated on purpose (its own header note explains why):
-- unit_id's hash formula must stay identical in both files, or a unit here fails to join back to
-- cost_unit by unit_id.
WITH priced AS (
    SELECT __WINDOW_DAYS__ AS window_days, u.workspace_id, u.usage_date, u.usage_unit, u.usage_quantity,
           u.custom_tags, u.usage_metadata, u.product_features, lp.list_rate
    FROM `system`.`billing`.`usage` u
    LEFT JOIN (
        SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
               CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
        FROM `system`.`billing`.`list_prices`
    ) lp
      ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
     AND u.usage_end_time >= lp.price_start_time
     AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
    WHERE u.usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      AND u.usage_date < __AS_OF_DATE__
      AND u.ingestion_date < __AS_OF_DATE__
),
classified AS (
    SELECT
        window_days, workspace_id, usage_date, usage_unit, usage_quantity, custom_tags, list_rate,
        usage_metadata.warehouse_id AS warehouse_id,
        usage_metadata.job_id AS job_id,
        usage_metadata.dlt_pipeline_id AS dlt_pipeline_id,
        CASE
            WHEN usage_metadata.warehouse_id IS NOT NULL THEN 'warehouse'
            WHEN usage_metadata.cluster_id IS NOT NULL THEN 'cluster'
            WHEN usage_metadata.endpoint_id IS NOT NULL OR usage_metadata.endpoint_name IS NOT NULL THEN 'endpoint'
            WHEN usage_metadata.app_id IS NOT NULL THEN 'app'
            WHEN usage_metadata.job_id IS NOT NULL OR usage_metadata.dlt_pipeline_id IS NOT NULL
                 OR usage_metadata.notebook_id IS NOT NULL OR usage_metadata.budget_policy_id IS NOT NULL
                 OR usage_metadata.usage_policy_id IS NOT NULL OR product_features.is_serverless IS TRUE
                 THEN 'serverless'
            ELSE 'other'
        END AS compute_kind,
        COALESCE(usage_metadata.warehouse_id, usage_metadata.cluster_id, usage_metadata.endpoint_id,
                 usage_metadata.endpoint_name, usage_metadata.app_id, usage_metadata.budget_policy_id,
                 usage_metadata.usage_policy_id) AS compute_id
    FROM priced
),
wh_days AS (
    SELECT window_days, workspace_id, warehouse_id, usage_date, SUM(usage_quantity) AS billed_dbus
    FROM classified
    WHERE compute_kind = 'warehouse' AND usage_unit = 'DBU'
    GROUP BY window_days, workspace_id, warehouse_id, usage_date
),
attr_days AS (
    SELECT __WINDOW_DAYS__ AS window_days, a.workspace_id, a.usage_metadata.warehouse_id AS warehouse_id,
           a.usage_date, SUM(a.active_usage_quantity) AS attributed_dbus
    FROM __SRC_BILLING_ATTRIBUTED_USAGE__ a
    WHERE a.usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      AND a.usage_date < __AS_OF_DATE__
      AND a.usage_metadata.warehouse_id IS NOT NULL
    GROUP BY a.workspace_id, a.usage_metadata.warehouse_id, a.usage_date
),
split_factor AS (
    SELECT wd.window_days, wd.workspace_id, wd.warehouse_id, wd.usage_date,
           wd.billed_dbus, COALESCE(ad.attributed_dbus, 0) AS attributed_dbus,
           CASE WHEN wd.billed_dbus > 0 AND COALESCE(ad.attributed_dbus, 0) > 0
                THEN LEAST(ad.attributed_dbus, wd.billed_dbus) / wd.billed_dbus
                ELSE 0 END AS split_f
    FROM wh_days wd
    LEFT JOIN attr_days ad
      ON ad.window_days = wd.window_days AND ad.workspace_id = wd.workspace_id
     AND ad.warehouse_id = wd.warehouse_id AND ad.usage_date = wd.usage_date
),
alloc_rows AS (
    -- query-part rows keep usage_date and compute_id (=warehouse_id): the per-day grain the
    -- attributed-tag-value proportional split below needs.
    SELECT c.window_days, c.workspace_id, c.usage_date, c.compute_kind, c.compute_id, c.custom_tags,
           c.usage_unit, 'query' AS work_kind, CAST(NULL AS STRING) AS work_id,
           CASE WHEN c.list_rate IS NOT NULL THEN c.usage_quantity * sf.split_f * c.list_rate END AS part_usd
    FROM classified c
    JOIN split_factor sf
      ON sf.window_days = c.window_days AND sf.workspace_id = c.workspace_id
     AND sf.warehouse_id = c.warehouse_id AND sf.usage_date = c.usage_date
    WHERE c.compute_kind = 'warehouse' AND c.usage_unit = 'DBU' AND sf.split_f > 0

    UNION ALL

    SELECT c.window_days, c.workspace_id, c.usage_date, c.compute_kind, c.compute_id, c.custom_tags,
           c.usage_unit,
           CASE WHEN sf.attributed_dbus > 0 THEN 'warehouse_idle' ELSE 'warehouse_unsplit' END AS work_kind,
           CAST(NULL AS STRING) AS work_id,
           CASE WHEN c.list_rate IS NOT NULL THEN c.usage_quantity * (1 - sf.split_f) * c.list_rate END AS part_usd
    FROM classified c
    JOIN split_factor sf
      ON sf.window_days = c.window_days AND sf.workspace_id = c.workspace_id
     AND sf.warehouse_id = c.warehouse_id AND sf.usage_date = c.usage_date
    WHERE c.compute_kind = 'warehouse' AND c.usage_unit = 'DBU' AND (1 - sf.split_f) > 0

    UNION ALL

    SELECT c.window_days, c.workspace_id, c.usage_date, c.compute_kind, c.compute_id, c.custom_tags,
           c.usage_unit,
           CASE WHEN c.job_id IS NOT NULL THEN 'job'
                WHEN c.dlt_pipeline_id IS NOT NULL THEN 'pipeline'
                ELSE 'none' END AS work_kind,
           COALESCE(c.job_id, c.dlt_pipeline_id) AS work_id,
           CASE WHEN c.list_rate IS NOT NULL THEN c.usage_quantity * c.list_rate END AS part_usd
    FROM classified c
    WHERE NOT (c.compute_kind = 'warehouse' AND c.usage_unit = 'DBU')
),
keyed AS (
    SELECT r.*,
           CASE WHEN r.custom_tags IS NULL OR size(r.custom_tags) = 0 THEN NULL
                ELSE array_join(array_sort(transform(map_entries(r.custom_tags), e -> concat(e.key, '=', e.value))), ',')
           END AS tags_key
    FROM alloc_rows r
),
hashed AS (
    SELECT *,
           md5(COALESCE(workspace_id, '~') || '|' || compute_kind || '|' || COALESCE(compute_id, '~')
               || '|' || work_kind || '|' || COALESCE(work_id, '~') || '|' || usage_unit || '|'
               || COALESCE(tags_key, '~')) AS unit_id
    FROM keyed
),

-- ---------------------------------------------------------------------------------------------
-- Compute level: the unit's OWN custom_tags map, exploded (dedup per normalised key). usd = the
-- whole unit's usd.
-- ---------------------------------------------------------------------------------------------
unit_totals AS (
    SELECT window_days, unit_id, ANY_VALUE(compute_kind) AS compute_kind,
           ANY_VALUE(compute_id) AS compute_id, ANY_VALUE(custom_tags) AS custom_tags,
           COALESCE(SUM(part_usd), 0) AS usd
    FROM hashed
    GROUP BY window_days, unit_id
),
compute_pairs AS (
    SELECT ut.window_days, ut.unit_id, ut.compute_kind, ut.compute_id, ut.usd, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM unit_totals ut
    LATERAL VIEW explode(ut.custom_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ut.window_days, ut.unit_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
compute_level AS (
    SELECT window_days, unit_id, 'compute' AS level, tag_key, raw_key, tag_value,
           CASE
               WHEN compute_kind = 'warehouse' THEN 'billing:warehouse'
               WHEN compute_kind = 'cluster' THEN 'billing:cluster'
               WHEN compute_kind = 'endpoint' THEN 'billing:endpoint'
               WHEN compute_kind = 'app' THEN 'billing:app'
               WHEN compute_kind = 'serverless' AND compute_id IS NOT NULL THEN 'billing:budget_policy'
               WHEN compute_kind = 'serverless' THEN 'billing:serverless'
               ELSE 'billing:other'
           END AS source,
           usd
    FROM compute_pairs
),

-- ---------------------------------------------------------------------------------------------
-- Work level, job/pipeline units: the job's/pipeline's own LATEST tags (lakeflow), not the bill.
-- ---------------------------------------------------------------------------------------------
work_jp_units AS (
    SELECT window_days, unit_id, ANY_VALUE(workspace_id) AS workspace_id,
           ANY_VALUE(work_kind) AS work_kind, ANY_VALUE(work_id) AS work_id,
           COALESCE(SUM(part_usd), 0) AS usd
    FROM hashed
    WHERE work_kind IN ('job', 'pipeline')
    GROUP BY window_days, unit_id
),
job_tags_latest AS (
    SELECT workspace_id, job_id, tags FROM (
        SELECT workspace_id, job_id, tags,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) AS rn
        FROM __SRC_LAKEFLOW_JOBS__
    ) x WHERE rn = 1
),
pipeline_tags_latest AS (
    SELECT workspace_id, pipeline_id, tags FROM (
        SELECT workspace_id, pipeline_id, tags,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) AS rn
        FROM __SRC_LAKEFLOW_PIPELINES__
    ) x WHERE rn = 1
),
work_job_pairs AS (
    SELECT u.window_days, u.unit_id, u.usd, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value,
           'job_tags' AS source
    FROM work_jp_units u
    JOIN job_tags_latest jt ON jt.workspace_id = u.workspace_id AND jt.job_id = u.work_id
    LATERAL VIEW explode(jt.tags) x AS raw_key, raw_value
    WHERE u.work_kind = 'job' AND raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY u.window_days, u.unit_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
work_pipeline_pairs AS (
    SELECT u.window_days, u.unit_id, u.usd, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value,
           'pipeline_tags' AS source
    FROM work_jp_units u
    JOIN pipeline_tags_latest pt ON pt.workspace_id = u.workspace_id AND pt.pipeline_id = u.work_id
    LATERAL VIEW explode(pt.tags) x AS raw_key, raw_value
    WHERE u.work_kind = 'pipeline' AND raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY u.window_days, u.unit_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
work_jp_level AS (
    SELECT window_days, unit_id, 'work' AS level, tag_key, raw_key, tag_value, source, usd FROM work_job_pairs
    UNION ALL
    SELECT window_days, unit_id, 'work' AS level, tag_key, raw_key, tag_value, source, usd FROM work_pipeline_pairs
),

-- ---------------------------------------------------------------------------------------------
-- Work level, query units: split each day's query-part $ across the attributed statements' own
-- query_tags, in proportion to their attributed DBUs for that day. attributed_query_tags is
-- always the source -- Databricks' own per-query cost split, never the resource's current tags.
-- ---------------------------------------------------------------------------------------------
query_alloc_by_day AS (
    SELECT window_days, unit_id, workspace_id, compute_id AS warehouse_id, usage_date,
           SUM(part_usd) AS day_usd
    FROM hashed
    WHERE work_kind = 'query'
    GROUP BY window_days, unit_id, workspace_id, compute_id, usage_date
),
attr_rows AS (
    SELECT __WINDOW_DAYS__ AS window_days, monotonically_increasing_id() AS arid, a.workspace_id,
           a.usage_metadata.warehouse_id AS warehouse_id, a.usage_date, a.active_usage_quantity,
           a.granular_tags.query_tags AS query_tags
    FROM __SRC_BILLING_ATTRIBUTED_USAGE__ a
    WHERE a.usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      AND a.usage_date < __AS_OF_DATE__
      AND a.usage_metadata.warehouse_id IS NOT NULL
),
attr_pairs AS (
    SELECT ar.window_days, ar.arid, ar.workspace_id, ar.warehouse_id, ar.usage_date,
           ar.active_usage_quantity, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM attr_rows ar
    LATERAL VIEW explode(ar.query_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ar.window_days, ar.arid, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
attr_value_by_day AS (
    -- A(K=V) per (workspace, warehouse, usage_date)
    SELECT window_days, workspace_id, warehouse_id, usage_date, tag_key, ANY_VALUE(raw_key) AS raw_key,
           tag_value, SUM(active_usage_quantity) AS a_kv
    FROM attr_pairs
    GROUP BY window_days, workspace_id, warehouse_id, usage_date, tag_key, tag_value
),
query_tag_alloc AS (
    SELECT qd.window_days, qd.unit_id, v.tag_key, v.raw_key, v.tag_value,
           qd.day_usd * v.a_kv / NULLIF(ad.attributed_dbus, 0) AS part_usd
    FROM query_alloc_by_day qd
    JOIN attr_days ad
      ON ad.window_days = qd.window_days AND ad.workspace_id = qd.workspace_id
     AND ad.warehouse_id = qd.warehouse_id AND ad.usage_date = qd.usage_date
    JOIN attr_value_by_day v
      ON v.window_days = qd.window_days AND v.workspace_id = qd.workspace_id
     AND v.warehouse_id = qd.warehouse_id AND v.usage_date = qd.usage_date
),
query_tag_level AS (
    SELECT window_days, unit_id, 'work' AS level, tag_key, ANY_VALUE(raw_key) AS raw_key, tag_value,
           'attributed_query_tags' AS source, SUM(part_usd) AS usd
    FROM query_tag_alloc
    GROUP BY window_days, unit_id, tag_key, tag_value
)

SELECT window_days, unit_id, level, tag_key, raw_key, tag_value, source, usd
FROM compute_level
WHERE usd <> 0

UNION ALL

SELECT window_days, unit_id, level, tag_key, raw_key, tag_value, source, usd
FROM work_jp_level
WHERE usd <> 0

UNION ALL

SELECT window_days, unit_id, level, tag_key, raw_key, tag_value, source, usd
FROM query_tag_level
WHERE usd <> 0
