{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.1). One row per (window_days, unit_id): every dollar of
-- system.billing.usage, already split into the buckets the nested rollup needs (section 3.5's
-- warehouse-day split into query / idle / not-split), classified by compute_kind/compute_id and
-- work_kind/work_id (section 4.2's enumeration). tags.cost_unit_tag (its own model, same base
-- logic duplicated on purpose -- see the header note there) explodes each unit's own tags into
-- per-key/value rows; this model carries no tag columns at all, only the dollar totals every tag
-- key's rollup starts from.
--
-- unit_id is stable across windows (section 4.2): md5 of workspace_id|compute_kind|compute_id|
-- work_kind|work_id|usage_unit|<the row's own custom_tags as sorted "k=v" pairs>, '~' for NULL.
-- Two billing rows for the same resource/day with a DIFFERENT custom_tags map (a tag changed
-- mid-snapshot) land in different units on purpose, so a unit's own tags are always unambiguous.
--
-- DuckDB only for now (P4-T todo: a Databricks dialect branch), same guard as tag_workspace.sql.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.cost_unit is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}

WITH
-- ---- priced billing rows, per window (section 3.6's dollar basis: DEC-66.1's effective list
-- price, the exact join cost_chargeback_by_allocation_tag.sql already uses) ------------------
{% for w in var('windows') %}
priced_{{ w }} AS (
    SELECT {{ w }} AS window_days, u.workspace_id, u.usage_date, u.usage_unit, u.usage_quantity,
           u.custom_tags, u.usage_metadata, u.product_features, lp.list_rate
    FROM {{ source('system_billing', 'usage') }} u
    LEFT JOIN (
        SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
               CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
        FROM {{ list_prices() }} list_prices
    ) lp
      ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
     AND u.usage_end_time >= lp.price_start_time
     AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
    WHERE u.usage_date >= {{ audit_today() }} - INTERVAL {{ w }} DAY
      AND u.usage_date < {{ audit_today() }}
),
{% endfor %}
priced AS (
    {% for w in var('windows') %}
    SELECT * FROM priced_{{ w }}
    {%- if not loop.last %}
    UNION ALL
    {% endif %}
    {%- endfor %}
),
classified AS (
    -- compute_kind/compute_id, section 4.2's enumeration, evaluated in priority order.
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
-- ---- section 3.5: split a warehouse's DBU bill between its queries, per (window, workspace,
-- warehouse, usage_date) -----------------------------------------------------------------------
wh_days AS (
    SELECT window_days, workspace_id, warehouse_id, usage_date, SUM(usage_quantity) AS billed_dbus
    FROM classified
    WHERE compute_kind = 'warehouse' AND usage_unit = 'DBU'
    GROUP BY window_days, workspace_id, warehouse_id, usage_date
),
{% for w in var('windows') %}
attr_{{ w }} AS (
    SELECT {{ w }} AS window_days, a.workspace_id, a.usage_metadata.warehouse_id AS warehouse_id,
           a.usage_date, SUM(a.active_usage_quantity) AS attributed_dbus
    FROM {{ source('system_billing', 'attributed_usage') }} a
    WHERE a.usage_date >= {{ audit_today() }} - INTERVAL {{ w }} DAY
      AND a.usage_date < {{ audit_today() }}
      AND a.usage_metadata.warehouse_id IS NOT NULL
    GROUP BY a.workspace_id, a.usage_metadata.warehouse_id, a.usage_date
),
{% endfor %}
attr_days AS (
    {% for w in var('windows') %}
    SELECT * FROM attr_{{ w }}
    {%- if not loop.last %}
    UNION ALL
    {% endif %}
    {%- endfor %}
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
-- ---- explode: a warehouse/DBU row becomes up to TWO allocation rows (query part, remainder);
-- every other row is ONE allocation row with work_kind resolved directly (4.2) ------------------
alloc_rows AS (
    SELECT c.window_days, c.workspace_id, c.compute_kind, c.compute_id, c.custom_tags, c.usage_unit,
           'query' AS work_kind, CAST(NULL AS VARCHAR) AS work_id,
           CASE WHEN c.list_rate IS NOT NULL THEN c.usage_quantity * sf.split_f * c.list_rate END AS part_usd,
           CASE WHEN c.list_rate IS NULL THEN c.usage_quantity * sf.split_f ELSE 0 END AS part_unpriced_qty,
           c.usage_quantity * sf.split_f AS part_qty
    FROM classified c
    JOIN split_factor sf
      ON sf.window_days = c.window_days AND sf.workspace_id = c.workspace_id
     AND sf.warehouse_id = c.warehouse_id AND sf.usage_date = c.usage_date
    WHERE c.compute_kind = 'warehouse' AND c.usage_unit = 'DBU' AND sf.split_f > 0

    UNION ALL

    SELECT c.window_days, c.workspace_id, c.compute_kind, c.compute_id, c.custom_tags, c.usage_unit,
           CASE WHEN sf.attributed_dbus > 0 THEN 'warehouse_idle' ELSE 'warehouse_unsplit' END AS work_kind,
           CAST(NULL AS VARCHAR) AS work_id,
           CASE WHEN c.list_rate IS NOT NULL THEN c.usage_quantity * (1 - sf.split_f) * c.list_rate END AS part_usd,
           CASE WHEN c.list_rate IS NULL THEN c.usage_quantity * (1 - sf.split_f) ELSE 0 END AS part_unpriced_qty,
           c.usage_quantity * (1 - sf.split_f) AS part_qty
    FROM classified c
    JOIN split_factor sf
      ON sf.window_days = c.window_days AND sf.workspace_id = c.workspace_id
     AND sf.warehouse_id = c.warehouse_id AND sf.usage_date = c.usage_date
    WHERE c.compute_kind = 'warehouse' AND c.usage_unit = 'DBU' AND (1 - sf.split_f) > 0

    UNION ALL

    SELECT c.window_days, c.workspace_id, c.compute_kind, c.compute_id, c.custom_tags, c.usage_unit,
           CASE WHEN c.job_id IS NOT NULL THEN 'job'
                WHEN c.dlt_pipeline_id IS NOT NULL THEN 'pipeline'
                ELSE 'none' END AS work_kind,
           COALESCE(c.job_id, c.dlt_pipeline_id) AS work_id,
           CASE WHEN c.list_rate IS NOT NULL THEN c.usage_quantity * c.list_rate END AS part_usd,
           CASE WHEN c.list_rate IS NULL THEN c.usage_quantity ELSE 0 END AS part_unpriced_qty,
           c.usage_quantity AS part_qty
    FROM classified c
    WHERE NOT (c.compute_kind = 'warehouse' AND c.usage_unit = 'DBU')
),
keyed AS (
    SELECT r.*,
           (SELECT string_agg(e.key || '=' || e.value, ',' ORDER BY e.key)
            FROM unnest(map_entries(r.custom_tags)) AS t(e)) AS tags_key
    FROM alloc_rows r
),
hashed AS (
    SELECT *,
           md5(COALESCE(workspace_id, '~') || '|' || compute_kind || '|' || COALESCE(compute_id, '~')
               || '|' || work_kind || '|' || COALESCE(work_id, '~') || '|' || usage_unit || '|'
               || COALESCE(tags_key, '~')) AS unit_id
    FROM keyed
)
SELECT
    window_days,
    unit_id,
    ANY_VALUE(workspace_id) AS workspace_id,
    ANY_VALUE(compute_kind) AS compute_kind,
    ANY_VALUE(compute_id) AS compute_id,
    ANY_VALUE(work_kind) AS work_kind,
    ANY_VALUE(work_id) AS work_id,
    ANY_VALUE(usage_unit) AS usage_unit,
    COALESCE(SUM(part_usd), 0) AS usd,
    COALESCE(SUM(part_unpriced_qty), 0) AS unpriced_quantity,
    COALESCE(SUM(part_qty), 0) AS quantity,
    COUNT(*) AS record_count
FROM hashed
GROUP BY window_days, unit_id
HAVING NOT (COALESCE(SUM(part_usd), 0) = 0 AND COALESCE(SUM(part_qty), 0) = 0)
