# Plan: the Tag filter on every screen

Start: week of 2026-10-12.

## Goal

The top-bar Tag filter (any tag name, one or more values, several names) narrows every screen the
same way. A row takes its value from the most specific level that has the tag:

1. the query's own query tag;
2. the job or pipeline that ran it;
3. its warehouse or cluster: own tag, its bill, or a usage policy on the bill;
4. its workspace.

Every row and every dollar gets exactly one value, so the values add up to the total with nothing
counted twice. The Tags tab follows the filter: "of the queries with purpose = X, 80% carry
cost_center".

## Today

- Exact at each row's own level:
  - the single-query lists;
  - job, pipeline and run checks;
  - warehouse and cluster checks;
  - table and column checks;
  - workspace checks.
- A warehouse's tag already reaches every query on it that has no tag of its own.
- Gaps:
  - **Per-warehouse sums ignore query tags.** Queries › Trend, Capacity, Top queries by $,
    start-up waits and warehouse cost are summed per warehouse before the export. A query-tag
    filter can't split them, so they go empty.
  - **The Tags tab ignores the filter.**
  - **Query-tag dollars cover spend panels only.** `tags.cost_day` already splits each
    warehouse-day by `attributed_usage` query tags, with idle time going to the warehouse's tag.
    The per-warehouse query checks above don't use it.

## Warehouse and workspace tags first

The main way to filter is proper tags on warehouses and workspaces, not on every query. A query
with no tag of its own takes its warehouse's tag, then its workspace's. This already holds for spend,
query lists, jobs and the per-warehouse numbers. To fix:

1. **Tags page:** follow the filter with the same fallback.
2. **Workspace tags from `config/workspace.csv`, on every cloud:** one row per workspace, with
   `workspace_id` and then one column per tag key. The choice is made per workspace: a workspace
   listed in the file takes its tags from there, on Azure too.
   - **Azure, workspace not in the file:** its tags come from its bills, where Azure copies the
     workspace resource's tags. For example, with 2 workspaces in the file, those 2 use the file
     and the rest use their bills.
   - **AWS and GCP, workspace not in the file:** the bills carry no workspace tags, so the fallback
     stops at the warehouse.
3. **Query tag differs from its warehouse's:** spend follows the query tag, while the per-warehouse
   numbers follow the warehouse's, so the two can disagree. Each screen says which level it matched
   on.
4. **Untagged warehouses:** list, per filter, the warehouses that matched only through the workspace
   tag or not at all, so they can be tagged.

## Steps

0. **Measure on the real workspace.** Use the SQL below to get:
   - statements per 30 and 62 days;
   - the number of slices (step 2);
   - whether `system.billing.attributed_usage` is readable, and which products it covers.
1. **Say so instead of going empty.** Small, no export change. A check that can't split by the
   picked tag says it matched on the warehouse's and workspace's tags only.
2. **Export query slices.** Medium. New check `query_slices`, one row per:
   - workspace, warehouse, day;
   - origin (SQL editor, dashboard, job, notebook, Genie, alert, other);
   - job, pipeline or notebook id;
   - the query tags (every key).

   Each row carries summed runs, failures, total, exec, queue, slot-wait and start-up seconds, read,
   spill and shuffle GB, result-cache runs, and attributed DBUs and dollars. Build the dbt twin,
   direct SQL, manifest entry and tests.
3. **Filter chain for slices.** Medium. The row's own query tags come first: a new chain term reads
   the slice's tag map, not a `tags.tag_entity` lookup. Then job or pipeline, warehouse, workspace,
   reusing `app/core/tags.py`.
4. **Queries screens on slices.** Medium to large. With a tag filter on, Trend, Capacity,
   per-warehouse queue and summed start-up wait read `query_slices`. With no filter they keep
   today's checks, and a test proves both give the same totals. Top queries by $ stays per query
   group (query text times tags would explode the rows) and says so.
5. **Dollars for query tags.** Medium.
   - Split each warehouse's dollars by `attributed_usage` per slice.
   - The rest of the warehouse's bill (idle time) goes to the warehouse's tag, then the workspace's.
   - Cost › By tag and the Overview spend include it.
   - A test proves the values add up to the bill.
6. **Tags tab follows the filter.** Small after step 2: mandatory-tag coverage is counted over the
   slices the filter keeps.
7. **Docs.** Guide, README, and the tag banner text.

## Stays per warehouse

These follow the warehouse's and workspace's tags only, and the screen says so:

- start-up wait by the clock (merged per warehouse);
- warehouse starts;
- idle time, auto-stop and autoscale.

## Tests

- **Filter off:** today's totals.
- **Values add up:** the sum over every value plus untagged equals the total.
- **AND across names:** two names must both match.
- **Warehouse tag inherited:** a query with no tag takes its warehouse's.
- **Tag precedence:** a query tag beats the warehouse tag.

## Open decisions

- **When to read slices:** always, or only with a filter on? Always means one source per number.
- **Hour in slices:** keep it for the by-hour workload mix? That makes up to 24 times more rows.
- **Cap for very large accounts:** for example, over 2 million slices, keep only the mandatory and
  top tags.
- **What `attributed_usage` covers:** SQL warehouses only; jobs and pipelines are not in it.

## Step 0 SQL

```sql
-- statements and slices over 62 days
SELECT COUNT(*) AS statements,
       COUNT(DISTINCT workspace_id, compute.warehouse_id, date(start_time),
             query_source.job_info.job_id, to_json(query_tags)) AS slices
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL 62 DAYS;

-- what attributed_usage covers over 30 days
SELECT billing_origin_product, COUNT(*) AS rows_, SUM(active_usage_quantity) AS dbus
FROM system.billing.attributed_usage
WHERE usage_date >= current_date() - INTERVAL 30 DAYS
GROUP BY billing_origin_product;
```
