-- query_id: cost_chargeback_by_allocation_tag
-- title: Chargeback in dollars by the allocation tag, with the share of spend missing it
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices
--   are generally available)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days; :warn_missing_pct (default 20)
--   percent of a workspace + usage_unit's own priced spend that carries no allocation-key tag,
--   at/above which WARN; :crit_missing_pct (default 50) percent at/above which CRITICAL;
--   :allocation_keys (default costcenter,team) the tag keys that charge spend back, first one
--   wins, written lower case without spaces, hyphens or underscores (department,project for a
--   company that tags those)
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Builds on the effective-list-price
--   join every priced cost query in this app already uses (DEC-66.1) and on the custom_tags
--   explode cost_chargeback_by_tag already proves works against a live workspace; confirm on
--   your account that a usage row you know carries a cost_center (or team) tag lands under that
--   exact value, and that a row you know carries only unrelated tags (or none at all) lands in
--   the is_missing_allocation_key = true row for its workspace + usage_unit.
-- read_this: One row = a workspace + usage_unit's spend under one allocation-tag value, at the
--   effective list price (DEC-66.1), over the window. is_missing_allocation_key = true is the
--   one row per workspace + usage_unit that could NOT be attributed to cost_center or team even
--   though the resource may carry other, unrelated tags -- read share_of_unit_pct and status on
--   that row for "how much of this spend has nowhere to be charged back". price_basis
--   (priced/free/unpriced) discloses whether net_list_cost_usd for that allocation value is a
--   real dollar estimate, a real $0 (free-usage SKUs), or understated by a pricing-coverage gap.
-- healthy: a real allocation-tag row, or the is_missing_allocation_key row when its
--   share_of_unit_pct sits below :warn_missing_pct
-- investigate_if: CRITICAL - the is_missing_allocation_key row's share_of_unit_pct is at/above
--   :crit_missing_pct; WARN - at/above :warn_missing_pct. NOT_ASSESSED - read
--   not_assessed_reason: either this workspace + usage_unit's own priced spend nets to nothing
--   measurable (every SKU in it is unpriced or free-usage), or the missing-key spend itself is
--   entirely unpriced (a real pricing-coverage gap) -- neither case has an honest percentage to
--   band, so this reads NOT_ASSESSED rather than a silent OK (DEC-57/58: NOT_ASSESSED is never
--   OK). A missing-key row whose spend is entirely free-usage is NOT one of these cases: that is
--   a real $0, so it reads status OK with share_of_unit_pct = 0, never NOT_ASSESSED.
-- actions: 1) open cost_chargeback_by_tag for this workspace to see whether the missing spend
--   carries OTHER tags (a tagging-convention gap) or none at all (a resource nobody tagged)
--   (free); 2) attach the account's allocation tag (cost_center, or team where that is the
--   convention -- see caveats) to the untagged resource's cluster/job/warehouse config, or a
--   serverless usage policy for serverless spend (config); 3) if a product line structurally
--   cannot carry the tag (shared infrastructure), accept and document the residual the same way
--   cost_dbsql_allocation_gap documents its own unattributable share (spend, in the sense of
--   accepted shared-infrastructure overhead).
-- not_assessed_reasons: missing_allocation_spend_unpriced: the untagged spend itself could not be
--   priced; no_priced_spend_in_window: this workspace and usage type had no priced spend at all in
--   the window to measure a share against
-- next: cost_chargeback_by_tag (the row-tag detail and every raw tag key/value behind this
--   query's own cost_center/team pick), cost_chargeback_by_identity (who ran the untagged
--   spend), cost_dollarized_by_sku_day (the SKU-level detail behind one allocation value's
--   dollars)
-- caveats: ALLOCATION KEY - config/tag_aliases.yml (DEC-60) canonicalises tag keys into
--   WORKSPACE ATTRIBUTES (one dominant value per workspace, read via dims.dim_workspace) -- a
--   different mechanism from this query, which reads the ROW-level custom_tags map directly, one
--   usage row at a time, so a workspace with mixed tagging still charges back correctly at the
--   row level rather than collapsing to one workspace-wide label. The keys are :allocation_keys
--   (default cost_center, then team; set your own in config/thresholds.yml under this check, e.g.
--   allocation_keys: department,project). Matching ignores case, spaces, hyphens and underscores,
--   so "cost_center", "costcenter" and "Cost-Center" are one key; the first key in the list wins
--   when one usage row carries two. A spelling that is not in the list (for example cc or
--   cost_centre) lands in the missing row. MISSING vs UNTAGGED - a row lands in
--   is_missing_allocation_key = true whenever it carries none of the keys, even when it carries
--   other tags entirely (env, owner, ...) -- this is the fix for
--   cost_chargeback_by_tag's "untagged" reading, which only ever meant "no tags at all". UNIT -
--   grouped by usage_unit and never summed across it (DEC-24): a DBU and a GB-month are not
--   comparable quantities, so net_usage_quantity is only ever summed within one usage_unit; the
--   dollar columns ARE comparable across usage_unit (DEC-66.1's one effective-list basis), but
--   share_of_unit_pct is still computed within one workspace + usage_unit at a time so a
--   heavily-tagged storage bill cannot hide a poorly-tagged compute bill on the same workspace.
--   PRICE - net_list_cost_usd is SUM(usage_quantity * list_prices.pricing.effective_list.default)
--   for that allocation value (DEC-66.1, the one dollar basis this app uses), an estimate, never
--   a negotiated or billed dollar. price_basis is 'unpriced' when any non-free-usage SKU in that
--   allocation value had no matching list_prices row (net_list_cost_usd then understates or is
--   NULL, never forced to 0), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0, also
--   read as NULL when nothing else in the group priced), 'priced' otherwise. A free-usage-only
--   row (price_basis = 'free') is a real $0, not a coverage gap: it reads status OK with
--   share_of_unit_pct = 0 -- this applies to the is_missing_allocation_key row exactly like any
--   other, so a missing-key bucket whose only spend is free-usage never falsely reads
--   NOT_ASSESSED or gets skipped from the percentage. NOT_ASSESSED on the missing-key row is
--   reserved for a genuine pricing-coverage gap (price_basis = 'unpriced') or for a workspace +
--   usage_unit whose own priced spend nets to nothing measurable. Corrections are
--   netted (SUM across all record_types -- never filter to ORIGINAL only). The current day is
--   excluded, same as every other windowed cost query, since billing.usage lands with ingestion
--   lag and the trailing day is provisional.
WITH priced_usage AS (
  -- Every usage row in the window, priced at the effective list price (DEC-66.1, the exact join
  -- T-69A settled on), carrying its own custom_tags map and sku_name for the price_basis check
  -- below. One row per system.billing.usage row (record_id is unique per row).
  SELECT u.record_id, u.workspace_id, u.usage_unit, u.usage_quantity, u.sku_name, u.custom_tags,
         lp.list_rate,
         u.usage_quantity * lp.list_rate AS list_cost
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
  WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date < current_date()
),
tag_pairs AS (
  -- one row per (usage row, custom_tags key/value pair); OUTER so a usage row with no tags at
  -- all still appears once with tag_key/tag_value NULL, same explode cost_chargeback_by_tag uses.
  SELECT p.record_id, p.workspace_id, p.usage_unit, p.usage_quantity, p.sku_name, p.list_rate,
         p.list_cost, t.tag_key, t.tag_value
  FROM priced_usage p
       LATERAL VIEW OUTER explode(p.custom_tags) t AS tag_key, tag_value
),
ranked AS (
  -- DEC-60 rule 4 normalization. A key earlier in the allocation_keys list outranks a later one when a
  -- row carries both; a row whose tag_key matches none (or has no tags at all) ranks NULL.
  SELECT record_id, workspace_id, usage_unit, usage_quantity, sku_name, list_rate, list_cost,
         tag_key, tag_value, norm_key,
         NULLIF(instr(concat(',', :allocation_keys, ','), concat(',', norm_key, ',')), 0) AS key_rank
  FROM (
    SELECT tag_pairs.*, lower(replace(replace(replace(tag_key, ' ', ''), '-', ''), '_', '')) AS norm_key
    FROM tag_pairs
  ) n
),
picked AS (
  -- ONE allocation identity per usage row: the highest-priority matching key's own value, or
  -- NULL (allocation_key NULL too) when the row matches neither key at all -- even when it
  -- carries OTHER tags (env, owner, ...), which is deliberately different from
  -- cost_chargeback_by_tag's "no tags at all" reading of untagged.
  SELECT record_id, workspace_id, usage_unit, usage_quantity, sku_name, list_rate, list_cost,
         CASE WHEN key_rank IS NULL THEN NULL WHEN norm_key = 'costcenter' THEN 'cost_center' ELSE norm_key END AS allocation_key,
         CASE WHEN key_rank IS NOT NULL THEN tag_value END              AS allocation_value
  FROM ranked
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY record_id
    ORDER BY CASE WHEN key_rank IS NULL THEN 1 ELSE 0 END, key_rank
  ) = 1
),
agg AS (
  SELECT workspace_id, usage_unit, allocation_key, allocation_value,
         SUM(usage_quantity)                                                 AS net_usage_quantity,
         SUM(list_cost)                                                      AS net_list_cost_usd,
         SUM(CASE WHEN list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS unpriced_quantity,
         SUM(CASE WHEN list_rate IS NOT NULL THEN usage_quantity ELSE 0 END) AS priced_quantity
  FROM picked
  GROUP BY workspace_id, usage_unit, allocation_key, allocation_value
),
unit_totals AS (
  -- this workspace + usage_unit's own total priced dollars across every allocation group
  -- (matched and missing) -- never summed across usage_unit (DEC-24 / this query's own contract).
  SELECT workspace_id, usage_unit, SUM(net_list_cost_usd) AS unit_total_cost_usd
  FROM agg
  GROUP BY workspace_id, usage_unit
)
SELECT a.workspace_id,
       a.usage_unit,
       a.allocation_key,
       a.allocation_value,
       (a.allocation_value IS NULL)                                             AS is_missing_allocation_key,
       ROUND(a.net_usage_quantity, 4)                                           AS net_usage_quantity,
       ROUND(a.net_list_cost_usd, 2)                                            AS net_list_cost_usd,
       ROUND(
         CASE WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0 THEN 0 ELSE a.net_list_cost_usd END
         * 100.0 / NULLIF(t.unit_total_cost_usd, 0), 1
       )                                                                         AS share_of_unit_pct,
       CASE
         WHEN a.unpriced_quantity > 0 THEN 'unpriced'
         WHEN a.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN a.allocation_value IS NOT NULL THEN 'OK'
         WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0           THEN 'OK'
         WHEN a.net_list_cost_usd IS NULL
              OR t.unit_total_cost_usd IS NULL
              OR t.unit_total_cost_usd = 0                                THEN 'NOT_ASSESSED'
         WHEN (a.net_list_cost_usd * 100.0 / t.unit_total_cost_usd) >= :crit_missing_pct
                                                                           THEN 'CRITICAL'
         WHEN (a.net_list_cost_usd * 100.0 / t.unit_total_cost_usd) >= :warn_missing_pct
                                                                           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.allocation_value IS NOT NULL THEN NULL
         WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0           THEN NULL
         WHEN a.net_list_cost_usd IS NULL THEN 'missing_allocation_spend_unpriced'
         WHEN t.unit_total_cost_usd IS NULL OR t.unit_total_cost_usd = 0
                                              THEN 'no_priced_spend_in_window'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
JOIN unit_totals t
  ON t.workspace_id IS NOT DISTINCT FROM a.workspace_id AND t.usage_unit = a.usage_unit
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         a.workspace_id, a.usage_unit, is_missing_allocation_key DESC, a.net_list_cost_usd DESC
