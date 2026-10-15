{#
    As-of pinning (PLAN.md 5.3). Every generated model body calls these two macros bare
    (no namespace -- DEC-11) instead of the dialect's own current_date()/current_timestamp(), so
    a snapshot captured on day N is always evaluated as of day N, and the `test` target always
    sees the one fixed fixture instant regardless of which real day the test suite runs on.

    Dispatch order (both macros): target `test` -> fixed literal; else the matching `as_of_*` var
    (regex-validated before being spliced into SQL, since it is not a bound parameter) -> else the
    dialect-native current_date()/current_timestamp() (duckdb: with parens; databricks: without).
#}

{% macro audit_today() %}
{%- if target.name == 'test' -%}
DATE '2026-09-21'
{%- elif var('as_of_date', none) is not none -%}
{%- set d = var('as_of_date') -%}
{%- if not (d is string and modules.re.match('^[0-9]{4}-[0-9]{2}-[0-9]{2}$', d)) -%}
{{ exceptions.raise_compiler_error("audit_today(): var('as_of_date') = " ~ d ~ " is not a YYYY-MM-DD date") }}
{%- endif -%}
CAST('{{ d }}' AS DATE)
{%- elif target.type == 'duckdb' -%}
current_date()
{%- else -%}
current_date
{%- endif -%}
{% endmacro %}

{% macro audit_now() %}
{%- if target.name == 'test' -%}
TIMESTAMP '2026-09-21 12:00:00'
{%- elif var('as_of_ts', none) is not none -%}
{%- set t = var('as_of_ts') -%}
{%- if not (t is string and modules.re.match('^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?$', t)) -%}
{{ exceptions.raise_compiler_error("audit_now(): var('as_of_ts') = " ~ t ~ " is not a YYYY-MM-DD HH:MM:SS timestamp") }}
{%- endif -%}
CAST('{{ t | replace("T", " ") }}' AS TIMESTAMP)
{%- elif target.type == 'duckdb' -%}
current_timestamp()
{%- else -%}
current_timestamp
{%- endif -%}
{% endmacro %}
