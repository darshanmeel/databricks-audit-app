-- hand-written; not generated. Databricks twin of dbt/models/tags/cost_reconciliation.sql: the
-- priced billing total per (window_days, usage_unit), computed directly from system.billing.usage,
-- plus the DBU split bookkeeping. __WINDOW_DAYS__ is filled in at export time.
--
-- This does NOT export unit_usd/unit_quantity/unit_unpriced_quantity: Databricks has no `tags`
-- schema to compare against at export time (only load_direct_results.py builds one, afterwards,
-- from this run's own parquet files, once tags/cost_unit.sql's own output is loaded too).
-- load_direct_results.py adds those three columns after loading, from the REAL loaded
-- tags.cost_unit -- an independent check on cost_unit's actual output, not a second copy of its
-- formula that would share any bug in it.
WITH priced AS (
    SELECT __WINDOW_DAYS__ AS window_days, u.workspace_id, u.usage_date, u.usage_unit, u.usage_quantity,
           u.usage_metadata, lp.list_rate
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
billing_totals AS (
    SELECT window_days, usage_unit,
           COALESCE(SUM(CASE WHEN list_rate IS NOT NULL THEN usage_quantity * list_rate END), 0) AS billing_usd,
           SUM(usage_quantity) AS billing_quantity,
           COALESCE(SUM(CASE WHEN list_rate IS NULL THEN usage_quantity END), 0) AS billing_unpriced_quantity
    FROM priced
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
attr_days AS (
    SELECT __WINDOW_DAYS__ AS window_days, a.workspace_id, a.usage_metadata.warehouse_id AS warehouse_id,
           a.usage_date, SUM(a.active_usage_quantity) AS attributed_dbus
    FROM __SRC_BILLING_ATTRIBUTED_USAGE__ a
    WHERE a.usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      AND a.usage_date < __AS_OF_DATE__
      AND a.usage_metadata.warehouse_id IS NOT NULL
    GROUP BY a.workspace_id, a.usage_metadata.warehouse_id, a.usage_date
),
attr_totals AS (
    -- attributed_unmatched_dbus: attributed DBUs with no billed warehouse-day in the window at all.
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
           SUM(CASE WHEN attributed_dbus > billed_dbus THEN 1 ELSE 0 END) AS scaled_warehouse_days
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
    b.billing_quantity,
    b.billing_unpriced_quantity,
    CASE WHEN b.usage_unit = 'DBU' THEN att.attributed_dbus END AS attributed_dbus,
    CASE WHEN b.usage_unit = 'DBU' THEN att.attributed_unmatched_dbus END AS attributed_unmatched_dbus,
    CASE WHEN b.usage_unit = 'DBU' THEN COALESCE(db.scaled_warehouse_days, 0) END AS scaled_warehouse_days,
    CASE WHEN b.usage_unit = 'DBU' THEN COALESCE(ws.warehouse_idle_usd, 0) END AS warehouse_idle_usd,
    CASE WHEN b.usage_unit = 'DBU' THEN COALESCE(ws.warehouse_unsplit_usd, 0) END AS warehouse_unsplit_usd
FROM billing_totals b
LEFT JOIN attr_totals att ON att.window_days = b.window_days
LEFT JOIN dbu_bookkeeping db ON db.window_days = b.window_days
LEFT JOIN warehouse_split_usd ws ON ws.window_days = b.window_days
