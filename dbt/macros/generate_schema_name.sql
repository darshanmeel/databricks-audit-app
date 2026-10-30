{#
    DEC-11: returns the custom schema name UNCHANGED -- no `<target_schema>_<custom_schema>`
    dbt-default prefixing. A model tagged `schema='findings'` (or `schema='dims'`, `schema='seeds'`)
    always resolves to `findings.f_<query_id>` / `dims.dim_workspace` / `seeds.<name>` on every
    target (dev, test, databricks), never `dev_findings.f_<query_id>` or similar. Every later
    task's hard-coded `findings.f_<qid>` reference depends on this override existing.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
