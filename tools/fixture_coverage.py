#!/usr/bin/env python3
"""tools/fixture_coverage.py -- T-24: the P2 exit gate's own coverage check (PLAN.md 7.1).

    python tools/fixture_coverage.py          human-readable report, exit 0
    python tools/fixture_coverage.py --check  exit 0 iff every executable query id has BOTH a
                                               grain entry and a pytest assertion; else list the
                                               offenders and exit 1

For every `executable` id in `app.core.registry.load_registry()` (109 today: the 3 DESCRIBE/
ANALYZE templates are excluded because `executable` is False for them, never via a hard-coded id
list -- see registry.py's own `executable = runnable AND reads[0] is not "none - ..."`), this
confirms:

  (a) a grain entry: some `config/grains/*.yml` file has a top-level key equal to the id (PLAN.md
      5.5 / DEC-24: `unique_grain: {columns: [window_days] + grain}`).
  (b) a pytest assertion: some `tests/test_findings/**/*.py` module calls `rows(...)` (imported
      directly, `from tests.dbutil import rows`) or `dbutil.rows(...)` with that id as the
      `query_id` argument -- see "What counts as a genuine reference" below.

Two extra structural checks, both reported as errors in `--check` (see PLAN.md 7.1 / this task's
own Quality requirements 2 and 4):

  (c) duplicate grains: the same query id defined in more than one `config/grains/*.yml` file.
      The grain files are deliberately split one-per-batch so no two tasks edit the same file; a
      duplicate means two different grains are silently racing in the generated dbt yml.
  (d) grain/model column drift: a grain's column list does not match the SAME id's
      `unique_grain` test columns in the generated `dbt/models/findings/**/_findings__*.yml`
      (skipped, with a note, if that generated file does not exist yet -- it is itself a build
      output, not something this script produces).

What counts as a genuine reference (quality requirement 1)
------------------------------------------------------------------------------------------------
This module parses every test file with `ast.parse` (stdlib) instead of grepping text, so a
match requires an actual `Call` node whose function is named `rows` or ends in `.rows` (i.e.
`rows(...)` or `dbutil.rows(...)`), with the id as either the first positional argument or a
`query_id=` keyword argument, in one of three shapes actually used under tests/test_findings/:

  1. a literal directly in the call:          dbutil.rows("some_id", 30)
  2. a local variable assigned the literal
     earlier in the SAME function (or at
     module level), then passed by name:      qid = "some_id"
                                               ...
                                               dbutil.rows(qid, 30)
  3. a `for` loop over a module/function-level list (or tuple/set) literal of ids, with the loop
     variable then passed to a `rows(...)` call inside the loop body:
                                               ALL_IDS = ["a", "b"]
                                               for qid in ALL_IDS:
                                                   dbutil.rows(qid, 30)

Shapes 2 and 3 are real, load-bearing conventions in this test suite (e.g.
tests/test_findings/test_overview_dbu_by_sku.py uses shape 2 exclusively for its one id;
tests/test_findings/test_compute_activity.py uses shape 3 for its cross-id checks). A textual
`query_id in open(file).read()` scan -- or a regex that only understands shape 1 -- would silently
mark ids that only ever appear in shape 2/3 as "missing pytest assertion", which is exactly the
kind of false negative this gate must not produce.

Because matching requires a real `ast.Call` node, an id that appears ONLY in a comment, a
docstring, a module header, or a `# next: <id>`-style prose reference is never counted: comments
are not part of the AST at all, and prose inside a docstring is a bare `Expr` statement, never a
variable assignment that later flows into a `rows(...)` call. tests/test_fixture_coverage.py
`test_comment_only_mention_is_not_a_reference` proves exactly this case over a fixture-level fake
module.

What this does NOT catch (documented limitation, not a bug): a variable that is assigned the
right id in one function but only ever passed to `rows()` from a DIFFERENT function of the same
name (shadowing across two functions) would be a false positive in principle; no case like that
exists in tests/test_findings/ today. Dynamically constructed ids (f-strings, string
concatenation, `getattr`-style indirection) are never resolved and correctly count as no
reference -- the same conservative-by-construction choice the registry itself makes for `next`
tokens.

Stdlib only, plus PyYAML (already a project dependency; config/grains/*.yml and the generated
dbt yml files are read with it, exactly like tools/generate_models.py does).
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.registry import load_registry  # noqa: E402

GRAINS_DIR = ROOT / "config" / "grains"
TEST_FINDINGS_DIR = ROOT / "tests" / "test_findings"
FINDINGS_MODELS_DIR = ROOT / "dbt" / "models" / "findings"


# --------------------------------------------------------------------------------------------
# (1) the authoritative id list -- app.core.registry's own `executable` rule, never a hard-coded
#     list (quality requirement 3).
# --------------------------------------------------------------------------------------------
def executable_ids() -> list[str]:
    return sorted(s.query_id for s in load_registry() if s.executable)


# --------------------------------------------------------------------------------------------
# (2) grains: config/grains/*.yml -> {query_id: columns}, plus every file that defines each id
#     (duplicate detection, quality requirement 2).
# --------------------------------------------------------------------------------------------
def load_grains(grains_dir: Path) -> tuple[dict[str, list], dict[str, list[Path]]]:
    """Returns (query_id -> its grain columns, from the FIRST file that defines it; query_id ->
    every config/grains/*.yml file that defines it, in filename order). A grain "entry" is any
    top-level mapping key in a *.yml file under grains_dir (PLAN.md 5.5: the filename does not
    matter, only that SOME grains file names the id)."""
    columns_by_id: dict[str, list] = {}
    files_by_id: dict[str, list[Path]] = {}
    if not grains_dir.exists():
        return columns_by_id, files_by_id
    for path in sorted(grains_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        for qid, cols in data.items():
            if not isinstance(qid, str):
                continue
            files_by_id.setdefault(qid, []).append(path)
            columns_by_id.setdefault(qid, cols)
    return columns_by_id, files_by_id


def duplicate_grains(files_by_id: dict[str, list[Path]]) -> dict[str, list[Path]]:
    return {qid: files for qid, files in files_by_id.items() if len(files) > 1}


# --------------------------------------------------------------------------------------------
# (3) pytest assertions: tests/test_findings/**/*.py -> the set of ids each module genuinely
#     references via a rows(...) / dbutil.rows(...) call (quality requirement 1 -- see the module
#     docstring above for exactly what counts).
# --------------------------------------------------------------------------------------------
def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_rows_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "rows"
    if isinstance(func, ast.Attribute):
        return func.attr == "rows"
    return False


def _collect_scope_bindings(
    stmts: list[ast.stmt],
    str_assigns: dict[str, set[str]],
    list_assigns: dict[str, set[str]],
    for_bindings: dict[str, set[str]],
) -> None:
    """Mutates the three dicts in place: `name = "literal"` assignments, `name = [...]` /
    `name = (...)` / `name = {...}` literal-of-strings assignments, and `for x in name:` loop
    bindings resolved against list_assigns seen so far (source order, matching how Python actually
    executes). Descends into nested if/for/while/with/try bodies, but NOT into a nested
    function/class def -- that is its own separate scope, walked separately by the caller."""
    for stmt in stmts:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            name = stmt.targets[0].id
            v = _const_str(stmt.value)
            if v is not None:
                str_assigns.setdefault(name, set()).add(v)
            elif isinstance(stmt.value, (ast.List, ast.Tuple, ast.Set)):
                items = {_const_str(e) for e in stmt.value.elts}
                items.discard(None)
                if items:
                    list_assigns.setdefault(name, set()).update(items)  # type: ignore[arg-type]
        if (
            isinstance(stmt, ast.For)
            and isinstance(stmt.target, ast.Name)
            and isinstance(stmt.iter, ast.Name)
        ):
            items = list_assigns.get(stmt.iter.id)
            if items:
                for_bindings.setdefault(stmt.target.id, set()).update(items)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            child = getattr(stmt, field, None)
            if isinstance(child, list):
                _collect_scope_bindings(child, str_assigns, list_assigns, for_bindings)


def extract_referenced_ids(text: str, filename: str) -> set[str]:
    """Every query id this module genuinely passes to a rows(...)/dbutil.rows(...) call (shapes
    1-3 from the module docstring). A file that fails to parse contributes no ids rather than
    raising -- this tool reports a coverage gap, it does not double as a syntax checker."""
    try:
        tree = ast.parse(text, filename=filename)
    except SyntaxError:
        return set()

    referenced: set[str] = set()

    def scan_calls(node: ast.AST, str_assigns: dict[str, set[str]], for_bindings: dict[str, set[str]]) -> None:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call) or not _is_rows_call(sub):
                continue
            arg: ast.expr | None = sub.args[0] if sub.args else None
            if arg is None:
                for kw in sub.keywords:
                    if kw.arg == "query_id":
                        arg = kw.value
                        break
            if arg is None:
                continue
            v = _const_str(arg)
            if v is not None:
                referenced.add(v)
            elif isinstance(arg, ast.Name):
                referenced.update(str_assigns.get(arg.id, ()))
                referenced.update(for_bindings.get(arg.id, ()))

    module_str: dict[str, set[str]] = {}
    module_list: dict[str, set[str]] = {}
    module_for: dict[str, set[str]] = {}
    _collect_scope_bindings(tree.body, module_str, module_list, module_for)

    top_level = [
        s for s in tree.body
        if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    scan_calls(ast.Module(body=top_level, type_ignores=[]), module_str, module_for)

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        str_assigns = {k: set(v) for k, v in module_str.items()}
        list_assigns = {k: set(v) for k, v in module_list.items()}
        for_bindings = {k: set(v) for k, v in module_for.items()}
        _collect_scope_bindings(fn.body, str_assigns, list_assigns, for_bindings)
        scan_calls(fn, str_assigns, for_bindings)

    return referenced


def load_tested_ids(test_findings_dir: Path) -> set[str]:
    """Union, over every tests/test_findings/**/*.py file, of extract_referenced_ids()."""
    tested: set[str] = set()
    if not test_findings_dir.exists():
        return tested
    for path in sorted(test_findings_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        tested.update(extract_referenced_ids(text, str(path)))
    return tested


# --------------------------------------------------------------------------------------------
# (4) grain/model column drift (quality requirement 4): the generated dbt yml's own
#     `unique_grain` test columns (window_days prepended, DEC-24) vs. the grain file's columns.
# --------------------------------------------------------------------------------------------
def load_model_grain_columns(findings_models_dir: Path) -> dict[str, list] | None:
    """query_id -> its `unique_grain` test's `columns` list (including the leading window_days),
    read from every dbt/models/findings/**/_findings__*.yml. Returns None if no such file exists
    yet (a checkout before the generator has run) -- that is a "not applicable yet" state, not a
    failure, matching how tools/gate.py's own steps skip a not-yet-built dependency."""
    paths = sorted(findings_models_dir.glob("**/_findings__*.yml"))
    if not paths:
        return None
    result: dict[str, list] = {}
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for model in data.get("models", []):
            meta = model.get("meta", {})
            qid = meta.get("query_id")
            if not qid:
                continue
            for dt in model.get("data_tests", []):
                if isinstance(dt, dict) and "unique_grain" in dt:
                    cols = dt["unique_grain"].get("arguments", {}).get("columns")
                    if cols is not None:
                        result[qid] = cols
    return result


def grain_column_mismatches(
    grain_columns: dict[str, list], model_columns: dict[str, list] | None
) -> list[tuple[str, list, list]]:
    """[(query_id, grain_file_columns, model_columns_sans_window_days), ...] for every id present
    in BOTH maps whose columns disagree. Only meaningful once model_columns is not None (the
    generated yml exists) -- a grain with no matching generated model yet is not a mismatch, it
    just has nothing to check against."""
    if model_columns is None:
        return []
    mismatches = []
    for qid, grain_cols in grain_columns.items():
        model_cols = model_columns.get(qid)
        if model_cols is None:
            continue
        expected = model_cols[1:] if model_cols and model_cols[0] == "window_days" else model_cols
        if list(grain_cols) != list(expected):
            mismatches.append((qid, list(grain_cols), list(expected)))
    return mismatches


# --------------------------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------------------------
def _display_path(p: Path) -> str:
    """p relative to ROOT when possible (the normal case: every real grain file lives under
    config/grains/), else str(p) unchanged -- so a fabricated path from a unit test's own tmp_path
    tree (never under ROOT) does not raise instead of just printing its own path."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


class Report:
    def __init__(
        self,
        ids: list[str],
        grain_columns: dict[str, list],
        grain_files: dict[str, list[Path]],
        tested_ids: set[str],
        model_columns: dict[str, list] | None,
    ):
        self.ids = ids
        self.has_grain = {qid: qid in grain_columns for qid in ids}
        self.has_test = {qid: qid in tested_ids for qid in ids}
        self.duplicates = duplicate_grains(grain_files)
        self.mismatches = grain_column_mismatches(grain_columns, model_columns)
        self.model_columns_available = model_columns is not None

    @property
    def covered_count(self) -> int:
        return sum(1 for qid in self.ids if self.has_grain[qid] and self.has_test[qid])

    @property
    def total(self) -> int:
        return len(self.ids)

    def summary_line(self) -> str:
        return f"fixture coverage: {self.covered_count}/{self.total} models have grain + pytest assertions"

    def offender_lines(self) -> list[str]:
        lines: list[str] = []
        for qid in self.ids:
            g, t = self.has_grain[qid], self.has_test[qid]
            if not g and not t:
                lines.append(f"{qid}: missing both")
            elif not g:
                lines.append(f"{qid}: missing grain")
            elif not t:
                lines.append(f"{qid}: missing pytest assertion")
        for qid in sorted(self.duplicates):
            files = ", ".join(_display_path(p) for p in self.duplicates[qid])
            lines.append(f"{qid}: duplicate grain defined in {files}")
        for qid, grain_cols, model_cols in sorted(self.mismatches):
            lines.append(
                f"{qid}: grain columns {grain_cols} do not match generated model's "
                f"unique_grain columns {model_cols}"
            )
        return lines

    def ok(self) -> bool:
        return self.covered_count == self.total and not self.duplicates and not self.mismatches


def build_report() -> Report:
    ids = executable_ids()
    grain_columns, grain_files = load_grains(GRAINS_DIR)
    tested_ids = load_tested_ids(TEST_FINDINGS_DIR)
    model_columns = load_model_grain_columns(FINDINGS_MODELS_DIR)
    return Report(ids, grain_columns, grain_files, tested_ids, model_columns)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--check", action="store_true",
        help="exit 1 and list offenders if any executable id is missing a grain, a pytest "
             "assertion, has a duplicate grain, or a grain/model column mismatch",
    )
    args = ap.parse_args(argv)

    report = build_report()

    if args.check:
        if report.ok():
            print(report.summary_line())
            return 0
        for line in report.offender_lines():
            print(line)
        return 1

    print(report.summary_line())
    if not report.model_columns_available:
        print("note: dbt/models/findings/**/_findings__*.yml not found yet -- "
              "grain/model column check (d) skipped")
    for line in report.offender_lines():
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
