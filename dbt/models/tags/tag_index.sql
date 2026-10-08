{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- P4-T-IDX (tasks/P4-T-SPEC.md, section 4.2 and 5.2). tags.tag_index: one row per (tag_key,
-- tag_value, source) over EVERY place a tag is seen anywhere in the snapshot -- the table
-- GET /api/tags reads (app/core/tags.read_tag_index). No configuration: every key and value ever
-- seen becomes searchable, with no alias map.
--
--   display_key    the raw spelling with the most object_count, ties alphabetical (section 5.2),
--                   computed once per tag_key and broadcast onto every row of that key.
--   raw_keys       every distinct raw spelling seen anywhere for this tag_key, comma-joined,
--                   sorted, capped at 10 (section 4.2).
--   object_count   what counts as "an object" is source-specific: billing rows (billing_custom_
--                   tags), workspaces (workspace_inferred, allocating only), entities (resource/
--                   job/pipeline tags, UC tags), statements (query tags).
--   workspace_count NULL for the four uc_* sources (a UC object has no workspace).
--   dbus           billing-derived sources only (billing_custom_tags, workspace_inferred,
--                   attributed_query_tags) -- NULL everywhere else (section 5.2/4.2).
--   first_seen/last_seen  by date, where the source has one; NULL where it does not (a resource's
--                   "latest tags" carries no history here).
--
-- Skips NULL keys and keys that normalise to "" (dbt/macros/norm_tag_key.sql), same as
-- tag_workspace.sql. DuckDB only for now (P4-T todo: a Databricks dialect branch).
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.tag_index is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}

WITH

-- =============================================================================================
-- 1. billing_custom_tags -- any tag on a DBU-billed usage row, complete days only (section 5.2).
-- =============================================================================================
billing_rows AS (
    SELECT ROW_NUMBER() OVER () AS rid, workspace_id, usage_date, usage_quantity, custom_tags
    FROM {{ source('system_billing', 'usage') }}
    WHERE usage_unit = 'DBU' AND usage_date < {{ audit_today() }}
),
billing_pairs AS (
    SELECT b.workspace_id, b.usage_date, b.usage_quantity, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM billing_rows b, unnest(map_entries(b.custom_tags)) AS x(e)
    WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY b.rid, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
),
src_billing_custom_tags AS (
    SELECT tag_key, tag_value, 'billing_custom_tags' AS source,
           COUNT(*) AS object_count, COUNT(DISTINCT workspace_id) AS workspace_count,
           SUM(usage_quantity) AS dbus, MIN(usage_date) AS first_seen, MAX(usage_date) AS last_seen
    FROM billing_pairs
    GROUP BY tag_key, tag_value
),

-- =============================================================================================
-- 2. workspace_inferred -- tags.tag_workspace's allocating rows only (a mixed/untagged inference
-- is not itself a searchable value).
-- =============================================================================================
src_workspace_inferred AS (
    SELECT tag_key, tag_value, 'workspace_inferred' AS source,
           COUNT(DISTINCT workspace_id) AS object_count, COUNT(DISTINCT workspace_id) AS workspace_count,
           SUM(share * dbus_tagged) AS dbus, CAST(NULL AS DATE) AS first_seen, CAST(NULL AS DATE) AS last_seen
    FROM {{ ref('tag_workspace') }}
    WHERE is_allocating
    GROUP BY tag_key, tag_value
),

-- =============================================================================================
-- 3. Resource / job / pipeline tags -- tags.tag_entity's latest-SCD2 rows (warehouse_tags,
-- cluster_tags, pool_tags, job_tags, pipeline_tags).
-- =============================================================================================
resource_entity AS (
    SELECT * FROM {{ ref('tag_entity') }}
    WHERE source IN ('warehouse_tags', 'cluster_tags', 'pool_tags', 'job_tags', 'pipeline_tags')
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
-- 4a. query_tags -- system.query.history, whole snapshot (statement grain -- see 4.2's caveat
-- about this being the largest tags table on an account that tags every statement).
-- =============================================================================================
query_pairs AS (
    SELECT q.workspace_id, q.statement_id, CAST(q.start_time AS DATE) AS stmt_date,
           e.key AS raw_key, {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM {{ source('system_query', 'history') }} q, unnest(map_entries(q.query_tags)) AS x(e)
    WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY q.statement_id, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
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
-- 4b. attributed_query_tags -- system.billing.attributed_usage.granular_tags.query_tags, the
-- per-statement DBU split Databricks itself computes -- a billing source, so `dbus` is populated.
-- =============================================================================================
attributed_pairs AS (
    SELECT a.workspace_id, a.record_id, a.usage_date, a.active_usage_quantity,
           e.key AS raw_key, {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM {{ source('system_billing', 'attributed_usage') }} a,
         unnest(map_entries(a.granular_tags.query_tags)) AS x(e)
    WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY a.record_id, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
),
src_attributed_query_tags AS (
    SELECT tag_key, tag_value, 'attributed_query_tags' AS source,
           COUNT(DISTINCT record_id) AS object_count, COUNT(DISTINCT workspace_id) AS workspace_count,
           SUM(active_usage_quantity) AS dbus, MIN(usage_date) AS first_seen, MAX(usage_date) AS last_seen
    FROM attributed_pairs
    GROUP BY tag_key, tag_value
),

-- =============================================================================================
-- 5. Unity Catalog tags -- tags.tag_entity's uc_* rows. workspace_count is always NULL: a UC
-- object has no workspace (section 4.2).
-- =============================================================================================
uc_entity AS (
    SELECT * FROM {{ ref('tag_entity') }}
    WHERE entity_type IN ('uc_table', 'uc_schema', 'uc_column', 'uc_volume')
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
           CAST(g.event_time AS DATE) AS ev_date,
           e.key AS raw_key, {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM {{ source('system_ai_gateway', 'usage') }} g, unnest(map_entries(g.endpoint_tags)) AS x(e)
    WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY g.request_id, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
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
-- display_key / raw_keys: computed once per tag_key over EVERY source's raw spellings (section
-- 5.2: "the most object_count wins, ties alphabetical").
-- =============================================================================================
all_raw_pairs AS (
    SELECT tag_key, raw_key FROM billing_pairs
    UNION ALL SELECT tag_key, display_key AS raw_key FROM {{ ref('tag_workspace') }} WHERE is_allocating
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
    SELECT tag_key, string_agg(raw_key, ',' ORDER BY raw_key) AS raw_keys
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
