{#
    classify_env(expr) -- T-23. Fuzzy env classification (PLAN.md D11 / 5.5, DEC-22).

    `expr` is a raw SQL expression (a column reference or a quoted string literal -- this macro
    interpolates it verbatim, it is not a bound parameter), e.g. `{{ classify_env('workspace_name') }}`
    or `{{ classify_env("'acme-prod'") }}`.

    Normalises `[-_./]+` (hyphen first -- needs no escaping in either engine's regex dialect) to
    a single space and lower-cases the input first, then word-boundary matches against the
    `env_patterns` var declared in dbt/dbt_project.yml (this macro is the sole consumer of that
    var -- word lists are never duplicated here). Bucket precedence is fixed: prod, then uat, then
    dev (first match wins); no match -> 'unknown'. `app/core/envmap.py` is the Python twin of this
    exact rule (with `re.ASCII` on its own word-boundary regex, matching RE2's ASCII-only `\b`);
    tests/test_envmap.py proves parity.

    Dispatch on target.type, following the audit_today()/audit_now() (dbt/macros/audit_time.sql)
    pattern (DEC-11: macros are called bare, no namespace): `regexp_matches` on duckdb, `RLIKE` on
    databricks. Two dialect differences beyond that, both reviewer-caught (T-23 fix round):
      - `regexp_replace`'s global-replace flag: duckdb needs the explicit 'g' option, Spark SQL
        (databricks) replaces every match by default.
      - The word-boundary token `\b`: DuckDB's standard `'...'` string literal does not process
        backslash escapes, so a single backslash reaches RE2 unchanged and `'\b'` in the generated
        SQL is exactly RE2's word-boundary token. Spark SQL string literals DO process backslash
        escapes, so a single `\b` embedded the same way would be unescaped to a literal backspace
        character before it ever reaches the regex engine (silently matching nothing, forever) --
        the databricks branch therefore emits a DOUBLED backslash (`\\b`) in the generated SQL, so
        Spark's own unescaping collapses it back to a single `\b` for Java's regex engine.

    A NULL `expr` (e.g. a billing-only workspace with no workspaces_latest row and no tag hint)
    normalises to NULL, matches nothing, and safely falls through to 'unknown' -- env is never
    guessed from a NULL or empty name.
#}
{% macro classify_env(expr) %}
{%- set patterns = var('env_patterns') -%}
{%- set buckets = ['prod', 'uat', 'dev'] -%}
{%- set norm -%}
{%- if target.type == 'duckdb' -%}
regexp_replace(lower({{ expr }}), '[-_./]+', ' ', 'g')
{%- else -%}
regexp_replace(lower({{ expr }}), '[-_./]+', ' ')
{%- endif -%}
{%- endset -%}
{%- set norm = norm | trim -%}
{%- if target.type == 'duckdb' -%}
{%- set b = '\\b' -%}
{%- else -%}
{%- set b = '\\\\b' -%}
{%- endif -%}
CASE
{%- for bucket in buckets %}
{%- set words = patterns[bucket] -%}
{%- set word_pattern = b ~ '(' ~ (words | join('|')) ~ ')' ~ b %}
    WHEN {% if target.type == 'duckdb' -%}
regexp_matches({{ norm }}, '{{ word_pattern }}')
{%- else -%}
{{ norm }} RLIKE '{{ word_pattern }}'
{%- endif %} THEN '{{ bucket }}'
{%- endfor %}
    ELSE 'unknown'
END
{%- endmacro %}
