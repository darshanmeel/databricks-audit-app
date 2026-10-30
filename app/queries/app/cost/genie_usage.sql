-- query_id: genie_usage
-- title: Genie usage by user, surface and channel - DBUs and list-price dollars, this period and the one before
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA. usage_metadata.genie (surface, channel, agent_id) is a
--   newer field of system.billing.usage - on an account whose table does not have it yet this
--   query errors and reads as not run, never as zero Genie usage
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days - the current period is the most
--   recent N days, the previous period is the equal-length N days immediately before it
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. The field names come from Databricks'
--   Genie cost documentation (usage_metadata.genie.surface / channel / agent_id,
--   billing_origin_product = 'GENIE'); the price join is the effective-list-price join every priced
--   cost query in this app uses (DEC-66.1). Confirm on your account that one person's Genie Code use
--   reads surface GENIE_CODE and channel UI, and that a Genie agent called from an app or a serving
--   endpoint reads a channel other than UI or a service principal in run_as.
-- read_this: One row = one day's Genie usage for one workspace, surface, channel, Genie agent,
--   run-as identity and free/paid split. period says whether the day is in the current window or
--   the equal-length window before it. surface is GENIE_CODE (the coding assistant: it writes SQL
--   or Python, a person runs it), GENIE_AGENTS (Genie agents, formerly Genie spaces: they answer
--   questions and run SQL themselves; agent_id names the agent) or GENIE_ONE (chat). channel is how
--   Genie was reached (UI is the browser). identity_type is user, service_principal or unknown,
--   from run_as. dbus are net DBUs; usd_list prices them at the effective list rate (free-tier rows
--   are a real $0, unpriced rows add nothing and are counted in unpriced_dbus).
--   previous_period_covered is FALSE when the account's billing history does not reach back to
--   the start of the previous period, so a change against it would overstate growth.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (who uses Genie, where, and how; see the Genie tab)
-- next: query_provenance_by_source (the SQL statements Genie agents ran on warehouses,
--   source_kind = 'genie'), cost_chargeback_by_service (Genie against every other product)
-- caveats: GRAIN - usage_date, workspace_id, surface, channel, agent_id, run_as, is_free; NULL is
--   its own value in every key (an older row with no genie struct reads surface, channel and
--   agent_id NULL). DEC-66.1 - the effective list price only, never a negotiated rate. Free tier -
--   a SKU whose name contains FREE (for example GENIE_FREE_USAGE) is free-tier usage, priced at a
--   real $0. DBU usage only (usage_unit = 'DBU'). Corrections are netted (SUM across every
--   record_type). The current day is excluded from both periods, since billing.usage lands with
--   ingestion lag. "Built into apps or agents" is not a column: read it as channel other than UI,
--   or a service principal in run_as. Genie called from inside another product (for example a
--   supervisor agent) is billed to that product, not here. run_as is masked the same way as every
--   other identity column when identity masking is on.
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM system.billing.usage
),
priced AS (
  SELECT u.usage_date, u.workspace_id,
         u.usage_metadata.genie.surface  AS surface,
         u.usage_metadata.genie.channel  AS channel,
         u.usage_metadata.genie.agent_id AS agent_id,
         u.identity_metadata.run_as      AS run_as_raw,
         CASE WHEN upper(u.sku_name) LIKE '%FREE%' THEN TRUE ELSE FALSE END AS is_free,
         u.usage_quantity,
         lp.list_rate
  FROM system.billing.usage u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM system.billing.list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.billing_origin_product = 'GENIE'
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_date >= dateadd(day, -(:period_days * 2), current_date())
    AND u.usage_date <  current_date()
),
agg AS (
  SELECT usage_date, workspace_id, surface, channel, agent_id, run_as_raw, is_free,
         SUM(usage_quantity)                                                        AS dbus,
         -- A free-tier row is a real $0; an unpriced row adds nothing to the dollars.
         SUM(CASE WHEN is_free THEN 0 ELSE usage_quantity * list_rate END)          AS usd_list,
         SUM(CASE WHEN NOT is_free AND list_rate IS NULL THEN usage_quantity ELSE 0 END) AS unpriced_dbus
  FROM priced
  GROUP BY usage_date, workspace_id, surface, channel, agent_id, run_as_raw, is_free
)
SELECT a.usage_date,
       CASE WHEN a.usage_date >= dateadd(day, -:period_days, current_date()) THEN 'current' ELSE 'previous' END AS period,
       a.workspace_id,
       a.surface,
       a.channel,
       a.agent_id,
       CASE
         WHEN a.run_as_raw IS NULL OR a.run_as_raw = '__REDACTED__' THEN 'unknown'
         WHEN a.run_as_raw LIKE '%@%' THEN 'user'
         ELSE 'service_principal'
       END AS identity_type,
       CASE WHEN run_as_raw IS NULL OR run_as_raw = '__REDACTED__' THEN run_as_raw WHEN run_as_raw RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN run_as_raw ELSE concat(substr(sha2(lower(trim(run_as_raw)), 256), 1, 8), ' ', substr(run_as_raw, 1, 2), '***') END AS run_as,
       a.is_free,
       ROUND(a.dbus, 4) AS dbus,
       ROUND(a.usd_list, 2) AS usd_list,
       ROUND(a.unpriced_dbus, 4) AS unpriced_dbus,
       CASE
         WHEN a.is_free THEN 'free'
         WHEN a.unpriced_dbus = 0 THEN 'priced'
         WHEN a.unpriced_dbus >= a.dbus THEN 'unpriced'
         ELSE 'partly_priced'
       END AS price_basis,
       CASE WHEN s.snapshot_start <= dateadd(day, -(:period_days * 2), current_date()) THEN TRUE ELSE FALSE END AS previous_period_covered
FROM agg a
CROSS JOIN snapshot s
ORDER BY a.usage_date DESC, a.workspace_id, usd_list DESC
