-- hand-written; not generated. Databricks twin of dbt/models/tags/tag_workspace_history.sql, as CTEs
-- only: tools/export_direct_results.py pastes it where a tags query says __WORKSPACE_HISTORY__, and
-- that query reads ws_history -- one row per (workspace_id, tag_key, valid_from), the workspace's
-- tag inferred per billed day from system.billing.usage.custom_tags. A run of days with one value
-- is a version once it has held 3 days (to the next change, or to today); a shorter run stays in
-- the version before it. The first version starts on the workspace's first billed day, so days
-- before a tag first showed read '__untagged__'. valid_to is exclusive, NULL on the current row.
wsh_billed AS (
    SELECT monotonically_increasing_id() AS rid, u.workspace_id, u.usage_date, u.usage_quantity, u.custom_tags
    FROM `system`.`billing`.`usage` u
    WHERE u.usage_unit = 'DBU' AND u.workspace_id IS NOT NULL
      AND u.usage_date < __AS_OF_DATE__
      AND u.ingestion_date < __AS_OF_DATE__
      -- an Azure workspace tag reaches serverless usage as well as classic compute, and a usage
      -- policy's tags are the policy's, not the workspace's
      AND u.usage_metadata.budget_policy_id IS NULL AND u.usage_metadata.usage_policy_id IS NULL
),
wsh_day_totals AS (
    SELECT workspace_id, usage_date, SUM(usage_quantity) AS dbus_total
    FROM wsh_billed
    GROUP BY workspace_id, usage_date
),
wsh_pairs AS (
    -- one (row, normalised key) pair; when a map holds two spellings of one key, the
    -- alphabetically first raw key wins
    SELECT b.workspace_id, b.usage_date, b.usage_quantity, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM wsh_billed b
    LATERAL VIEW explode(b.custom_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY b.rid, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
),
wsh_day_values AS (
    SELECT workspace_id, tag_key, usage_date, tag_value, SUM(usage_quantity) AS dbus
    FROM wsh_pairs
    GROUP BY workspace_id, tag_key, usage_date, tag_value
),
wsh_day_top AS (
    SELECT workspace_id, tag_key, usage_date, tag_value, dbus,
           SUM(dbus) OVER (PARTITION BY workspace_id, tag_key, usage_date) AS dbus_tagged,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key, usage_date ORDER BY dbus DESC, tag_value) AS rn
    FROM wsh_day_values
),
wsh_day_state AS (
    -- every billed day of the workspace for every key it ever carried
    SELECT k.workspace_id, k.tag_key, t.usage_date,
           CASE
               WHEN d.dbus_tagged IS NULL OR d.dbus_tagged / NULLIF(t.dbus_total, 0) < __COVERAGE_FLOOR__
                    OR t.dbus_total <= 0 THEN '__untagged__'
               WHEN d.dbus / NULLIF(d.dbus_tagged, 0) >= __SHARE_FLOOR__ THEN d.tag_value
               ELSE '__mixed__'
           END AS state
    FROM (SELECT DISTINCT workspace_id, tag_key FROM wsh_day_values) k
    JOIN wsh_day_totals t ON t.workspace_id = k.workspace_id
    LEFT JOIN wsh_day_top d
      ON d.workspace_id = k.workspace_id AND d.tag_key = k.tag_key AND d.usage_date = t.usage_date AND d.rn = 1
),
wsh_runs AS (
    SELECT workspace_id, tag_key, state, MIN(usage_date) AS run_from
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key ORDER BY usage_date)
               - ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key, state ORDER BY usage_date) AS grp
        FROM wsh_day_state
    ) d
    GROUP BY workspace_id, tag_key, state, grp
),
wsh_kept AS (
    -- a run stands once it has held 3 days, counted to the next run or to today
    SELECT *,
           CASE WHEN datediff(COALESCE(
                         LEAD(run_from) OVER (PARTITION BY workspace_id, tag_key ORDER BY run_from),
                         __AS_OF_DATE__), run_from) >= 3
                THEN state END AS kept_state
    FROM wsh_runs
),
wsh_smoothed AS (
    -- a short run takes the value of the last run that stands before it; a short first run keeps its own
    SELECT workspace_id, tag_key, run_from,
           COALESCE(
               kept_state,
               last_value(kept_state, true) OVER (
                   PARTITION BY workspace_id, tag_key ORDER BY run_from
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
               state) AS state
    FROM wsh_kept
),
wsh_versions AS (
    SELECT workspace_id, tag_key, state AS tag_value, MIN(run_from) AS valid_from
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key ORDER BY run_from)
               - ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key, state ORDER BY run_from) AS grp
        FROM wsh_smoothed
    ) s
    GROUP BY workspace_id, tag_key, state, grp
),
wsh_dated AS (
    SELECT *, LEAD(valid_from) OVER (PARTITION BY workspace_id, tag_key ORDER BY valid_from) AS valid_to
    FROM wsh_versions
),
wsh_version_values AS (
    SELECT v.workspace_id, v.tag_key, v.valid_from, d.tag_value, SUM(d.dbus) AS dbus
    FROM wsh_dated v
    JOIN wsh_day_values d
      ON d.workspace_id = v.workspace_id AND d.tag_key = v.tag_key
     AND d.usage_date >= v.valid_from AND (v.valid_to IS NULL OR d.usage_date < v.valid_to)
    GROUP BY v.workspace_id, v.tag_key, v.valid_from, d.tag_value
),
wsh_version_ranked AS (
    SELECT *,
           SUM(dbus) OVER (PARTITION BY workspace_id, tag_key, valid_from) AS dbus_tagged,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, tag_key, valid_from ORDER BY dbus DESC, tag_value) AS rn
    FROM wsh_version_values
),
wsh_top_values AS (
    SELECT workspace_id, tag_key, valid_from,
           transform(array_sort(collect_list(named_struct('rn', rn, 'v', tag_value))), x -> x.v) AS top_values
    FROM wsh_version_ranked
    WHERE rn <= 5
    GROUP BY workspace_id, tag_key, valid_from
),
wsh_version_totals AS (
    SELECT v.workspace_id, v.tag_key, v.valid_from, SUM(t.dbus_total) AS dbus_total
    FROM wsh_dated v
    JOIN wsh_day_totals t
      ON t.workspace_id = v.workspace_id
     AND t.usage_date >= v.valid_from AND (v.valid_to IS NULL OR t.usage_date < v.valid_to)
    GROUP BY v.workspace_id, v.tag_key, v.valid_from
),
wsh_display AS (
    -- the raw spelling carrying the most DBUs account-wide names the key on screen
    SELECT tag_key, raw_key AS display_key FROM (
        SELECT tag_key, raw_key,
               ROW_NUMBER() OVER (PARTITION BY tag_key ORDER BY SUM(usage_quantity) DESC, raw_key) AS rn
        FROM wsh_pairs GROUP BY tag_key, raw_key
    ) d WHERE rn = 1
),
wsh_measured AS (
    SELECT v.workspace_id, v.tag_key, d.display_key, v.tag_value, v.valid_from, v.valid_to,
           r.tag_value AS top_value, tv.top_values,
           r.dbus / NULLIF(r.dbus_tagged, 0) AS share,
           COALESCE(r.dbus_tagged, 0) / NULLIF(t.dbus_total, 0) AS coverage,
           COALESCE(r.dbus_tagged, 0) AS dbus_tagged, t.dbus_total,
           CASE WHEN v.valid_to IS NULL THEN 'since ' || CAST(v.valid_from AS STRING)
                ELSE 'from ' || CAST(v.valid_from AS STRING) || ' to ' || CAST(date_sub(v.valid_to, 1) AS STRING)
           END AS period
    FROM wsh_dated v
    JOIN wsh_version_totals t ON t.workspace_id = v.workspace_id AND t.tag_key = v.tag_key AND t.valid_from = v.valid_from
    JOIN wsh_display d ON d.tag_key = v.tag_key
    LEFT JOIN wsh_version_ranked r
      ON r.workspace_id = v.workspace_id AND r.tag_key = v.tag_key AND r.valid_from = v.valid_from AND r.rn = 1
    LEFT JOIN wsh_top_values tv
      ON tv.workspace_id = v.workspace_id AND tv.tag_key = v.tag_key AND tv.valid_from = v.valid_from
),
ws_history AS (
    SELECT
        workspace_id, tag_key, display_key, tag_value,
        tag_value NOT IN ('__mixed__', '__untagged__') AS is_allocating,
        valid_from, valid_to, valid_to IS NULL AS is_current,
        top_value, top_values, share, coverage, dbus_tagged, dbus_total,
        CASE
            WHEN top_value IS NULL THEN
                period || ': no billed usage of this workspace carries ' || display_key
            WHEN tag_value = '__untagged__' THEN
                period || ': only ' || CAST(ROUND(COALESCE(coverage, 0) * 100, 1) AS STRING)
                || '% of this workspace\'s billed DBUs carry ' || display_key || ' (top value "' || top_value
                || '"); at least __COVERAGE_FLOOR_PCT__% is needed to infer a workspace tag'
            WHEN tag_value = '__mixed__' THEN
                period || ': no single ' || display_key || ' value reaches __SHARE_FLOOR_PCT__% of the '
                || display_key || '-tagged DBUs (top "' || top_value || '" at '
                || CAST(ROUND(share * 100, 1) AS STRING) || '%); tagged DBUs are '
                || CAST(ROUND(coverage * 100, 1) AS STRING) || '% of this workspace\'s billed DBUs'
            ELSE
                period || ': ' || display_key || ' "' || tag_value || '" carries '
                || CAST(ROUND(share * 100, 1) AS STRING) || '% of the ' || display_key
                || '-tagged DBUs, which are ' || CAST(ROUND(coverage * 100, 1) AS STRING)
                || '% of this workspace\'s billed DBUs'
        END AS reason
    FROM wsh_measured
)
