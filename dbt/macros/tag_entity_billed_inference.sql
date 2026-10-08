{#
    tag_entity_billed_inference(base_cte, entity_type) -- P4-T-IDX (tasks/P4-T-SPEC.md 5.2). Reuses
    dbt/models/tags/tag_workspace.sql's own dominant-value inference (appendix B), partitioned by a
    billing entity id instead of workspace_id alone, for tags.tag_entity's `billed_*` rows (4.2:
    "the billed_* rows reuse appendix B's inference with the partition changed from workspace_id to
    the entity id (+workspace_id)").

    `base_cte` must already be filtered to rows whose entity id is not null, and must carry columns
    rid (a globally unique per-source-row id, e.g. ROW_NUMBER() OVER () from the caller's shared
    base CTE), workspace_id, entity_id, usage_quantity, custom_tags (a MAP(VARCHAR, VARCHAR)) and
    source (this row's own billing:<kind> classification, section 3.3). `entity_type` is a plain
    string literal (e.g. 'billed_warehouse').

    Returns a full SELECT (used directly inside a UNION ALL, not a named CTE) with tag_entity's own
    columns: entity_type, workspace_id, entity_id, tag_key, raw_key, tag_value, is_allocating,
    source, share, coverage, reason. Only (workspace_id, entity_id, tag_key) combinations with at
    least one tagged DBU row are ever emitted -- an entity with zero tagged usage for a key gets no
    row at all here (same "a workspace with no row here for a key simply does not carry it" rule as
    tag_workspace.sql; untagged is inferred downstream by absence). `source` is the DBU-weighted
    dominant classification among the rows that fed this (entity_id, tag_key)'s value (arg_max),
    since the same entity id could in principle carry rows billed under more than one compute_kind.

    DuckDB only, matching tag_workspace.sql's own scope -- the caller raises the dialect guard.
#}
{% macro tag_entity_billed_inference(base_cte, entity_type) %}
{%- set share_floor = var('share_floor', 0.6) -%}
{%- set coverage_floor = var('coverage_floor', 0.5) -%}
(
    WITH pairs AS (
        SELECT b.workspace_id, b.entity_id, b.rid, e.key AS raw_key,
               {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value,
               b.usage_quantity, b.source
        FROM {{ base_cte }} b, unnest(map_entries(b.custom_tags)) AS x(e)
        WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
        QUALIFY ROW_NUMBER() OVER (PARTITION BY b.rid, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
    ),
    per_value AS (
        SELECT workspace_id, entity_id, tag_key, tag_value, SUM(usage_quantity) AS dbus,
               arg_max(source, usage_quantity) AS src
        FROM pairs
        GROUP BY workspace_id, entity_id, tag_key, tag_value
    ),
    per_key AS (
        SELECT workspace_id, entity_id, tag_key, SUM(dbus) AS dbus_tagged
        FROM per_value
        GROUP BY workspace_id, entity_id, tag_key
    ),
    totals AS (
        SELECT workspace_id, entity_id, SUM(usage_quantity) AS dbus_total
        FROM {{ base_cte }}
        GROUP BY workspace_id, entity_id
    ),
    ranked AS (
        SELECT workspace_id, entity_id, tag_key, tag_value, dbus, src,
               ROW_NUMBER() OVER (
                   PARTITION BY workspace_id, entity_id, tag_key ORDER BY dbus DESC, tag_value
               ) AS rn
        FROM per_value
    ),
    display AS (
        SELECT workspace_id, entity_id, tag_key, raw_key,
               ROW_NUMBER() OVER (
                   PARTITION BY workspace_id, entity_id, tag_key ORDER BY SUM(usage_quantity) DESC, raw_key
               ) AS rn
        FROM pairs
        GROUP BY workspace_id, entity_id, tag_key, raw_key
    ),
    measured AS (
        SELECT r.workspace_id, r.entity_id, r.tag_key, d.raw_key, r.tag_value AS top_value,
               r.src AS source,
               r.dbus / NULLIF(k.dbus_tagged, 0) AS share,
               k.dbus_tagged / NULLIF(t.dbus_total, 0) AS coverage
        FROM ranked r
        JOIN per_key k
            ON k.workspace_id = r.workspace_id AND k.entity_id = r.entity_id AND k.tag_key = r.tag_key
        JOIN totals t
            ON t.workspace_id = r.workspace_id AND t.entity_id = r.entity_id
        JOIN display d
            ON d.workspace_id = r.workspace_id AND d.entity_id = r.entity_id AND d.tag_key = r.tag_key
           AND d.rn = 1
        WHERE r.rn = 1
    )
    SELECT
        '{{ entity_type }}' AS entity_type,
        workspace_id,
        entity_id,
        tag_key,
        raw_key,
        CASE
            WHEN coverage IS NULL OR coverage < {{ coverage_floor }} THEN '__untagged__'
            WHEN share >= {{ share_floor }} THEN top_value
            ELSE '__mixed__'
        END AS tag_value,
        (coverage >= {{ coverage_floor }} AND share >= {{ share_floor }}) IS TRUE AS is_allocating,
        source,
        share,
        coverage,
        CASE
            WHEN coverage IS NULL OR coverage < {{ coverage_floor }} THEN
                'only ' || CAST(ROUND(COALESCE(coverage, 0) * 100, 1) AS VARCHAR)
                || '% of this {{ entity_type }}''s billed DBUs carry ' || raw_key || ' (top value "'
                || top_value || '"); at least ' || CAST(ROUND({{ coverage_floor }} * 100, 1) AS VARCHAR)
                || '% is needed to infer a tag'
            WHEN share >= {{ share_floor }} THEN
                raw_key || ' "' || top_value || '" carries ' || CAST(ROUND(share * 100, 1) AS VARCHAR)
                || '% of the ' || raw_key || '-tagged DBUs, which are '
                || CAST(ROUND(coverage * 100, 1) AS VARCHAR) || '% of this {{ entity_type }}''s billed DBUs'
            ELSE
                'no single ' || raw_key || ' value reaches ' || CAST(ROUND({{ share_floor }} * 100, 1) AS VARCHAR)
                || '% of the ' || raw_key || '-tagged DBUs (top "' || top_value || '" at '
                || CAST(ROUND(share * 100, 1) AS VARCHAR) || '%); tagged DBUs are '
                || CAST(ROUND(coverage * 100, 1) AS VARCHAR) || '% of this {{ entity_type }}''s billed DBUs'
        END AS reason
    FROM measured
)
{%- endmacro %}
