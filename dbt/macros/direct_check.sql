{#
    dbt/macros/direct_check.sql -- a custom materialization for dbt/models/databricks_direct/**
    (item DBX-DIRECT). It creates NOTHING in the workspace: it either runs the model's compiled
    query once and discards the rows (var direct_mode = "run", the default) or asks the warehouse
    to plan it without executing (var direct_mode = "explain"), then records what happened so
    `dbt run --select path:models/databricks_direct` (tools/verify_databricks.py) can report
    pass/fail per query straight from target/run_results.json.

    Why a materialization, not a run-operation macro (the more obvious tool for "run N queries and
    report each result"): Jinja has no try/except, so a run-operation macro that loops over N
    queries stops dead the moment one of them raises -- there is no way to catch a query's failure
    inside the loop and move on to the next one. dbt itself already solves exactly this problem for
    models: it builds each model as its own graph node, keeps going when one node errors, and
    writes every outcome (success/error, message, timing) into target/run_results.json. Making
    every generated d_<query_id> file a model, materialized through this macro, is what buys that
    behaviour "one query's failure never stops the others" for free -- ordinary dbt node semantics,
    just without ever creating a relation.

    var("direct_mode", "run") picks the check:
      "run"     SELECT * FROM (<compiled sql>) -- executes the query in full and fetches every row
                (so a bad JOIN/CAST that only fails at execution time, not at parse time, is caught
                too), then discards them; only the row count is kept.
      "explain" EXPLAIN <compiled sql> -- asks the warehouse to plan the query without running it
                (cheapest check: catches missing tables/columns and syntax errors, misses
                runtime-only failures such as a divide-by-zero). Spark/Databricks' EXPLAIN does NOT
                raise on a planning error -- it returns an ordinary result row whose text says so
                (e.g. "Error occurred during query planning: ..."). This macro scans that text
                itself and raises a compiler error when it looks like a planning failure, so a
                broken query still shows as an "error" node in run_results.json instead of a quiet
                pass with an error message sitting unread in the result set.

    "table" mode never reaches this materialization at all: the generated model's own config sets
    materialized="table" in that case, so dbt's built-in table materialization runs instead and the
    query's result lands in <AUDIT_DBX_CATALOG>.audit_direct.d_<query_id> -- the drop-in-
    replacement path. "run"/"explain" are the cheap, nothing-persisted verification path; both
    modes compile the exact same generated SQL body as "table" mode (tools/generate_direct_models.py
    writes one file, `direct_mode` only switches what dbt does with it).

    Always returns {"relations": []}: nothing is ever created, renamed or dropped here.

    Review fix (DBX-DIRECT): "creates nothing" was not actually true for run/explain mode. Before
    any node runs, dbt's RunTask.before_run calls create_schemas for every selected relational,
    non-ephemeral model regardless of its materialization -- so a plain `dbt run` would issue
    `CREATE SCHEMA IF NOT EXISTS <catalog>.audit_direct` even when direct_mode is "run" or
    "explain", which this file's own header (and docs/DATABRICKS_VERIFY.md's permissions section)
    promises never happens. The `databricks__create_schema` override below is the documented way
    to change that: dbt's own `create_schema` macro dispatches to `adapter.dispatch('create_schema',
    'dbt')`, and a macro named `<adapter_type>__create_schema` defined in the root project (this
    one) wins that dispatch over dbt-databricks' own global-project implementation. For
    direct_mode != "table" and the audit_direct schema specifically, it logs instead of issuing the
    CREATE -- run/explain never need more than SELECT on system.* plus USE CATALOG on
    AUDIT_DBX_CATALOG. Table mode (which does need CREATE SCHEMA/CREATE TABLE) is untouched: it
    falls through to the real `create schema if not exists` statement, same as any other relation.

    Extension (item DBX-ADMIN): the audit_admin schema (dbt/models/databricks_admin/**, generated
    by tools/generate_admin_models.py) is skipped in EVERY mode, unconditionally -- not only when
    direct_mode != "table" as for audit_direct. Those models are always disabled in table mode
    (their own config's `enabled=` already excludes `var("direct_mode") == "table"`), so a real
    CREATE SCHEMA for audit_admin should never even be reachable -- but this override makes that a
    guarantee of this macro, not an assumption resting on every admin model's config staying
    correct: the admin-library-originals folder is a pass/fail check only and must never create
    anything in the workspace, under any variable combination.
#}

{% macro databricks__create_schema(relation) -%}
{%- set schema_lower = (relation.schema or '') | lower -%}
{%- if schema_lower == 'audit_admin' -%}
  {{ log('direct_check: audit_admin (admin-library-originals pass/fail check) never creates a schema; not creating ' ~ relation, info=False) }}
{%- elif var('direct_mode', 'run') != 'table' and schema_lower == 'audit_direct' -%}
  {{ log('direct_check: direct_mode=' ~ var('direct_mode', 'run') ~ ', not creating schema ' ~ relation, info=False) }}
{%- else -%}
  {%- call statement('create_schema') -%}
    create schema if not exists {{ relation.without_identifier() }}
  {%- endcall -%}
{%- endif -%}
{%- endmacro %}


{% macro _direct_check_plan_has_error(plan_text) %}
{#- Spark EXPLAIN's own way of saying "this didn't parse/analyze" -- substrings, matched
    case-insensitively against the concatenated plan text (see the materialization below). #}
{%- set markers = [
    'error occurred during query planning',
    'analysisexception',
    'parseexception',
    'parse_syntax_error',
] -%}
{%- set lowered = (plan_text or '') | lower -%}
{%- set found = namespace(value=false) -%}
{%- for marker in markers -%}
  {%- if marker in lowered -%}
    {%- set found.value = true -%}
  {%- endif -%}
{%- endfor -%}
{{ return(found.value) }}
{% endmacro %}


{% materialization direct_check, adapter="databricks" %}

  {%- set mode = var('direct_mode', 'run') -%}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {%- if mode == 'explain' -%}
    {%- set check_sql = 'EXPLAIN ' ~ sql -%}
  {%- else -%}
    {%- set check_sql = 'SELECT * FROM (\n' ~ sql ~ '\n) AS direct_check_sq' -%}
  {%- endif -%}

  {% call statement('main', fetch_result=True) -%}
    {{ check_sql }}
  {%- endcall %}

  {%- set result = load_result('main') -%}
  {%- set result_table = result['table'] if result is not none else none -%}
  {%- set n_rows = (result_table.rows | length) if result_table is not none else 0 -%}

  {%- if mode == 'explain' -%}
    {#- Every cell of every plan row, concatenated: the error text (when there is one) is Spark's
        entire response to EXPLAIN, not a specific known column. #}
    {%- set plan_parts = [] -%}
    {%- if result_table is not none -%}
      {%- for row in result_table.rows -%}
        {%- for value in row -%}
          {%- if value -%}
            {%- do plan_parts.append(value | string) -%}
          {%- endif -%}
        {%- endfor -%}
      {%- endfor -%}
    {%- endif -%}
    {%- set plan_text = plan_parts | join('\n') -%}
    {%- if _direct_check_plan_has_error(plan_text) -%}
      {{ exceptions.raise_compiler_error(
          'direct_check (explain): the warehouse reported a planning error:\n' ~ plan_text[:2000]
      ) }}
    {%- endif -%}
    {%- set check_message = 'direct_check explain ok (' ~ n_rows ~ ' plan row(s))' -%}
  {%- else -%}
    {%- set check_message = 'direct_check run ok (' ~ n_rows ~ ' row(s))' -%}
  {%- endif -%}

  {#- Record the outcome under store_raw_result explicitly rather than trusting whatever
      `statement()` already stored via the raw adapter response: a SELECT/EXPLAIN's AdapterResponse
      often carries no rows_affected at all, and this way the row count in run_results.json is
      always the number of rows this macro actually fetched. Fall back to store_result with the
      real adapter response on a dbt-core old enough not to expose store_raw_result. #}
  {%- if store_raw_result is defined -%}
    {{ store_raw_result('main', message=check_message, code='OK', rows_affected=n_rows) }}
  {%- else -%}
    {{ store_result('main', response=(result['response'] if result is not none else None), agate_table=result_table) }}
  {%- endif -%}

  {{ run_hooks(post_hooks, inside_transaction=True) }}
  {{ adapter.commit() }}
  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': []}) }}

{% endmaterialization %}
