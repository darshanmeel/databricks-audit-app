{{ config(materialized='table', tags=['dims']) }}
-- T-23 / T-63 (DEC-60). Two outputs from system.billing.usage's custom_tags map, sharing one
-- source scan, BOTH ranked by summed DBUs (usage_quantity WHERE usage_unit = 'DBU') rather than
-- COUNT(*) (DEC-60 rule 2 -- this replaces T-23's original COUNT(*) ranking; harmless for env,
-- the whole point for a cost centre -- "whose budget is this" is a dollar question, not a row
-- count):
--
--   1. tag_hint -- T-23's original contract, kept byte-identical in shape: the single dominant
--      raw env-ish tag value (custom_tags['env'/'environment'/'stage'/'tier']) per workspace,
--      consumed by dim_workspace's classify_env(tag_hint) fallback. One row per workspace,
--      canonical_key IS NULL on these rows.
--
--   2. one row per (workspace_id, canonical_key) for every canonical key declared in the
--      `tag_aliases` dbt var (fed from config/tag_aliases.yml by tools/dbt_run.py's own
--      load_tag_aliases() -- team/cost_center/business_unit/domain today, DEC-60 rule 5 "the
--      alias map is the user's"): value, share, dbus, reason. canonical_key IS NOT NULL on
--      these rows.
--        - share = this key's dominant raw tag value's DBUs / the WORKSPACE's total DBUs --
--          every billed DBU on the workspace, tagged or not, is the denominator, so share
--          answers "whose budget is this workspace" honestly rather than "of the tagged
--          fraction only".
--        - below `share_floor` (config/settings.yml `tag_share_floor`, fed as the `share_floor`
--          var -- DEC-60 rule 3, never hard-coded here) the value is 'mixed', never the
--          plurality winner.
--        - a workspace with billed DBUs but no usage row carrying that key's tag at all is
--          'not_tagged'.
--        - a workspace with NO billed DBUs at all gets NO row here for that key -- distinct
--          from 'not_tagged' (DEC-60 rule 3's third case); dim_workspace's own COALESCE turns
--          that missing row into 'no_usage'.
--
-- Matching (DEC-60 rule 4): case-insensitive, spaces/hyphens/underscores ignored. An EMPTY alias
-- list means the canonical key's OWN NAME is the only alias -- never "match nothing".
--
-- NOTE the plain (non-whitespace-trimming) set tags right below, deliberately not the dashed
-- trim-whitespace form: a dashed set tag strips the newline on both sides of itself, and two of
-- them back to back collapse this whole header comment onto the SAME line as the WITH clause
-- below -- which a line comment then swallows whole, silently deleting the WITH clause entirely
-- (verified empirically: dbt build --target test raised "Parser Error: syntax error" on the
-- first CTE's closing paren, because the swallowed WITH-clause opener left that CTE's own SELECT
-- body parsing as the outer CREATE TABLE's bare body instead of a named CTE).
{% set canonical = var('tag_aliases', {}) %}
{% set canonical = canonical if canonical is mapping else {} %}
{% set share_floor = var('share_floor', 0.6) %}
-- The SAME window (complete days only) and coverage floor tags.tag_workspace uses, so a
-- workspace gets an attribute value here only when tag_workspace would give it one too -- before
-- this fix a workspace could read a confident dominant value off a sliver of coverage (0.0002%
-- tag coverage claiming 100% of a workspace's spend) that tag_workspace would call '__untagged__'.
{% set coverage_floor = var('coverage_floor', 0.5) %}

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
attribute_candidates AS (
    {%- for key, aliases in canonical.items() %}
    {%- set alias_list = aliases if aliases else [key] %}
    {%- set norm_literals = [] %}
    {%- for a in alias_list %}
    {%- set n = a | lower | replace(' ', '') | replace('-', '') | replace('_', '') | replace("'", "''") %}
    {%- do norm_literals.append("'" ~ n ~ "'") %}
    {%- endfor %}
    SELECT workspace_id, '{{ key }}' AS canonical_key, raw_value, usage_quantity
    FROM tag_pairs
    WHERE {{ norm_tag_key('raw_key') }} IN ({{ norm_literals | join(', ') }})
    {%- if not loop.last %}
    UNION ALL
    {%- endif %}
    {%- endfor %}
),
attribute_matched AS (
    SELECT workspace_id, canonical_key, raw_value AS value, SUM(usage_quantity) AS dbus
    FROM attribute_candidates
    GROUP BY workspace_id, canonical_key, raw_value
),
attribute_ranked AS (
    SELECT workspace_id, canonical_key, value, dbus,
           ROW_NUMBER() OVER (
               PARTITION BY workspace_id, canonical_key ORDER BY dbus DESC, value ASC
           ) AS rn
    FROM attribute_matched
),
attribute_top AS (
    SELECT workspace_id, canonical_key, value AS top_value, dbus AS top_dbus
    FROM attribute_ranked
    WHERE rn = 1
),
-- DBUs that actually carry this key on this workspace. This -- not the workspace total -- is the
-- denominator for DOMINANCE, because "is one value dominant?" is a question about the tagged
-- population only. Diluting it by untagged spend made a workspace whose ONLY team value was "a"
-- read as "mixed" at 0.0%, which is the opposite of the truth. How much is tagged at all is a
-- separate and equally real fact, so it ships as its own column rather than collapsing into the
-- word "mixed" -- DEC-60 rule 3 exists to keep these states apart, not to merge a third into one.
attribute_key_totals AS (
    SELECT workspace_id, canonical_key, SUM(dbus) AS key_dbus
    FROM attribute_matched
    GROUP BY workspace_id, canonical_key
),
canonical_keys AS (
    {%- for key in canonical.keys() %}
    SELECT '{{ key }}' AS canonical_key
    {%- if not loop.last %}
    UNION ALL
    {%- endif %}
    {%- endfor %}
),
workspace_key_grid AS (
    SELECT w.workspace_id, w.total_dbus, k.canonical_key
    FROM workspace_dbu_totals w
    CROSS JOIN canonical_keys k
),
attribute_hints AS (
    SELECT
        g.workspace_id,
        g.canonical_key,
        CASE
            WHEN m.top_value IS NULL THEN 'not_tagged'
            WHEN (t.key_dbus * 1.0 / NULLIF(g.total_dbus, 0)) < {{ coverage_floor }} THEN 'not_tagged'
            WHEN (m.top_dbus * 1.0 / NULLIF(t.key_dbus, 0)) >= {{ share_floor }} THEN m.top_value
            ELSE 'mixed'
        END AS value,
        -- share = DOMINANCE among the DBUs that actually carry this key. It drives the label.
        CASE WHEN m.top_value IS NULL THEN NULL
             ELSE m.top_dbus * 1.0 / NULLIF(t.key_dbus, 0) END AS share,
        -- coverage = how much of the workspace carries this key at all. The same value at 99%
        -- dominance on 3% coverage is a different world from 99% on 99%, and a chargeback audit
        -- cares about precisely that gap -- so it is its own column, never folded into 'mixed'.
        CASE WHEN m.top_value IS NULL THEN NULL
             ELSE t.key_dbus * 1.0 / NULLIF(g.total_dbus, 0) END AS coverage,
        m.top_dbus AS dbus,
        CASE
            WHEN m.top_value IS NULL THEN
                'no usage row on this workspace carried a ' || g.canonical_key
                || ' tag matching the configured aliases'
            WHEN (t.key_dbus * 1.0 / NULLIF(g.total_dbus, 0)) < {{ coverage_floor }} THEN
                'only ' || CAST(ROUND(t.key_dbus * 100.0 / NULLIF(g.total_dbus, 0), 1) AS VARCHAR)
                || '% of this workspace''s billed DBUs carry ' || g.canonical_key
                || ' (top value "' || m.top_value || '"); at least '
                || CAST(ROUND({{ coverage_floor }} * 100, 1) AS VARCHAR)
                || '% is needed to infer a workspace tag'
            WHEN (m.top_dbus * 1.0 / NULLIF(t.key_dbus, 0)) >= {{ share_floor }} THEN
                'dominant ' || g.canonical_key || ' value "' || m.top_value || '" carries '
                || CAST(ROUND(m.top_dbus * 100.0 / NULLIF(t.key_dbus, 0), 1) AS VARCHAR)
                || '% of the ' || g.canonical_key || '-tagged DBUs, which are '
                || CAST(ROUND(t.key_dbus * 100.0 / NULLIF(g.total_dbus, 0), 1) AS VARCHAR)
                || '% of this workspace''s billed DBUs'
            ELSE
                'no single ' || g.canonical_key || ' value reaches the '
                || CAST(ROUND({{ share_floor }} * 100, 1) AS VARCHAR)
                || '% share floor (top: "' || m.top_value || '" at '
                || CAST(ROUND(m.top_dbus * 100.0 / NULLIF(t.key_dbus, 0), 1) AS VARCHAR)
                || '% of tagged DBUs); ' || g.canonical_key || ' tags cover '
                || CAST(ROUND(t.key_dbus * 100.0 / NULLIF(g.total_dbus, 0), 1) AS VARCHAR)
                || '% of this workspace''s billed DBUs'
        END AS reason
    FROM workspace_key_grid g
    LEFT JOIN attribute_top m
        ON m.workspace_id = g.workspace_id AND m.canonical_key = g.canonical_key
    LEFT JOIN attribute_key_totals t
        ON t.workspace_id = g.workspace_id AND t.canonical_key = g.canonical_key
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
