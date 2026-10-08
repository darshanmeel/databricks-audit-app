{#
    norm_tag_key(expr) -- T-63 (DEC-60 rule 4). Normalises a raw custom_tags MAP key (or an alias
    string) for tag-attribute matching: lower-cased, with spaces/hyphens/underscores removed, so
    "Cost Center", "cost-center" and "cost_center" all normalise to the same value. Called bare,
    no namespace (DEC-11, matching audit_today()/audit_now()/param()/classify_env()).

    `expr` is a raw SQL expression (a column reference), interpolated verbatim -- not a bound
    parameter. Defined here (dbt/macros/), not inline in the model, because a `{% macro %}` block
    written directly inside a model .sql file is not callable from that same model's SQL body --
    verified empirically against `dbt build --target test` (compile error: "'norm_tag_key' is
    undefined") before this macro existed; classify_env.sql is the same-shaped precedent for
    "define here, call bare from the model".

    Same dialect split as classify_env.sql's own `regexp_replace` calls, for the same reason:
    DuckDB needs the explicit 'g' (global) flag as a 4th argument; Spark SQL's regexp_replace
    replaces every match by default and takes no 4th argument at all.
#}
{% macro norm_tag_key(expr) %}
{%- if target.type == 'duckdb' -%}
regexp_replace(lower({{ expr }}), '[ _-]+', '', 'g')
{%- else -%}
regexp_replace(lower({{ expr }}), '[ _-]+', '')
{%- endif -%}
{% endmacro %}
