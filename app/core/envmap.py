"""app/core/envmap.py

Python twin of dbt/macros/classify_env.sql (T-23, PLAN.md D11 / 5.5, DEC-22): classifies a
workspace name (or a custom_tags env/environment/stage/tier hint) into prod/uat/dev/unknown using
the identical normalise-then-word-boundary rule the dbt macro uses, so the app can classify a
workspace the same way outside a dbt build (e.g. before the first `dbt build` has ever run).

Word lists are loaded from dbt/dbt_project.yml's `vars.env_patterns` at call time -- never
duplicated here as a second hard-coded copy (classify_env.sql's own header makes the same promise
in the other direction: it is the sole consumer of that var). tests/test_envmap.py's parity test
proves this module and the dbt macro produce identical output over the same set of names.

    load_env_patterns() -> dict[str, list[str]]   vars.env_patterns, read fresh every call
    classify_env(name) -> str                     'prod' | 'uat' | 'dev' | 'unknown'

Normalisation rule (copied verbatim from PLAN.md D11 / classify_env.sql): lower-case the input,
collapse any run of `_`, `-`, `.` or `/` to a single space, then word-boundary match against the
env_patterns word lists in fixed precedence order (prod, then uat, then dev -- first match wins).
A `None` name (or a name that matches no word list) always classifies 'unknown' -- env is never
guessed from a partial or fuzzy match.

Stdlib + pyyaml only.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
DBT_PROJECT_YML = ROOT / "dbt" / "dbt_project.yml"

# Matches classify_env.sql's normalisation pattern exactly: "[-_./]+" collapsed to one space.
# (Python's re has no backslash-unescaping-before-the-regex-engine issue the way Spark SQL string
# literals do, so this class's character order does not matter functionally here -- kept aligned
# with the SQL macro's char class purely for readability across the two files.)
_NORMALIZE_RE = re.compile(r"[-_./]+")

# Fixed precedence, matching classify_env.sql's `buckets` list: first match wins.
_BUCKET_ORDER = ("prod", "uat", "dev")


def load_env_patterns(path: Path = DBT_PROJECT_YML) -> dict[str, list[str]]:
    """Read `vars.env_patterns` from dbt/dbt_project.yml -- the single source of truth both this
    module and dbt/macros/classify_env.sql read from (no second hard-coded word-list copy)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    patterns = (doc.get("vars") or {}).get("env_patterns") or {}
    if not patterns:
        raise ValueError(f"{path}: vars.env_patterns is missing or empty")
    return patterns


def _word_boundary_pattern(words: list[str]) -> "re.Pattern[str]":
    # re.ASCII: Python's \b is Unicode-aware by default (treats a U+00FC letter next to "prod" as
    # a word character just like an ASCII letter would be, so \b never lands next to it the way
    # it would in DuckDB's RE2 engine, which is ASCII-only) -- without this flag, a name
    # containing a non-ASCII letter could classify differently in envmap.py than in
    # classify_env.sql. Reviewer-caught parity defect (T-23 fix round): a name spelled with a
    # U+00FC in place of the second "u" classified 'prod' via dbt/RE2 but 'unknown' via Python
    # before this flag was added (see tests/test_envmap.py's PARITY_CASES for the exact string).
    return re.compile(
        r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.ASCII
    )


def classify_env(name: Optional[str], patterns: Optional[dict] = None) -> str:
    """Classify `name` into 'prod' | 'uat' | 'dev' | 'unknown', the Python twin of
    dbt/macros/classify_env.sql: normalise (lower-case, collapse `-_./` runs to a space), then
    word-boundary match against the prod / uat / dev word lists in that fixed order (first match
    wins). `name` may be None (e.g. no workspace name and no tag hint available) -- always
    returns 'unknown' in that case, never a guess. `patterns` defaults to
    `load_env_patterns()` (dbt/dbt_project.yml's `vars.env_patterns`); pass it explicitly to
    avoid re-reading the file on every call (tests/test_envmap.py's parity loop does this)."""
    if name is None:
        return "unknown"
    if patterns is None:
        patterns = load_env_patterns()
    normalized = _NORMALIZE_RE.sub(" ", name.lower())
    for bucket in _BUCKET_ORDER:
        words = patterns.get(bucket) or []
        if not words:
            continue
        if _word_boundary_pattern(words).search(normalized):
            return bucket
    return "unknown"
