{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- P4-T (tasks/P4-T-SPEC.md, section 4.1 and appendix B). SHARED by the TAG-IDX and TAG-ROLL lanes:
-- written byte-identical in both from the spec; owned by TAG-IDX. Do not edit it in TAG-ROLL.
--
-- The workspace level of the tag rollup. No system table holds workspace tags (DEC-60), so a
-- workspace's value for a tag key is inferred from system.billing.usage.custom_tags, for EVERY key
-- seen there (no configuration, no alias map): one row per (workspace_id, tag_key) that at least
-- one billed DBU row of that workspace carries.
--
--   tag_key       the normalised key (norm_tag_key: lower-cased, spaces/hyphens/underscores
--                 removed), so "Cost Center", "cost-center" and "cost_center" are one key.
--   share         DOMINANCE: the top value's DBUs over the DBUs that carry this key (DEC-63).
--   coverage      the DBUs that carry this key over the workspace's billed DBUs (DEC-63).
--   tag_value     the top value when share >= share_floor AND coverage >= coverage_floor;
--                 '__mixed__' when coverage is enough but no value dominates;
--                 '__untagged__' when too little of the workspace carries the key to infer
--                 anything (DEC-63: 100% dominance on 3% coverage is not evidence).
--   top_values    up to 5 values, most DBUs first, so a '__mixed__' row can name them.
--   is_allocating TRUE only for a real inferred value. '__mixed__' and '__untagged__' never
--                 allocate spend; the rollup and the tag filter fall through to "untagged".
--
-- A workspace with no row here for a key simply does not carry it (untagged, reason given by the
-- consumer). Complete days only (usage_date < audit_today()), whole snapshot, not windowed: one
-- value per workspace and key, shared by every window and every tab (same rule as DEC-60's
-- int_workspace_tag_hints, which stays as it is for the configured canonical keys).
--
-- DuckDB only for now (P4-T todo: a Databricks dialect branch).
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.tag_workspace is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}
{% set share_floor = var('share_floor', 0.6) %}
{% set coverage_floor = var('coverage_floor', 0.5) %}

WITH billed AS (
    SELECT ROW_NUMBER() OVER () AS rid, u.workspace_id, u.usage_quantity, u.custom_tags
    FROM {{ source('system_billing', 'usage') }} u
    WHERE u.usage_unit = 'DBU'
      AND u.workspace_id IS NOT NULL
      AND u.usage_date < {{ audit_today() }}
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
    -- one (row, normalised key) pair: when one map holds two spellings of the same key, the
    -- alphabetically first raw key wins, so no row is ever counted twice for one key
    SELECT b.workspace_id, b.usage_quantity, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM billed b, unnest(map_entries(b.custom_tags)) AS x(e)
    WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY b.rid, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
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
    SELECT workspace_id, tag_key, list(tag_value ORDER BY rn) AS top_values
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
        WHEN coverage IS NULL OR coverage < {{ coverage_floor }} THEN '__untagged__'
        WHEN share >= {{ share_floor }} THEN top_value
        ELSE '__mixed__'
    END AS tag_value,
    (coverage >= {{ coverage_floor }} AND share >= {{ share_floor }}) IS TRUE AS is_allocating,
    top_value,
    top_values,
    share,
    coverage,
    dbus_tagged,
    dbus_total,
    CASE
        WHEN coverage IS NULL OR coverage < {{ coverage_floor }} THEN
            'only ' || CAST(ROUND(COALESCE(coverage, 0) * 100, 1) AS VARCHAR) || '% of this workspace''s billed DBUs carry '
            || display_key || ' (top value "' || top_value || '"); at least '
            || CAST(ROUND({{ coverage_floor }} * 100, 1) AS VARCHAR) || '% is needed to infer a workspace tag'
        WHEN share >= {{ share_floor }} THEN
            display_key || ' "' || top_value || '" carries ' || CAST(ROUND(share * 100, 1) AS VARCHAR)
            || '% of the ' || display_key || '-tagged DBUs, which are '
            || CAST(ROUND(coverage * 100, 1) AS VARCHAR) || '% of this workspace''s billed DBUs'
        ELSE
            'no single ' || display_key || ' value reaches ' || CAST(ROUND({{ share_floor }} * 100, 1) AS VARCHAR)
            || '% of the ' || display_key || '-tagged DBUs (top "' || top_value || '" at '
            || CAST(ROUND(share * 100, 1) AS VARCHAR) || '%); tagged DBUs are '
            || CAST(ROUND(coverage * 100, 1) AS VARCHAR) || '% of this workspace''s billed DBUs'
    END AS reason
FROM measured
