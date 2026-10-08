{#
    Generic dbt test: fails when `model` has more than one row per the given column combination.
    Two calling conventions are both accepted:
      - single-column dbt convention:  unique_grain: {column_name: run_id}
      - multi-column convention:       unique_grain: {columns: [window_days, run_id]}
    T-06's worked example calls the multi-column form:
    `unique_grain: {columns: [window_days] + grain}`.
    Severity comes from `var('grain_severity', 'error')`: tools/dbt_run.py passes `warn` for the
    dev target because masked-key grains (DECISIONS.md DEC-48) can collide on a real snapshot; the
    test target keeps `error`.
#}
{% test unique_grain(model, column_name=none, columns=none) %}
{{ config(severity=var('grain_severity', 'error')) }}

{%- if columns is not none -%}
    {%- set grain_columns = columns -%}
{%- elif column_name is not none -%}
    {%- set grain_columns = [column_name] -%}
{%- else -%}
    {{ exceptions.raise_compiler_error("unique_grain: pass either column_name or columns") }}
{%- endif -%}

{%- if grain_columns | length == 0 -%}
    {{ exceptions.raise_compiler_error("unique_grain: columns list is empty") }}
{%- endif -%}

select
    {{ grain_columns | join(', ') }},
    count(*) as n_rows
from {{ model }}
group by {{ grain_columns | join(', ') }}
having count(*) > 1

{% endtest %}
