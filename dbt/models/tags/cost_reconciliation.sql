{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.1). Rule 3's proof, one row per (window_days,
-- usage_unit): the SAME priced billing total, computed here directly from system.billing.usage
-- (never read from tags.cost_unit), against tags.cost_unit's own total -- so a defect in
-- cost_unit.sql's splitting/classification cannot also hide itself from this check. The singular
-- dbt test dbt/tests/tags_cost_reconciles.sql fails the build the moment the two disagree by
-- 0.005 or more; tests/test_tag_rollup_models.py's own reconciliation test proves the same thing
-- a second, independent way (straight from the fixture parquet).
--
-- The DBU-only columns (attributed_dbus, attributed_unmatched_dbus, scaled_warehouse_days,
-- warehouse_idle_usd, warehouse_unsplit_usd) are NULL on every other usage_unit row -- they are
-- section 3.5's own warehouse-split bookkeeping, which only ever touches DBU rows.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.cost_reconciliation is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}

WITH
{% for w in var('windows') %}
priced_{{ w }} AS (
    SELECT {{ w }} AS window_days, u.workspace_id, u.usage_date, u.usage_unit, u.usage_quantity,
           u.usage_metadata, lp.list_rate
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
billing_totals AS (
    SELECT window_days, usage_unit,
           COALESCE(SUM(CASE WHEN list_rate IS NOT NULL THEN usage_quantity * list_rate END), 0) AS billing_usd,
           SUM(usage_quantity) AS billing_quantity,
           COALESCE(SUM(CASE WHEN list_rate IS NULL THEN usage_quantity END), 0) AS billing_unpriced_quantity
    FROM priced
    GROUP BY window_days, usage_unit
),
unit_totals AS (
    SELECT window_days, usage_unit,
           SUM(usd) AS unit_usd, SUM(quantity) AS unit_quantity, SUM(unpriced_quantity) AS unit_unpriced_quantity
    FROM {{ ref('cost_unit') }}
    GROUP BY window_days, usage_unit
),
-- ---- section 3.5's own DBU bookkeeping (warehouse rows only) -----------------------------------
wh_days AS (
    SELECT window_days, workspace_id, usage_metadata.warehouse_id AS warehouse_id, usage_date,
           SUM(usage_quantity) AS billed_dbus
    FROM priced
    WHERE usage_unit = 'DBU' AND usage_metadata.warehouse_id IS NOT NULL
    GROUP BY window_days, workspace_id, usage_metadata.warehouse_id, usage_date
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
attr_totals AS (
    -- section 4.5's reconciliation.attributed_dbus/attributed_unmatched_dbus: attributed_unmatched
    -- is attributed DBUs with no billed warehouse-day in the window at all (a dollar the split
    -- never touches, since there is no bill to split -- section 3.5's own "dropped" rule).
    SELECT ad.window_days,
           SUM(ad.attributed_dbus) AS attributed_dbus,
           SUM(CASE WHEN wd.billed_dbus IS NULL THEN ad.attributed_dbus ELSE 0 END) AS attributed_unmatched_dbus
    FROM attr_days ad
    LEFT JOIN wh_days wd
      ON wd.window_days = ad.window_days AND wd.workspace_id = ad.workspace_id
     AND wd.warehouse_id = ad.warehouse_id AND wd.usage_date = ad.usage_date
    GROUP BY ad.window_days
),
split_days AS (
    SELECT wd.window_days, wd.workspace_id, wd.warehouse_id, wd.usage_date, wd.billed_dbus,
           COALESCE(ad.attributed_dbus, 0) AS attributed_dbus,
           CASE WHEN wd.billed_dbus > 0 AND COALESCE(ad.attributed_dbus, 0) > 0
                THEN LEAST(ad.attributed_dbus, wd.billed_dbus) / wd.billed_dbus
                ELSE 0 END AS split_f
    FROM wh_days wd
    LEFT JOIN attr_days ad
      ON ad.window_days = wd.window_days AND ad.workspace_id = wd.workspace_id
     AND ad.warehouse_id = wd.warehouse_id AND ad.usage_date = wd.usage_date
),
dbu_bookkeeping AS (
    SELECT window_days,
           COUNT(*) FILTER (WHERE attributed_dbus > billed_dbus) AS scaled_warehouse_days
    FROM split_days
    GROUP BY window_days
),
priced_warehouse AS (
    SELECT p.window_days, p.workspace_id, p.usage_metadata.warehouse_id AS warehouse_id, p.usage_date,
           p.usage_quantity, p.list_rate
    FROM priced p
    WHERE p.usage_unit = 'DBU' AND p.usage_metadata.warehouse_id IS NOT NULL
),
warehouse_split_usd AS (
    SELECT pw.window_days,
           SUM(CASE WHEN sf.attributed_dbus > 0 AND pw.list_rate IS NOT NULL
                    THEN pw.usage_quantity * (1 - sf.split_f) * pw.list_rate ELSE 0 END) AS warehouse_idle_usd,
           SUM(CASE WHEN sf.attributed_dbus = 0 AND pw.list_rate IS NOT NULL
                    THEN pw.usage_quantity * pw.list_rate ELSE 0 END) AS warehouse_unsplit_usd
    FROM priced_warehouse pw
    JOIN split_days sf
      ON sf.window_days = pw.window_days AND sf.workspace_id = pw.workspace_id
     AND sf.warehouse_id = pw.warehouse_id AND sf.usage_date = pw.usage_date
    GROUP BY pw.window_days
)
SELECT
    b.window_days,
    b.usage_unit,
    b.billing_usd,
    COALESCE(u.unit_usd, 0) AS unit_usd,
    b.billing_quantity,
    COALESCE(u.unit_quantity, 0) AS unit_quantity,
    b.billing_unpriced_quantity,
    COALESCE(u.unit_unpriced_quantity, 0) AS unit_unpriced_quantity,
    CASE WHEN b.usage_unit = 'DBU' THEN att.attributed_dbus END AS attributed_dbus,
    CASE WHEN b.usage_unit = 'DBU' THEN att.attributed_unmatched_dbus END AS attributed_unmatched_dbus,
    CASE WHEN b.usage_unit = 'DBU' THEN COALESCE(db.scaled_warehouse_days, 0) END AS scaled_warehouse_days,
    CASE WHEN b.usage_unit = 'DBU' THEN COALESCE(ws.warehouse_idle_usd, 0) END AS warehouse_idle_usd,
    CASE WHEN b.usage_unit = 'DBU' THEN COALESCE(ws.warehouse_unsplit_usd, 0) END AS warehouse_unsplit_usd
FROM billing_totals b
LEFT JOIN unit_totals u ON u.window_days = b.window_days AND u.usage_unit = b.usage_unit
LEFT JOIN attr_totals att ON att.window_days = b.window_days
LEFT JOIN dbu_bookkeeping db ON db.window_days = b.window_days
LEFT JOIN warehouse_split_usd ws ON ws.window_days = b.window_days
