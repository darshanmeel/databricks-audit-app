{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- The workspace level of the tag rollup as a history (SCD type 2): one row per (workspace_id,
-- tag_key, valid_from). No system table holds a workspace's own tags, so each billed day's value
-- is inferred from system.billing.usage.custom_tags, for every key seen there: the day's top value
-- when it carries at least share_floor of the key's tagged DBUs and the key covers at least
-- coverage_floor of the day's billed DBUs, '__mixed__' when no value dominates, '__untagged__'
-- when too little carries the key. A run of days with one value is a version once it has held for
-- 3 days (to the next change, or to today for the latest); a shorter run (a weekend on other
-- compute, a tag added yesterday) stays in the version before it.
--
-- The first version starts on the workspace's first billed day, so the days before a tag first
-- showed read '__untagged__' and never take a later value. valid_to is the next version's
-- valid_from (exclusive), NULL on the current row; a day with no bill stays in its version.
-- share, coverage and top_values are measured over the version's own days. Spend is matched to
-- the version valid on its usage date (cost_unit_tag, perf_unit_tag, app/core/tag_spend.py);
-- tags.tag_workspace is the current row.
-- Databricks twin: app/direct_sql/tags/_workspace_history.sql.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.tag_workspace_history is DuckDB-only; app/direct_sql/tags/_workspace_history.sql is its Databricks twin") }}
{% endif %}
{% set share_floor = var('share_floor', 0.6) %}
{% set coverage_floor = var('coverage_floor', 0.5) %}

WITH billed AS (
    SELECT ROW_NUMBER() OVER () AS rid, u.workspace_id, u.usage_date, u.usage_quantity, u.custom_tags
    FROM {{ source('system_billing', 'usage') }} u
    WHERE u.usage_unit = 'DBU'
      AND u.workspace_id IS NOT NULL
      AND u.usage_date < {{ audit_today() }}
      -- an Azure workspace tag reaches serverless usage as well as classic compute, and a usage
      -- policy's tags are the policy's, not the workspace's
      AND u.usage_metadata.budget_policy_id IS NULL AND u.usage_metadata.usage_policy_id IS NULL
),
day_totals AS (
    SELECT workspace_id, usage_date, SUM(usage_quantity) AS dbus_total
    FROM billed
    GROUP BY workspace_id, usage_date
),
pairs AS (
    -- one (row, normalised key) pair: when one map holds two spellings of the same key, the
    -- alphabetically first raw key wins, so no row is ever counted twice for one key
    SELECT b.workspace_id, b.usage_date, b.usage_quantity, e.key AS raw_key,
           {{ norm_tag_key('e.key') }} AS tag_key, trim(e.value) AS tag_value
    FROM billed b, unnest(map_entries(b.custom_tags)) AS x(e)
    WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY b.rid, {{ norm_tag_key('e.key') }} ORDER BY e.key) = 1
),
day_values AS (
    SELECT workspace_id, tag_key, usage_date, tag_value, SUM(usage_quantity) AS dbus
    FROM pairs
    GROUP BY workspace_id, tag_key, usage_date, tag_value
),
day_top AS (
    SELECT workspace_id, tag_key, usage_date, tag_value, dbus,
           SUM(dbus) OVER (PARTITION BY workspace_id, tag_key, usage_date) AS dbus_tagged,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key, usage_date ORDER BY dbus DESC, tag_value) AS rn
    FROM day_values
),
day_state AS (
    -- every billed day of the workspace for every key it ever carried, so the days before a key
    -- first showed are '__untagged__' days of their own
    SELECT k.workspace_id, k.tag_key, t.usage_date,
           CASE
               WHEN d.dbus_tagged IS NULL OR d.dbus_tagged / NULLIF(t.dbus_total, 0) < {{ coverage_floor }}
                    OR t.dbus_total <= 0 THEN '__untagged__'
               WHEN d.dbus / NULLIF(d.dbus_tagged, 0) >= {{ share_floor }} THEN d.tag_value
               ELSE '__mixed__'
           END AS state
    FROM (SELECT DISTINCT workspace_id, tag_key FROM day_values) k
    JOIN day_totals t ON t.workspace_id = k.workspace_id
    LEFT JOIN day_top d
      ON d.workspace_id = k.workspace_id AND d.tag_key = k.tag_key AND d.usage_date = t.usage_date AND d.rn = 1
),
runs AS (
    SELECT workspace_id, tag_key, state, MIN(usage_date) AS run_from
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key ORDER BY usage_date)
               - ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key, state ORDER BY usage_date) AS grp
        FROM day_state
    ) d
    GROUP BY workspace_id, tag_key, state, grp
),
kept AS (
    -- a run stands once it has held 3 days, counted to the next run or to today
    SELECT *,
           CASE WHEN date_diff('day', run_from, COALESCE(
                         LEAD(run_from) OVER (PARTITION BY workspace_id, tag_key ORDER BY run_from),
                         {{ audit_today() }})) >= 3
                THEN state END AS kept_state
    FROM runs
),
smoothed AS (
    -- a short run takes the value of the last run that stands before it; a short first run keeps its own
    SELECT workspace_id, tag_key, run_from,
           COALESCE(
               kept_state,
               LAST_VALUE(kept_state IGNORE NULLS) OVER (
                   PARTITION BY workspace_id, tag_key ORDER BY run_from
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
               state) AS state
    FROM kept
),
versions AS (
    SELECT workspace_id, tag_key, state AS tag_value, MIN(run_from) AS valid_from
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key ORDER BY run_from)
               - ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key, state ORDER BY run_from) AS grp
        FROM smoothed
    ) s
    GROUP BY workspace_id, tag_key, state, grp
),
dated AS (
    SELECT *, LEAD(valid_from) OVER (PARTITION BY workspace_id, tag_key ORDER BY valid_from) AS valid_to
    FROM versions
),
version_values AS (
    SELECT v.workspace_id, v.tag_key, v.valid_from, d.tag_value, SUM(d.dbus) AS dbus
    FROM dated v
    JOIN day_values d
      ON d.workspace_id = v.workspace_id AND d.tag_key = v.tag_key
     AND d.usage_date >= v.valid_from AND (v.valid_to IS NULL OR d.usage_date < v.valid_to)
    GROUP BY v.workspace_id, v.tag_key, v.valid_from, d.tag_value
),
version_ranked AS (
    SELECT *,
           SUM(dbus) OVER (PARTITION BY workspace_id, tag_key, valid_from) AS dbus_tagged,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key, valid_from ORDER BY dbus DESC, tag_value) AS rn
    FROM version_values
),
-- the values carrying the most DBUs, so a mixed tag can name them
top_values AS (
    SELECT workspace_id, tag_key, valid_from, list(tag_value ORDER BY rn) AS top_values
    FROM version_ranked
    WHERE rn <= 5
    GROUP BY workspace_id, tag_key, valid_from
),
version_totals AS (
    SELECT v.workspace_id, v.tag_key, v.valid_from, SUM(t.dbus_total) AS dbus_total
    FROM dated v
    JOIN day_totals t
      ON t.workspace_id = v.workspace_id
     AND t.usage_date >= v.valid_from AND (v.valid_to IS NULL OR t.usage_date < v.valid_to)
    GROUP BY v.workspace_id, v.tag_key, v.valid_from
),
display AS (
    -- the raw spelling carrying the most DBUs account-wide names the key on screen
    SELECT tag_key, raw_key AS display_key
    FROM (
        SELECT tag_key, raw_key,
               ROW_NUMBER() OVER (PARTITION BY tag_key ORDER BY SUM(usage_quantity) DESC, raw_key) AS rn
        FROM pairs
        GROUP BY tag_key, raw_key
    ) d
    WHERE rn = 1
),
measured AS (
    SELECT v.workspace_id, v.tag_key, d.display_key, v.tag_value, v.valid_from, v.valid_to,
           r.tag_value AS top_value, tv.top_values,
           r.dbus / NULLIF(r.dbus_tagged, 0) AS share,
           COALESCE(r.dbus_tagged, 0) / NULLIF(t.dbus_total, 0) AS coverage,
           COALESCE(r.dbus_tagged, 0) AS dbus_tagged, t.dbus_total,
           CASE WHEN v.valid_to IS NULL THEN 'since ' || CAST(v.valid_from AS VARCHAR)
                ELSE 'from ' || CAST(v.valid_from AS VARCHAR) || ' to '
                     || CAST(CAST(v.valid_to - INTERVAL 1 DAY AS DATE) AS VARCHAR) END AS period
    FROM dated v
    JOIN version_totals t ON t.workspace_id = v.workspace_id AND t.tag_key = v.tag_key AND t.valid_from = v.valid_from
    JOIN display d ON d.tag_key = v.tag_key
    LEFT JOIN version_ranked r
      ON r.workspace_id = v.workspace_id AND r.tag_key = v.tag_key AND r.valid_from = v.valid_from AND r.rn = 1
    LEFT JOIN top_values tv
      ON tv.workspace_id = v.workspace_id AND tv.tag_key = v.tag_key AND tv.valid_from = v.valid_from
)
SELECT
    workspace_id,
    tag_key,
    display_key,
    tag_value,
    tag_value NOT IN ('__mixed__', '__untagged__') AS is_allocating,
    valid_from,
    valid_to,
    valid_to IS NULL AS is_current,
    top_value,
    top_values,
    share,
    coverage,
    dbus_tagged,
    dbus_total,
    CASE
        WHEN top_value IS NULL THEN
            period || ': no billed usage of this workspace carries ' || display_key
        WHEN tag_value = '__untagged__' THEN
            period || ': only ' || CAST(ROUND(COALESCE(coverage, 0) * 100, 1) AS VARCHAR)
            || '% of this workspace''s billed DBUs carry ' || display_key || ' (top value "' || top_value
            || '"); at least ' || CAST(ROUND({{ coverage_floor }} * 100, 1) AS VARCHAR)
            || '% is needed to infer a workspace tag'
        WHEN tag_value = '__mixed__' THEN
            period || ': no single ' || display_key || ' value reaches '
            || CAST(ROUND({{ share_floor }} * 100, 1) AS VARCHAR) || '% of the ' || display_key
            || '-tagged DBUs (top "' || top_value || '" at ' || CAST(ROUND(share * 100, 1) AS VARCHAR)
            || '%); tagged DBUs are ' || CAST(ROUND(coverage * 100, 1) AS VARCHAR)
            || '% of this workspace''s billed DBUs'
        ELSE
            period || ': ' || display_key || ' "' || tag_value || '" carries '
            || CAST(ROUND(share * 100, 1) AS VARCHAR) || '% of the ' || display_key
            || '-tagged DBUs, which are ' || CAST(ROUND(coverage * 100, 1) AS VARCHAR)
            || '% of this workspace''s billed DBUs'
    END AS reason
FROM measured
