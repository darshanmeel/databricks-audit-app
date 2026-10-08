#!/usr/bin/env python3
"""Generate tests/fixtures/ddl.py FROM the vendored catalog schema dump.

    python tools/build_fixture_ddl.py

Parses app/queries/vendored/databricks_system_catalog_schema.txt for the 47 system tables this
library reads (app.core.registry.SOURCE_CONTRACT), maps every Spark column type to its DuckDB /
pyarrow equivalent, and writes tests/fixtures/ddl.py exposing DDL (DuckDB CREATE TABLE strings),
ARROW_SCHEMA (pyarrow.Schema objects, for an empty typed parquet file) and FLAGS, all keyed
"<schema>__<table>" (e.g. "billing__usage").

A source with no detail block in the dump (8 on 2026-09-22, derived mechanically here -- see
DECISIONS.md DEC-07) is flagged MISSING_FROM_DUMP and gets an all-VARCHAR column list derived by
scanning every vendored query body that app.core.registry says reads it.

system.billing.usage.usage_metadata is a special case: the dump's own struct type for it is
truncated ("... 25 more fields", the only truncated type in the file). The truncation marker is
dropped (never emitted as a field), and every "usage_metadata.<field>" dot-access reference found
by scanning ALL vendored query bodies is unioned into the struct on top of whatever the dump
already lists (VARCHAR-typed, since the dump gives no type for those extra fields). A nested
reference "usage_metadata.<field>.<sub>" makes <field> a STRUCT of every <sub> referenced.

Stdlib + pyarrow (pinned in requirements.txt).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pyarrow as pa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.core.registry import SOURCE_CONTRACT, load_registry  # noqa: E402

DUMP_PATH = ROOT / "app" / "queries" / "vendored" / "databricks_system_catalog_schema.txt"
OUT_PATH = ROOT / "tests" / "fixtures" / "ddl.py"


# ---------------------------------------------------------------------------------------------
# Spark type string -> (DuckDB DDL type string, pyarrow type). A small recursive-descent parser:
# struct/array/map nest arbitrarily deep in the real dump (system.query.history.query_parameters
# is 30+ levels of struct<...>/array<...>), so this cannot be regex-substitution.
# ---------------------------------------------------------------------------------------------


class TypeParseError(ValueError):
    pass


class _TypeParser:
    """Parses one Spark type string into a small type tree: ("scalar", name) |
    ("array", elem) | ("map", key, val) | ("struct", [(fname, ftype), ...]). A struct's
    truncation marker ("... N more fields", see module docstring) is dropped while parsing,
    never emitted as a field."""

    def __init__(self, s: str):
        self.s = s
        self.n = len(s)
        self.i = 0

    def _peek(self) -> str:
        return self.s[self.i] if self.i < self.n else ""

    def _skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def _expect(self, ch: str) -> None:
        self._skip_ws()
        if self._peek() != ch:
            raise TypeParseError(f"expected {ch!r} at offset {self.i} in {self.s!r}")
        self.i += 1

    def parse(self):
        start = self.i
        while self.i < self.n and self.s[self.i] not in "<>,:()":
            self.i += 1
        name = self.s[start:self.i].strip()
        if self._peek() == "(":
            # e.g. decimal(38,18) -- consume the balanced parens verbatim into `name`.
            pstart = self.i
            depth = 0
            while self.i < self.n:
                c = self.s[self.i]
                self.i += 1
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
            name = name + self.s[pstart:self.i]
        if self._peek() == "<":
            self.i += 1
            lname = name.lower()
            if lname == "array":
                elem = self.parse()
                self._expect(">")
                return ("array", elem)
            if lname == "map":
                key = self.parse()
                self._expect(",")
                val = self.parse()
                self._expect(">")
                return ("map", key, val)
            if lname == "struct":
                fields: list[tuple[str, tuple]] = []
                while True:
                    self._skip_ws()
                    if self.s[self.i:self.i + 3] == "...":
                        # Truncation marker: "... N more fields>". Consume to the closing '>'
                        # (not consuming '>' itself) and stop -- drop this pseudo-field.
                        while self.i < self.n and self.s[self.i] != ">":
                            self.i += 1
                        break
                    fstart = self.i
                    while self.i < self.n and self.s[self.i] != ":":
                        self.i += 1
                    fname = self.s[fstart:self.i].strip()
                    self._expect(":")
                    ftype = self.parse()
                    fields.append((fname, ftype))
                    if self._peek() == ",":
                        self.i += 1
                        continue
                    break
                self._expect(">")
                return ("struct", fields)
            raise TypeParseError(f"unknown container type {name!r} in {self.s!r}")
        return ("scalar", name)


_DECIMAL_RE = re.compile(r"^decimal\(\s*\d+\s*,\s*\d+\s*\)$", re.IGNORECASE)

# Spark scalar type name (lowercased) -> (DuckDB DDL type, pyarrow type). Anything not listed
# here passes through unchanged (uppercased) as DuckDB DDL and as pyarrow.string() -- the same
# "unlisted things pass through" rule the Inputs section states for the type map as a whole.
_DDL_SCALARS = {
    "string": "VARCHAR",
    "int": "INTEGER",
    "bigint": "BIGINT",
    "tinyint": "TINYINT",
    "smallint": "SMALLINT",
    "double": "DOUBLE",
    "float": "FLOAT",
    "boolean": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "date": "DATE",
    "binary": "BLOB",
}
_PA_SCALARS = {
    "string": pa.string(),
    "int": pa.int32(),
    "bigint": pa.int64(),
    "tinyint": pa.int8(),
    "smallint": pa.int16(),
    "double": pa.float64(),
    "float": pa.float32(),
    "boolean": pa.bool_(),
    "timestamp": pa.timestamp("us"),
    "date": pa.date32(),
    "binary": pa.binary(),
}


def _render(node) -> tuple[str, "pa.DataType"]:
    """type tree -> (DuckDB DDL type string, pyarrow type)."""
    kind = node[0]
    if kind == "scalar":
        raw = node[1]
        if _DECIMAL_RE.match(raw):
            # decimal(38,18) -> DOUBLE (Inputs type map).
            return "DOUBLE", pa.float64()
        key = raw.lower()
        if key in _DDL_SCALARS:
            return _DDL_SCALARS[key], _PA_SCALARS[key]
        return raw.upper(), pa.string()
    if kind == "array":
        ddl_elem, pa_elem = _render(node[1])
        return f"{ddl_elem}[]", pa.list_(pa_elem)
    if kind == "map":
        ddl_k, pa_k = _render(node[1])
        ddl_v, pa_v = _render(node[2])
        return f"MAP({ddl_k}, {ddl_v})", pa.map_(pa_k, pa_v)
    if kind == "struct":
        ddl_fields = []
        pa_fields = []
        for fname, ftype in node[1]:
            ddl_t, pa_t = _render(ftype)
            # Quoted: a Spark struct field name can collide with a DuckDB reserved word (e.g.
            # "default" in billing.list_prices.pricing, "window"/"function" in
            # query.history.query_parameters) -- unquoted, those fail to parse as DDL.
            ddl_fields.append(f'"{fname}" {ddl_t}')
            pa_fields.append(pa.field(fname, pa_t))
        return f"STRUCT({', '.join(ddl_fields)})", pa.struct(pa_fields)
    raise TypeParseError(f"unknown type node {node!r}")


def spark_type_to_duckdb_and_arrow(spark_type: str) -> tuple[str, "pa.DataType"]:
    node = _TypeParser(spark_type.strip()).parse()
    return _render(node)


# ---------------------------------------------------------------------------------------------
# Catalog dump parser
# ---------------------------------------------------------------------------------------------

# A table detail block is introduced by a line consisting of a run of Unicode box-drawing dash
# characters (code point 0x2500, BOX DRAWINGS LIGHT HORIZONTAL; built at runtime via chr(0x2500)
# below to keep this file ASCII-only), then a space, then "system.<schema>.<table>" at the end
# of the line (e.g. two of that dash character then " system.billing.usage", verified on the
# real dump). Matching on the qualified name at the *end* of the line (never a line that equals
# it verbatim) is what the Inputs section requires.
_DASH = chr(0x2500)
_HEADER_RE = re.compile(
    r"^" + _DASH + r"+\s+(system\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*$"
)
_COL_RE = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$")
_SELECTABLE_RE = re.compile(r"^\s*selectable:\s*yes\s*\((\d+)\s*cols?\)\s*$")


# App-owned addition: system.storage.table_metrics_history backs 3 app-owned storage checks, not
# a vendored query, so it has no detail block in the vendored catalog dump (databricks_system_
# catalog_schema.txt). Its columns come from a live DESCRIBE of this table on an account that has
# it enabled -- merged into the parsed dump below so it renders through the exact same path as
# every dump-backed table (real types, not the MISSING_FROM_DUMP all-VARCHAR fallback).
APP_OWNED_DUMP: dict[str, list[tuple[str, str]]] = {
    "system.storage.table_metrics_history": [
        ("account_id", "string"),
        ("metastore_id", "string"),
        ("catalog_name", "string"),
        ("schema_name", "string"),
        ("table_name", "string"),
        ("table_id", "string"),
        ("table_type", "string"),
        ("table_owner", "string"),
        ("table_creation_time", "timestamp"),
        ("table_dropped_time", "timestamp"),
        ("snapshot_date", "date"),
        ("active_bytes", "bigint"),
        ("active_files", "bigint"),
        ("predictive_optimization_enabled", "boolean"),
    ],
}


def _read_lines(path: Path) -> list[str]:
    """The file is CRLF-terminated throughout; strip a trailing '\\r' from every line before
    matching against it (Inputs)."""
    raw = path.read_bytes().decode("utf-8")
    return [ln[:-1] if ln.endswith("\r") else ln for ln in raw.split("\n")]


def parse_dump(path: Path) -> dict[str, list[tuple[str, str]]]:
    """"system.<schema>.<table>" -> [(col_name, spark_type), ...] for every table with a detail
    block in the dump.

    Column scanning stops at the first line that does not match the "<name> : <type>" pattern,
    rather than trusting the preceding "selectable: yes (<n> cols)" line's declared count. This
    is what makes the parser resilient to the one corrupted block found in the real, vendored
    dump (system.information_schema.catalog_privileges' column list is truncated mid-line by
    stray text that was apparently captured into the source file when it was generated -- see
    Hand-off notes); everything else in the dump matches its declared count exactly."""
    lines = _read_lines(path)
    tables: dict[str, list[tuple[str, str]]] = {}
    i = 0
    n = len(lines)
    while i < n:
        m = _HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        qualified = m.group(1)
        j = i + 1
        if j < n and lines[j].strip().startswith("size:"):
            j += 1
        declared: int | None = None
        sm = _SELECTABLE_RE.match(lines[j]) if j < n else None
        if sm:
            declared = int(sm.group(1))
            j += 1
        if j < n and lines[j].strip().startswith("columns ("):
            j += 1
        cols: list[tuple[str, str]] = []
        while j < n:
            cm = _COL_RE.match(lines[j])
            if not cm:
                break
            cols.append((cm.group(1), cm.group(2)))
            j += 1
        if declared is not None and declared != len(cols):
            print(
                f"warning: {qualified}: dump declares {declared} columns, parsed {len(cols)} "
                "(truncated block in the catalog dump; columns after the cut are unknown)",
                file=sys.stderr,
            )
        tables[qualified] = cols
        i = j
    return tables


# ---------------------------------------------------------------------------------------------
# usage_metadata supplementation
# ---------------------------------------------------------------------------------------------

_USAGE_METADATA_FIELD_RE = re.compile(
    r"\busage_metadata\.([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?"
)


def usage_metadata_extra_fields(bodies: list[str], known: set[str]) -> list[tuple[str, list[str]]]:
    """Every 'usage_metadata.<field>' dot-access field referenced across ALL vendored query
    bodies (not just cost_*) that is not already in `known` (the dump's own field list, after
    its truncation marker is dropped), as (field, sub-fields): a field read as
    'usage_metadata.<field>.<sub>' is a struct of those sub-fields, any other one a scalar (an
    empty list). Never drops a field the dump has, only adds ones it is missing (Inputs)."""
    subs: dict[str, set[str]] = {}
    for body in bodies:
        for field, sub in _USAGE_METADATA_FIELD_RE.findall(body):
            subs.setdefault(field, set())
            if sub:
                subs[field].add(sub)
    return [(f, sorted(subs[f])) for f in sorted(set(subs) - known)]


# ---------------------------------------------------------------------------------------------
# MISSING_FROM_DUMP fallback: an all-VARCHAR column list for a source with no detail block,
# derived by scanning every vendored query body that reads it for "<table_or_alias>.<column>"
# references.
# ---------------------------------------------------------------------------------------------

# SQL keywords / functions that show up as bare tokens in the vendored governance_access bodies
# that reference the 8 sources absent from the dump (see Hand-off notes for the queries this was
# derived from) -- excluded so the bare-token fallback below does not mistake them for columns.
_SQL_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "AS", "ON", "JOIN", "LEFT", "RIGHT",
    "INNER", "OUTER", "CROSS", "UNION", "ALL", "DISTINCT", "CASE", "WHEN", "THEN", "ELSE", "END",
    "AND", "OR", "NOT", "NULL", "IS", "IN", "LIKE", "RLIKE", "BETWEEN", "DESC", "ASC", "LIMIT",
    "WITH", "TRUE", "FALSE", "INTERVAL", "DAY", "DAYS", "OVER", "PARTITION", "HAVING", "EXISTS",
    "COUNT", "SUM", "MAX", "MIN", "AVG", "CAST", "SUBSTR", "SUBSTRING", "CONCAT", "COALESCE",
    "LOWER", "UPPER", "TRIM", "NVL", "DATEADD", "DATEDIFF", "CURRENT_DATE", "CURRENT_TIMESTAMP",
    "SYSTEM",
}


def _table_alias_bindings(body: str, schema: str, table: str) -> set[str]:
    """Every alias token bound to system.<schema>.<table> in this body via a
    'FROM/JOIN system.<schema>.<table> [AS] <alias>' occurrence."""
    pat = re.compile(
        r"system\." + re.escape(schema) + r"\." + re.escape(table)
        + r"\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b",
        re.IGNORECASE,
    )
    aliases = set()
    for m in pat.finditer(body):
        cand = m.group(1)
        if cand.upper() not in _SQL_KEYWORDS:
            aliases.add(cand)
    return aliases


def _all_alias_bindings(body: str) -> set[str]:
    aliases: set[str] = set()
    for schema, tables in SOURCE_CONTRACT.items():
        for table in tables:
            aliases |= _table_alias_bindings(body, schema, table)
    return aliases


def _strip_comments_and_strings(body: str) -> str:
    """Drop '--' line comments, then string literals (replaced with a space each so token
    offsets/boundaries still line up) -- comment prose is not SQL and must never leak into the
    bare-token fallback below."""
    no_comments = re.sub(r"--[^\n]*", " ", body)
    return re.sub(r"'(?:[^'\\]|\\.)*'", " ", no_comments)


def infer_missing_columns(schema: str, table: str, bodies: list[str]) -> list[str]:
    """All-VARCHAR column-name candidates for a source with no detail block in the dump.

    Preferred form: every '<alias>.<column>' reference for an alias bound to this table's own
    'FROM/JOIN system.<schema>.<table>' occurrence in the body (e.g. 'dc.catalog_name' for
    'FROM system.data_classification.results dc').

    Fallback (used only for a body where the table is referenced with no alias at all, which is
    the common case for a single-table information_schema inventory query): every bare,
    unqualified identifier in that body's SELECT/WHERE/GROUP BY clauses that is not a SQL
    keyword/function, not an alias bound to some OTHER table in the same body, and not itself
    immediately followed by '(' (a function call) or immediately preceded by 'AS ' (that specific
    occurrence is an output alias, not a source column).

    This deliberately biases toward over-inclusion: a stray extra VARCHAR column (e.g. an output
    alias also picked up from an unrelated ORDER BY reference) is harmless, but dropping a real
    column a query reads (which a global, every-occurrence alias exclusion could do whenever a
    column is aliased to its own name, e.g. 'MAX(latest_detected_time) AS latest_detected_time')
    would break any later build against this fixture -- so only the exact token immediately after
    'AS ' is excluded, not every bare occurrence of that name elsewhere in the body."""
    cols: set[str] = set()
    for raw_body in bodies:
        body = _strip_comments_and_strings(raw_body)
        own_aliases = _table_alias_bindings(body, schema, table)
        if own_aliases:
            for alias in own_aliases:
                pat = re.compile(r"\b" + re.escape(alias) + r"\.([A-Za-z_][A-Za-z0-9_]*)")
                cols.update(m.lower() for m in pat.findall(body))
            continue
        other_aliases = _all_alias_bindings(body) - own_aliases
        for m in re.finditer(r"(?<!\.)\b([A-Za-z_][A-Za-z0-9_]*)\b", body):
            tok = m.group(1)
            tok_lower = tok.lower()
            if tok.upper() in _SQL_KEYWORDS or tok in other_aliases:
                continue
            if tok_lower in (schema, table, "system"):
                continue
            if body[m.end():m.end() + 1] == "(":
                continue  # function call
            before = body[:m.start()].rstrip()
            if before[-2:].upper() == "AS" and (len(before) == 2 or not before[-3].isalnum()):
                continue  # this occurrence is an output alias, not a source column
            cols.add(tok_lower)
    return sorted(cols)


# ---------------------------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------------------------


def build() -> tuple[dict[str, str], dict[str, "pa.Schema"], dict[str, str]]:
    dump = parse_dump(DUMP_PATH)
    dump.update(APP_OWNED_DUMP)
    specs = load_registry()

    # source key ("schema__table") -> every executable query body that reads it (used both by
    # the MISSING_FROM_DUMP fallback and by the usage_metadata supplementation scan below).
    bodies_by_source: dict[str, list[str]] = {}
    all_bodies: list[str] = []
    for spec in specs:
        all_bodies.append(spec.body)
        for schema, table in spec.sources:
            bodies_by_source.setdefault(f"{schema}__{table}", []).append(spec.body)

    ddl: dict[str, str] = {}
    arrow_schema: dict[str, "pa.Schema"] = {}
    flags: dict[str, str] = {}

    for schema in sorted(SOURCE_CONTRACT):
        for table in sorted(SOURCE_CONTRACT[schema]):
            key = f"{schema}__{table}"
            qualified = f"system.{schema}.{table}"
            dump_cols = dump.get(qualified)

            if dump_cols is None:
                flags[key] = "MISSING_FROM_DUMP"
                col_names = infer_missing_columns(schema, table, bodies_by_source.get(key, []))
                ddl_cols = [f'"{c}" VARCHAR' for c in col_names]
                pa_fields = [pa.field(c, pa.string()) for c in col_names]
                if not ddl_cols:
                    # No query reference found at all (should not happen for the current 8 --
                    # every one is read by at least one query -- but stay valid DuckDB DDL if it
                    # ever does): a single placeholder VARCHAR column.
                    ddl_cols = ['"_no_columns_inferred" VARCHAR']
                    pa_fields = [pa.field("_no_columns_inferred", pa.string())]
                ddl[key] = f"CREATE TABLE {key} (\n  " + ",\n  ".join(ddl_cols) + "\n)"
                arrow_schema[key] = pa.schema(pa_fields)
                continue

            col_specs = list(dump_cols)
            if key == "billing__usage":
                # Re-render usage_metadata with its extra, query-referenced fields unioned in.
                new_col_specs = []
                for cname, ctype in col_specs:
                    if cname == "usage_metadata":
                        node = _TypeParser(ctype.strip()).parse()
                        # `known` = usage_metadata's OWN sub-field names (after the truncation
                        # marker was already dropped by the parser), not the outer table's
                        # column names -- using the wrong set here would treat every already-
                        # present sub-field as "extra" and emit a duplicate STRUCT argument.
                        known = {fname for fname, _ in node[1]}
                        extra = usage_metadata_extra_fields(all_bodies, known)
                        extra_fields = [
                            (f, ("struct", [(s, ("scalar", "string")) for s in sub]) if sub else ("scalar", "string"))
                            for f, sub in extra
                        ]
                        node = (node[0], list(node[1]) + extra_fields)
                        ddl_t, pa_t = _render(node)
                        new_col_specs.append((cname, ddl_t, pa_t))
                    else:
                        ddl_t, pa_t = spark_type_to_duckdb_and_arrow(ctype)
                        new_col_specs.append((cname, ddl_t, pa_t))
                rendered = new_col_specs
            else:
                rendered = [
                    (cname, *spark_type_to_duckdb_and_arrow(ctype)) for cname, ctype in col_specs
                ]

            ddl_cols = [f'"{cname}" {ddl_t}' for cname, ddl_t, _ in rendered]
            pa_fields = [pa.field(cname, pa_t) for cname, _, pa_t in rendered]
            ddl[key] = f"CREATE TABLE {key} (\n  " + ",\n  ".join(ddl_cols) + "\n)"
            arrow_schema[key] = pa.schema(pa_fields)

    return ddl, arrow_schema, flags


def _pa_type_repr(t: "pa.DataType") -> str:
    """A pyarrow type as a Python source expression using only `pa.*` calls (no DataType.__repr__
    round-tripping, which is not guaranteed stable across pyarrow versions)."""
    if pa.types.is_string(t):
        return "pa.string()"
    if pa.types.is_int8(t):
        return "pa.int8()"
    if pa.types.is_int16(t):
        return "pa.int16()"
    if pa.types.is_int32(t):
        return "pa.int32()"
    if pa.types.is_int64(t):
        return "pa.int64()"
    if pa.types.is_float32(t):
        return "pa.float32()"
    if pa.types.is_float64(t):
        return "pa.float64()"
    if pa.types.is_boolean(t):
        return "pa.bool_()"
    if pa.types.is_date32(t):
        return "pa.date32()"
    if pa.types.is_timestamp(t):
        return f"pa.timestamp({t.unit!r})"
    if pa.types.is_binary(t):
        return "pa.binary()"
    if pa.types.is_list(t):
        return f"pa.list_({_pa_type_repr(t.value_type)})"
    if pa.types.is_map(t):
        return f"pa.map_({_pa_type_repr(t.key_type)}, {_pa_type_repr(t.item_type)})"
    if pa.types.is_struct(t):
        parts = ", ".join(
            f"pa.field({t.field(i).name!r}, {_pa_type_repr(t.field(i).type)})"
            for i in range(t.num_fields)
        )
        return f"pa.struct([{parts}])"
    raise TypeParseError(f"no repr rule for pyarrow type {t!r}")


def _schema_repr(schema: "pa.Schema") -> str:
    fields = ",\n        ".join(
        f"pa.field({f.name!r}, {_pa_type_repr(f.type)})" for f in schema
    )
    return f"pa.schema([\n        {fields},\n    ])"


def render(ddl: dict[str, str], arrow_schema: dict[str, "pa.Schema"], flags: dict[str, str]) -> str:
    lines = [
        '"""tests/fixtures/ddl.py -- GENERATED by tools/build_fixture_ddl.py. Do not edit.',
        "",
        "One DuckDB CREATE TABLE string (DDL) and one pyarrow.Schema (ARROW_SCHEMA) per one of",
        "the 47 system tables the vendored query library reads, keyed \"<schema>__<table>\".",
        "FLAGS marks a source MISSING_FROM_DUMP when the catalog schema dump has no detail block",
        "for it (its column list is instead derived, all-VARCHAR, from the vendored query bodies",
        "that read it). See tools/build_fixture_ddl.py for how these are derived.",
        '"""',
        "from __future__ import annotations",
        "",
        "import pyarrow as pa",
        "",
        "DDL: dict[str, str] = {",
    ]
    for key in sorted(ddl):
        lines.append(f"    {key!r}: {ddl[key]!r},")
    lines.append("}")
    lines.append("")
    lines.append("ARROW_SCHEMA: dict[str, pa.Schema] = {")
    for key in sorted(arrow_schema):
        lines.append(f"    {key!r}: {_schema_repr(arrow_schema[key])},")
    lines.append("}")
    lines.append("")
    lines.append("FLAGS: dict[str, str] = {")
    for key in sorted(flags):
        lines.append(f"    {key!r}: {flags[key]!r},")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ddl, arrow_schema, flags = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(ddl, arrow_schema, flags))

    by_schema: dict[str, int] = {}
    for key in ddl:
        schema = key.split("__", 1)[0]
        by_schema[schema] = by_schema.get(schema, 0) + 1
    summary = ", ".join(f"{s}={n}" for s, n in sorted(by_schema.items()))
    print(f"wrote {OUT_PATH} ({len(ddl)} sources)")
    print(f"per-schema table count: {summary}")
    missing = sorted(k for k, v in flags.items() if v == "MISSING_FROM_DUMP")
    print(f"MISSING_FROM_DUMP ({len(missing)}): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
