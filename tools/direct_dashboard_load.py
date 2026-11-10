"""tools/direct_dashboard_load.py

Dashboard load: the queries of every dashboard in this set (Lakehouse Health and the direct ones), per
dashboard, tab, day and warehouse, found by the dashboard and tab query tags each of them sets.
tools/build_direct_dashboards.py writes it with the other direct dashboards.
"""
from __future__ import annotations

from tools import build_databricks_dashboards as bdd
from tools.build_direct_dashboards import (
    END, START, Dataset, _page, bar, counter, dataset_json, field_filter, header, place, table, time_window,
)

TITLE = "Dashboard load"
P = "load"


def boards() -> list[tuple[str, str]]:
    """(tag value, title) of every dashboard the two builders write."""
    from tools.build_direct_dashboards import dashboards

    titles = [bdd.dashboard_name(t) for t in bdd.TABS] + [title for _s, title, _m, _d in dashboards()]
    return [(bdd.tag_slug(t), t) for t in titles]


def load() -> Dataset:
    values = ",\n         ".join(f"('{slug}', '{title.replace(chr(39), chr(39) * 2)}')" for slug, title in boards())
    return Dataset(
        "dashboard_load", "Dashboard load: the queries of every dashboard in this set, per tab, from their query tags",
        [], ["wh"],
        f"""boards AS (
  SELECT * FROM VALUES
         {values}
         AS t(slug, dashboard)
),
q AS (
  SELECT query_tags['dashboard'] AS slug, query_tags['tab'] AS tab, CAST(start_time AS DATE) AS day,
         date_trunc('HOUR', start_time) AS hr,
         executed_by, CAST(compute.warehouse_id AS STRING) AS warehouse_id, execution_status,
         COALESCE(from_result_cache, false) AS cached,
         COALESCE(total_duration_ms, 0) AS duration_ms,
         CASE WHEN from_result_cache THEN 0
              ELSE COALESCE(total_task_duration_ms, execution_duration_ms, total_duration_ms, 0) END AS weight_ms,
         COALESCE(waiting_at_capacity_duration_ms, 0) AS queued_ms, COALESCE(read_bytes, 0) AS read_bytes
  FROM system.query.history
  -- the SET QUERY_TAGS statements are not the dashboard's work; on a reused session they carry a tab
  WHERE query_tags['dashboard'] IS NOT NULL AND query_tags['tab'] IS NOT NULL
    AND upper(COALESCE(statement_type, '')) <> 'SET'
    AND start_time >= {START} AND start_time < {END}
),
all_work AS (
  -- everything those warehouses ran per hour: a query's $ is its share of the hour's work
  SELECT CAST(compute.warehouse_id AS STRING) AS warehouse_id, date_trunc('HOUR', start_time) AS hr,
         SUM(CASE WHEN from_result_cache THEN 0
                  ELSE COALESCE(total_task_duration_ms, execution_duration_ms, total_duration_ms, 0) END) AS all_ms
  FROM system.query.history
  WHERE start_time >= {START} AND start_time < {END}
    AND CAST(compute.warehouse_id AS STRING) IN (SELECT warehouse_id FROM q)
  GROUP BY 1, 2
),
price AS (
  -- overlapping price rows end where the next one starts, so no usage row is priced twice
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time, list_rate
  FROM (
    SELECT sku_name, cloud, usage_unit, price_start_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate,
           CASE WHEN price_end_time IS NULL THEN nxt WHEN nxt IS NULL THEN price_end_time
                ELSE least(price_end_time, nxt) END AS price_end_time
    FROM (
      SELECT *, LEAD(price_start_time) OVER (PARTITION BY sku_name, cloud, usage_unit
                                             ORDER BY price_start_time, price_end_time NULLS LAST) AS nxt
      FROM system.billing.list_prices
      WHERE currency_code = 'USD'
    )
  )
  WHERE price_end_time IS NULL OR price_end_time > price_start_time
),
bill AS (
  -- those warehouses' billed $ per hour, at list price
  SELECT CAST(u.usage_metadata.warehouse_id AS STRING) AS warehouse_id, date_trunc('HOUR', u.usage_start_time) AS hr,
         SUM(u.usage_quantity * p.list_rate) AS usd
  FROM system.billing.usage u
  JOIN price p
    ON p.sku_name = u.sku_name AND p.cloud = u.cloud AND p.usage_unit = u.usage_unit
   AND u.usage_end_time >= p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= CAST({START} AS DATE) AND u.usage_date < CAST({END} AS DATE)
    AND CAST(u.usage_metadata.warehouse_id AS STRING) IN (SELECT warehouse_id FROM q)
  GROUP BY 1, 2
)
SELECT b.dashboard, q.tab, q.day, q.executed_by AS run_as,
       COALESCE(wh.warehouse_name, q.warehouse_id, 'No warehouse') AS warehouse,
       COUNT(*) AS queries,
       SUM(CASE WHEN q.execution_status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
       SUM(CASE WHEN q.cached THEN 1 ELSE 0 END) AS cached,
       ROUND(SUM(q.duration_ms) / 3600000, 3) AS hours,
       ROUND(SUM(q.queued_ms) / 3600000, 3) AS queued_hours,
       ROUND(MAX(q.duration_ms) / 1000, 1) AS slowest_s,
       ROUND(SUM(q.read_bytes) / 1e9, 2) AS gb_read,
       ROUND(SUM(COALESCE(bl.usd * q.weight_ms / NULLIF(a.all_ms, 0), 0)), 2) AS est_usd
FROM q
JOIN boards b ON b.slug = q.slug
LEFT JOIN wh ON wh.warehouse_id = q.warehouse_id
LEFT JOIN all_work a ON a.warehouse_id = q.warehouse_id AND a.hr = q.hr
LEFT JOIN bill bl ON bl.warehouse_id = q.warehouse_id AND bl.hr = q.hr
GROUP BY ALL
ORDER BY q.day DESC, hours DESC""",
        "Every query of the dashboards in this set, found by their dashboard and tab query tags, per dashboard, tab, "
        "day, identity and warehouse: count, failures, cache hits, run time, queue time, data read and estimated $.",
        "The whole dashboard.",
        "One row per dashboard, tab, day, identity and warehouse; newest first.",
        {"boards": "The dashboards of this set: their dashboard tag value and title.",
         "q": "Their queries in the window, tagged with a dashboard and a tab; SET statements left out.",
         "all_work": "Everything those warehouses ran per hour, by task time; a cached result counts 0.",
         "price": "List price of every SKU over time.",
         "bill": "Those warehouses' billed $ per hour at list price. A query's estimated $ is the hour's $ times "
                 "its share of the hour's work."},
        head="List price; dates from the Time Window.",
    )


DATASETS: list[Dataset] = []


def dashboard() -> dict:
    L = load()
    DATASETS[:] = [L]
    layout = place([
        (2, [header("load:head", TITLE,
                    "The queries of every Lakehouse Health and direct dashboard, from their dashboard and tab query "
                    "tags: how many, how long, how long queued, on which warehouse and roughly what they cost.")]),
        (1, [time_window("load", [L]), field_filter("load:board", "dashboard", "Dashboard", [L]),
             field_filter("load:tab", "tab", "Tab", [L]), field_filter("load:wh", "warehouse", "Warehouse", [L])]),
        (3, [(counter("load:queries", L, "queries", "Queries", "In the Time Window"), 2),
             (counter("load:hours", L, "hours", "Query hours", "Run time, summed"), 2),
             (counter("load:queued", L, "queued_hours", "Queued hours", "Waiting for a free warehouse slot"), 3),
             (counter("load:failed", L, "failed", "Failed queries", "A widget showed an error"), 2),
             (counter("load:usd", L, "est_usd", "Estimated $", "Its share of the warehouse bill, list price", money=True), 3)]),
        (7, [_by_day(L),
             bar("load:by_board", L, "dashboard", "Dashboard", "hours", "Hours", "Query hours by dashboard",
                 "By warehouse", money=False, color="warehouse")]),
        (10, [table("load:table", L, [("day", "Day", "text"), ("dashboard", "Dashboard", "text"),
                                      ("tab", "Tab", "text"), ("run_as", "Run as", "text"),
                                      ("warehouse", "Warehouse", "text"), ("queries", "Queries", "int"),
                                      ("failed", "Failed", "int"), ("cached", "From cache", "int"),
                                      ("hours", "Hours", "num"), ("queued_hours", "Queued hours", "num"),
                                      ("slowest_s", "Slowest (s)", "num"), ("gb_read", "GB read", "num"),
                                      ("est_usd", "Estimated $", "money")],
                    "Queries per dashboard, tab and day, newest first")]),
    ])
    return bdd.tag_dashboard_queries({"datasets": [dataset_json(L)],
                                      "pages": [_page("load", TITLE, layout, prefix=P)]}, TITLE)


def _by_day(d: Dataset) -> dict:
    w = bdd.bar_widget("load:by_day", d.name, "day", "queries", "Queries per day", category_label="Day",
                       value_label="Queries", color_col="dashboard", temporal=True, width=1, height=1, x=0, y=0)
    w["widget"]["spec"]["frame"].update({"description": "By dashboard", "showDescription": True})
    return w
