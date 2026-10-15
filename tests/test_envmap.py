"""tests/test_envmap.py -- T-23.

(a) Function-level parity between dbt/macros/classify_env.sql and app/core/envmap.py over a
    shared table of name -> expected env, covering every word in every bucket of
    dbt/dbt_project.yml's `vars.env_patterns` plus the normalisation rule (`[-_./]+` -> space)
    plus a non-ASCII name (RE2 vs Python `\b` parity, T-23 fix round) plus an unrecognisable name
    -> 'unknown' (DEC-22's "unknown id -> unknown" is proven here, at function level, since this
    task does not own tests/fixtures/billing.py and cannot add a 4th real workspace whose name
    matches nothing). The dbt side is executed for real via `dbt show --inline --output json`
    against the `test` target (not just compiled -- this needs the actual classification value,
    not the rendered SQL text), the same DBT_ARGS convention tests/test_dbt_skeleton.py uses. No
    mocking of dbt itself.

(b)/(c) Reading the built dims.dim_workspace table (via a read-only DuckDB connection to
    tests/db_audit_test.duckdb, tests/dbutil.py's DB_PATH -- dims.* is a different schema than
    tests/dbutil.py's own rows() helper reads, so this file opens its own read-only connection
    instead): the 3 shared fixture workspaces (tests/fixtures/billing.py: 1111 acme-prod, 2222
    acme-dev, 3333 acme-uat) classify prod/dev/uat by name, EXCEPT 2222, which
    dbt/seeds/workspace_env_overrides.csv overrides to 'uat' (env_source 'override') -- proving a
    seed override wins over the name-derived classification. The drill-down workspace 9001
    dd-prod (tests/fixtures/drilldown.py) also classifies prod by name.

Requires `python tests/fixtures/build_fixtures.py` followed by `python tools/dbt_run.py build
--target test` (or, for just these tables, `python tools/dbt_run.py build --target test --select
"+dim_workspace dim_list_prices"` -- the `+dim_workspace` selector pulls in the
workspace_env_overrides seed too, since dim_workspace refs it) to have already been run -- this
file only reads the result, it does not build it (same division of responsibility as
tests/dbutil.py).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
DBT_ARGS = ["--project-dir", "dbt", "--profiles-dir", "dbt"]
DB_PATH = ROOT / "tests" / "db_audit_test.duckdb"

# Other tasks' reviewer/writer agents build dbt and open tests/db_audit_test.duckdb concurrently
# in this checkout (see tasks/T-23-dims-models-and-seeds.md's Hand-off notes / the orchestrator's
# working rules); a `dbt` subprocess or a DuckDB connection can transiently fail with a "being
# used by another process" IO error that has nothing to do with this test's own correctness. Both
# helpers below retry on exactly that message before giving up, so this file does not flake under
# the documented concurrent-build environment.
_LOCK_RETRY_ATTEMPTS = 8
_LOCK_RETRY_DELAY_S = 8
_LOCK_MARKER = "being used by another process"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


envmap = _load(ROOT / "app" / "core" / "envmap.py", "envmap_under_test")


def _dbt(*args: str) -> subprocess.CompletedProcess:
    """Runs `dbt <args> <DBT_ARGS>`, retrying on the transient "file is being used by another
    process" IO error a concurrent dbt build elsewhere in this checkout can cause (see the
    _LOCK_RETRY_* module comment above)."""
    last: subprocess.CompletedProcess | None = None
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        result = subprocess.run(
            ["dbt", *args, *DBT_ARGS],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _LOCK_MARKER not in (result.stdout + result.stderr):
            return result
        last = result
        time.sleep(_LOCK_RETRY_DELAY_S)
    return last


# ---------------------------------------------------------------------------------------------
# Shared name -> expected-env table. Covers every word in every bucket of
# dbt/dbt_project.yml's vars.env_patterns (prod: prod/prd/production/live; uat: uat/test/qa/
# stag/staging/stage/sandbox/sit/nonprod/preprod; dev: dev/devel -- DEC-49 added staging/stage
# to uat, since "stag" alone never word-boundary-matches "staging" or "stage"), the
# `[-_./]+` -> space normalisation rule with each of the four separator characters, mixed case,
# a near-miss that must NOT match via word-boundary ("notprodx" contains "prod" but not as a
# whole word), a non-ASCII name (T-23 fix round: Python's `\b` is Unicode-aware by default while
# DuckDB's RE2 `\b` is ASCII-only -- a name with a U+00FC letter classified 'prod' via dbt/RE2
# but 'unknown' via a plain Python `\b` before `app/core/envmap.py`'s `_word_boundary_pattern` added
# `re.ASCII`), and an unrecognisable name.
# ---------------------------------------------------------------------------------------------
PARITY_CASES: list[tuple[str | None, str]] = [
    ("acme-prod", "prod"),
    ("acme_prd", "prod"),
    ("acme.production", "prod"),
    ("acme/live", "prod"),
    ("acme-uat", "uat"),
    ("acme_test", "uat"),
    ("acme.qa", "uat"),
    ("acme-stag", "uat"),
    ("acme-staging", "uat"),
    ("acme_stage", "uat"),
    ("acme_sandbox", "uat"),
    ("acme.sit", "uat"),
    ("acme-nonprod", "uat"),
    ("acme_preprod", "uat"),
    ("acme-dev", "dev"),
    ("acme_devel", "dev"),
    ("ACME-PROD", "prod"),
    ("notprodx", "unknown"),
    ("predatory", "unknown"),
    ("unrelated-name", "unknown"),
    ("prod\u00fcktion", "prod"),
    (None, "unknown"),
]

PARITY_NAMES = [name for name, _ in PARITY_CASES]
PARITY_EXPECTED = [env for _, env in PARITY_CASES]


def _classify_env_via_dbt(names: list[str | None]) -> list[str]:
    """Runs classify_env() for real (dbt show --inline, executed against the `test` target, not
    just compiled) over `names` in one query and returns the classification for each, in order."""
    selects = []
    for i, name in enumerate(names):
        if name is None:
            literal = "CAST(NULL AS VARCHAR)"
        else:
            literal = "'" + name.replace("'", "''") + "'"
        selects.append(f"SELECT {i} AS idx, {literal} AS name")
    values_sql = " UNION ALL ".join(selects)
    inline = (
        f"WITH t AS ({values_sql}) "
        "SELECT idx, {{ classify_env('t.name') }} AS env FROM t ORDER BY idx"
    )
    result = _dbt(
        "show", "--target", "test", "--inline", inline,
        "--output", "json", "--limit", str(len(names)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    brace = result.stdout.index("{")
    payload = json.loads(result.stdout[brace:])
    by_idx = {row["idx"]: row["env"] for row in payload["show"]}
    return [by_idx[i] for i in range(len(names))]


def test_parity_cases_cover_all_four_buckets():
    """Sanity check on the shared table itself: every one of prod/uat/dev/unknown appears."""
    assert set(PARITY_EXPECTED) == {"prod", "uat", "dev", "unknown"}


def test_envmap_matches_expected_table():
    patterns = envmap.load_env_patterns()
    got = [envmap.classify_env(name, patterns) for name in PARITY_NAMES]
    assert got == PARITY_EXPECTED, list(zip(PARITY_NAMES, PARITY_EXPECTED, got))


def test_classify_env_dbt_matches_expected_table():
    got = _classify_env_via_dbt(PARITY_NAMES)
    assert got == PARITY_EXPECTED, list(zip(PARITY_NAMES, PARITY_EXPECTED, got))


def test_classify_env_parity_dbt_vs_python():
    """The parity assertion itself: dbt's classify_env() and envmap.py's classify_env() must
    agree, name for name, independent of what either one's "expected" answer is defined to be."""
    patterns = envmap.load_env_patterns()
    python_side = [envmap.classify_env(name, patterns) for name in PARITY_NAMES]
    dbt_side = _classify_env_via_dbt(PARITY_NAMES)
    assert python_side == dbt_side, list(zip(PARITY_NAMES, python_side, dbt_side))


def test_unknown_id_classifies_unknown_never_guessed():
    """DEC-22 / tasks/T-23's DECIDE: proven at function level with an arbitrary unclassifiable
    name string, since this task does not own tests/fixtures/billing.py."""
    assert envmap.classify_env("this-name-matches-no-known-pattern") == "unknown"
    assert envmap.classify_env(None) == "unknown"


# ---------------------------------------------------------------------------------------------
# (b)/(c) Built dims.dim_workspace assertions.
# ---------------------------------------------------------------------------------------------

FIXTURE_WORKSPACES_BY_NAME = {
    # workspace_id -> (workspace_name, expected env classified purely from that name)
    "1111": ("acme-prod", "prod"),
    "3333": ("acme-uat", "uat"),
    "9001": ("dd-prod", "prod"),
}
OVERRIDDEN_WORKSPACE_ID = "2222"
OVERRIDDEN_WORKSPACE_NAME = "acme-dev"
OVERRIDDEN_ENV = "uat"  # dbt/seeds/workspace_env_overrides.csv


@pytest.fixture(scope="module")
def dim_workspace_rows() -> dict[str, dict]:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} does not exist yet -- run `python tests/fixtures/build_fixtures.py` then "
            '`python tools/dbt_run.py build --target test` (or, for just these tables, '
            '`python tools/dbt_run.py build --target test --select "+dim_workspace '
            'dim_list_prices"` -- the selector must be quoted as one argument) first'
        )
    con = None
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            con = duckdb.connect(str(DB_PATH), read_only=True)
            break
        except duckdb.Error as exc:
            if _LOCK_MARKER not in str(exc) or attempt == _LOCK_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_LOCK_RETRY_DELAY_S)
    try:
        cur = con.execute(
            "SELECT workspace_id, name, url, env, env_source, env_reason FROM dims.dim_workspace"
        )
        cols = [d[0] for d in cur.description]
        return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}
    finally:
        con.close()


def test_fixture_workspaces_classify_by_name(dim_workspace_rows):
    for workspace_id, (name, expected_env) in FIXTURE_WORKSPACES_BY_NAME.items():
        row = dim_workspace_rows[workspace_id]
        assert row["name"] == name
        assert row["env"] == expected_env
        assert row["env_source"] == "name"


def test_seed_override_wins_over_name(dim_workspace_rows):
    row = dim_workspace_rows[OVERRIDDEN_WORKSPACE_ID]
    assert row["name"] == OVERRIDDEN_WORKSPACE_NAME
    assert row["env"] == OVERRIDDEN_ENV
    assert row["env_source"] == "override"
    # The name-derived classification (what env_source would be without the override) is 'dev' --
    # proves the override actually changed the outcome, not just that it is present.
    assert envmap.classify_env(row["name"]) == "dev"
    assert envmap.classify_env(row["name"]) != row["env"]
