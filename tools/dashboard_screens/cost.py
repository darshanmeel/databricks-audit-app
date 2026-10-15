"""Cost tab (web/src/tabs/tab_cost.tsx, cost_trend_by.tsx, tab_cost_before_after.tsx):
Trend, By product & SKU, Allocation, Chargeback, By resource, Before & after, Pricing & policy."""
from tools.build_databricks_dashboards import PRODUCT_LABEL_CASE_SQL
from tools.dashboard_kit import INT, MONEY, NUM, PCT, bar, kpi, table

PRODUCT_CASE = PRODUCT_LABEL_CASE_SQL.format(col="billing_origin_product")
SERVERLESS_ONLY = "('MODEL_SERVING','VECTOR_SEARCH','GENIE','AI_FUNCTIONS','AI_GATEWAY','AGENT_BRICKS','LAKEBASE','APPS')"


def _p(sql: str) -> str:
    # __PRODUCT__ -> the app's own product-label CASE, substituted before the kit sees the SQL so
    # no marker (and no stray {col}) ever reaches the dashboard json.
    return sql.replace("__PRODUCT__", PRODUCT_CASE)


def build(d):
    trend(d)
    product(d)
    allocation(d)
    chargeback(d)
    resource(d)
    before_after(d)
    pricing(d)


def trend(d):
    p = d.page("trend")
    p.kpis(
        kpi("Spend", """
WITH r AS (SELECT * FROM {named:cost_dollarized_by_sku_day} WHERE {ws})
SELECT ROUND(COALESCE((SELECT SUM(net_list_cost) FROM r), 0) * {disc}, 0) AS value,
       COALESCE(concat('Daily avg ', CAST(ROUND(COALESCE((SELECT SUM(net_list_cost) FROM r), 0) * {disc}
              / NULLIF((SELECT count(DISTINCT usage_date) FROM r), 0), 0) AS STRING)), 'No billed usage') AS target
""", target="target", fmt=MONEY,
            how="List price across every SKU this window, with your discount; daily average is the total over days billed."),
        kpi("Vs previous period", _p("""
WITH r AS (SELECT * FROM {named:cost_period_over_period} WHERE {ws} AND status <> 'NOT_ASSESSED')
SELECT ROUND(COALESCE((SELECT SUM(est_change_usd_list) FROM r), 0) * {disc}, 0) AS value,
       COALESCE((SELECT concat('Biggest mover: ', COALESCE(nm_workspace, 'Account-level'), ' ',
                 __PRODUCT__)
                 FROM r ORDER BY abs(est_change_usd_list) DESC LIMIT 1), 'No prior period to compare') AS target
"""), target="target", fmt=MONEY,
            how="Change vs the same-length period before, summed over judged workspace x product rows only."),
        kpi("Spike days", """
WITH d AS (
  SELECT usage_date, SUM(net_list_cost) AS usd
  FROM {named:cost_dollarized_by_sku_day}
  WHERE {ws}
  GROUP BY 1
),
m AS (
  SELECT usage_date, usd,
         percentile(usd, 0.5) OVER (ORDER BY usage_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS med,
         count(*) OVER (ORDER BY usage_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS n
  FROM d
),
spikes AS (SELECT * FROM m WHERE n >= 5 AND med > 0 AND usd >= med * 1.15)
SELECT (SELECT count(*) FROM spikes) AS value,
       COALESCE((SELECT concat('Worst: ', date_format(usage_date, 'yyyy-MM-dd'), ', ',
                 CAST(ROUND(usd * {disc}, 0) AS STRING), ' vs usual ', CAST(ROUND(med * {disc}, 0) AS STRING))
                 FROM spikes ORDER BY usd / med DESC LIMIT 1), 'No spike this window') AS target
""", target="target", tone="warn",
            how="Days at least 15% over the median of the 7 days before, needing 5+ prior days; worst day named."),
        kpi("Last full month", """
WITH r AS (SELECT * FROM {named:cost_monthly_actuals} WHERE {ws} AND NOT is_partial_month)
SELECT ROUND(COALESCE((SELECT SUM(net_list_cost_usd) FROM r WHERE month_start = (SELECT max(month_start) FROM r)), 0) * {disc}, 0) AS value,
       COALESCE((SELECT date_format(max(month_start), 'MMMM yyyy') FROM r), 'No full month yet') AS target
""", target="target", fmt=MONEY,
            how="List price for the most recent calendar month that is fully billed; the current, partial month is excluded."),
    )
    p.charts(
        bar("Daily spend by product", _p("""
WITH base AS (
  SELECT usage_date, __PRODUCT__ AS product, net_list_cost
  FROM {named:cost_dollarized_by_sku_day}
  WHERE {ws}
)
SELECT usage_date AS day, product, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM base
GROUP BY usage_date, product
"""), "day", "usd", series="product", time=True, fmt=MONEY, category_title="Day", value_title="Spend",
            how="List price per day, stacked by product."),
        bar("Monthly spend by product", _p("""
WITH base AS (
  SELECT month_start, __PRODUCT__ AS product, net_list_cost_usd
  FROM {named:cost_monthly_actuals}
  WHERE {ws} AND month_start >= add_months((SELECT max(month_start) FROM {S}.cost_monthly_actuals), -17)
)
SELECT month_start AS month, product, ROUND(SUM(net_list_cost_usd) * {disc}, 0) AS usd
FROM base
GROUP BY month_start, product
"""), "month", "usd", series="product", time=True, fmt=MONEY, category_title="Month", value_title="Spend",
            how="List price per calendar month for the last 18 months, stacked by product."),
    )
    p.charts(
        bar("Biggest movers vs previous period", _p("""
WITH base AS (
  SELECT __PRODUCT__ AS product, est_change_usd_list
  FROM {named:cost_period_over_period}
  WHERE {ws} AND status <> 'NOT_ASSESSED'
)
SELECT product, ROUND(SUM(est_change_usd_list) * {disc}, 0) AS usd
FROM base
GROUP BY product
ORDER BY abs(usd) DESC
LIMIT 10
"""), "product", "usd", fmt=MONEY, category_title="Product", value_title="Change vs previous period",
            how="Change vs the prior period by product, judged rows only; largest absolute move first."),
        bar("Spend by workspace", """
SELECT COALESCE(nm_workspace, 'Account-level (no workspace)') AS workspace, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:cost_dollarized_by_sku_day}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "workspace", "usd", fmt=MONEY, category_title="Workspace", value_title="Spend",
            how="Top 10 workspaces by list price this window, from cost_dollarized_by_sku_day."),
    )
    p.table(table("Biggest changes vs previous period", """
SELECT COALESCE(nm_workspace, 'Account-level (no workspace)') AS workspace,
       __PRODUCT__ AS product,
       ROUND(est_current_usd_list * {disc}, 0) AS current_usd,
       ROUND(est_previous_usd_list * {disc}, 0) AS previous_usd,
       ROUND(est_change_usd_list * {disc}, 0) AS change_usd,
       change_pct,
       status
FROM {named:cost_period_over_period}
WHERE {ws} AND status <> 'NOT_ASSESSED'
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, abs(est_change_usd_list) DESC
LIMIT 50
""".replace("__PRODUCT__", PRODUCT_CASE), [("workspace", "Workspace"), ("product", "Product"),
      ("current_usd", "This window", MONEY), ("previous_usd", "Previous window", MONEY),
      ("change_usd", "Change", MONEY), ("change_pct", "Change %", NUM), ("status", "Status")],
        how="Judged workspace x product rows only, worst (Critical, Warn) first, then by the size of the $ change."))
    p.checks()


def product(d):
    p = d.page("product")
    p.kpis(
        kpi("Spend", """
SELECT ROUND(COALESCE(SUM(net_list_cost), 0) * {disc}, 0) AS value,
       concat(CAST(count(DISTINCT sku_name) AS STRING), ' SKUs') AS target
FROM {named:cost_dollarized_by_sku_day}
WHERE {ws}
""", target="target", fmt=MONEY,
            how="List price this window across every billed SKU, from cost_dollarized_by_sku_day."),
        kpi("Top SKU's share (%)", """
WITH r AS (SELECT sku_name, SUM(net_list_cost) AS usd FROM {named:cost_dollarized_by_sku_day} WHERE {ws} GROUP BY 1)
SELECT ROUND(100.0 * COALESCE((SELECT max(usd) FROM r), 0) / NULLIF((SELECT SUM(usd) FROM r), 0), 0) AS value,
       COALESCE((SELECT sku_name FROM r ORDER BY usd DESC LIMIT 1), 'No SKU billed') AS target
""", target="target", fmt=PCT,
            how="The single biggest SKU's list-price $ as a share of all list-price spend this window."),
        kpi("Serverless share (%)", """
SELECT ROUND(100.0 * SUM(serverless_net_dbus) / NULLIF(SUM(total_net_dbus), 0), 0) AS value,
       'Basis: DBUs' AS target
FROM {named:overview_serverless_classic_split} WHERE {ws}
""", target="target", fmt=PCT,
            how="Serverless DBUs over total DBUs this window; counts serverless-only products (serving, Genie, ...) too."),
        kpi("Photon share (%)", """
WITH r AS (SELECT * FROM {named:cost_premium_serverless_photon} WHERE {ws})
SELECT ROUND(100.0 * COALESCE((SELECT SUM(CASE WHEN is_photon THEN net_usage_quantity ELSE 0 END) FROM r), 0)
       / NULLIF((SELECT SUM(net_usage_quantity) FROM r), 0), 0) AS value, 'Basis: DBUs' AS target
""", target="target", fmt=PCT,
            how="Photon-billed DBUs over total DBUs this window, from cost_premium_serverless_photon."),
    )
    p.charts(
        bar("Spend by SKU", """
SELECT sku_name AS sku, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:cost_dollarized_by_sku_day}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "sku", "usd", fmt=MONEY, category_title="SKU", value_title="Spend",
            how="Top 10 SKUs by list-price $ this window, from cost_dollarized_by_sku_day."),
        bar("DBUs by product", _p("""
WITH base AS (
  SELECT __PRODUCT__ AS product, net_usage_quantity, usage_unit
  FROM {named:cost_by_billing_origin_product}
  WHERE {ws}
)
SELECT product, ROUND(SUM(net_usage_quantity), 0) AS dbus
FROM base
WHERE usage_unit = 'DBU'
GROUP BY product
ORDER BY dbus DESC
LIMIT 10
"""), "product", "dbus", fmt=INT, category_title="Product", value_title="DBUs",
            how="Top 10 products by DBUs this window; DBU-priced usage only, from cost_by_billing_origin_product."),
    )
    p.table(table("Fastest-growing SKUs", """
WITH per_ws AS (
  SELECT DISTINCT workspace_id, sku_name, usd_last_3m_avg, usd_prior_3m_avg, status
  FROM {named:cost_sku_trend_12m}
  WHERE {ws} AND status IN ('CRITICAL', 'WARN')
),
agg AS (
  SELECT sku_name,
         SUM(usd_last_3m_avg) AS last3m,
         SUM(usd_prior_3m_avg) AS prior3m,
         MIN(CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END) AS sev_rank
  FROM per_ws GROUP BY 1
)
SELECT sku_name AS sku,
       CASE sev_rank WHEN 0 THEN 'CRITICAL' WHEN 1 THEN 'WARN' ELSE 'OK' END AS status,
       ROUND(100.0 * (last3m - prior3m) / NULLIF(prior3m, 0), 1) AS growth_pct,
       ROUND(last3m * {disc}, 0) AS last_3m_usd,
       ROUND(prior3m * {disc}, 0) AS prior_3m_usd
FROM agg
ORDER BY sev_rank, growth_pct DESC NULLS LAST
LIMIT 30
""", [("sku", "SKU"), ("status", "Status"), ("growth_pct", "Growth %", NUM),
      ("last_3m_usd", "$/mo, last 3 months", MONEY), ("prior_3m_usd", "$/mo, prior 3 months", MONEY)],
        how="SKUs the 12-month trend check flags; growth is the last 3 months' $ vs the 3 months before, at list price."))
    p.checks()


def allocation(d):
    p = d.page("allocation")
    p.kpis(
        kpi("Charged back", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_allocation_tag} WHERE {ws})
SELECT ROUND(COALESCE((SELECT SUM(net_list_cost_usd) FROM r WHERE NOT is_missing_allocation_key), 0) * {disc}, 0) AS value,
       'Has a cost-allocation tag' AS target
""", target="target", fmt=MONEY,
            how="List price this window matching one of snapshot_info.chargeback_keys (first match wins)."),
        kpi("No tag", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_allocation_tag} WHERE {ws})
SELECT ROUND(COALESCE((SELECT SUM(net_list_cost_usd) FROM r WHERE is_missing_allocation_key), 0) * {disc}, 0) AS value,
       concat(CAST(ROUND(100.0 * COALESCE((SELECT SUM(net_list_cost_usd) FROM r WHERE is_missing_allocation_key), 0)
              / NULLIF((SELECT SUM(net_list_cost_usd) FROM r), 0), 0) AS STRING), '% of spend') AS target
""", target="target", fmt=MONEY, tone="warn",
            how="List price matching none of snapshot_info.chargeback_keys; matched ignoring case, spaces, dashes, underscores."),
        kpi("Worst workspace (%)", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_allocation_tag} WHERE {ws}),
w AS (
  SELECT COALESCE(ws_name, 'Account-level (no workspace)') AS workspace,
         SUM(net_list_cost_usd) AS total,
         SUM(CASE WHEN is_missing_allocation_key THEN net_list_cost_usd ELSE 0 END) AS missing
  FROM r GROUP BY 1
),
ranked AS (SELECT *, 100.0 * missing / NULLIF(total, 0) AS pct FROM w WHERE total > 0)
SELECT ROUND(COALESCE((SELECT pct FROM ranked ORDER BY pct DESC LIMIT 1), 0), 0) AS value,
       COALESCE((SELECT concat(workspace, ': ', CAST(ROUND(missing * {disc}, 0) AS STRING), ' of ',
                 CAST(ROUND(total * {disc}, 0) AS STRING))
                 FROM ranked ORDER BY pct DESC LIMIT 1), 'No spend') AS target
""", target="target", fmt=PCT, tone="warn",
            how="Workspace with the highest untagged share of its own spend; $ amounts at list price, your discount applied."),
        kpi("No identity recorded (%)", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_identity} WHERE {ws})
SELECT ROUND(100.0 * COALESCE((SELECT SUM(net_usage_quantity) FROM r
             WHERE identity_type IS NULL OR lower(identity_type) LIKE '%unknown%' OR lower(identity_type) LIKE '%not%recorded%'), 0)
       / NULLIF((SELECT SUM(net_usage_quantity) FROM r), 0), 0) AS value,
       'Model serving and SQL warehouse usage bills to the endpoint, not a person' AS target
""", target="target", fmt=PCT,
            how="DBUs with no run-as, owner or creator identity, as a share of all DBUs this window."),
    )
    p.charts(
        bar("Spend by chargeback tag", """
WITH ck AS (
  SELECT DISTINCT lower(regexp_replace(trim(key_raw), '[ _-]', '')) AS norm_key
  FROM (SELECT explode(split(chargeback_keys, ',')) AS key_raw FROM {S}.snapshot_info) e
),
r AS (SELECT * FROM {named:cost_chargeback_by_tag_value} WHERE {ws})
SELECT r.tag_key AS key_name, COALESCE(r.tag_value, 'Untagged') AS tag_value,
       ROUND(SUM(r.usd_list) * {disc}, 0) AS usd
FROM r JOIN ck ON lower(regexp_replace(r.tag_key, '[ _-]', '')) = ck.norm_key
WHERE NOT r.is_untagged
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 20
""", "tag_value", "usd", series="key_name", fmt=MONEY, category_title="Tag value", value_title="Spend",
            how="Tag values for each key in snapshot_info.chargeback_keys, one series per key, matched key names loosely."),
        bar("Untagged share by workspace", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_allocation_tag} WHERE {ws}),
w AS (
  SELECT COALESCE(ws_name, 'Account-level (no workspace)') AS workspace,
         SUM(net_list_cost_usd) AS total,
         SUM(CASE WHEN is_missing_allocation_key THEN net_list_cost_usd ELSE 0 END) AS missing
  FROM r GROUP BY 1
)
SELECT workspace, ROUND(100.0 * missing / NULLIF(total, 0), 0) AS pct
FROM w WHERE total > 0
ORDER BY pct DESC LIMIT 10
""", "workspace", "pct", fmt=NUM, category_title="Workspace", value_title="Untagged %",
            how="Share of each workspace's list-price spend with no chargeback-tag match; 10 worst workspaces."),
    )
    p.text(["Who ran it, by identity: see cost_chargeback_by_identity under every check on this page below."],
           height=1)
    p.table(table("Unnamed workspaces", """
SELECT n_workspace AS workspace, date_format(first_used, 'yyyy-MM-dd') AS first_used,
       date_format(last_used, 'yyyy-MM-dd') AS last_used, lifetime_days,
       ROUND(dbus_365d, 0) AS dbus_365d, ROUND(net_list_cost_usd_365d * {disc}, 0) AS usd_365d, status
FROM {named:cost_unnamed_workspaces}
WHERE {ws}
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, net_list_cost_usd_365d DESC NULLS LAST
LIMIT 50
""", [("workspace", "Workspace"), ("first_used", "First used"), ("last_used", "Last used"),
      ("lifetime_days", "Lifetime days", INT), ("dbus_365d", "DBUs, 365d", NUM),
      ("usd_365d", "$, 365d", MONEY), ("status", "Status")],
        how="Workspaces billed with no name on record, worst (Critical, Warn) first, then by 365-day list price."))
    p.checks()


def chargeback(d):
    p = d.page("chargeback")
    p.kpis(
        kpi("Charged back by workspace", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_workspace} WHERE {ws} AND status <> 'NOT_ASSESSED')
SELECT ROUND(COALESCE((SELECT SUM(usd_list) FROM r), 0) * {disc}, 0) AS value,
       concat(CAST((SELECT count(*) FROM r) AS STRING), ' workspaces') AS target
""", target="target", fmt=MONEY,
            how="List price this window summed per workspace, judged rows only, from cost_chargeback_by_workspace."),
        kpi("Biggest mover up", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_workspace} WHERE {ws} AND status <> 'NOT_ASSESSED')
SELECT ROUND(COALESCE((SELECT max(change_usd_list) FROM r WHERE change_usd_list > 0), 0) * {disc}, 0) AS value,
       COALESCE((SELECT COALESCE(nm_workspace, 'Account-level')
                 FROM r WHERE change_usd_list > 0 ORDER BY change_usd_list DESC LIMIT 1), 'Nothing grew this window') AS target
""", target="target", fmt=MONEY, tone="warn",
            how="Workspace with the largest $ increase vs the previous period, judged rows only."),
        kpi("Biggest mover down", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_workspace} WHERE {ws} AND status <> 'NOT_ASSESSED')
SELECT ROUND(COALESCE((SELECT min(change_usd_list) FROM r WHERE change_usd_list < 0), 0) * {disc}, 0) AS value,
       COALESCE((SELECT COALESCE(nm_workspace, 'Account-level')
                 FROM r WHERE change_usd_list < 0 ORDER BY change_usd_list ASC LIMIT 1), 'Nothing fell this window') AS target
""", target="target", fmt=MONEY,
            how="Workspace with the largest $ decrease vs the previous period, judged rows only."),
        kpi("Not assessed", """
WITH r AS (SELECT * FROM {named:cost_chargeback_by_workspace} WHERE {ws})
SELECT (SELECT count(*) FROM r WHERE status = 'NOT_ASSESSED') AS value, 'Not enough history to judge' AS target
""", target="target",
            how="Workspaces with too little billing history yet to compare against a previous period."),
    )
    p.charts(
        bar("Spend by workspace", """
SELECT COALESCE(nm_workspace, 'Account-level (no workspace)') AS workspace, ROUND(SUM(usd_list) * {disc}, 0) AS usd
FROM {named:cost_chargeback_by_workspace}
WHERE {ws} AND status <> 'NOT_ASSESSED'
GROUP BY 1 ORDER BY 2 DESC LIMIT 15
""", "workspace", "usd", fmt=MONEY, category_title="Workspace", value_title="Spend",
            how="Top 15 workspaces by list price this window, judged rows only."),
        bar("Biggest movers by workspace", """
SELECT COALESCE(nm_workspace, 'Account-level (no workspace)') AS workspace, ROUND(SUM(change_usd_list) * {disc}, 0) AS usd
FROM {named:cost_chargeback_by_workspace}
WHERE {ws} AND status <> 'NOT_ASSESSED'
GROUP BY 1 ORDER BY abs(usd) DESC LIMIT 15
""", "workspace", "usd", fmt=MONEY, category_title="Workspace", value_title="Change vs previous period",
            how="Change vs the previous period per workspace, judged rows only; largest absolute move first."),
    )
    p.table(table("Chargeback by workspace", """
SELECT n_workspace AS workspace, ROUND(usd_list * {disc}, 0) AS usd, share_of_total_pct,
       ROUND(change_usd_list * {disc}, 0) AS change_usd, change_pct, status
FROM {named:cost_chargeback_by_workspace}
WHERE {ws}
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END, usd_list DESC NULLS LAST
LIMIT 50
""", [("workspace", "Workspace"), ("usd", "$ this window", MONEY), ("share_of_total_pct", "Share %", NUM),
      ("change_usd", "Change", MONEY), ("change_pct", "Change %", NUM), ("status", "Status")],
        how="Every workspace's $, share and change vs the previous period; worst (Critical, Warn) first, then by $."))
    p.checks()


def resource(d):
    p = d.page("resource")
    p.kpis(
        kpi("Top job (DBU)", """
WITH j AS (
  SELECT nm_job AS job, SUM(net_usage_quantity) AS dbus
  FROM {named:cost_by_job} WHERE {ws} GROUP BY 1
)
SELECT ROUND(COALESCE((SELECT max(dbus) FROM j), 0), 0) AS value,
       COALESCE((SELECT concat(job, ', ', CAST(ROUND(dbus, 0) AS STRING), ' DBU') FROM j ORDER BY dbus DESC LIMIT 1), 'No job billed') AS target
""", target="target",
            how="Job with the most DBUs this window, from cost_by_job."),
        kpi("Top compute resource (DBU)", """
WITH res AS (
  SELECT CASE WHEN cluster_id IS NOT NULL THEN nm_cluster WHEN warehouse_id IS NOT NULL THEN nm_warehouse
              WHEN instance_pool_id IS NOT NULL THEN concat('pool ', instance_pool_id) ELSE '(serverless / account-level)' END AS resource,
         SUM(net_usage_quantity) AS dbus
  FROM {named:cost_by_compute_resource} WHERE {ws} GROUP BY 1
)
SELECT ROUND(COALESCE((SELECT max(dbus) FROM res), 0), 0) AS value,
       COALESCE((SELECT concat(resource, ', ', CAST(ROUND(dbus, 0) AS STRING), ' DBU') FROM res ORDER BY dbus DESC LIMIT 1), 'No resource billed') AS target
""", target="target",
            how="Cluster, warehouse or pool with the most DBUs this window, from cost_by_compute_resource."),
        kpi("Warehouse spend, top 10", """
WITH r AS (SELECT * FROM {named:compute_warehouse_idle_minutes} WHERE {ws} AND est_usd_list IS NOT NULL),
ranked AS (SELECT *, ROW_NUMBER() OVER (ORDER BY est_usd_list DESC) AS rn FROM r)
SELECT ROUND(COALESCE((SELECT SUM(est_usd_list) FROM ranked WHERE rn <= 10), 0) * {disc}, 0) AS value,
       concat('of ', CAST(ROUND(COALESCE((SELECT SUM(est_usd_list) FROM r), 0) * {disc}, 0) AS STRING), ' all warehouses') AS target
""", target="target", fmt=MONEY,
            how="List price of the 10 highest-spending warehouses this window, against the spend of every warehouse."),
        kpi("GenAI serving", """
SELECT ROUND(COALESCE(SUM(usd_list), 0) * {disc}, 0) AS value,
       concat(CAST(count(DISTINCT endpoint_name) AS STRING), ' endpoints') AS target
FROM {named:cost_genai_token_gpu} WHERE {ws}
""", target="target", fmt=MONEY,
            how="List price for model-serving and GenAI endpoints this window, from cost_genai_token_gpu."),
    )
    p.charts(
        bar("DBUs by job", """
SELECT nm_job AS job, ROUND(SUM(net_usage_quantity), 0) AS dbus
FROM {named:cost_by_job}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "job", "dbus", fmt=INT, category_title="Job", value_title="DBUs",
            how="Top 10 jobs by DBUs this window, from cost_by_job."),
        bar("DBUs by compute resource", """
SELECT CASE WHEN cluster_id IS NOT NULL THEN nm_cluster WHEN warehouse_id IS NOT NULL THEN nm_warehouse
            WHEN instance_pool_id IS NOT NULL THEN concat('pool ', instance_pool_id) ELSE '(serverless / account-level)' END AS resource,
       ROUND(SUM(net_usage_quantity), 0) AS dbus
FROM {named:cost_by_compute_resource}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "resource", "dbus", fmt=INT, category_title="Resource", value_title="DBUs",
            how="Top 10 clusters, warehouses or pools by DBUs this window, from cost_by_compute_resource."),
    )
    p.charts(
        bar("Top warehouses by cost", """
SELECT warehouse_id, workspace_id, lbl_warehouse AS warehouse, ROUND(SUM(est_usd_list) * {disc}, 0) AS usd
FROM {named:compute_warehouse_idle_minutes}
WHERE {ws} AND est_usd_list IS NOT NULL
GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 10
""", "warehouse", "usd", fmt=MONEY, category_title="Warehouse", value_title="Spend",
            how="Top 10 warehouses by list price this window; same-named warehouses in different workspaces stay separate bars."),
        bar("Notebooks by DBU", """
SELECT nm_notebook AS notebook, ROUND(SUM(net_usage_quantity), 0) AS dbus
FROM {named:cost_by_notebook}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "notebook", "dbus", fmt=INT, category_title="Notebook", value_title="DBUs",
            how="Top 10 notebooks by DBUs this window, from cost_by_notebook."),
    )
    p.text(["Default storage and GenAI serving: see their own rows under every check on this page below. "
            "Serving endpoint cost has its own home: ML & AI > Spend (not reproduced here)."], height=1)
    p.checks()


# Warehouses only (the app's default level): the last 7 days against every day before them, $/day
# on each side, over the resource_type='warehouse' rows of cost_daily_by_resource. per_wh groups by
# the warehouse id, not its name, so same-named warehouses in different workspaces stay separate;
# pooled "is_other" rows (no warehouse_id) are kept here for the totals, labeled "Other warehouses".
_BA_CTE = """
WITH r AS (SELECT * FROM {named:cost_daily_by_resource} WHERE {ws} AND resource_type = 'warehouse'),
bound AS (SELECT date_sub(max(usage_date), 6) AS fix_day FROM r),
win AS (
  SELECT b.fix_day,
         count(DISTINCT CASE WHEN r.usage_date < b.fix_day THEN r.usage_date END) AS n_before,
         count(DISTINCT CASE WHEN r.usage_date >= b.fix_day THEN r.usage_date END) AS n_after
  FROM r, bound b
  GROUP BY b.fix_day
),
per_wh AS (
  SELECT r.warehouse_id, r.workspace_id, MAX(COALESCE(r.lbl_warehouse, 'Other warehouses')) AS warehouse,
         SUM(CASE WHEN r.usage_date < w.fix_day THEN r.usd_list ELSE 0 END) AS before_raw,
         SUM(CASE WHEN r.usage_date >= w.fix_day THEN r.usd_list ELSE 0 END) AS after_raw,
         MAX(w.n_before) AS n_before,
         MAX(w.n_after) AS n_after
  FROM r, win w
  GROUP BY r.warehouse_id, r.workspace_id
)
"""


def before_after(d):
    p = d.page("before_after")
    p.kpis(
        kpi("Before, $ a day", _BA_CTE + """
SELECT ROUND(COALESCE((SELECT SUM(before_raw) FROM per_wh), 0) * {disc}
       / NULLIF((SELECT max(n_before) FROM per_wh), 0), 2) AS value, 'Warehouses, before the split' AS target
""", target="target", fmt=MONEY,
            how="Average $/day across all warehouses, every day before the last 7 (list price, your discount applied)."),
        kpi("After, $ a day", _BA_CTE + """
SELECT ROUND(COALESCE((SELECT SUM(after_raw) FROM per_wh), 0) * {disc}
       / NULLIF((SELECT max(n_after) FROM per_wh), 0), 2) AS value, 'Warehouses, the last 7 days' AS target
""", target="target", fmt=MONEY,
            how="Average $/day across all warehouses, the last 7 days (list price, your discount applied)."),
        kpi("Change", _BA_CTE + """
SELECT ROUND((COALESCE((SELECT SUM(after_raw) FROM per_wh), 0) / NULLIF((SELECT max(n_after) FROM per_wh), 0)
       - COALESCE((SELECT SUM(before_raw) FROM per_wh), 0) / NULLIF((SELECT max(n_before) FROM per_wh), 0)) * {disc}, 2) AS value,
       'Last 7 days vs before, $ a day' AS target
""", target="target", fmt=MONEY,
            how="After minus before, $ a day; positive means warehouses now cost more per day."),
        kpi("Warehouses with activity", _BA_CTE + """
SELECT (SELECT count(*) FROM per_wh WHERE before_raw > 0 OR after_raw > 0) AS value,
       concat(CAST((SELECT count_if(after_raw > before_raw) FROM per_wh WHERE before_raw > 0 OR after_raw > 0) AS STRING), ' up, ',
              CAST((SELECT count_if(after_raw < before_raw) FROM per_wh WHERE before_raw > 0 OR after_raw > 0) AS STRING), ' down') AS target
""", target="target",
            how="Warehouses billed before or after the split; how many went up vs down, $/day."),
    )
    p.chart(bar("Warehouses by change, $ a day", _BA_CTE + """
SELECT warehouse, ROUND((after_raw / NULLIF(n_after, 0) - before_raw / NULLIF(n_before, 0)) * {disc}, 2) AS change_usd
FROM per_wh
WHERE warehouse_id IS NOT NULL AND (before_raw > 0 OR after_raw > 0)
ORDER BY abs(change_usd) DESC LIMIT 10
""", "warehouse", "change_usd", fmt=MONEY, category_title="Warehouse", value_title="Change, $ a day",
        how="Per-warehouse $/day change, last 7 days vs before; pooled 'Other warehouses' row kept out of this chart only."))
    p.table(table("Warehouses, before vs after", _BA_CTE + """
SELECT warehouse,
       ROUND(before_raw * {disc} / NULLIF(n_before, 0), 2) AS before_usd,
       ROUND(after_raw * {disc} / NULLIF(n_after, 0), 2) AS after_usd,
       ROUND((after_raw / NULLIF(n_after, 0) - before_raw / NULLIF(n_before, 0)) * {disc}, 2) AS change_usd,
       ROUND(100.0 * (after_raw / NULLIF(n_after, 0) - before_raw / NULLIF(n_before, 0))
             / NULLIF(before_raw / NULLIF(n_before, 0), 0), 1) AS change_pct
FROM per_wh
WHERE warehouse_id IS NOT NULL AND (before_raw > 0 OR after_raw > 0)
ORDER BY abs(after_raw / NULLIF(n_after, 0) - before_raw / NULLIF(n_before, 0)) DESC
LIMIT 30
""", [("warehouse", "Warehouse"), ("before_usd", "Before, $/day", MONEY), ("after_usd", "After, $/day", MONEY),
      ("change_usd", "Change, $/day", MONEY), ("change_pct", "Change %", NUM)],
        how="Same split as the chart above it, $/day and % change per warehouse; largest $/day change first."))
    p.text(["Warehouses only, last 7 days vs every day before them -- the app also covers jobs, "
            "clusters and pipelines with a date you pick; not reproduced here."], height=1)
    p.checks()


def pricing(d):
    p = d.page("pricing")
    p.kpis(
        kpi("Serverless with no policy", """
WITH r AS (
  SELECT * FROM {named:cost_usage_policy_coverage}
  WHERE {ws} AND (is_serverless OR billing_origin_product IN """ + SERVERLESS_ONLY + """)
)
SELECT ROUND(COALESCE((SELECT SUM(net_usage_quantity) FROM r WHERE policy_coverage = 'none'), 0), 0) AS value,
       'DBUs with no usage/budget policy' AS target
""", target="target", tone="warn",
            how="Serverless (or serverless-only product) DBUs this window with no usage or budget policy attached."),
        kpi("No tag on serverless", """
WITH r AS (
  SELECT * FROM {named:cost_usage_policy_coverage}
  WHERE {ws} AND (is_serverless OR billing_origin_product IN """ + SERVERLESS_ONLY + """)
)
SELECT ROUND(COALESCE((SELECT SUM(net_usage_quantity) FROM r WHERE tag_coverage = 'untagged'), 0), 0) AS value,
       'DBUs with no tag' AS target
""", target="target", tone="warn",
            how="Serverless (or serverless-only product) DBUs this window with no tag at all."),
        kpi("Restatement (%)", """
WITH r AS (SELECT * FROM {named:cost_restatement_trust_metric} WHERE {ws})
SELECT ROUND(100.0 * COALESCE((SELECT SUM(retracted_abs_quantity) FROM r), 0)
       / NULLIF((SELECT SUM(original_usage_quantity) FROM r), 0), 1) AS value,
       'Prior-month usage since retracted' AS target
""", target="target", fmt=PCT,
            how="Prior-month usage quantity Databricks has since retracted, as a % of what it first billed."),
        kpi("Spend by cloud", """
SELECT ROUND(COALESCE(SUM(net_list_cost), 0) * {disc}, 0) AS value,
       'Databricks usage by cloud, not your cloud bill' AS target
FROM {named:cost_cloud_infra}
WHERE {ws}
""", target="target", fmt=MONEY,
            how="List price this window by cloud, from cost_cloud_infra; your infra bill from the cloud provider may differ."),
    )
    p.charts(
        bar("Usage policy coverage, serverless DBUs", """
WITH r AS (
  SELECT * FROM {named:cost_usage_policy_coverage}
  WHERE {ws} AND (is_serverless OR billing_origin_product IN """ + SERVERLESS_ONLY + """)
)
SELECT CASE policy_coverage WHEN 'none' THEN 'No policy' WHEN 'usage_policy' THEN 'Usage policy'
            WHEN 'budget_policy_legacy' THEN 'Budget policy (legacy)' ELSE COALESCE(policy_coverage, 'Unknown') END AS coverage,
       ROUND(SUM(net_usage_quantity), 0) AS dbus
FROM r GROUP BY 1 ORDER BY 2 DESC
""", "coverage", "dbus", fmt=INT, category_title="Coverage", value_title="DBUs",
            how="Serverless DBUs this window by usage/budget-policy coverage."),
        bar("Tag coverage, serverless DBUs", """
WITH r AS (
  SELECT * FROM {named:cost_usage_policy_coverage}
  WHERE {ws} AND (is_serverless OR billing_origin_product IN """ + SERVERLESS_ONLY + """)
)
SELECT CASE tag_coverage WHEN 'untagged' THEN 'Untagged' WHEN 'tagged' THEN 'Tagged' ELSE COALESCE(tag_coverage, 'Unknown') END AS coverage,
       ROUND(SUM(net_usage_quantity), 0) AS dbus
FROM r GROUP BY 1 ORDER BY 2 DESC
""", "coverage", "dbus", fmt=INT, category_title="Coverage", value_title="DBUs",
            how="Serverless DBUs this window by tag coverage (any tag at all, not just the chargeback keys)."),
    )
    p.charts(
        bar("Effective list price by SKU (rate gap)", """
SELECT sku_name AS sku, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:cost_actual_vs_list_by_sku}
WHERE {ws} AND status <> 'NOT_ASSESSED'
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "sku", "usd", fmt=MONEY, category_title="SKU", value_title="Spend",
            how="Top 10 SKUs by list price this window, where the actual-vs-list check could run."),
        bar("Spend by cloud", """
SELECT COALESCE(cloud, '(unknown)') AS cloud, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:cost_cloud_infra}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC
""", "cloud", "usd", fmt=MONEY, category_title="Cloud", value_title="Spend",
            how="List price this window by cloud, from cost_cloud_infra."),
    )
    p.text(["Networking egress by usage type: see cost_networking_egress under every check on this page below."],
           height=1)
    p.checks()
