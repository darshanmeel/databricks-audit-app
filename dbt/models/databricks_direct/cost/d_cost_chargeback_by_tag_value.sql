{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/cost/cost_chargeback_by_tag_value.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM {{ source('system_billing', 'usage') }}
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM {{ list_prices() }} list_prices
),
priced AS (
  SELECT u.workspace_id, u.custom_tags, u.sku_name,
         (u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})) AS is_current,
         u.usage_quantity * p.list_rate AS usd_list,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM {{ source('system_billing', 'usage') }} u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= dateadd(day, -({{ w }} * 2), {{ audit_today() }})
    AND u.usage_date <  {{ audit_today() }}
),
totals AS (
  -- account-wide current/previous totals across every usage_unit combined, over EVERY usage row
  -- regardless of tags or workspace - the denominator for share_of_total_pct (ws_totals below is
  -- the per-workspace twin the (untagged) row itself subtracts from).
  SELECT
    SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_total_usd,
    SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_total_usd,
    SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_total_unpriced_q,
    SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_total_priced_q,
    SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_total_unpriced_q,
    SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_total_priced_q
  FROM priced
),
ws_totals AS (
  -- same shape as `totals` above, but per workspace -- the (untagged) row's own subtraction has
  -- to happen within one workspace at a time now that the grain is workspace + tag_key (see
  -- caveats); `totals` itself stays account-wide, still the ONLY source for share_of_total_pct.
  SELECT workspace_id,
    SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_total_usd,
    SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_total_usd,
    SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_total_unpriced_q,
    SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_total_priced_q,
    SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_total_unpriced_q,
    SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_total_priced_q
  FROM priced
  GROUP BY workspace_id
),
tagged AS (
  -- one row per (usage row, tag_key/tag_value pair it carries). A plain (non-OUTER) explode
  -- already drops a usage row with no tags at all - its dollars are recovered per key by the
  -- untagged_values subtraction further down instead.
  SELECT p.workspace_id, p.is_current, p.usd_list, p.unpriced_q, p.priced_q, t.tag_key, t.tag_value
  FROM priced p
  LATERAL VIEW explode(p.custom_tags) t AS tag_key, tag_value
),
key_present AS (
  -- per workspace + real key found anywhere in the window: current/previous dollars and
  -- quantities from usage rows that DO carry that key - what untagged_values below subtracts from
  -- `ws_totals` to get "this workspace's rows missing this key", without a second explode or a
  -- key x usage cross join.
  SELECT workspace_id, tag_key,
         SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_present_usd,
         SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_present_usd,
         SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_present_unpriced_q,
         SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_present_priced_q,
         SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_present_unpriced_q,
         SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_present_priced_q
  FROM tagged
  GROUP BY workspace_id, tag_key
),
value_agg AS (
  SELECT workspace_id, tag_key, tag_value,
         SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_usd,
         SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_usd,
         SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_unpriced_q,
         SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_priced_q,
         SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_unpriced_q,
         SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_priced_q
  FROM tagged
  GROUP BY workspace_id, tag_key, tag_value
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY workspace_id, tag_key ORDER BY COALESCE(cur_usd, 0) DESC, tag_value
         ) AS value_rank
  FROM value_agg
),
top_values AS (
  SELECT workspace_id, tag_key, tag_value, cur_usd, prev_usd,
         cur_unpriced_q, cur_priced_q, prev_unpriced_q, prev_priced_q,
         FALSE AS is_other, FALSE AS is_untagged, CAST(NULL AS BIGINT) AS pooled_count
  FROM ranked
  WHERE value_rank <= {{ param('cost_chargeback_by_tag_value', 'top_values_per_key', 10) }}
),
other_values AS (
  -- every value ranked beyond {{ param('cost_chargeback_by_tag_value', 'top_values_per_key', 10) }} for its workspace + key, rolled into one row so
  -- a long-tail tag key (many distinct values) never blows up the row count. pooled_count is how
  -- many distinct values that row folds in -- the app's own "Other (n)" label needs it (cbLabel,
  -- tab_cost.jsx), same as every other chargeback cut's own is_other row.
  SELECT workspace_id, tag_key, CAST(NULL AS STRING) AS tag_value,
         SUM(cur_usd) AS cur_usd, SUM(prev_usd) AS prev_usd,
         SUM(cur_unpriced_q) AS cur_unpriced_q, SUM(cur_priced_q) AS cur_priced_q,
         SUM(prev_unpriced_q) AS prev_unpriced_q, SUM(prev_priced_q) AS prev_priced_q,
         TRUE AS is_other, FALSE AS is_untagged, COUNT(*) AS pooled_count
  FROM ranked
  WHERE value_rank > {{ param('cost_chargeback_by_tag_value', 'top_values_per_key', 10) }}
  GROUP BY workspace_id, tag_key
),
untagged_values AS (
  SELECT kp.workspace_id, kp.tag_key, CAST(NULL AS STRING) AS tag_value,
         t.cur_total_usd  - COALESCE(kp.cur_present_usd, 0)  AS cur_usd,
         t.prev_total_usd - COALESCE(kp.prev_present_usd, 0) AS prev_usd,
         t.cur_total_unpriced_q  - COALESCE(kp.cur_present_unpriced_q, 0)  AS cur_unpriced_q,
         t.cur_total_priced_q   - COALESCE(kp.cur_present_priced_q, 0)    AS cur_priced_q,
         t.prev_total_unpriced_q - COALESCE(kp.prev_present_unpriced_q, 0) AS prev_unpriced_q,
         t.prev_total_priced_q  - COALESCE(kp.prev_present_priced_q, 0)   AS prev_priced_q,
         FALSE AS is_other, TRUE AS is_untagged, CAST(NULL AS BIGINT) AS pooled_count
  FROM key_present kp
  JOIN ws_totals t ON t.workspace_id IS NOT DISTINCT FROM kp.workspace_id
),
combined AS (
  SELECT * FROM top_values
  UNION ALL SELECT * FROM other_values
  UNION ALL SELECT * FROM untagged_values
),
resolved AS (
  -- same NULL-vs-real-zero resolution cost_period_over_period's own `agg` CTE uses: a side with
  -- NO matching rows at all (its own unpriced quantity is 0 too) is a genuine 0, never NOT_ASSESSED.
  SELECT *,
         CASE WHEN cur_usd  IS NULL AND cur_unpriced_q  = 0 THEN 0 ELSE cur_usd  END AS eff_cur_usd,
         CASE WHEN prev_usd IS NULL AND prev_unpriced_q = 0 THEN 0 ELSE prev_usd END AS eff_prev_usd
  FROM combined
)
SELECT
  r.workspace_id,
  r.tag_key,
  CASE WHEN r.is_other THEN '(other)' WHEN r.is_untagged THEN '(untagged)' ELSE r.tag_value END AS tag_value,
  r.is_other,
  r.is_untagged,
  r.pooled_count,
  ROUND(r.eff_cur_usd, 2)                                                    AS usd_list,
  ROUND(r.eff_cur_usd * 100.0 / NULLIF(t.cur_total_usd, 0), 1)               AS share_of_total_pct,
  ROUND(r.eff_prev_usd, 2)                                                   AS prev_usd_list,
  ROUND(r.eff_cur_usd - r.eff_prev_usd, 2)                                   AS change_usd_list,
  ROUND((r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100, 1) AS change_pct,
  CASE
    WHEN (r.cur_unpriced_q + r.prev_unpriced_q) > 0 THEN 'unpriced'
    WHEN (r.cur_priced_q + r.prev_priced_q) = 0     THEN 'free'
    ELSE 'priced'
  END                                                                         AS price_basis,
  CASE
    WHEN s.snapshot_start > dateadd(day, -({{ w }} * 2), {{ audit_today() }})
      THEN 'NOT_ASSESSED'
    WHEN r.cur_usd IS NULL AND r.cur_unpriced_q > 0 THEN 'NOT_ASSESSED'
    WHEN r.prev_usd IS NULL AND r.prev_unpriced_q > 0 THEN 'NOT_ASSESSED'
    WHEN r.is_other THEN 'OK'
    WHEN COALESCE(r.eff_cur_usd, 0) < {{ param('cost_chargeback_by_tag_value', 'min_spend_usd', 20) }} THEN 'OK'
    WHEN r.eff_prev_usd = 0 THEN 'CRITICAL'
    WHEN (r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100 >= {{ param('cost_chargeback_by_tag_value', 'crit_increase_pct', 50) }}
      THEN 'CRITICAL'
    WHEN (r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100 >= {{ param('cost_chargeback_by_tag_value', 'warn_increase_pct', 25) }}
      THEN 'WARN'
    ELSE 'OK'
  END                                                                         AS status,
  CASE
    WHEN s.snapshot_start > dateadd(day, -({{ w }} * 2), {{ audit_today() }})
      THEN 'previous_window_not_covered'
    WHEN r.cur_usd IS NULL AND r.cur_unpriced_q > 0 THEN 'current_period_unpriced'
    WHEN r.prev_usd IS NULL AND r.prev_unpriced_q > 0 THEN 'previous_period_unpriced'
    ELSE NULL
  END                                                                         AS not_assessed_reason
FROM resolved r
CROSS JOIN totals t
CROSS JOIN snapshot s
ORDER BY workspace_id, tag_key,
         is_untagged, is_other,
         CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         usd_list DESC NULLS LAST
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
