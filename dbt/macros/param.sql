{#
    param(qid, name, default): renders a threshold literal by looking up config/thresholds.yml's
    content (merged into `--vars` by tools/dbt_run.py under the `thresholds` var), in this order,
    first match wins:
      1. var('thresholds', {})[qid][name]
      2. var('thresholds', {})['_all'][name]
      3. default (the query header's own default)
    Called bare in every generated model body (DEC-11): {{ param('cost_by_job', 'top_n', 100) }}.
#}
{% macro param(qid, name, default) %}
{%- set thresholds = var('thresholds', {}) -%}
{%- set qid_map = thresholds[qid] if (thresholds is mapping and qid in thresholds and thresholds[qid] is mapping) else {} -%}
{%- set all_map = thresholds['_all'] if (thresholds is mapping and '_all' in thresholds and thresholds['_all'] is mapping) else {} -%}
{%- if name in qid_map -%}
{%- set value = qid_map[name] -%}
{%- elif name in all_map -%}
{%- set value = all_map[name] -%}
{%- else -%}
{%- set value = default -%}
{%- endif -%}
{%- if value is string -%}
'{{ value | replace("'", "''") }}'
{%- elif value is sameas true -%}
TRUE
{%- elif value is sameas false -%}
FALSE
{%- else -%}
{{ value }}
{%- endif -%}
{% endmacro %}
