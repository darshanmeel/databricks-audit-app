"""tests/fixtures/jinja_stub.py -- render one generated dbt model with plain jinja2, no dbt.

    render(model_text, target_type="duckdb", windows=(7, 30, 90), thresholds=None,
           source=None, audit_today=..., audit_now=..., mask_user_identities=False) -> str

Stand-ins for everything a generated model calls (tools/generate_models.py's output shape,
PLAN.md 5.3):
  config(...)            -> ""                       (dbt's model config; renders nothing)
  var('windows', d)      -> the `windows` list        var('thresholds', d) -> the thresholds dict
  target.type / .name    -> target_type / target_name
  source('system_<s>', '<t>') -> `source(schema, table)` if given, else an in-memory table name
                            `<schema>__<table>` (tests/fixtures/ddl.py's naming)
  param(qid, name, dflt) -> `thresholds(qid, name, dflt)` when thresholds is callable, else the
                            dbt/macros/param.sql lookup order over a dict: [qid][name], then
                            ['_all'][name], then the default; strings quoted, bools TRUE/FALSE
  audit_today() / audit_now() -> the fixed literals (defaults = the `test` target's values in
                            dbt/macros/audit_time.sql)

Deliberately tiny: it exists so tests/test_generate_models.py can assert on rendered SQL text
and execute it on DuckDB without invoking dbt at all.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import jinja2

DEFAULT_AUDIT_TODAY = "DATE '2026-09-21'"
DEFAULT_AUDIT_NOW = "TIMESTAMP '2026-09-21 12:00:00'"
_LIST_PRICES_MACRO = (Path(__file__).resolve().parent.parent.parent / "dbt" / "macros" / "list_prices.sql").read_text(encoding="utf-8")


def memory_source(source_name: str, table: str) -> str:
    """`source('system_billing', 'usage')` -> `billing__usage` (the DDL table names)."""
    schema = source_name[len("system_"):] if source_name.startswith("system_") else source_name
    return f"{schema}__{table}"


def parquet_source(snapshot_dir: str) -> Callable[[str, str], str]:
    """A `source()` stand-in that resolves to the read_parquet(...) form sources.yml uses."""
    def _source(source_name: str, table: str) -> str:
        schema = source_name[len("system_"):] if source_name.startswith("system_") else source_name
        return f"read_parquet('{snapshot_dir}/{schema}__{table}/*.parquet', union_by_name=true)"
    return _source


def _sql_literal(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def make_param(thresholds) -> Callable[[str, str, object], str]:
    if callable(thresholds):
        return lambda qid, name, default: _sql_literal(thresholds(qid, name, default))
    thresholds = thresholds or {}

    def _param(qid: str, name: str, default):
        qid_map = thresholds.get(qid) if isinstance(thresholds.get(qid), dict) else {}
        all_map = thresholds.get("_all") if isinstance(thresholds.get("_all"), dict) else {}
        if name in qid_map:
            return _sql_literal(qid_map[name])
        if name in all_map:
            return _sql_literal(all_map[name])
        return _sql_literal(default)
    return _param


_GUID_LITERAL = "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"


def make_mask_user(target_type: str, mask_user_identities: bool) -> Callable[..., str]:
    """dbt/macros/mask_user.sql, reproduced: off -> the plain column; on -> DEC-66.3's CASE."""
    def _mask_user(expr, real_id=None):
        if not mask_user_identities:
            return expr
        if target_type == "duckdb":
            guid_test = f"regexp_matches({expr}, {_GUID_LITERAL})"
            hashed = f"sha256(lower(trim({expr})))"
        else:
            guid_test = f"{expr} RLIKE {_GUID_LITERAL}"
            hashed = f"sha2(lower(trim({expr})), 256)"
        id_part = f"COALESCE({real_id}, substr({hashed}, 1, 8))" if real_id else f"substr({hashed}, 1, 8)"
        return (
            f"CASE WHEN {expr} IS NULL OR {expr} = '__REDACTED__' THEN {expr} "
            f"WHEN {guid_test} THEN {expr} "
            f"ELSE concat({id_part}, ' ', substr({expr}, 1, 2), '***') END"
        )
    return _mask_user


def render(
    model_text: str,
    *,
    target_type: str = "duckdb",
    target_name: str = "test",
    windows=(7, 30, 90),
    thresholds=None,
    source: Callable[[str, str], str] | None = None,
    audit_today: str = DEFAULT_AUDIT_TODAY,
    audit_now: str = DEFAULT_AUDIT_NOW,
    mask_user_identities: bool = False,
) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    windows_list = list(windows)
    known_vars = {"windows": windows_list, "thresholds": thresholds if isinstance(thresholds, dict) else {}}

    def _var(name, default=None):
        return known_vars.get(name, default)

    env.globals.update(
        config=lambda **kwargs: "",
        var=_var,
        target=SimpleNamespace(type=target_type, name=target_name),
        source=source or memory_source,
        param=make_param(thresholds),
        audit_today=lambda: audit_today,
        audit_now=lambda: audit_now,
        mask_user=make_mask_user(target_type, mask_user_identities),
    )
    # dbt/macros/list_prices.sql itself, rendered by the same env so its own {{ source(...) }}
    # call resolves through the `source` global just set above -- test renders see dbt's own text.
    env.globals["list_prices"] = env.from_string(_LIST_PRICES_MACRO).module.list_prices
    return env.from_string(model_text).render()
