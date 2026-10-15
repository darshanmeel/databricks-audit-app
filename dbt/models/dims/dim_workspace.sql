{{ config(materialized='table', tags=['dims']) }}
-- T-23. One row per workspace id ever seen anywhere (PLAN.md 5.5 / 7.3, tasks/T-23): a
-- billing-only or jobs-only workspace (no system.access grant, so it never shows up in
-- workspaces_latest) still gets a row here, so every downstream finding can join dim_workspace
-- on workspace_id without losing rows.
--
-- env = COALESCE(seed override, classify_env(name), classify_env(tag hint), 'unknown'), first
-- match wins, env is never guessed. classify_env() always returns a non-NULL string (including
-- the literal 'unknown'), so the cascade below treats 'unknown' as "no match" via NULLIF before
-- COALESCE-ing to the next candidate -- a plain COALESCE over three classify_env() calls would
-- never fall past the first one, since 'unknown' is not NULL.
--
-- T-63 (DEC-60): one <key>/<key>_share/<key>_coverage/<key>_reason column set per canonical key
-- in the
-- `tag_aliases` dbt var (config/tag_aliases.yml, fed by tools/dbt_run.py -- team/cost_center/
-- business_unit/domain today), the exact same shape as env/env_source/env_reason above, sourced
-- from int_workspace_tag_hints's new per-(workspace_id, canonical_key) rows. A workspace with no
-- row there for a key (zero billed DBUs at all) reads 'no_usage' -- distinct from 'not_tagged'
-- (billed DBUs, but none tagged with that key) and from 'mixed' (tagged, but no value clears the
-- share floor) -- DEC-60 rule 3's three-way distinction.
--
-- T-71: in_snapshot_region / billed_in_snapshot. tools/snapshot.py connects through ONE
-- workspace, so every regional system table holds rows only for the workspaces of that
-- metastore's region, while billing.usage and workspaces_latest are account-wide.
-- in_snapshot_region is TRUE when the workspace has a row in any regional presence table
-- (regional_ids below), FALSE when it has none (app/api/service.py then reads its regional
-- findings as NOT_ASSESSED, "outside the snapshot's region"), and NULL for every workspace
-- when the snapshot holds no regional row at all (not exported, not granted or empty), so
-- nothing can be placed inside or outside the region. billed_in_snapshot: any billing.usage
-- row at all.
--
-- Plain (non-whitespace-trimming) set tags deliberately -- see int_workspace_tag_hints.sql's own
-- note on why a dashed, whitespace-trimming set tag right after a comment block silently merges
-- onto the same line as the SQL below and gets swallowed by that line comment.
{% set canonical = var('tag_aliases', {}) %}
{% set canonical = canonical if canonical is mapping else {} %}
WITH ids_workspaces_latest AS (
    SELECT DISTINCT workspace_id
    FROM {{ source('system_access', 'workspaces_latest') }}
    WHERE workspace_id IS NOT NULL
),
ids_usage AS (
    SELECT DISTINCT workspace_id
    FROM {{ source('system_billing', 'usage') }}
    WHERE workspace_id IS NOT NULL
),
ids_jobs AS (
    SELECT DISTINCT workspace_id
    FROM {{ source('system_lakeflow', 'job_run_timeline') }}
    WHERE workspace_id IS NOT NULL
),
all_ids AS (
    SELECT workspace_id FROM ids_workspaces_latest
    UNION
    SELECT workspace_id FROM ids_usage
    UNION
    SELECT workspace_id FROM ids_jobs
),
-- T-71: the regional presence tables -- the entity tables of each regional schema (SCD2 /
-- full history, so an idle but configured workspace still appears) plus the busiest event
-- tables. access.audit rows with workspace_id '0' are account-level (global) events and do
-- not place a workspace in the region.
regional_ids AS (
    SELECT workspace_id FROM {{ source('system_compute', 'clusters') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_compute', 'warehouses') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_compute', 'instance_pools') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_lakeflow', 'jobs') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_lakeflow', 'job_run_timeline') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_lakeflow', 'pipelines') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_query', 'history') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_serving', 'served_entities') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_storage', 'predictive_optimization_operations_history') }} WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM {{ source('system_access', 'audit') }} WHERE workspace_id IS NOT NULL AND workspace_id <> '0'
),
regional_flags AS (
    SELECT
        all_ids.workspace_id,
        CASE
            WHEN (SELECT count(*) FROM regional_ids) = 0 THEN CAST(NULL AS BOOLEAN)
            WHEN r.workspace_id IS NOT NULL THEN TRUE
            ELSE FALSE
        END AS in_snapshot_region,
        (u.workspace_id IS NOT NULL) AS billed_in_snapshot
    FROM all_ids
    LEFT JOIN regional_ids r ON r.workspace_id = all_ids.workspace_id
    LEFT JOIN ids_usage u ON u.workspace_id = all_ids.workspace_id
),
-- latest row per workspace (PLAN.md 5.5): a workspace can in principle appear more than once in
-- workspaces_latest over time, so this always picks the most recently created row.
latest_ws AS (
    SELECT workspace_id, workspace_name, workspace_url
    FROM (
        SELECT
            workspace_id,
            workspace_name,
            workspace_url,
            ROW_NUMBER() OVER (PARTITION BY workspace_id ORDER BY create_time DESC) AS rn
        FROM {{ source('system_access', 'workspaces_latest') }}
    ) w
    WHERE rn = 1
),
-- dbt seed infers a CSV column's type from its own content (agate), so a seed with only
-- numeric-looking workspace_id values (e.g. "2222") loads as INTEGER while every source table's
-- workspace_id is VARCHAR (tests/fixtures/ddl.py) -- explicit CAST keeps the join/COALESCE below
-- type-safe regardless of what agate inferred for this seed.
seed AS (
    SELECT
        CAST(workspace_id AS VARCHAR) AS workspace_id,
        env AS seed_env,
        CAST(name AS VARCHAR) AS seed_name,
        CAST(url AS VARCHAR) AS seed_url,
        note AS seed_note
    FROM {{ ref('workspace_env_overrides') }}
),
tag_hints AS (
    SELECT workspace_id, tag_hint
    FROM {{ ref('int_workspace_tag_hints') }}
    WHERE canonical_key IS NULL
),
{%- if canonical %}
attribute_hints AS (
    SELECT workspace_id, canonical_key, value, share, coverage, reason
    FROM {{ ref('int_workspace_tag_hints') }}
    WHERE canonical_key IS NOT NULL
),
{%- for key in canonical.keys() %}
{{ key }}_hint AS (
    SELECT workspace_id, value, share, coverage, reason
    FROM attribute_hints
    WHERE canonical_key = '{{ key }}'
),
{%- endfor %}
{%- endif %}
joined AS (
    SELECT
        all_ids.workspace_id,
        COALESCE(latest_ws.workspace_name, seed.seed_name) AS name,
        COALESCE(latest_ws.workspace_url, seed.seed_url) AS url,
        seed.seed_env,
        seed.seed_note,
        tag_hints.tag_hint
    FROM all_ids
    LEFT JOIN latest_ws ON latest_ws.workspace_id = all_ids.workspace_id
    LEFT JOIN seed ON seed.workspace_id = all_ids.workspace_id
    LEFT JOIN tag_hints ON tag_hints.workspace_id = all_ids.workspace_id
),
classified AS (
    SELECT
        joined.*,
        NULLIF({{ classify_env('joined.name') }}, 'unknown') AS name_env,
        NULLIF({{ classify_env('joined.tag_hint') }}, 'unknown') AS tag_env
    FROM joined
)
SELECT
    classified.workspace_id,
    classified.name,
    classified.url,
    COALESCE(seed_env, name_env, tag_env, 'unknown') AS env,
    CASE
        WHEN seed_env IS NOT NULL THEN 'override'
        WHEN name_env IS NOT NULL THEN 'name'
        WHEN tag_env IS NOT NULL THEN 'tag'
        ELSE 'none'
    END AS env_source,
    CASE
        WHEN seed_env IS NOT NULL THEN
            'seed override in workspace_env_overrides.csv'
            || CASE WHEN seed_note IS NOT NULL AND seed_note <> '' THEN ': ' || seed_note ELSE '' END
        WHEN name_env IS NOT NULL THEN
            'workspace name "' || name || '" matched the ' || name_env || ' word list'
        WHEN tag_env IS NOT NULL THEN
            'custom_tags hint "' || tag_hint || '" matched the ' || tag_env || ' word list'
        ELSE
            'no seed override, workspace name, or tag hint matched a known env pattern'
    END AS env_reason,
    regional_flags.in_snapshot_region,
    regional_flags.billed_in_snapshot
    {%- for key in canonical.keys() %}
    ,
    COALESCE({{ key }}_hint.value, 'no_usage') AS {{ key }},
    {{ key }}_hint.share AS {{ key }}_share,
    {{ key }}_hint.coverage AS {{ key }}_coverage,
    COALESCE(
        {{ key }}_hint.reason,
        'this workspace has no billed DBU usage in system.billing.usage at all, so no '
        || '{{ key }}' || ' value could be attributed'
    ) AS {{ key }}_reason
    {%- endfor %}
FROM classified
LEFT JOIN regional_flags ON regional_flags.workspace_id = classified.workspace_id
{%- for key in canonical.keys() %}
LEFT JOIN {{ key }}_hint ON {{ key }}_hint.workspace_id = classified.workspace_id
{%- endfor %}
ORDER BY classified.workspace_id
