{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- Per billed warehouse, all-purpose cluster, job, pipeline and serverless notebook: each tag key's value on its last day on the
-- bill, the first day of that value's latest unbroken run on the bill's billed days, the key's last
-- day and the bill's own last day, so the Tags page can say "on the bill since <date>" and count a
-- tag that is on the bill today, with every value the key has had there. Job and pipeline runs'
-- own clusters are left out (one per run); their job or pipeline carries the dates. A
-- serverless notebook's bill carries its usage policy's tags.
-- Databricks twin: app/direct_sql/tags/bill_tag_dates.sql.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.bill_tag_dates is DuckDB-only for now") }}
{% endif %}

WITH billed AS (
    SELECT usage_date, workspace_id, custom_tags,
           usage_metadata.warehouse_id AS warehouse_id,
           usage_metadata.cluster_id AS cluster_id,
           usage_metadata.job_id AS job_id,
           usage_metadata.dlt_pipeline_id AS dlt_pipeline_id,
           usage_metadata.notebook_id AS notebook_id,
           COALESCE(usage_metadata.usage_policy_id, usage_metadata.budget_policy_id) IS NOT NULL AS has_policy
    FROM {{ source('system_billing', 'usage') }}
    WHERE usage_unit = 'DBU' AND workspace_id IS NOT NULL AND usage_date < {{ audit_today() }}
),
entities AS (
    SELECT 'billed_warehouse' AS entity_type, workspace_id, warehouse_id AS entity_id, usage_date, custom_tags, has_policy
    FROM billed WHERE warehouse_id IS NOT NULL
    UNION ALL
    SELECT 'billed_cluster', workspace_id, cluster_id, usage_date, custom_tags, has_policy
    FROM billed WHERE cluster_id IS NOT NULL AND job_id IS NULL AND dlt_pipeline_id IS NULL
    UNION ALL
    SELECT 'billed_job', workspace_id, job_id, usage_date, custom_tags, has_policy
    FROM billed WHERE job_id IS NOT NULL
    UNION ALL
    SELECT 'billed_pipeline', workspace_id, dlt_pipeline_id, usage_date, custom_tags, has_policy
    FROM billed WHERE dlt_pipeline_id IS NOT NULL
    UNION ALL
    SELECT 'billed_notebook', workspace_id, notebook_id, usage_date, custom_tags, has_policy
    FROM billed
    WHERE notebook_id IS NOT NULL AND warehouse_id IS NULL AND cluster_id IS NULL
      AND job_id IS NULL AND dlt_pipeline_id IS NULL
),
entity_days AS (
    SELECT DISTINCT entity_type, workspace_id, entity_id, usage_date
    FROM entities
),
entity_last AS (
    SELECT entity_type, workspace_id, entity_id, MAX(usage_date) AS entity_last_date
    FROM entity_days
    GROUP BY entity_type, workspace_id, entity_id
),
pairs AS (
    SELECT b.entity_type, b.workspace_id, b.entity_id, b.usage_date, b.has_policy, e.key AS raw_key, trim(e.value) AS raw_value,
           {{ norm_tag_key('e.key') }} AS tag_key
    FROM entities b, unnest(map_entries(b.custom_tags)) AS x(e)
    WHERE e.key IS NOT NULL AND e.value IS NOT NULL AND {{ norm_tag_key('e.key') }} <> ''
),
-- every key is kept locally; the export filters by settings export_tag_keys
keyed AS (
    SELECT * FROM pairs
),
dated AS (
    SELECT entity_type, workspace_id, entity_id, tag_key,
           max_by(raw_key, usage_date) AS raw_key, max_by(raw_value, usage_date) AS last_value,
           list_sort(list_distinct(list(raw_value))) AS all_values,
           MAX(usage_date) AS last_date,
           -- a usage policy put the key there on its last day: the policy's value, even when it equals the workspace's
           COALESCE(MAX(CASE WHEN has_policy THEN usage_date END) = MAX(usage_date), FALSE) AS from_policy
    FROM keyed
    GROUP BY entity_type, workspace_id, entity_id, tag_key
),
-- Days the key carried its latest value.
current_days AS (
    SELECT DISTINCT k.entity_type, k.workspace_id, k.entity_id, k.tag_key, k.usage_date
    FROM keyed k
    JOIN dated d
        ON d.entity_type = k.entity_type AND d.workspace_id = k.workspace_id
       AND d.entity_id = k.entity_id AND d.tag_key = k.tag_key AND k.raw_value = d.last_value
),
-- The last billed day before that without it: the value has run unbroken since the next one.
broken AS (
    SELECT d.entity_type, d.workspace_id, d.entity_id, d.tag_key, MAX(ed.usage_date) AS break_date
    FROM dated d
    JOIN entity_days ed
        ON ed.entity_type = d.entity_type AND ed.workspace_id = d.workspace_id
       AND ed.entity_id = d.entity_id AND ed.usage_date < d.last_date
    LEFT JOIN current_days c
        ON c.entity_type = d.entity_type AND c.workspace_id = d.workspace_id AND c.entity_id = d.entity_id
       AND c.tag_key = d.tag_key AND c.usage_date = ed.usage_date
    WHERE c.usage_date IS NULL
    GROUP BY d.entity_type, d.workspace_id, d.entity_id, d.tag_key
)
SELECT d.entity_type, d.workspace_id, d.entity_id, d.tag_key, d.raw_key, d.last_value, any_value(d.all_values) AS all_values,
       MIN(c.usage_date) AS first_date, d.last_date, l.entity_last_date, d.from_policy
FROM dated d
JOIN entity_last l
    ON l.entity_type = d.entity_type AND l.workspace_id = d.workspace_id AND l.entity_id = d.entity_id
LEFT JOIN broken b
    ON b.entity_type = d.entity_type AND b.workspace_id = d.workspace_id
   AND b.entity_id = d.entity_id AND b.tag_key = d.tag_key
JOIN current_days c
    ON c.entity_type = d.entity_type AND c.workspace_id = d.workspace_id AND c.entity_id = d.entity_id
   AND c.tag_key = d.tag_key AND (b.break_date IS NULL OR c.usage_date > b.break_date)
GROUP BY d.entity_type, d.workspace_id, d.entity_id, d.tag_key, d.raw_key, d.last_value, d.last_date, l.entity_last_date, d.from_policy
