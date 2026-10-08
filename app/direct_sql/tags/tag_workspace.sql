-- hand-written; not generated. Databricks twin of dbt/models/tags/tag_workspace.sql: the workspace
-- level of the tag rollup, inferred from system.billing.usage.custom_tags for every key. Whole
-- snapshot, complete days only -- no __WINDOW_DAYS__ marker here. __SHARE_FLOOR__/
-- __COVERAGE_FLOOR__ and their __SHARE_FLOOR_PCT__/__COVERAGE_FLOOR_PCT__ display twins are filled
-- in at export time from config/settings.yml, same as __WINDOW_DAYS__.
WITH billed AS (
    SELECT monotonically_increasing_id() AS rid, u.workspace_id, u.usage_quantity, u.custom_tags
    FROM `system`.`billing`.`usage` u
    WHERE u.usage_unit = 'DBU'
      AND u.workspace_id IS NOT NULL
      AND u.usage_date < __AS_OF_DATE__
      AND u.ingestion_date < __AS_OF_DATE__
      -- every billed row without a usage policy: an Azure workspace tag reaches serverless usage as
      -- well as classic compute, and a policy's tags are the policy's, not the workspace's
      AND u.usage_metadata.budget_policy_id IS NULL AND u.usage_metadata.usage_policy_id IS NULL
),
totals AS (
    SELECT workspace_id, SUM(usage_quantity) AS dbus_total
    FROM billed
    GROUP BY workspace_id
),
pairs AS (
    -- one (row, normalised key) pair; when a map holds two spellings of one key, the
    -- alphabetically first raw key wins.
    SELECT b.workspace_id, b.usage_quantity, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key,
           trim(raw_value) AS tag_value
    FROM billed b
    LATERAL VIEW explode(b.custom_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY b.rid, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
per_value AS (
    SELECT workspace_id, tag_key, tag_value, SUM(usage_quantity) AS dbus
    FROM pairs
    GROUP BY workspace_id, tag_key, tag_value
),
per_key AS (
    SELECT workspace_id, tag_key, SUM(dbus) AS dbus_tagged
    FROM per_value
    GROUP BY workspace_id, tag_key
),
ranked AS (
    SELECT workspace_id, tag_key, tag_value, dbus,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key ORDER BY dbus DESC, tag_value) AS rn
    FROM per_value
),
-- the values carrying the most DBUs, so a mixed tag can name them
top_values AS (
    SELECT workspace_id, tag_key,
           transform(array_sort(collect_list(named_struct('rn', rn, 'v', tag_value))), x -> x.v) AS top_values
    FROM ranked
    WHERE rn <= 5
    GROUP BY workspace_id, tag_key
),
display AS (
    -- the raw spelling carrying the most DBUs account-wide names the key on screen
    SELECT tag_key, raw_key AS display_key
    FROM (
        SELECT tag_key, raw_key,
               ROW_NUMBER() OVER (PARTITION BY tag_key ORDER BY SUM(usage_quantity) DESC, raw_key) AS rn
        FROM pairs
        GROUP BY tag_key, raw_key
    ) d
    WHERE rn = 1
),
measured AS (
    SELECT r.workspace_id, r.tag_key, d.display_key, r.tag_value AS top_value,
           r.dbus / NULLIF(k.dbus_tagged, 0) AS share,
           k.dbus_tagged / NULLIF(t.dbus_total, 0) AS coverage,
           k.dbus_tagged, t.dbus_total, v.top_values
    FROM ranked r
    JOIN per_key k ON k.workspace_id = r.workspace_id AND k.tag_key = r.tag_key
    JOIN top_values v ON v.workspace_id = r.workspace_id AND v.tag_key = r.tag_key
    JOIN totals t ON t.workspace_id = r.workspace_id
    JOIN display d ON d.tag_key = r.tag_key
    WHERE r.rn = 1
)
SELECT
    workspace_id,
    tag_key,
    display_key,
    CASE
        WHEN coverage IS NULL OR coverage < __COVERAGE_FLOOR__ THEN '__untagged__'
        WHEN share >= __SHARE_FLOOR__ THEN top_value
        ELSE '__mixed__'
    END AS tag_value,
    (coverage >= __COVERAGE_FLOOR__ AND share >= __SHARE_FLOOR__) IS TRUE AS is_allocating,
    top_value,
    top_values,
    share,
    coverage,
    dbus_tagged,
    dbus_total,
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
            || '%); tagged DBUs are ' || CAST(ROUND(coverage * 100, 1) AS STRING)
            || '% of this workspace\'s billed DBUs'
    END AS reason
FROM measured
