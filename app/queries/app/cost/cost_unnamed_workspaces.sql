-- query_id: cost_unnamed_workspaces
-- title: Spend on workspaces without a name
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.access.workspaces_latest
-- requires: SELECT on system.billing, system.access; GA (system.billing.usage,
--   system.billing.list_prices and system.access.workspaces_latest are generally available)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days for net_list_cost_usd, priced at the
--   effective list price the same way every other cost_* query here is (the 365-day totals and
--   the 90-day WARN check are both fixed, not driven by this parameter)
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Builds on the effective-list-price join
--   every priced cost query in this app already uses (DEC-66.1) and on cost_workspace_names's own
--   "one row per system.access.workspaces_latest workspace" read; confirm on your account that a
--   workspace_id you know is billed but absent from workspaces_latest (a deleted workspace, or one
--   outside what this account can see) shows up here, and that an ordinary, named workspace never
--   does.
-- read_this: One row = a workspace billed in the last 365 days with no name in workspaces_latest -
--   a deleted or inaccessible workspace. dbus_365d and net_list_cost_usd_365d are its 365-day
--   totals; net_list_cost_usd is the same effective-list dollar figure but only for the selected
--   window (:period_days) - the number that says whether money is still moving today.
--   lifetime_days is last_used minus first_used plus one day; short_lived flags one under 30 days
--   (spun up and torn down quickly, rather than a long-lived workspace that later dropped out of
--   the account).
-- healthy: status = OK - no billed spend in the selected window, and last_used is more than 90
--   days ago (a closed-down workspace, kept here for reference only)
-- investigate_if: status = CRITICAL - it has billed spend in the selected window (money is still
--   going to a workspace nobody can see by name); WARN - no spend in the window, but last_used is
--   within the last 90 days (recently active, worth a look even though nothing billed this window)
-- actions: 1) look the workspace_id up in the Azure/AWS/GCP portal or the Databricks account
--   console to find who owns it (free); 2) if it still exists, get it added back to this account's
--   identity so system.access.workspaces_latest picks it up, or confirm it belongs to a different
--   account or region entirely (free); 3) if it is deleted, confirm with your cloud biller that
--   nothing still charges to it, and close out any resource still attached to it (config)
-- next: cost_workspace_names (every workspace this account can see, by name), cost_monthly_actuals
--   (this workspace_id's own month-by-month total, run with a workspace filter)
-- caveats: POPULATION - system.billing.usage, last 365 days (datediff(current_date(), usage_date)
--   <= 365), workspace_id IS NOT NULL (an account-level row with no workspace_id is never a
--   "workspace" and is excluded), matched against system.access.workspaces_latest on workspace_id
--   with a LEFT JOIN ... IS NULL: a workspace with ANY row there, however old, never appears here.
--   The 365-day population window and the 90-day WARN check are both fixed constants, not
--   :period_days - only net_list_cost_usd moves with the window selector. PRICE - net_list_cost_usd
--   and net_list_cost_usd_365d are SUM(usage_quantity * list_prices.pricing.effective_list.default)
--   (DEC-66.1, the one dollar basis this app uses), never a negotiated or billed dollar; a SKU with
--   no matching price row is left out of the sum (a coverage gap, never forced to 0), so a fully
--   unpriced window can understate net_list_cost_usd - there is no price_basis column on this
--   query, since every value here is either a real dollar estimate or a genuine gap, and the point
--   of the check (is money still moving here) is unaffected either way. dbus_365d counts only
--   usage_unit = 'DBU' rows (DEC-24: a DBU and a GB-month or a token are not comparable
--   quantities), while the dollar columns sum every usage_unit together (DEC-66.1: dollars are
--   comparable across units). GRAIN - one row per workspace_id; first_used/last_used/active_days
--   come from DISTINCT usage_date, not from any create/delete timestamp (a deleted workspace
--   carries none here). SCOPE - both source tables are account-wide (GLOBAL_SOURCES), so this
--   check is unaffected by which single workspace tools/snapshot.py connected through to pull the
--   regional tables. The in-flight day (today) is excluded, the same `usage_date < current_date()`
--   cut-off every other windowed cost query in this app uses.
WITH priced AS (
  SELECT u.workspace_id, u.usage_date, u.usage_unit, u.usage_quantity,
         u.usage_quantity * p.list_rate AS list_cost
  FROM system.billing.usage u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM system.billing.list_prices
  ) p
    ON  u.sku_name   = p.sku_name
    AND u.cloud      = p.cloud
    AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.workspace_id IS NOT NULL
    AND datediff(current_date(), u.usage_date) <= 365
    AND u.usage_date < current_date()
),
by_ws AS (
  SELECT workspace_id,
         MIN(usage_date)                                                          AS first_used,
         MAX(usage_date)                                                          AS last_used,
         COUNT(DISTINCT usage_date)                                               AS active_days,
         SUM(CASE WHEN upper(usage_unit) = 'DBU' THEN usage_quantity ELSE 0 END)  AS dbus_365d,
         SUM(list_cost)                                                           AS net_list_cost_usd_365d,
         SUM(CASE WHEN usage_date >= current_date() - INTERVAL :period_days DAYS
                  THEN list_cost END)                                             AS window_cost
  FROM priced
  GROUP BY workspace_id
)
SELECT
  b.workspace_id,
  b.first_used,
  b.last_used,
  b.active_days,
  ROUND(b.dbus_365d, 2)                          AS dbus_365d,
  ROUND(b.net_list_cost_usd_365d, 2)             AS net_list_cost_usd_365d,
  ROUND(COALESCE(b.window_cost, 0), 2)           AS net_list_cost_usd,
  datediff(b.last_used, b.first_used) + 1        AS lifetime_days,
  (datediff(b.last_used, b.first_used) + 1) < 30 AS short_lived,
  CASE
    WHEN COALESCE(b.window_cost, 0) > 0              THEN 'CRITICAL'
    WHEN datediff(current_date(), b.last_used) <= 90 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM by_ws b
LEFT JOIN system.access.workspaces_latest wl ON wl.workspace_id = b.workspace_id
WHERE wl.workspace_id IS NULL
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         net_list_cost_usd DESC NULLS LAST, b.last_used DESC, b.workspace_id
