{#
    mask_user(expr, real_id=none): optional identity mask, gated by the mask_user_identities var.
    Off (default): `expr` unchanged, raw. On: NULL/'__REDACTED__'/a GUID pass through unchanged,
    else `<real_id or first 8 hex chars of sha256(lower(trim(expr)))> <first 2 raw chars>***`,
    matching app/core/identity.format_identity. Never called for resource names.
#}
{% macro mask_user(expr, real_id=none) %}
{%- if not var('mask_user_identities', false) -%}
{{ expr }}
{%- else -%}
CASE
  WHEN {{ expr }} IS NULL OR {{ expr }} = '__REDACTED__' THEN {{ expr }}
  WHEN {% if target.type == 'duckdb' -%}
  regexp_matches({{ expr }}, '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
  {%- else -%}
  {{ expr }} RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
  {%- endif %} THEN {{ expr }}
  ELSE concat({% if real_id -%}
  COALESCE({{ real_id }}, substr({% if target.type == 'duckdb' %}sha256(lower(trim({{ expr }}))){% else %}sha2(lower(trim({{ expr }})), 256){% endif %}, 1, 8))
  {%- else -%}
  substr({% if target.type == 'duckdb' %}sha256(lower(trim({{ expr }}))){% else %}sha2(lower(trim({{ expr }})), 256){% endif %}, 1, 8)
  {%- endif %}, ' ', substr({{ expr }}, 1, 2), '***')
END
{%- endif -%}
{% endmacro %}
