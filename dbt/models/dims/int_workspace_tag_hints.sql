{{ config(materialized='table', tags=['dims']) }}
-- Two outputs about a workspace's tags on its bill (system.billing.usage custom_tags):
--
--   1. tag_hint: the dominant env-ish tag value (custom_tags['env'/'environment'/'stage'/'tier'],
--      ranked by DBUs) per workspace, read by dim_workspace's classify_env(tag_hint) fallback.
--      canonical_key IS NULL on these rows.
--
--   2. one row per (workspace_id, canonical_key) for every top tag in config/tag_aliases.yml (the
--      `tag_aliases` var): the workspace's current tag for that key from tags.tag_workspace, so the
--      workspace list and every tag screen give one answer. 'mixed' when no value dominates,
--      'not_tagged' when too little carries the key or none does. A workspace with no billed DBUs
--      gets no row; dim_workspace turns that into 'no_usage'.
--
-- NOTE the plain (non-whitespace-trimming) set tags below: a dashed set tag strips the newline on
-- both sides and folds this header onto the WITH line, where a line comment swallows it.
{% set canonical = var('tag_aliases', {}) %}
{% set canonical = canonical if canonical is mapping else {} %}

WITH tag_pairs AS (
    -- Every (workspace_id, raw tag key, raw tag value, usage_quantity) on a DBU-billed usage row
    -- that carries at least one custom tag. DEC-60 rule 2: only DBU-billed rows are candidates at
    -- all here -- a tag on a TOKEN/BYTES/GB/HOURS-billed row contributes zero DBU spend and
    -- competes for nothing.
    {%- if target.type == 'duckdb' %}
    SELECT u.workspace_id, t.raw_key, t.raw_value, u.usage_quantity
    FROM {{ source('system_billing', 'usage') }} u
         LEFT JOIN LATERAL (
             SELECT e.key AS raw_key, e.value AS raw_value
             FROM unnest(map_entries(u.custom_tags)) AS x(e)
         ) t ON TRUE
    WHERE u.usage_unit = 'DBU' AND u.workspace_id IS NOT NULL
      AND t.raw_key IS NOT NULL AND t.raw_value IS NOT NULL
      AND u.usage_date < {{ audit_today() }} AND u.usage_metadata.budget_policy_id IS NULL AND u.usage_metadata.usage_policy_id IS NULL
    {%- else %}
    SELECT u.workspace_id, tag_key AS raw_key, tag_value AS raw_value, u.usage_quantity
    FROM {{ source('system_billing', 'usage') }} u
         LATERAL VIEW OUTER explode(u.custom_tags) t AS tag_key, tag_value
    WHERE u.usage_unit = 'DBU' AND u.workspace_id IS NOT NULL
      AND tag_key IS NOT NULL AND tag_value IS NOT NULL
      AND u.usage_date < {{ audit_today() }} AND u.usage_metadata.budget_policy_id IS NULL AND u.usage_metadata.usage_policy_id IS NULL
    {%- endif %}
),

-- ---------------------------------------------------------------------------------------------
-- 1. tag_hint -- T-23's original four env-ish keys, unchanged keys, now DBU-ranked.
-- ---------------------------------------------------------------------------------------------
env_hint_agg AS (
    SELECT workspace_id, raw_value AS tag_value, SUM(usage_quantity) AS dbus
    FROM tag_pairs
    WHERE raw_key IN ('env', 'environment', 'stage', 'tier')
    GROUP BY workspace_id, raw_value
),
env_hint_ranked AS (
    SELECT workspace_id, tag_value, dbus,
           ROW_NUMBER() OVER (PARTITION BY workspace_id ORDER BY dbus DESC, tag_value ASC) AS rn
    FROM env_hint_agg
),
env_hint AS (
    SELECT workspace_id, tag_value AS tag_hint
    FROM env_hint_ranked
    WHERE rn = 1
),

-- ---------------------------------------------------------------------------------------------
-- 2. Per-canonical-key attribute hints.
-- ---------------------------------------------------------------------------------------------
workspace_dbu_totals AS (
    -- Every workspace's total billed DBUs, tagged or not -- the share denominator, and the
    -- thing that decides whether a workspace even gets a row for a key at all (zero billed DBUs
    -- -> no row -> dim_workspace's 'no_usage', distinct from 'not_tagged', DEC-60 rule 3).
    -- workspace_id IS NOT NULL excludes account-level usage (no workspace at all to attribute a
    -- tag to, matching dim_workspace's own ids_usage CTE's identical filter) -- keeping a NULL
    -- group here would give int_workspace_tag_hints one row per canonical key with
    -- workspace_id = NULL, which dims/_dims.yml's not_null test (rightly) rejects.
    SELECT u.workspace_id, SUM(u.usage_quantity) AS total_dbus
    FROM {{ source('system_billing', 'usage') }} u
    WHERE u.usage_unit = 'DBU' AND u.workspace_id IS NOT NULL AND u.usage_date < {{ audit_today() }}
      -- a usage policy's tags are the policy's, not the workspace's (as tags.tag_workspace)
      AND u.usage_metadata.budget_policy_id IS NULL AND u.usage_metadata.usage_policy_id IS NULL
    GROUP BY u.workspace_id
),
{%- if canonical %}
canonical_keys AS (
    {%- for key in canonical.keys() %}
    SELECT '{{ key }}' AS canonical_key,
           '{{ key | lower | replace(' ', '') | replace('-', '') | replace('_', '') | replace("'", "''") }}' AS norm_key
    {%- if not loop.last %}
    UNION ALL
    {%- endif %}
    {%- endfor %}
),
attribute_hints AS (
    -- the workspace's current tag (tags.tag_workspace), so this dim and every tag screen agree;
    -- share is dominance among the DBUs carrying the key, coverage how much of the workspace does
    SELECT
        g.workspace_id,
        k.canonical_key,
        CASE WHEN tw.tag_value IS NULL OR tw.tag_value = '__untagged__' THEN 'not_tagged'
             WHEN tw.tag_value = '__mixed__' THEN 'mixed'
             ELSE tw.tag_value END AS value,
        tw.share,
        CASE WHEN tw.top_value IS NOT NULL THEN tw.coverage END AS coverage,
        tw.share * tw.dbus_tagged AS dbus,
        COALESCE(tw.reason, 'no usage row on this workspace carried a ' || k.canonical_key || ' tag') AS reason
    FROM workspace_dbu_totals g
    CROSS JOIN canonical_keys k
    LEFT JOIN {{ ref('tag_workspace') }} tw
      ON tw.workspace_id = g.workspace_id AND tw.tag_key = k.norm_key
)
{%- else %}
attribute_hints AS (
    SELECT
        CAST(NULL AS VARCHAR) AS workspace_id, CAST(NULL AS VARCHAR) AS canonical_key,
        CAST(NULL AS VARCHAR) AS value, CAST(NULL AS DOUBLE) AS share,
        CAST(NULL AS DOUBLE) AS coverage,
        CAST(NULL AS DOUBLE) AS dbus, CAST(NULL AS VARCHAR) AS reason
    WHERE FALSE
)
{%- endif %}

SELECT
    workspace_id, tag_hint,
    CAST(NULL AS VARCHAR) AS canonical_key, CAST(NULL AS VARCHAR) AS value,
    CAST(NULL AS DOUBLE) AS share, CAST(NULL AS DOUBLE) AS coverage,
    CAST(NULL AS DOUBLE) AS dbus, CAST(NULL AS VARCHAR) AS reason
FROM env_hint

UNION ALL

SELECT
    workspace_id, CAST(NULL AS VARCHAR) AS tag_hint,
    canonical_key, value, share, coverage, dbus, reason
FROM attribute_hints
