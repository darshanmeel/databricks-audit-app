-- query_id: cost_failed_statement_waste
-- title: Failed SQL statements and the compute they used (possible waste)
-- domain: cost   tier: standard
-- reads: system.query.history, system.billing.attributed_usage, system.billing.usage,
--   system.billing.list_prices
-- requires: SELECT on system.query, system.billing; GA. SCOPE: SQL warehouse statements only -
--   serverless notebook and job statements have no warehouse and are not priced here
-- empty_if: schema_not_enabled, compute_scope_gap, no_activity
-- params: :period_days (default 30) rolling window in days; :warn_waste_usd (default 5) dollars of
--   compute used by failed statements on one warehouse that flags WARN; :crit_waste_usd (default 50)
--   the same that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: active_usage_quantity and usage_metadata.dbsql_statement_id are in the
--   system-catalog schema dump (T-48), but whether attributed_usage is populated on an account, and
--   whether dbsql_statement_id equals query.history's statement_id, are not yet confirmed on a live
--   workspace (P4-04). Confirm on your account: 1) a statement you ran has an attributed_usage row
--   with the same statement id; 2) a statement that failed after running for a while has one too.
-- read_this: One row = one SQL warehouse (workspace_id, warehouse_id) with at least one FAILED
--   statement in the window. est_wasted_usd_list is the POSSIBLE waste: the compute Databricks
--   attributed to those failed statements, at the effective list price; for a warehouse with no
--   attributed_usage rows, their share of each billed hour of the warehouse, by task time (the
--   split query_top_by_cost uses; cost_basis says which). Some of this is
--   unavoidable - a statement can fail on bad input or a permission change - but a warehouse where
--   failures keep costing money points at something to fix. A failed statement with no attributed
--   row counts $0 (it failed before using compute). Cancelled statements are shown
--   (est_canceled_usd_list) and NOT counted: cancelling a runaway query is often the fix.
--   waste_reason says it in one line.
-- healthy: status = OK - failed statements on the warehouse used under :warn_waste_usd dollars of
--   compute in the window (field heuristic).
-- investigate_if: status = WARN (at/above :warn_waste_usd) or CRITICAL (at/above :crit_waste_usd)
--   dollars of compute used by failed statements (field heuristic). NOT_ASSESSED is not a pass:
--   read not_assessed_reason.
-- actions: 1) find the failing statements and their error in query_failed_queries_daily, and fix the
--   SQL, permission or schema problem they point to (free); 2) add a statement timeout or a
--   fail-fast check in the calling job or app, so a statement bound to fail stops early (config);
--   3) when the failures are out-of-memory, size the warehouse up (spend).
-- next: query_failed_queries_daily (the failed statements by day, user and error),
--   cost_dbsql_allocation_gap (how much of the warehouse's usage attributed_usage covers)
-- not_assessed_reasons: no_billing_rows: failed statements found, but the warehouse has no billed
--   usage in the window, so there is nothing to price; unpriced: the failed statements' usage has
--   no list price, so there is no dollar figure (failed_dbus is shown instead)
-- caveats: FAILED only: execution_status FAILED counts as waste; CANCELED is shown, not counted; a
--   statement's cost is the sum of its system.billing.attributed_usage active_usage_quantity rows
--   (usage_metadata.dbsql_statement_id = statement_id, same workspace), DBU only, at the effective
--   list price (DEC-66.1) - an estimate, not your invoice. attributed_usage covers SQL warehouses
--   only, so statements with no warehouse_id are left out. A warehouse with no attributed_usage row
--   at all in the window is priced by run time instead (cost_basis = run_time_share): each
--   billed hour's DBUs at list price are split across the statements running in it by
--   total_task_duration_ms (execution_duration_ms where missing), as query_top_by_cost does.
--   Hours with no statement stay unsplit, but idle minutes inside a busy hour are shared, so on
--   those warehouses it can overlap compute_warehouse_idle_minutes a little; with attributed rows
--   it never does. A warehouse with neither reads NOT_ASSESSED (no_billing_rows). Statements and
--   usage rows are both taken from the
--   window's days, so a statement that ran across midnight into today counts only its in-window
--   part. Regional (query.history). No identities are emitted.
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
stmts AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, q.statement_id, q.execution_status,
         q.start_time, COALESCE(q.end_time, q.update_time, q.start_time) AS end_time,
         COALESCE(q.total_task_duration_ms, q.execution_duration_ms, 0) AS weight_ms
  FROM system.query.history q
  WHERE q.start_time >= current_date() - INTERVAL :period_days DAYS
    AND q.start_time < current_date()
    AND q.compute.warehouse_id IS NOT NULL
),
attr AS (
  SELECT a.workspace_id,
         a.usage_metadata.warehouse_id       AS warehouse_id,
         a.usage_metadata.dbsql_statement_id AS statement_id,
         SUM(a.active_usage_quantity)                  AS dbus,
         SUM(a.active_usage_quantity * p.list_rate)    AS usd,
         SUM(CASE WHEN p.list_rate IS NULL AND upper(a.sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN a.active_usage_quantity ELSE 0 END) AS unpriced_q
  FROM system.billing.attributed_usage a
  LEFT JOIN price p
    ON  a.sku_name = p.sku_name AND a.cloud = p.cloud AND a.usage_unit = p.usage_unit
    AND a.end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR a.end_time < p.price_end_time)
  WHERE a.usage_date >= current_date() - INTERVAL :period_days DAYS
    AND a.usage_date < current_date()
    AND upper(a.usage_unit) = 'DBU'
    AND a.usage_metadata.dbsql_statement_id IS NOT NULL
  GROUP BY a.workspace_id, a.usage_metadata.warehouse_id, a.usage_metadata.dbsql_statement_id
),
wh_attr AS (
  SELECT workspace_id, warehouse_id, COUNT(*) AS attributed_statements
  FROM attr GROUP BY workspace_id, warehouse_id
),
-- Where attributed_usage has no rows: each billed hour split across the statements running in it
-- by task time, the same split query_top_by_cost uses.
bill_hour AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         date_trunc('HOUR', u.usage_start_time) AS usage_hour,
         SUM(u.usage_quantity)                              AS hour_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0))   AS hour_usd,
         SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN u.usage_quantity ELSE 0 END)         AS hour_unpriced_q
  FROM system.billing.usage u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= current_date() - INTERVAL :period_days DAYS
    AND u.usage_date < current_date()
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id, date_trunc('HOUR', u.usage_start_time)
),
wh_bill AS (
  SELECT workspace_id, warehouse_id, SUM(hour_usd) AS usd, SUM(hour_unpriced_q) AS unpriced_q
  FROM bill_hour
  GROUP BY workspace_id, warehouse_id
),
stmt_hours AS (
  -- a statement spanning hours is weighted into each by its overlap
  SELECT s.workspace_id, s.warehouse_id, s.statement_id, s.execution_status, bh.usage_hour,
         s.weight_ms *
         CASE WHEN date_trunc('HOUR', s.start_time) = date_trunc('HOUR', s.end_time) THEN 1.0
              ELSE (LEAST(unix_millis(s.end_time), unix_millis(bh.usage_hour) + 3600000)
                      - GREATEST(unix_millis(s.start_time), unix_millis(bh.usage_hour))) * 1.0
                   / GREATEST(unix_millis(s.end_time) - unix_millis(s.start_time), 1)
         END AS hour_weight_ms
  FROM stmts s
  JOIN bill_hour bh
    ON  bh.workspace_id = s.workspace_id AND bh.warehouse_id = s.warehouse_id
    AND bh.usage_hour BETWEEN date_trunc('HOUR', s.start_time) AND date_trunc('HOUR', s.end_time)
),
hour_totals AS (
  SELECT workspace_id, warehouse_id, usage_hour, SUM(hour_weight_ms) AS total_weight_ms
  FROM stmt_hours
  GROUP BY workspace_id, warehouse_id, usage_hour
),
hour_split AS (
  SELECT sh.workspace_id, sh.warehouse_id,
         SUM(CASE WHEN sh.execution_status = 'FAILED'
                  THEN bh.hour_dbus * sh.hour_weight_ms / ht.total_weight_ms END)       AS failed_dbus,
         SUM(CASE WHEN sh.execution_status = 'FAILED'
                  THEN bh.hour_usd * sh.hour_weight_ms / ht.total_weight_ms END)        AS failed_usd,
         SUM(CASE WHEN sh.execution_status = 'FAILED'
                  THEN bh.hour_unpriced_q * sh.hour_weight_ms / ht.total_weight_ms END) AS failed_unpriced_q,
         SUM(CASE WHEN sh.execution_status = 'CANCELED'
                  THEN bh.hour_usd * sh.hour_weight_ms / ht.total_weight_ms END)        AS canceled_usd,
         COUNT(DISTINCT CASE WHEN sh.execution_status = 'FAILED' AND sh.hour_weight_ms > 0
                             THEN sh.statement_id END)                                   AS failed_ran
  FROM stmt_hours sh
  JOIN hour_totals ht
    ON  ht.workspace_id = sh.workspace_id AND ht.warehouse_id = sh.warehouse_id
    AND ht.usage_hour = sh.usage_hour AND ht.total_weight_ms > 0
  JOIN bill_hour bh
    ON  bh.workspace_id = sh.workspace_id AND bh.warehouse_id = sh.warehouse_id
    AND bh.usage_hour = sh.usage_hour
  GROUP BY sh.workspace_id, sh.warehouse_id
),
per_wh AS (
  SELECT s.workspace_id, s.warehouse_id,
         COUNT(*) AS statements,
         SUM(CASE WHEN s.execution_status = 'FAILED'   THEN 1 ELSE 0 END) AS failed_statements,
         SUM(CASE WHEN s.execution_status = 'CANCELED' THEN 1 ELSE 0 END) AS canceled_statements,
         SUM(CASE WHEN s.execution_status = 'FAILED' AND a.statement_id IS NOT NULL THEN 1 ELSE 0 END) AS failed_with_cost,
         SUM(CASE WHEN s.execution_status = 'FAILED'   THEN a.dbus END)       AS failed_dbus_raw,
         SUM(CASE WHEN s.execution_status = 'FAILED'   THEN a.usd END)        AS failed_usd,
         SUM(CASE WHEN s.execution_status = 'FAILED'   THEN a.unpriced_q END) AS failed_unpriced_q,
         SUM(CASE WHEN s.execution_status = 'CANCELED' THEN a.usd END)        AS canceled_usd,
         SUM(a.usd)        AS attributed_usd,
         SUM(a.unpriced_q) AS unpriced_q
  FROM stmts s
  LEFT JOIN attr a ON a.workspace_id = s.workspace_id AND a.statement_id = s.statement_id
  GROUP BY s.workspace_id, s.warehouse_id
),
judged AS (
  SELECT p.*,
         CASE WHEN w.warehouse_id IS NOT NULL THEN 'statement'
              WHEN b.warehouse_id IS NOT NULL THEN 'run_time_share' END AS cost_basis,
         b.usd AS bill_usd, b.unpriced_q AS bill_unpriced_q,
         h.failed_dbus AS h_failed_dbus, h.failed_usd AS h_failed_usd,
         h.failed_unpriced_q AS h_failed_unpriced_q, h.canceled_usd AS h_canceled_usd, h.failed_ran
  FROM per_wh p
  LEFT JOIN wh_attr w    ON w.workspace_id = p.workspace_id AND w.warehouse_id = p.warehouse_id
  LEFT JOIN wh_bill b    ON b.workspace_id = p.workspace_id AND b.warehouse_id = p.warehouse_id
  LEFT JOIN hour_split h ON h.workspace_id = p.workspace_id AND h.warehouse_id = p.warehouse_id
  WHERE p.failed_statements > 0
),
priced AS (
  SELECT j.*,
         CASE WHEN cost_basis = 'statement' THEN failed_with_cost ELSE COALESCE(failed_ran, 0) END AS failed_used,
         CASE WHEN cost_basis = 'statement' THEN COALESCE(failed_dbus_raw, 0)
              ELSE COALESCE(h_failed_dbus, 0) END                                    AS failed_dbus_v,
         CASE WHEN cost_basis = 'statement' THEN COALESCE(failed_usd, 0)
              ELSE COALESCE(h_failed_usd, 0) END                                     AS waste_raw,
         CASE WHEN cost_basis = 'statement' THEN COALESCE(canceled_usd, 0)
              ELSE COALESCE(h_canceled_usd, 0) END                                   AS canceled_raw,
         CASE WHEN cost_basis = 'statement' THEN attributed_usd ELSE bill_usd END     AS base_usd,
         CASE WHEN cost_basis = 'statement' THEN COALESCE(unpriced_q, 0)
              ELSE COALESCE(bill_unpriced_q, 0) END                                  AS base_unpriced_q,
         CASE WHEN cost_basis IS NULL THEN 'no_billing_rows'
              WHEN cost_basis = 'statement' AND failed_unpriced_q > 0 THEN 'unpriced'
              WHEN cost_basis = 'run_time_share' AND h_failed_unpriced_q > 0 THEN 'unpriced'
         END AS na_reason
  FROM judged j
)
SELECT workspace_id, warehouse_id, statements, failed_statements, canceled_statements,
       failed_used AS failed_with_cost,
       CASE WHEN na_reason IS NULL OR na_reason = 'unpriced' THEN ROUND(failed_dbus_v, 2) END AS failed_dbus,
       CASE WHEN na_reason IS NULL THEN ROUND(waste_raw, 2) END                     AS est_wasted_usd_list,
       CASE WHEN na_reason IS NULL THEN ROUND(canceled_raw, 2) END                  AS est_canceled_usd_list,
       CASE WHEN na_reason IS NULL AND base_unpriced_q = 0
            THEN ROUND(COALESCE(base_usd, 0), 2) END                                AS est_attributed_usd_list,
       CASE WHEN na_reason IS NULL AND base_unpriced_q = 0
            THEN ROUND(waste_raw * 100.0 / NULLIF(base_usd, 0), 1) END              AS failed_cost_share_pct,
       CASE WHEN na_reason = 'no_billing_rows' THEN NULL
            WHEN base_unpriced_q > 0 THEN 'unpriced' ELSE 'priced' END              AS price_basis,
       cost_basis,
       CASE
         WHEN na_reason = 'no_billing_rows' THEN
           'failed statements found, but the warehouse has no billed usage in the window, so there is nothing to price'
         WHEN na_reason = 'unpriced' THEN
           'the failed statements'' usage has no list price, so there is no dollar figure (failed_dbus is shown)'
         WHEN cost_basis = 'run_time_share' THEN
           CONCAT_WS('; ',
             CONCAT(failed_statements, ' of ', statements, ' statements failed'),
             CONCAT(failed_used, ' of them ran in billed hours, priced at their share of the task time in those hours (no per-statement cost in billing.attributed_usage)'),
             CASE WHEN canceled_statements > 0
                  THEN CONCAT(canceled_statements, ' canceled, not counted as waste') END)
         ELSE
           CONCAT_WS('; ',
             CONCAT(failed_statements, ' of ', statements, ' statements failed'),
             CONCAT(failed_used, ' of them used billed compute'),
             CASE WHEN canceled_statements > 0
                  THEN CONCAT(canceled_statements, ' canceled, not counted as waste') END)
       END      AS waste_reason,
       na_reason      AS not_assessed_reason,
       CASE
         WHEN na_reason IS NOT NULL          THEN 'NOT_ASSESSED'
         WHEN waste_raw >= :crit_waste_usd   THEN 'CRITICAL'
         WHEN waste_raw >= :warn_waste_usd   THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM priced
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_wasted_usd_list DESC NULLS LAST, failed_statements DESC, workspace_id, warehouse_id
