#!/usr/bin/env python3
"""Phase-aware gate script for the Databricks Audit app.

    python tools/gate.py --quick     lint + sync_queries --check + generate_models --check +
                                      web type check and tests + pytest
    python tools/gate.py             full: fixtures + dbt build --target test + pytest +
                                      fixture_coverage --check

Every step below PROBES for its dependency's presence at runtime (a file-existence check) instead
of hard-importing or hard-requiring it up front. A dependency that is not present yet is skipped
with a `skip: ...` note and does not fail the gate; once the dependency exists, the step runs for
real.

Stdlib only.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Line-buffer stdout so this script's own print() calls are always flushed to the terminal
# before a subprocess we spawn next writes to the same (shared) file descriptor -- otherwise the
# "$ <cmd>" announcement can appear after the subprocess's own output.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass


class Step:
    """One gate step's outcome. status is 'ok', 'skip', or 'fail'."""

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name = name
        self.status = status
        self.detail = detail

    def summary(self) -> str:
        if self.detail:
            return f"{self.name}={self.status}({self.detail})"
        return f"{self.name}={self.status}"


def _run(cmd: list[str]) -> int:
    """Run a command from the repo root, streaming its output live, return the exit code."""
    print(f"$ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    return r.returncode


def step_lint_headers(steps: list[Step]) -> bool:
    """Always present from this task on."""
    rc = _run([sys.executable, "tools/lint_headers.py"])
    steps.append(Step("lint_headers", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_sync_queries(steps: list[Step]) -> bool:
    """Only present from T-02 on."""
    path = ROOT / "tools" / "sync_queries.py"
    if not path.exists():
        print("skip: tools/sync_queries.py not present yet")
        steps.append(Step("sync_queries", "skip"))
        return True
    rc = _run([sys.executable, "tools/sync_queries.py", "--check"])
    steps.append(Step("sync_queries", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_generate_models(steps: list[Step]) -> bool:
    """Only present from T-06 on."""
    path = ROOT / "tools" / "generate_models.py"
    if not path.exists():
        print("skip: tools/generate_models.py not present yet")
        steps.append(Step("generate_models", "skip"))
        return True
    rc = _run([sys.executable, "tools/generate_models.py", "--check"])
    steps.append(Step("generate_models", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_generate_direct_models(steps: list[Step]) -> bool:
    """Only present from item DBX-DIRECT on."""
    path = ROOT / "tools" / "generate_direct_models.py"
    if not path.exists():
        print("skip: tools/generate_direct_models.py not present yet")
        steps.append(Step("generate_direct_models", "skip"))
        return True
    rc = _run([sys.executable, "tools/generate_direct_models.py", "--check"])
    steps.append(Step("generate_direct_models", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_build_direct_sql(steps: list[Step]) -> bool:
    rc = _run([sys.executable, "tools/build_direct_sql.py", "--check"])
    steps.append(Step("build_direct_sql", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_web(steps: list[Step]) -> bool:
    """Only when web/node_modules is installed -- the built page in app/web/dist is committed, so a
    Python-only checkout still runs the rest of the gate (tests/test_web_dist.py checks the build)."""
    npm = shutil.which("npm")
    if npm is None or not (ROOT / "web" / "node_modules").exists():
        print("skip: web/node_modules not installed (cd web && npm ci)")
        steps.append(Step("web", "skip"))
        return True
    rc = _run([npm, "--prefix", "web", "run", "typecheck"]) or _run([npm, "--prefix", "web", "test"])
    steps.append(Step("web", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_pytest(steps: list[Step]) -> bool:
    """Always run. Exit code 5 ('no tests collected') is non-fatal: tests/ is intentionally
    empty until T-02 adds tests/test_sync_queries.py (DEC-04 / DEC-05)."""
    rc = _run([sys.executable, "-m", "pytest", "-q"])
    if rc == 5:
        print("no tests collected yet")
        steps.append(Step("pytest", "ok", "no tests collected yet"))
        return True
    steps.append(Step("pytest", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_build_fixtures(steps: list[Step]) -> bool:
    """Only present from T-07 on."""
    path = ROOT / "tests" / "fixtures" / "build_fixtures.py"
    if not path.exists():
        print("skip: tests/fixtures/build_fixtures.py not present yet")
        steps.append(Step("build_fixtures", "skip"))
        return True
    rc = _run([sys.executable, "tests/fixtures/build_fixtures.py"])
    steps.append(Step("build_fixtures", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_dbt_build_test(steps: list[Step]) -> bool:
    """Only present from T-05 on (the dbt project skeleton). Goes through tools/dbt_run.py (DEC-12)
    rather than shelling out to a bare `dbt build` directly, so the gate-built test database gets
    the same --vars wiring (tag_aliases, share_floor, thresholds, windows -- see T-63/DEC-60) any
    other caller of `python tools/dbt_run.py build --target test` gets; `--target test` always
    propagates dbt's own exit code unchanged (tools/dbt_run.py's own contract), so this step's
    ok/fail semantics are unchanged."""
    path = ROOT / "dbt" / "dbt_project.yml"
    if not path.exists():
        print("skip: dbt/dbt_project.yml not present yet")
        steps.append(Step("dbt_build_test", "skip"))
        return True
    rc = _run([sys.executable, "tools/dbt_run.py", "build", "--target", "test"])
    steps.append(Step("dbt_build_test", "ok" if rc == 0 else "fail"))
    return rc == 0


def step_fixture_coverage(steps: list[Step]) -> bool:
    """Only present from T-24 on. The full gate runs `tools/fixture_coverage.py --check` (every
    executable query id has a grain entry and a pytest assertion, plus the duplicate-grain and
    grain/model-column checks) -- --quick does not run this step."""
    path = ROOT / "tools" / "fixture_coverage.py"
    if not path.exists():
        print("skip: tools/fixture_coverage.py not present yet")
        steps.append(Step("fixture_coverage", "skip"))
        return True
    rc = _run([sys.executable, "tools/fixture_coverage.py", "--check"])
    steps.append(Step("fixture_coverage", "ok" if rc == 0 else "fail"))
    return rc == 0


def _finish(steps: list[Step], all_ok: bool) -> int:
    print("gate summary: " + " ".join(s.summary() for s in steps))
    if all_ok:
        print("GATE OK")
        return 0
    print("gate failed -- see the failing step's output above", file=sys.stderr)
    return 1


def quick() -> int:
    steps: list[Step] = []
    ok = True
    ok &= step_lint_headers(steps)
    ok &= step_sync_queries(steps)
    ok &= step_generate_models(steps)
    ok &= step_generate_direct_models(steps)
    ok &= step_build_direct_sql(steps)
    ok &= step_web(steps)
    ok &= step_pytest(steps)
    return _finish(steps, ok)


def full() -> int:
    steps: list[Step] = []
    ok = True
    ok &= step_build_fixtures(steps)
    ok &= step_dbt_build_test(steps)
    ok &= step_pytest(steps)
    ok &= step_fixture_coverage(steps)
    return _finish(steps, ok)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--quick", action="store_true", help="lint + sync_queries + generate_models + web checks + pytest")
    args = ap.parse_args()

    if args.quick:
        return quick()
    return full()


if __name__ == "__main__":
    raise SystemExit(main())
