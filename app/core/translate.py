"""app/core/translate.py -- Databricks SQL -> DuckDB SQL (PLAN.md section 5.4).

    translate(sql) -> str        apply every rule in RULES, in order, then refuse any construct
                                 that is still Databricks-only (raises Untranslatable)
    pin_time(sql) -> str         only the as-of pinning pair (current_date() -> {{ audit_today() }},
                                 current_timestamp() -> {{ audit_now() }}); the generator applies
                                 this same function on its own to the Databricks branch
    RULES                        the ordered rule table (name, description, example in/out, fn)
    Untranslatable / UNTRANSLATABLE   the exception raised for a construct with no rule
    strip_resource_mask(sql)     a resource-name preview CASE -> the plain column
    mask_user_identities(sql)    an identity-mask CASE -> a `mask_user()` macro call

The input is a query body in *sentinel form*: tools/generate_models.py has already replaced
`:period_days` with `__W__` and every other known `:param` with `__P__<name>__`. Every rule here
treats those tokens as plain identifiers and leaves them untouched (see test_tokens_untouched).

Every rewrite is driven by `mask(sql)`, a same-length copy of the input in which the contents of
'...' string literals and the text of `--` comments are blanked, so a regex never fires inside a
literal or a comment, and a balanced-parenthesis walk never sees a quoted or commented bracket.
Rules therefore never touch string literals or comments: a comment that mentions `size(` or
`system.lakeflow.jobs` survives verbatim in both dialect branches.

The rule table is the ported and extended DuckDB harness of the subaudit (tests/harness.py there):
the same mechanical translation convention, generalised from fixed-literal regexes to a
balanced-parenthesis call rewriter so nested arguments (CASE, CAST, LEAST, nested calls) are safe.

One rule (`row_number_star_to_qualify`) rewrites a statement SHAPE rather than a call, because
DuckDB 1.5.1 mis-binds `(SELECT a.*, ROW_NUMBER() OVER (w) AS rn FROM src a) WHERE rn = 1` and
returns shifted values under the right column names; it is the first rule in the table and it
raises rather than leave a shape it recognised but could not finish.

Stdlib only; app/core is imported by the Streamlit app.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


class Untranslatable(ValueError):
    """A construct with no translation rule (raised by translate(), never swallowed)."""


UNTRANSLATABLE = Untranslatable

# ---------------------------------------------------------------------------------------------
# masking helpers
# ---------------------------------------------------------------------------------------------


def spans(sql: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) for every '...' string literal (kind 'literal', quotes included; a
    doubled '' escape stays inside the literal) and every `--` comment (kind 'comment', up to but
    excluding the newline), left to right, non-overlapping."""
    found: list[tuple[int, int, str]] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)          # j is the closing quote, or n if unterminated
            found.append((i, end, "literal"))
            i = end
        elif c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            if j < 0:
                j = n
            found.append((i, j, "comment"))
            i = j
        else:
            i += 1
    return found


def mask(sql: str) -> str:
    """Same length as `sql`. Characters inside a '...' literal (the two quote characters are
    kept, the content -- including any doubled '' escape -- becomes spaces) and every character
    from a `--` comment marker to the end of its line become spaces. Regexes and bracket walks
    run on the mask and slice the original by position."""
    chars = list(sql)
    for start, end, kind in spans(sql):
        if kind == "literal":
            inner_end = end - 1 if sql[end - 1] == "'" and end - 1 > start else end
            for k in range(start + 1, inner_end):
                chars[k] = " "
        else:
            for k in range(start, end):
                chars[k] = " "
    return "".join(chars)


def strip_comments(sql: str) -> str:
    """The SQL with every `--` comment removed (string literals intact); trailing whitespace on
    each line trimmed. Used by the generator for meta extraction and by tests for
    'no leftover token outside comments/literals' assertions."""
    out: list[str] = []
    last = 0
    for start, end, kind in spans(sql):
        if kind == "comment":
            out.append(sql[last:start])
            last = end
    out.append(sql[last:])
    return "\n".join(line.rstrip() for line in "".join(out).split("\n"))


def _sub(pattern: re.Pattern, sql: str, repl: Callable[[re.Match, tuple], str]) -> str:
    """re.sub over mask(sql); `repl` receives the match (positions valid in both strings) and the
    tuple of ORIGINAL-text group slices (None for a group that did not participate)."""
    m_text = mask(sql)
    out: list[str] = []
    last = 0
    for m in pattern.finditer(m_text):
        groups = tuple(
            sql[m.start(g):m.end(g)] if m.start(g) >= 0 else None
            for g in range(1, pattern.groups + 1)
        )
        out.append(sql[last:m.start()])
        out.append(repl(m, groups))
        last = m.end()
    out.append(sql[last:])
    return "".join(out)


def _matching_paren(m_text: str, open_idx: int) -> int:
    """Index of the ')' matching the '(' at open_idx in a masked string."""
    depth = 0
    for k in range(open_idx, len(m_text)):
        ch = m_text[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return k
    raise Untranslatable(f"unbalanced parenthesis near: {m_text[open_idx:open_idx + 60]!r}")


def _enclosing_open(m_text: str, idx: int) -> int | None:
    """Index of the '(' that directly encloses position `idx` in a masked string, or None when
    `idx` sits at the top level. The mirror of _matching_paren, walking left."""
    depth = 0
    for k in range(idx - 1, -1, -1):
        ch = m_text[k]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                return k
            depth -= 1
    return None


def _split_args(inner: str) -> list[str]:
    """Split a call's argument text at top-level commas (parentheses and brackets nest; literals
    and comments are masked); returns the ORIGINAL slices, stripped."""
    m_text = mask(inner)
    args: list[str] = []
    depth = 0
    start = 0
    for k, ch in enumerate(m_text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(inner[start:k].strip())
            start = k + 1
    tail = inner[start:].strip()
    if tail or args:
        args.append(tail)
    return args


def _rewrite_calls(sql: str, name: str, fn: Callable[[list[str], str], str | None]) -> str:
    """Balanced-parenthesis call rewriter. For every `name(` (case-insensitive, not preceded by a
    word character or '.') outside literals/comments, the argument text is first rewritten
    recursively (nested same-name calls), then `fn(args, inner_text)` returns the replacement
    text, or None to keep `name(<rewritten inner>)` as it was."""
    pat = re.compile(r"(?<![\w.])" + re.escape(name) + r"\s*\(", re.IGNORECASE)
    m_text = mask(sql)
    out: list[str] = []
    i = 0
    while True:
        m = pat.search(m_text, i)
        if not m:
            out.append(sql[i:])
            break
        open_idx = m.end() - 1
        close_idx = _matching_paren(m_text, open_idx)
        inner = sql[open_idx + 1:close_idx]
        inner_rw = _rewrite_calls(inner, name, fn)
        # a rewritten call re-joins its arguments on one line, so a `--` comment inside the
        # argument list is dropped first (it would otherwise swallow the rest of the call)
        rep = fn(_split_args(strip_comments(inner_rw)), inner_rw)
        out.append(sql[i:m.start()])
        out.append(rep if rep is not None else sql[m.start():open_idx + 1] + inner_rw + ")")
        i = close_idx + 1
    return "".join(out)


_UNITS = {"DAY", "HOUR", "MINUTE", "SECOND", "WEEK", "MONTH", "YEAR"}


def _unit(raw: str, where: str) -> str:
    """DAY/DAYS/day -> DAY (and the other calendar units); anything else has no rule."""
    u = raw.strip().upper()
    if u.endswith("S") and u[:-1] in _UNITS:
        u = u[:-1]
    if u not in _UNITS:
        raise Untranslatable(f"{where}: unsupported unit {raw.strip()!r}")
    return u


def _shift(base: str, delta: str, unit: str) -> str:
    """`(base +/- (X) * INTERVAL 1 UNIT)` -- a leading '-' on the delta flips the sign so the
    emitted text matches PLAN.md 5.4's `(current_date() - (X) * INTERVAL 1 DAY)` shape."""
    d = delta.strip()
    if d.startswith("-"):
        return f"({base.strip()} - ({d[1:].strip()}) * INTERVAL 1 {unit})"
    if d.startswith("+"):
        d = d[1:].strip()
    return f"({base.strip()} + ({d}) * INTERVAL 1 {unit})"


# ---------------------------------------------------------------------------------------------
# Optional identity masking, resource names never masked -- a pre-pass on the tokenised body so
# both the DuckDB and Databricks branches get the identical rewrite (vendored text stays as-is).
# ---------------------------------------------------------------------------------------------

_IDENT_EXPR = r"[A-Za-z_][A-Za-z0-9_]*(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*"
_SUBSCRIPT_EXPR = _IDENT_EXPR + r"(?:\s*\[\s*'[^']*'\s*\])?"
_COALESCE2_EXPR = r"COALESCE\s*\(\s*" + _IDENT_EXPR + r"\s*,\s*" + _IDENT_EXPR + r"\s*\)"
_RESOURCE_X = r"(?:" + _COALESCE2_EXPR + r"|" + _SUBSCRIPT_EXPR + r")"

# The vendored library's own resource-name mask (a first-2-chars preview) is never a real privacy
# control (a name is not personal data): dropped unconditionally, for both branches, no setting gates it.
_RESOURCE_MASK_RE = re.compile(
    r"CASE\s+WHEN\s+(?P<x>" + _RESOURCE_X + r")\s+IS\s+NULL\s+THEN\s+(?:(?P=x)|NULL)"
    r"\s+ELSE\s+concat\(\s*substr\(\s*(?P=x)\s*,\s*1\s*,\s*2\s*\)\s*,\s*'\*\*\*\*'\s*\)\s*END",
    re.IGNORECASE,
)


def strip_resource_mask(sql: str) -> str:
    """Every `CASE WHEN x IS NULL THEN x|NULL ELSE concat(substr(x, 1, 2), '****') END` -> `x`.
    Matches against `sql` directly, not `mask(sql)`, since the pattern's own '****' literal would
    otherwise never match its blanked-out form; safe because comment lines are already stripped."""
    return _RESOURCE_MASK_RE.sub(lambda m: m.group("x"), sql)


_GUID_LITERAL = "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"
_REAL_ID_EXPR = r"[A-Za-z_]\w*(?:\s*\(\s*[A-Za-z_][\w.]*\s*\))?"

# DEC-66.3's identity mask (a GUID or NULL/'__REDACTED__' passed through; otherwise an 8-hex-char
# hash + the first 2 raw chars + '***'), with or without a real per-row id ahead of the hash
# (query_costly_statements.sql and its siblings COALESCE one in). Rewritten to one call to the
# `mask_user` dbt macro, which renders the same CASE only when mask_user_identities is on, and the
# bare column otherwise -- making the masking optional instead of always running.
_IDENTITY_MASK_COALESCE_RE = re.compile(
    r"CASE\s+WHEN\s+(?P<x>" + _IDENT_EXPR + r")\s+IS\s+NULL\s+OR\s+(?P=x)\s*=\s*'__REDACTED__'"
    r"\s+THEN\s+(?P=x)\s+WHEN\s+(?P=x)\s+RLIKE\s+" + re.escape(_GUID_LITERAL) + r"\s+THEN\s+(?P=x)"
    r"\s+ELSE\s+concat\(\s*COALESCE\(\s*(?P<real_id>" + _REAL_ID_EXPR + r")\s*,\s*"
    r"substr\(\s*sha2\(\s*lower\(\s*trim\(\s*(?P=x)\s*\)\s*\)\s*,\s*256\s*\)\s*,\s*1\s*,\s*8\s*\)\s*\)"
    r"\s*,\s*' '\s*,\s*substr\(\s*(?P=x)\s*,\s*1\s*,\s*2\s*\)\s*,\s*'\*\*\*'\s*\)\s*END",
    re.IGNORECASE,
)
_IDENTITY_MASK_RE = re.compile(
    r"CASE\s+WHEN\s+(?P<x>" + _IDENT_EXPR + r")\s+IS\s+NULL\s+OR\s+(?P=x)\s*=\s*'__REDACTED__'"
    r"\s+THEN\s+(?P=x)\s+WHEN\s+(?P=x)\s+RLIKE\s+" + re.escape(_GUID_LITERAL) + r"\s+THEN\s+(?P=x)"
    r"\s+ELSE\s+concat\(\s*substr\(\s*sha2\(\s*lower\(\s*trim\(\s*(?P=x)\s*\)\s*\)\s*,\s*256\s*\)"
    r"\s*,\s*1\s*,\s*8\s*\)\s*,\s*' '\s*,\s*substr\(\s*(?P=x)\s*,\s*1\s*,\s*2\s*\)\s*,\s*'\*\*\*'\s*\)"
    r"\s*END",
    re.IGNORECASE,
)


def mask_user_identities(sql: str) -> str:
    """Every DEC-66.3 identity-mask CASE -> `{{ mask_user('x'[, 'real_id']) }}`. Must run before
    translate()/pin_time(): the RLIKE/sha2 it removes would otherwise need a translation rule of
    their own. Matches against `sql` directly, same reason as strip_resource_mask above."""
    sql = _IDENTITY_MASK_COALESCE_RE.sub(
        lambda m: f"{{{{ mask_user('{m.group('x')}', '{m.group('real_id')}') }}}}", sql
    )
    return _IDENTITY_MASK_RE.sub(lambda m: f"{{{{ mask_user('{m.group('x')}') }}}}", sql)


# ---------------------------------------------------------------------------------------------
# rule functions (each takes the whole SQL text and returns it rewritten)
# ---------------------------------------------------------------------------------------------


# ROW_NUMBER() star-dedup -> QUALIFY.  DuckDB 1.5.1 mis-binds the projection taken from a
# derived table that combines `SELECT <alias>.*` with a window function when the outer query
# filters on the window column: the column NAMES come back in the requested order while the
# VALUES come back shifted (T-36, reproduced against the serving.served_entities fixture DDL).
# It is a binder bug, not an optimizer one, and it is silent -- wrong data under right labels.
# Rewriting the outer `WHERE <rn> = 1` into a `QUALIFY` inside the derived table avoids it and
# keeps the `<alias>.*` star, so the rule needs no schema knowledge.  Spark binds the original
# shape correctly, so this runs on the DuckDB branch only (the generator applies only pin_time
# to the Databricks branch, which therefore stays byte-equivalent to the library text).

_ROW_NUMBER_OVER_RE = re.compile(r"(?<![\w.])ROW_NUMBER\s*\(\s*\)\s*OVER\s*\(", re.IGNORECASE)
# the derived table's whole select list up to the window call: exactly `SELECT <alias>.* ,`
_STAR_SELECT_LIST_RE = re.compile(r"\s*SELECT\s+([A-Za-z_]\w*\s*\.\s*\*)\s*,\s*\Z", re.IGNORECASE)
_WINDOW_ALIAS_RE = re.compile(r"\s*(?:AS\s+)?([A-Za-z_]\w*)", re.IGNORECASE)
_EQ_ONE_RE = re.compile(r"\s*=\s*1(?![\w.])")
_NEXT_WORD_RE = re.compile(r"\s*([A-Za-z_]\w*)")
# a word that may not be read as the row-number alias or as the derived table's alias
_NOT_AN_ALIAS = frozenset(
    "AS FROM WHERE GROUP ORDER HAVING QUALIFY LIMIT OFFSET WINDOW UNION EXCEPT INTERSECT "
    "JOIN INNER LEFT RIGHT FULL CROSS NATURAL ON USING".split()
)
# what may legally follow a complete `WHERE <rn> = 1`; anything else (AND, OR, an operator)
# means the outer filter is not only the row-number test, so the clause cannot just be dropped
_CLAUSE_AFTER_WHERE = frozenset(
    "GROUP ORDER HAVING QUALIFY LIMIT OFFSET WINDOW UNION EXCEPT INTERSECT".split()
)


def _r_row_number_star_to_qualify(sql: str) -> str:
    """`(SELECT a.*, ROW_NUMBER() OVER (w) AS rn FROM src a) WHERE rn = 1`
    -> `(SELECT a.* FROM src a QUALIFY ROW_NUMBER() OVER (w) = 1)`.

    Matched only when ALL of these hold -- each one is what makes the rewrite total, i.e. the
    output columns and rows identical -- and anything short of the full shape is left alone:

      * the derived table's select list is exactly `<alias>.*` followed by the ROW_NUMBER call
        and nothing else, so dropping the row-number column restores exactly `<alias>.*`;
      * the window call is aliased (`AS <rn>` or a bare `<rn>`) and `FROM` follows it directly;
      * the very next thing after the derived table's `)` (and an optional table alias) is a
        `WHERE` whose first operand is that `<rn>`.

    Once that last condition holds the rule is committed and raises rather than leave the
    known-bad shape in place: the outer predicate must then be exactly `<rn> = 1` (optionally
    qualified by the derived table's own alias) and must end the WHERE clause, and `<rn>` must
    not be referenced anywhere else, because the rewrite deletes that column outright.

    Not matched, deliberately: an unqualified `SELECT *` star -- the outer WHERE of both bodies
    that use one carries extra conjuncts, so the clause cannot be dropped, and moving them under
    a QUALIFY would change which rows the window sees; a derived table that lists its columns
    explicitly (no `.*`, so no bug); and any body with no outer `WHERE <rn> = 1` (measured
    correct on 1.5.1 -- the bug needs the filter on the window column).
    """
    pos = 0
    while True:
        m_text = mask(sql)
        m = _ROW_NUMBER_OVER_RE.search(m_text, pos)
        if not m:
            return sql
        pos = m.end()                       # default: this occurrence is not the shape
        over_close = _matching_paren(m_text, m.end() - 1)

        # (1) `ROW_NUMBER() OVER (...) [AS] <rn> FROM` -- an unaliased window (the QUALIFY
        #     bodies, which end `) = 1`) or one followed by more select items is not the shape.
        am = _WINDOW_ALIAS_RE.match(m_text, over_close + 1)
        if am is None:
            continue
        rn = sql[am.start(1):am.end(1)]
        if rn.upper() in _NOT_AN_ALIAS:
            continue
        nw = _NEXT_WORD_RE.match(m_text, am.end(1))
        if nw is None or nw.group(1).upper() != "FROM":
            continue

        # (2) the enclosing parenthesis must open a derived table whose select list is exactly
        #     `<alias>.*` plus this window call.
        open_idx = _enclosing_open(m_text, m.start())
        if open_idx is None:
            continue
        sm = _STAR_SELECT_LIST_RE.match(m_text, open_idx + 1, m.start())
        if sm is None:
            continue
        close_idx = _matching_paren(m_text, open_idx)

        # (3) the outer filter must be on that alias, immediately after the derived table.
        wm = re.compile(
            r"\s*(?:(?:AS\s+)?(?!WHERE\b)(?P<t>[A-Za-z_]\w*)\s+)?WHERE\s+"
            r"(?:(?P<q>[A-Za-z_]\w*)\s*\.\s*)?" + re.escape(rn) + r"\b",
            re.IGNORECASE,
        ).match(m_text, close_idx + 1)
        if wm is None:
            continue

        # ---- committed: a shape we recognised but cannot finish is an error, never a skip ----
        where = f"row_number_star_to_qualify ({rn} = 1 over {sql[open_idx:open_idx + 40]!r})"
        talias = wm.group("t")
        qual = wm.group("q")
        if talias is not None and talias.upper() in _NOT_AN_ALIAS:
            raise Untranslatable(
                f"{where}: {talias!r} between the derived table and its WHERE is not a table alias")
        if qual is not None and (talias is None or qual.upper() != talias.upper()):
            raise Untranslatable(
                f"{where}: the outer filter qualifies {rn} with {qual!r}, which is not the "
                "derived table's alias")
        em = _EQ_ONE_RE.match(m_text, wm.end())
        if em is None:
            raise Untranslatable(
                f"{where}: the outer filter on {rn} is not `= 1`; DuckDB 1.5.1 mis-binds this "
                "shape, so it may not be emitted unrewritten")
        nxt = m_text[em.end():].lstrip()
        nxt_word = _NEXT_WORD_RE.match(m_text, em.end())
        if nxt and nxt[0] not in ");" and not (
                nxt_word is not None and nxt_word.group(1).upper() in _CLAUSE_AFTER_WHERE):
            raise Untranslatable(
                f"{where}: the outer WHERE carries more than the row-number test "
                f"({nxt[:40]!r} follows), so it cannot be moved into a QUALIFY unchanged")
        uses = len(re.findall(r"\b" + re.escape(rn) + r"\b", m_text))
        if uses != 2:
            raise Untranslatable(
                f"{where}: {rn} is referenced {uses} times (expected exactly the AS binding and "
                "the outer WHERE); the rewrite deletes that column, so it is not safe here")

        head = sql[open_idx:sm.end(1)]                  # `(\n  SELECT\n    a.*`
        window = sql[m.start():over_close + 1]          # `ROW_NUMBER() OVER (...)`, verbatim
        tail = sql[am.end(1):close_idx]                 # `\n  FROM src a\n`
        body = tail.rstrip()
        if not body.strip():
            raise Untranslatable(f"{where}: the derived table has no FROM clause")
        trailing = tail[len(body):]                     # whitespace before the derived table's )
        last_line = body[body.rfind("\n") + 1:]
        indent = last_line[:len(last_line) - len(last_line.lstrip())]
        sep = "\n" + indent if "\n" in body else " "
        rep = head + body + sep + "QUALIFY " + window + " = 1" + trailing + ")"
        if talias:
            rep += " " + talias
        sql = sql[:open_idx] + rep + sql[em.end():]
        pos = open_idx + len(rep)


def _r_dateadd(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 3:
            raise Untranslatable(f"dateadd with {len(args)} args: dateadd({_inner})")
        return _shift(args[2], args[1], _unit(args[0], "dateadd"))
    return _rewrite_calls(sql, "dateadd", fn)


def _r_date_add(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 2:
            raise Untranslatable(f"date_add with {len(args)} args: date_add({_inner})")
        return _shift(args[0], args[1], "DAY")
    return _rewrite_calls(sql, "date_add", fn)


def _r_date_sub(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 2:
            raise Untranslatable(f"date_sub with {len(args)} args: date_sub({_inner})")
        d = args[1].strip()
        flipped = d[1:].strip() if d.startswith("-") else "-" + d
        return _shift(args[0], flipped, "DAY")
    return _rewrite_calls(sql, "date_sub", fn)


def _r_timestampdiff(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 3:
            raise Untranslatable(f"timestampdiff with {len(args)} args")
        unit = _unit(args[0], "timestampdiff").lower()
        return f"date_diff('{unit}', {args[1]}, {args[2]})"
    return _rewrite_calls(sql, "timestampdiff", fn)


def _r_percentile_approx(sql: str) -> str:
    def fn(args, _inner):
        if len(args) not in (2, 3):
            raise Untranslatable(f"percentile_approx with {len(args)} args")
        return f"quantile_cont({args[0]}, {args[1]})"   # a 3rd (accuracy) arg is dropped
    return _rewrite_calls(sql, "percentile_approx", fn)


def _r_percentile(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 2:
            raise Untranslatable(f"PERCENTILE with {len(args)} args")
        return f"quantile_cont({args[0]}, {args[1]})"
    return _rewrite_calls(sql, "percentile", fn)


def _r_try_element_at(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 2:
            raise Untranslatable(f"try_element_at with {len(args)} args")
        return f"({args[0]}[{args[1]}])"
    return _rewrite_calls(sql, "try_element_at", fn)


def _r_sha2(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 2 or args[1].strip() != "256":
            raise Untranslatable(f"sha2 with bit length other than 256: sha2({_inner})")
        return f"sha256({args[0]})"
    return _rewrite_calls(sql, "sha2", fn)


_DOLLAR_REF = re.compile(r"\$(\d)")


def _r_regexp_replace(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 3:
            raise Untranslatable(f"regexp_replace with {len(args)} args (only the 3-arg form has a rule)")
        replacement = _DOLLAR_REF.sub(lambda m: "\\" + m.group(1), args[2])
        return f"regexp_replace({args[0]}, {args[1]}, {replacement}, 'g')"
    return _rewrite_calls(sql, "regexp_replace", fn)


_SIZE_RE = re.compile(r"(?<![\w.])(?:size|cardinality)\s*\(", re.IGNORECASE)


def _r_size(sql: str) -> str:
    return _sub(_SIZE_RE, sql, lambda m, g: "len(")


_RLIKE_RE = re.compile(r"\bRLIKE\b", re.IGNORECASE)
_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")


def _operand_before(m_text: str, end: int) -> int:
    """Start index of the expression ending just before `end` (an identifier chain, optionally
    a call `f(...)` or a subscript `x[...]`)."""
    k = end
    while k > 0 and m_text[k - 1] in " \t\n":
        k -= 1
    if k == 0:
        raise Untranslatable("RLIKE with no left operand")
    if m_text[k - 1] in ")]":
        closer = m_text[k - 1]
        opener = "(" if closer == ")" else "["
        depth = 0
        j = k - 1
        while j >= 0:
            if m_text[j] == closer:
                depth += 1
            elif m_text[j] == opener:
                depth -= 1
                if depth == 0:
                    break
            j -= 1
        if j < 0:
            raise Untranslatable("RLIKE: unbalanced bracket in left operand")
        k = j
    while k > 0 and m_text[k - 1] in _IDENT_CHARS:
        k -= 1
    return k


def _operand_after(m_text: str, start: int) -> int:
    """End index (exclusive) of the expression starting at/after `start`: a string literal, or
    an identifier chain optionally followed by a balanced call."""
    k = start
    while k < len(m_text) and m_text[k] in " \t\n":
        k += 1
    if k >= len(m_text):
        raise Untranslatable("RLIKE with no right operand")
    if m_text[k] == "'":
        j = m_text.find("'", k + 1)
        if j < 0:
            raise Untranslatable("RLIKE: unterminated string literal")
        return j + 1
    j = k
    while j < len(m_text) and m_text[j] in _IDENT_CHARS:
        j += 1
    if j < len(m_text) and m_text[j] == "(":
        j = _matching_paren(m_text, j) + 1
    if j == k:
        raise Untranslatable("RLIKE: unrecognised right operand")
    return j


def _r_rlike(sql: str) -> str:
    while True:
        m_text = mask(sql)
        m = _RLIKE_RE.search(m_text)
        if not m:
            return sql
        left_start = _operand_before(m_text, m.start())
        right_end = _operand_after(m_text, m.end())
        left = sql[left_start:m.start()].strip()
        right = sql[m.end():right_end].strip()
        sql = sql[:left_start] + f"regexp_matches({left}, {right})" + sql[right_end:]


_UNIX_TS_RE = re.compile(r"(?<![\w.])unix_timestamp\s*\(\s*\)", re.IGNORECASE)
_UNIX_TS_ARG_RE = re.compile(r"(?<![\w.])unix_timestamp\s*\(", re.IGNORECASE)


def _r_unix_timestamp(sql: str) -> str:
    sql = _sub(_UNIX_TS_RE, sql, lambda m, g: "epoch(current_timestamp())")
    return _sub(_UNIX_TS_ARG_RE, sql, lambda m, g: "epoch(")


_UNIX_MILLIS_RE = re.compile(r"(?<![\w.])unix_millis\s*\(", re.IGNORECASE)


def _r_unix_millis(sql: str) -> str:
    return _sub(_UNIX_MILLIS_RE, sql, lambda m, g: "epoch_ms(")


def _r_datediff(sql: str) -> str:
    def fn(args, _inner):
        if len(args) == 2:
            return f"date_diff('day', {args[1]}, {args[0]})"
        if len(args) == 3:
            return f"date_diff('{_unit(args[0], 'datediff').lower()}', {args[1]}, {args[2]})"
        raise Untranslatable(f"datediff with {len(args)} args")
    return _rewrite_calls(sql, "datediff", fn)


def _r_date_cast(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 1:
            raise Untranslatable(f"DATE() with {len(args)} args")
        return f"CAST({args[0]} AS DATE)"
    return _rewrite_calls(sql, "date", fn)


def _r_collect_set(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 1:
            raise Untranslatable(f"collect_set with {len(args)} args")
        return f"list_distinct(list({args[0]}))"
    return _rewrite_calls(sql, "collect_set", fn)


def _r_sort_array(sql: str) -> str:
    def fn(args, _inner):
        if len(args) != 1:
            raise Untranslatable(f"sort_array with {len(args)} args")
        return f"list_sort({args[0]})"
    return _rewrite_calls(sql, "sort_array", fn)


_ARRAY_JOIN_RE = re.compile(r"(?<![\w.])array_join\s*\(", re.IGNORECASE)


def _r_array_join(sql: str) -> str:
    return _sub(_ARRAY_JOIN_RE, sql, lambda m, g: "array_to_string(")


_LATERAL_VIEW_RE = re.compile(
    r"\bLATERAL\s+VIEW\s+(OUTER\s+)?explode\s*\(([^()]*)\)\s+(\w+)\s+AS\s+(\w+)(?:\s*,\s*(\w+))?",
    re.IGNORECASE,
)


def _r_lateral_view(sql: str) -> str:
    def repl(m, g):
        outer, expr, alias, c1, c2 = g
        join = "LEFT JOIN LATERAL" if outer else "JOIN LATERAL"
        if c2:
            sub = (f"SELECT e.key AS {c1}, e.value AS {c2} "
                   f"FROM unnest(map_entries({expr.strip()})) AS x(e)")
        else:
            sub = f"SELECT unnest({expr.strip()}) AS {c1}"
        return f"{join} ({sub}) {alias} ON TRUE"
    return _sub(_LATERAL_VIEW_RE, sql, repl)


_EXPLODE_RE = re.compile(r"(?<![\w.])explode\s*\(", re.IGNORECASE)


def _r_explode(sql: str) -> str:
    return _sub(_EXPLODE_RE, sql, lambda m, g: "UNNEST(")


_INTERVAL_LITERAL_RE = re.compile(
    r"\bINTERVAL\s+(\d+)\s+(DAY|HOUR|MINUTE|SECOND|WEEK|MONTH|YEAR)S\b", re.IGNORECASE
)


def _r_interval_literal(sql: str) -> str:
    return _sub(_INTERVAL_LITERAL_RE, sql, lambda m, g: f"INTERVAL {g[0]} {g[1].upper()}")


_INTERVAL_EXPR_RE = re.compile(
    r"\bINTERVAL\s+(__W__|__P__\w+?__|\([^()]*\))\s+(DAY|HOUR|MINUTE|SECOND|WEEK|MONTH|YEAR)S?\b",
    re.IGNORECASE,
)


def _r_interval_expr(sql: str) -> str:
    return _sub(_INTERVAL_EXPR_RE, sql, lambda m, g: f"(({g[0]}) * INTERVAL 1 {g[1].upper()})")


_CURRENT_DATE_RE = re.compile(r"(?<![\w.])current_date(?:\s*\(\s*\))?(?![\w(])", re.IGNORECASE)
_CURRENT_TS_RE = re.compile(r"(?<![\w.])current_timestamp(?:\s*\(\s*\))?(?![\w(])", re.IGNORECASE)
_NOW_RE = re.compile(r"(?<![\w.])now\s*\(\s*\)", re.IGNORECASE)


def pin_time(sql: str) -> str:
    """`current_date()` -> `{{ audit_today() }}` and `current_timestamp()` -> `{{ audit_now() }}`
    (the bare, parenthesis-less spellings too), outside literals and comments. PLAN.md 5.3 step 4:
    the generator applies exactly this function on its own to the Databricks branch, and it is
    also (with `now()`) the last rule of the DuckDB table."""
    sql = _sub(_CURRENT_DATE_RE, sql, lambda m, g: "{{ audit_today() }}")
    return _sub(_CURRENT_TS_RE, sql, lambda m, g: "{{ audit_now() }}")


def _r_pin_time(sql: str) -> str:
    return pin_time(sql)


def _r_now(sql: str) -> str:
    return _sub(_NOW_RE, sql, lambda m, g: "{{ audit_now() }}")


# ---------------------------------------------------------------------------------------------
# the rule table
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    name: str
    describe: str
    example_in: str      # the exact Databricks form found in a vendored body (sentinel form)
    example_out: str     # what translate() emits for it (after the WHOLE table has run)
    fn: Callable[[str], str]


RULES: list[Rule] = [
    Rule("row_number_star_to_qualify",
         "(SELECT a.*, ROW_NUMBER() OVER (w) AS rn FROM src a) WHERE rn = 1 -> "
         "(SELECT a.* FROM src a QUALIFY ROW_NUMBER() OVER (w) = 1); DuckDB 1.5.1 mis-binds the "
         "star + window + outer-filter shape and returns shifted VALUES under the right column "
         "NAMES (T-36). A statement-shape rule, applied before the function rules",
         "FROM (\n"
         "    SELECT\n"
         "      se.*,\n"
         "      ROW_NUMBER() OVER (\n"
         "        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id\n"
         "        ORDER BY se.change_time DESC\n"
         "      ) AS _rn\n"
         "    FROM system.serving.served_entities se\n"
         "  )\n"
         "  WHERE _rn = 1",
         "FROM (\n"
         "    SELECT\n"
         "      se.*\n"
         "    FROM system.serving.served_entities se\n"
         "    QUALIFY ROW_NUMBER() OVER (\n"
         "        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id\n"
         "        ORDER BY se.change_time DESC\n"
         "      ) = 1\n"
         "  )",
         _r_row_number_star_to_qualify),
    Rule("dateadd",
         "dateadd(unit, -X, base) -> (base - (X) * INTERVAL 1 UNIT); X may be __W__, __P__x__, a "
         "number, CAST(...) or LEAST(...); day and hour forms",
         "dateadd(day, -LEAST(__W__, 90), current_date())",
         "({{ audit_today() }} - (LEAST(__W__, 90)) * INTERVAL 1 DAY)",
         _r_dateadd),
    Rule("date_add",
         "date_add(base, -X) -> (base - (X) * INTERVAL 1 DAY)",
         "date_add(current_date(), -CAST(__W__ AS INT))",
         "({{ audit_today() }} - (CAST(__W__ AS INT)) * INTERVAL 1 DAY)",
         _r_date_add),
    Rule("date_sub",
         "date_sub(base, X) -> (base - (X) * INTERVAL 1 DAY)",
         "date_sub(current_date(), __W__)",
         "({{ audit_today() }} - (__W__) * INTERVAL 1 DAY)",
         _r_date_sub),
    Rule("timestampdiff",
         "timestampdiff(SECOND, a, b) -> date_diff('second', a, b)",
         "timestampdiff(SECOND, run_start, CASE WHEN result_state IS NULL THEN current_timestamp() ELSE last_seen END)",
         "date_diff('second', run_start, CASE WHEN result_state IS NULL THEN {{ audit_now() }} ELSE last_seen END)",
         _r_timestampdiff),
    Rule("percentile_approx",
         "percentile_approx(x, p) -> quantile_cont(x, p)",
         "percentile_approx(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5)",
         "quantile_cont(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5)",
         _r_percentile_approx),
    Rule("percentile",
         "PERCENTILE(x, p) -> quantile_cont(x, p)",
         "PERCENTILE(setup_duration_seconds, 0.95)",
         "quantile_cont(setup_duration_seconds, 0.95)",
         _r_percentile),
    Rule("try_element_at",
         "try_element_at(x, n) -> (x[n]) and try_element_at(m, 'k') -> (m['k']); 1-based, NULL out of range",
         "MAX(try_element_at(t.compute, 1).type)",
         "MAX((t.compute[1]).type)",
         _r_try_element_at),
    Rule("sha2",
         "sha2(x, 256) -> sha256(x)",
         "sha2(s.statement_text_devalued, 256)",
         "sha256(s.statement_text_devalued)",
         _r_sha2),
    Rule("regexp_replace",
         "regexp_replace(a, b, c) -> regexp_replace(a, b, c, 'g') with $N -> \\N in the replacement",
         "regexp_replace(s.statement_text_devalued, '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '$1?')",
         "regexp_replace(s.statement_text_devalued, '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '\\1?', 'g')",
         _r_regexp_replace),
    Rule("size",
         "size( / cardinality( -> len(",
         "MAX(CASE WHEN t.compute_ids IS NULL THEN NULL ELSE size(t.compute_ids) END)",
         "MAX(CASE WHEN t.compute_ids IS NULL THEN NULL ELSE len(t.compute_ids) END)",
         _r_size),
    Rule("rlike",
         "X RLIKE 'p' -> regexp_matches(X, 'p'); X is an identifier chain, a call or a subscript",
         "WHEN identity_metadata.run_as RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}$' THEN identity_metadata.run_as",
         "WHEN regexp_matches(identity_metadata.run_as, '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}$') THEN identity_metadata.run_as",
         _r_rlike),
    Rule("unix_timestamp",
         "unix_timestamp( -> epoch(  (unix_timestamp() -> epoch(current_timestamp()))",
         "unix_timestamp(next_event_time) - unix_timestamp(event_time)",
         "epoch(next_event_time) - epoch(event_time)",
         _r_unix_timestamp),
    Rule("unix_millis",
         "unix_millis( -> epoch_ms(",
         "unix_millis(end_time) - unix_millis(start_time)",
         "epoch_ms(end_time) - epoch_ms(start_time)",
         _r_unix_millis),
    Rule("datediff",
         "datediff(a, b) -> date_diff('day', b, a)",
         "datediff(current_date(), DATE(inv.last_altered))",
         "date_diff('day', CAST(inv.last_altered AS DATE), {{ audit_today() }})",
         _r_datediff),
    Rule("date_cast",
         "DATE(x) -> CAST(x AS DATE)",
         "u.usage_date >= DATE(dp.price_start_time)",
         "u.usage_date >= CAST(dp.price_start_time AS DATE)",
         _r_date_cast),
    Rule("collect_set",
         "collect_set(x) -> list_distinct(list(x))",
         "array_join(collect_set(concat(st.TAG_NAME, '=', st.TAG_VALUE)), ', ')",
         "array_to_string(list_distinct(list(concat(st.TAG_NAME, '=', st.TAG_VALUE))), ', ')",
         _r_collect_set),
    Rule("sort_array",
         "sort_array(x) -> list_sort(x)",
         "array_join(sort_array(collect_set(TAG_NAME)), ', ')",
         "array_to_string(list_sort(list_distinct(list(TAG_NAME))), ', ')",
         _r_sort_array),
    Rule("array_join",
         "array_join( -> array_to_string(",
         "array_join(collect_set(TAG_NAME), ', ') AS tag_names",
         "array_to_string(list_distinct(list(TAG_NAME)), ', ') AS tag_names",
         _r_array_join),
    Rule("lateral_view",
         "LATERAL VIEW OUTER explode(m) t AS k, v -> LEFT JOIN LATERAL (SELECT e.key AS k, e.value AS v "
         "FROM unnest(map_entries(m)) AS x(e)) t ON TRUE (one query; flagged for review)",
         "FROM system.billing.usage\n     LATERAL VIEW OUTER explode(custom_tags) t AS tag_key, tag_value",
         "FROM system.billing.usage\n     LEFT JOIN LATERAL (SELECT e.key AS tag_key, e.value AS tag_value "
         "FROM unnest(map_entries(custom_tags)) AS x(e)) t ON TRUE",
         _r_lateral_view),
    Rule("explode",
         "select-list EXPLODE(arr) AS c -> UNNEST(arr) AS c",
         "SELECT workspace_id, job_id, run_id, task_key, EXPLODE(compute_ids) AS compute_id",
         "SELECT workspace_id, job_id, run_id, task_key, UNNEST(compute_ids) AS compute_id",
         _r_explode),
    Rule("interval_literal",
         "INTERVAL N DAYS -> INTERVAL N DAY (literal N)",
         "event_time >= current_timestamp() - INTERVAL 7 DAYS",
         "event_time >= {{ audit_now() }} - INTERVAL 7 DAY",
         _r_interval_literal),
    Rule("interval_expr",
         "INTERVAL X DAYS? -> ((X) * INTERVAL 1 DAY) for a token or parenthesised X",
         "event_time >= current_timestamp() - INTERVAL __W__ DAYS",
         "event_time >= {{ audit_now() }} - ((__W__) * INTERVAL 1 DAY)",
         _r_interval_expr),
    Rule("pin_time",
         "current_date() -> {{ audit_today() }}, current_timestamp() -> {{ audit_now() }} (last)",
         "usage_date >= current_date() - INTERVAL __W__ DAYS AND usage_date < current_date()",
         "usage_date >= {{ audit_today() }} - ((__W__) * INTERVAL 1 DAY) AND usage_date < {{ audit_today() }}",
         _r_pin_time),
    Rule("now",
         "now() -> {{ audit_now() }}",
         "period_end_time < date_trunc('DAY', now())",
         "period_end_time < date_trunc('DAY', {{ audit_now() }})",
         _r_now),
]

# Databricks-only names with no rule above: their survival after the table has run is an error,
# never a silent pass-through to a DuckDB binder failure at build time. Verified absent from
# DuckDB 1.5.1 (tools probe, 2026-09-22); the names a rule rewrites are listed too so that a form
# the rule refused (e.g. a 4-arg regexp_replace) is still caught.
_UNSUPPORTED_FUNCTIONS = (
    "dateadd", "date_sub", "timestampdiff", "timestampadd", "percentile_approx", "percentile",
    "approx_percentile", "try_element_at", "sha2", "size", "unix_timestamp", "collect_set",
    "collect_list", "array_join", "explode", "posexplode", "named_struct", "from_unixtime",
    "date_format", "to_date", "get_json_object", "transform", "locate", "nvl", "nvl2",
    "array_size", "array_max", "array_min", "slice", "inline", "stack", "months_between",
    "add_months", "next_day", "initcap", "format_number", "format_string", "soundex",
    "xxhash64", "crc32", "unbase64", "assert_true", "raise_error", "rand", "randn",
    "monotonically_increasing_id", "try_divide", "try_add", "try_subtract", "try_multiply",
    "pmod", "width_bucket", "expm1", "log1p", "hypot", "signum", "conv", "bround", "rint",
    "nanvl", "isnull", "isnotnull", "every", "count_min_sketch", "histogram_numeric",
    "timestamp_seconds", "timestamp_millis", "timestamp_micros", "unix_seconds", "unix_millis",
    "unix_micros", "unix_date", "date_from_unix_date", "make_interval", "to_unix_timestamp",
    "to_utc_timestamp", "from_utc_timestamp", "current_timezone", "convert_timezone",
)
_UNSUPPORTED_FUNC_RE = re.compile(
    r"(?<![\w.])(" + "|".join(_UNSUPPORTED_FUNCTIONS) + r")\s*\(", re.IGNORECASE
)
_UNSUPPORTED_KEYWORD_RE = re.compile(
    r"\b(RLIKE|LATERAL\s+VIEW|DISTRIBUTE\s+BY|CLUSTER\s+BY|SORT\s+BY)\b", re.IGNORECASE
)


def untranslated(sql: str) -> list[str]:
    """Every Databricks-only construct still present (outside literals/comments) after the rule
    table has run; empty means translate() may return the text."""
    m_text = mask(sql)
    hits = [m.group(1) + "(" for m in _UNSUPPORTED_FUNC_RE.finditer(m_text)]
    hits += [m.group(1) for m in _UNSUPPORTED_KEYWORD_RE.finditer(m_text)]
    return hits


def translate(sql: str) -> str:
    """Databricks SQL (sentinel form) -> DuckDB SQL with the as-of macros. Raises Untranslatable
    for any construct no rule covers."""
    for rule in RULES:
        sql = rule.fn(sql)
    left = untranslated(sql)
    if left:
        raise Untranslatable("no translation rule for: " + ", ".join(sorted(set(left))))
    return sql


def rules_table() -> str:
    """The RULES table as a plain-text table (name | example in -> out), for hand-off notes."""
    lines = []
    for r in RULES:
        lines.append(f"{r.name}: {r.describe}")
        lines.append(f"    in : {r.example_in!r}")
        lines.append(f"    out: {r.example_out!r}")
    return "\n".join(lines)
