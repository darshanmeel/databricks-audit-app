"""Tags tab (web/src/tabs/tab_tags.tsx): mandatory-tag coverage. The app's own tag-compliance
tables (GET /api/tag_compliance, /api/workspace_tags) are computed live and never snapshotted, so
this page is built from the chargeback/coverage checks that carry the same gaps in snapshot form:
cost_chargeback_by_allocation_tag (spend with no cost-center/team tag), cost_chargeback_by_tag_value
(spend by tag key and value, one untagged row per key and workspace -- the source for the per-key
KPIs and charts below), cost_unnamed_workspaces (billed workspaces missing from the account's own
workspace list -- nothing to tag them with) and cost_usage_policy_coverage (serverless usage with
no usage/budget policy attached, policy tags being the usual way DBSQL/serverless spend picks up a
cost-center tag at all).

The three "Tag N" KPIs and the two per-key charts read their key names from the snapshot's own
snapshot_info.tag_keys (settings mandatory_tag_keys, 'cost_center,domain,environment' by default)
instead of a fixed list, and match them to cost_chargeback_by_tag_value's own tag_key the way the
app does: lower(regexp_replace(key, '[ _-]', '')) on both sides."""
from tools.dashboard_kit import MONEY, NUM, bar, kpi, table

IDS = ["cost_chargeback_by_tag", "cost_chargeback_by_allocation_tag", "cost_chargeback_by_tag_value",
       "cost_usage_policy_coverage", "cost_unnamed_workspaces"]

# snapshot_info.tag_keys, split into its first 3 positions and matched to cost_chargeback_by_tag_value's
# own tag_key by the app's normalization -- never a hard-coded key name.
_KEYS_CTE = """keys AS (
  SELECT 1 AS key_num, trim(try_element_at(split(tag_keys, ','), 1)) AS raw_key FROM {S}.snapshot_info
  UNION ALL SELECT 2, trim(try_element_at(split(tag_keys, ','), 2)) FROM {S}.snapshot_info
  UNION ALL SELECT 3, trim(try_element_at(split(tag_keys, ','), 3)) FROM {S}.snapshot_info
),
nk AS (
  SELECT key_num, raw_key, lower(regexp_replace(raw_key, '[ _-]', '')) AS norm_key
  FROM keys WHERE raw_key IS NOT NULL AND raw_key <> ''
)"""


def _untagged_kpi(n: int):
    sql = ("""
WITH """ + _KEYS_CTE + """,
v AS (
  SELECT t.*, lower(regexp_replace(t.tag_key, '[ _-]', '')) AS norm_tag_key
  FROM {named:cost_chargeback_by_tag_value} t
  WHERE {ws}
),
agg AS (
  SELECT nk.raw_key,
         SUM(CASE WHEN v.is_untagged THEN v.usd_list ELSE 0 END) AS untagged_usd,
         SUM(v.usd_list) AS total_usd
  FROM nk LEFT JOIN v ON v.norm_tag_key = nk.norm_key
  WHERE nk.key_num = $N
  GROUP BY nk.raw_key
)
SELECT ROUND(COALESCE((SELECT untagged_usd FROM agg), 0) * {disc}, 0) AS value,
       COALESCE((SELECT concat(raw_key, ': ',
                 CAST(COALESCE(ROUND(100.0 * COALESCE(untagged_usd, 0) / NULLIF(total_usd, 0), 0), 0) AS STRING),
                 '% of spend untagged')
                 FROM agg),
                 'No tag key configured at position $N') AS target
""").replace("$N", str(n))
    return kpi(f"Tag {n}", sql, target="target", fmt=MONEY, tone="warn",
               how=f"Key {n} of the snapshot's tag_keys setting; $ and % are its untagged spend, from "
                   "cost_chargeback_by_tag_value.")


def build(d):
    p = d.page("mandatory")
    p.kpis(
        kpi("Spend with no cost-center tag", """
WITH a AS (SELECT * FROM {named:cost_chargeback_by_allocation_tag} WHERE {ws})
SELECT ROUND(COALESCE((SELECT SUM(net_list_cost_usd) FROM a WHERE is_missing_allocation_key), 0) * {disc}, 0) AS value,
       concat(CAST(ROUND(100.0 * COALESCE((SELECT SUM(net_list_cost_usd) FROM a WHERE is_missing_allocation_key), 0)
                    / NULLIF((SELECT SUM(net_list_cost_usd) FROM a), 0), 0) AS STRING),
              '% of priced spend; worst: ',
              COALESCE((SELECT nm_workspace FROM a WHERE is_missing_allocation_key ORDER BY net_list_cost_usd DESC LIMIT 1), 'none')) AS target
""", target="target", fmt=MONEY, tone="warn",
            how="Spend matching none of the chargeback keys (cost_center, domain by default, first match wins)."),
        kpi("Unnamed workspaces still billing", """
WITH u AS (SELECT * FROM {named:cost_unnamed_workspaces} WHERE {ws})
SELECT (SELECT count(*) FROM u WHERE status = 'CRITICAL') AS value,
       COALESCE((SELECT concat('Worst: workspace ', CAST(workspace_id AS STRING), ', $', CAST(ROUND(net_list_cost_usd * {disc}, 0) AS STRING))
                 FROM u WHERE status = 'CRITICAL' ORDER BY net_list_cost_usd DESC LIMIT 1), 'None currently billing') AS target
""", target="target", tone="crit",
            how="Billed workspace ids missing from the account's own list; Critical if still billing now, Warn if stopped within 90 days."),
        kpi("Serverless DBUs with no policy", """
WITH c AS (SELECT * FROM {named:cost_usage_policy_coverage} WHERE {ws} AND is_serverless)
SELECT ROUND(COALESCE((SELECT SUM(net_usage_quantity) FROM c WHERE status = 'WARN'), 0), 0) AS value,
       concat(CAST(ROUND(100.0 * COALESCE((SELECT SUM(net_usage_quantity) FROM c WHERE status = 'WARN'), 0)
                    / NULLIF((SELECT SUM(net_usage_quantity) FROM c), 0), 0) AS STRING),
              '% of serverless DBUs; a usage or budget policy is the usual way this spend carries a tag') AS target
""", target="target", fmt=NUM, tone="warn",
            how="Serverless DBUs with no usage or budget policy attached (policy_coverage = none)."),
    )
    p.kpis(_untagged_kpi(1), _untagged_kpi(2), _untagged_kpi(3))
    p.text(["Missing means no tag at every level checked: the object's own tag, its job or compute, then its workspace. "
            "A workspace with no name on the account's own list can carry no workspace tag either."], height=1)
    p.charts(
        bar("Spend with no cost-center tag, by workspace", """
SELECT nm_workspace AS workspace, ROUND(SUM(net_list_cost_usd) * {disc}, 0) AS usd
FROM {named:cost_chargeback_by_allocation_tag}
WHERE {ws} AND is_missing_allocation_key
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "workspace", "usd", fmt=MONEY, category_title="Workspace", value_title="Spend with no tag",
            how="Same chargeback-key gap as the KPI above, broken out by workspace."),
        bar("Serverless DBUs by usage-policy coverage", """
SELECT CASE policy_coverage WHEN 'usage_policy' THEN 'Usage policy'
            WHEN 'budget_policy_legacy' THEN 'Budget policy (legacy)' ELSE 'No policy' END AS coverage,
       SUM(net_usage_quantity) AS dbus
FROM {named:cost_usage_policy_coverage}
WHERE {ws} AND is_serverless
GROUP BY 1
""", "coverage", "dbus", fmt=NUM, category_title="Coverage", value_title="DBUs",
            how="Serverless DBUs split by whether a usage policy, a legacy budget policy, or no policy priced them."),
    )
    p.charts(
        bar("Spend by tag value, per key", """
WITH """ + _KEYS_CTE + """,
v AS (
  SELECT t.*, lower(regexp_replace(t.tag_key, '[ _-]', '')) AS norm_tag_key
  FROM {named:cost_chargeback_by_tag_value} t
  WHERE {ws} AND NOT t.is_untagged
),
matched AS (
  SELECT nk.raw_key AS tag_key, COALESCE(v.tag_value, '(other)') AS value_label, SUM(v.usd_list) AS usd
  FROM nk JOIN v ON v.norm_tag_key = nk.norm_key
  GROUP BY 1, 2
)
SELECT tag_key, value_label, ROUND(usd * {disc}, 0) AS usd
FROM matched
QUALIFY ROW_NUMBER() OVER (PARTITION BY tag_key ORDER BY usd DESC) <= 6
""", "value_label", "usd", series="tag_key", fmt=MONEY, category_title="Tag value", value_title="Spend",
            how="Top 6 values per key by $ spend; '(other)' pools the rest. Untagged rows are excluded, shown in the chart below."),
        bar("Untagged spend by workspace, per key", """
WITH """ + _KEYS_CTE + """,
v AS (
  SELECT t.*, lower(regexp_replace(t.tag_key, '[ _-]', '')) AS norm_tag_key
  FROM {named:cost_chargeback_by_tag_value} t
  WHERE {ws} AND t.is_untagged
)
SELECT nk.raw_key AS tag_key, COALESCE(v.nm_workspace, 'Unknown workspace') AS workspace,
       ROUND(SUM(v.usd_list) * {disc}, 0) AS usd
FROM nk JOIN v ON v.norm_tag_key = nk.norm_key
GROUP BY 1, 2
""", "workspace", "usd", series="tag_key", fmt=MONEY, category_title="Workspace", value_title="Untagged spend",
            how="Per workspace, $ spend with no value for that key at all, from cost_chargeback_by_tag_value's own untagged row."),
    )
    p.table(table("Workspaces missing from the account's list", """
SELECT status, CAST(workspace_id AS STRING) AS workspace, date_format(first_used, 'yyyy-MM-dd') AS first_used,
       date_format(last_used, 'yyyy-MM-dd') AS last_used, active_days, dbus_365d,
       ROUND(net_list_cost_usd * {disc}, 0) AS usd_window, ROUND(net_list_cost_usd_365d * {disc}, 0) AS usd_365d
FROM {named:cost_unnamed_workspaces}
WHERE {ws}
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, net_list_cost_usd DESC NULLS LAST
LIMIT 200
""", [("status", "Status"), ("workspace", "Workspace id"), ("first_used", "First used"), ("last_used", "Last used"),
      ("active_days", "Active days", NUM), ("dbus_365d", "DBUs, 365d", NUM),
      ("usd_window", "Spend in window", MONEY), ("usd_365d", "Spend, 365d", MONEY)],
            how="Billed workspace ids with no match in dim_workspace, worst (still billing) first."))
    p.table(table("Spend with no cost-center tag, worst rows", """
SELECT status, nm_workspace AS workspace, usage_unit, ROUND(net_usage_quantity, 2) AS net_usage_quantity,
       ROUND(net_list_cost_usd * {disc}, 0) AS usd, share_of_unit_pct
FROM {named:cost_chargeback_by_allocation_tag}
WHERE {ws} AND is_missing_allocation_key AND status IN ('CRITICAL', 'WARN')
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 ELSE 1 END, net_list_cost_usd DESC NULLS LAST
LIMIT 200
""", [("status", "Status"), ("workspace", "Workspace"), ("usage_unit", "Unit"),
      ("net_usage_quantity", "Quantity", NUM), ("usd", "Spend", MONEY), ("share_of_unit_pct", "Share of unit %", NUM)],
            how="Rows flagged Critical (50%+ of unit spend unmatched) or Warn (20%+), from cost_chargeback_by_allocation_tag."))
    p.checks(IDS, [])
