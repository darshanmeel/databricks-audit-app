#!/usr/bin/env python3
"""tools/dbt_run.py -- the one wrapper script anything in this repo uses to invoke dbt (DEC-12).

    python tools/dbt_run.py build --target dev|test [--select <selector>] [--exclude <selector>]

`--select` / `--exclude` pass straight through to the underlying `dbt build` call unchanged (DEC-12
-- e.g. `python tools/dbt_run.py build --target dev --select tag:dims` builds only dim models).

What this wrapper adds over a raw `dbt build --project-dir dbt --profiles-dir dbt`:
  - merges `--vars` (as one JSON blob, a valid YAML subset dbt accepts) from:
      * `thresholds`  -- loaded from config/thresholds.yml
      * `windows`     -- from dbt/dbt_project.yml's own `vars.windows`, or [7, 30, 90] if absent
      * `tag_aliases` -- config/tag_aliases.yml's `top_tags` keys, as {key: []}
      * `share_floor` -- config/settings.yml's `tag_share_floor`, default 0.6 (T-63/DEC-60)
      * `coverage_floor` -- config/settings.yml's `tag_coverage_floor`, default 0.5 (P4-T/DEC-63)
      * `mask_user_identities` -- config/settings.yml's `privacy.mask_user_identities`, default
        False
      * `grain_severity` -- `warn` for `--target dev` ONLY (DEC-48; the `unique_grain` generic
        test reads it, default `error`)
      * `as_of_date` / `as_of_ts` -- for `--target dev` ONLY, read from snapshot/manifest.json's
        `as_of_date` / `as_of` keys (only when that file exists -- an as-yet-unrun snapshot leaves
        dev on the native current_date()/current_timestamp() fallback in dbt/macros/audit_time.sql).
        Omitted entirely for `--target test`, since audit_time.sql fixes the test-target literals
        regardless of any var.
  - sets `AUDIT_SNAPSHOT_DIR`: `tests/fixtures/parquet` for `--target test` (always overridden);
    for `--target dev`, hard-defaults to the real snapshot directory `snapshot` (only applied when
    the surrounding environment does not already set it, so a caller can still override it).
  - for `--target dev` only: builds into a temporary DuckDB file (seeded with a copy of the
    current final db first, when one exists, so a --select/--exclude build stays additive rather
    than dropping every table it did not rebuild) and atomically swaps it into place with
    `os.replace` (short retry loop -- a reader, e.g. the running app, may hold the real file
    open). `--target test` writes tests/db_audit_test.duckdb directly; there is no concurrent
    reader to protect against there.
  - prints a scorecard (pass/fail/skip/error counts) parsed from target/run_results.json after the
    build; that file is removed before the build starts, so a build that errors before any node
    runs reports "no run results" instead of a stale count from a previous build.

Partial `--target dev` builds: dbt build exits nonzero the moment ANY node errors, but on a
real account a single vendored query hitting a schema difference is expected, not fatal (the
app's NOT_ASSESSED contract exists precisely for this). So for `--target dev` the decision to keep
or discard the just-built tmp database is made from `target/run_results.json`, never from dbt's
exit code alone:
  - dbt exits 0 -> no node errored -> always swap, this script exits 0.
  - dbt exits nonzero -> classify from run_results.json: if it is missing (a parse/compile
    failure before a single node ran) or shows zero models with status "success", the build
    produced nothing usable -- discard the tmp db (any previous database is left untouched) and
    exit with dbt's own nonzero code (never 3). Otherwise at least one model landed -- swap the tmp
    db in anyway and exit **3**, this script's "built, but some models failed" code, distinct from
    a clean build (0) and from a wholly unusable one (any other nonzero). The failed models simply
    render as NOT_ASSESSED in the app; see README.md.

`--target test` is never affected by any of the above: it has no tmp/final swap at all, and always
propagates dbt's own exit code unchanged -- a fixture build that errors is a bug in this repo's
own code and the gate must still fail hard on it.

Stdlib + pyyaml only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DBT_PROJECT_DIR = ROOT / "dbt"
DBT_PROFILES_DIR = ROOT / "dbt"
THRESHOLDS_PATH = ROOT / "config" / "thresholds.yml"
DBT_PROJECT_YML = DBT_PROJECT_DIR / "dbt_project.yml"
SNAPSHOT_MANIFEST = ROOT / "snapshot" / "manifest.json"
DEFAULT_WINDOWS = [7, 30, 90]
# T-63 (DEC-60): config/tag_aliases.yml and config/settings.yml's `tag_share_floor`, fed to dbt
# the same way THRESHOLDS_PATH already is below -- the only way an external config file this repo
# lets a person edit (app/core/config.py's load/save) ever reaches dbt Jinja is via --vars (dbt
# has no "read an arbitrary file" Jinja call). dbt/models/dims/int_workspace_tag_hints.sql is the
# sole consumer of both vars.
TAG_ALIASES_PATH = ROOT / "config" / "tag_aliases.yml"
SETTINGS_PATH = ROOT / "config" / "settings.yml"
DEFAULT_SHARE_FLOOR = 0.6
# P4-T (tasks/P4-T-SPEC.md 5.1/DEC-63): config/settings.yml's `tag_coverage_floor`, fed to dbt the
# same way DEFAULT_SHARE_FLOOR already is -- dbt/models/tags/tag_workspace.sql (and
# tag_entity.sql's billed_* inference) is the sole consumer.
DEFAULT_COVERAGE_FLOOR = 0.5
# config/settings.yml's privacy.mask_user_identities, fed to dbt the same way the two floors
# above are -- dbt/macros/mask_user.sql is the sole consumer.
DEFAULT_MASK_USER_IDENTITIES = False

try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass


def dbt_executable() -> str | None:
    """The `dbt` console-script installed for THIS Python (sys.executable), never a bare `dbt`
    resolved blind from PATH -- if the venv this repo was installed into is not on the calling
    shell's PATH (a common Windows trap: py.exe, a --user install, or the Microsoft Store Python),
    a plain PATH lookup can silently run some OTHER Python's dbt, or find none at all and crash
    with a WinError 2 traceback deep inside subprocess.run. Checked in this order: this
    interpreter's own Scripts/bin folder, then its user-scheme Scripts/bin folder (a --user
    install), then a plain PATH lookup as a last resort. None only when none of the three finds
    anything."""
    for folder in (
        sysconfig.get_path("scripts"),
        sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")),
    ):
        found = shutil.which("dbt", path=folder)
        if found:
            return found
    return shutil.which("dbt")


def load_thresholds() -> dict:
    """config/thresholds.yml: ONLY overrides, shape {<query_id>: {<param>: value}, _all: {...}}.
    A missing or empty file is a valid, complete threshold set (every param falls through to its
    query header's own default) -- returns {} in that case, never raises."""
    if not THRESHOLDS_PATH.exists():
        return {}
    data = yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    return data or {}


def load_tag_aliases() -> dict:
    """config/tag_aliases.yml's top tags as {key: []}, the `tag_aliases` var shape
    int_workspace_tag_hints.sql reads (an empty list matches the key's own name). {} when absent."""
    if not TAG_ALIASES_PATH.exists():
        return {}
    data = yaml.safe_load(TAG_ALIASES_PATH.read_text(encoding="utf-8")) or {}
    top = data.get("top_tags")
    return {key: [] for key in top} if isinstance(top, dict) else {}


def load_share_floor() -> float:
    """config/settings.yml's `tag_share_floor` (T-63/DEC-60 rule 3), defaulting to
    DEFAULT_SHARE_FLOOR when the file or key is absent -- never raises."""
    if not SETTINGS_PATH.exists():
        return DEFAULT_SHARE_FLOOR
    data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    floor = data.get("tag_share_floor", DEFAULT_SHARE_FLOOR)
    return float(floor) if isinstance(floor, (int, float)) else DEFAULT_SHARE_FLOOR


def load_coverage_floor() -> float:
    """config/settings.yml's `tag_coverage_floor` (P4-T/DEC-63), defaulting to
    DEFAULT_COVERAGE_FLOOR when the file or key is absent -- never raises. Mirrors
    load_share_floor() exactly."""
    if not SETTINGS_PATH.exists():
        return DEFAULT_COVERAGE_FLOOR
    data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    floor = data.get("tag_coverage_floor", DEFAULT_COVERAGE_FLOOR)
    return float(floor) if isinstance(floor, (int, float)) else DEFAULT_COVERAGE_FLOOR


def load_query_source() -> str:
    """settings query_source (a settings.local.yml value wins): the tag cost_audit_self_usage matches."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.core import config as app_config

    return app_config.query_source()


def load_mask_user_identities() -> bool:
    """config/settings.yml's `privacy.mask_user_identities`, defaulting to False when the file or
    key is absent -- never raises."""
    if not SETTINGS_PATH.exists():
        return DEFAULT_MASK_USER_IDENTITIES
    data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    privacy = data.get("privacy")
    value = privacy.get("mask_user_identities") if isinstance(privacy, dict) else None
    return bool(value) if isinstance(value, bool) else DEFAULT_MASK_USER_IDENTITIES


def load_windows() -> list[int]:
    if not DBT_PROJECT_YML.exists():
        return list(DEFAULT_WINDOWS)
    data = yaml.safe_load(DBT_PROJECT_YML.read_text(encoding="utf-8")) or {}
    windows = (data.get("vars") or {}).get("windows")
    if not windows:
        return list(DEFAULT_WINDOWS)
    return list(windows)


def load_as_of(path: Path | None = None) -> dict:
    """Only ever consulted for --target dev. Reads `path` (an explicit manifest.json path -- e.g.
    the real AUDIT_SNAPSHOT_DIR/manifest.json a `--out` snapshot dir was built into) or, when
    `path` is None, the module-level SNAPSHOT_MANIFEST default, for its `as_of_date` / `as_of`
    keys into the `as_of_date` / `as_of_ts` vars. Returns {} when that manifest does not exist yet
    (dev then falls through to audit_time.sql's native current_date()/current_timestamp() branch)
    -- never raises for a missing snapshot."""
    manifest_path = path if path is not None else SNAPSHOT_MANIFEST
    if not manifest_path.exists():
        return {}
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    out: dict = {}
    if data.get("as_of_date") is not None:
        out["as_of_date"] = data["as_of_date"]
    if data.get("as_of") is not None:
        out["as_of_ts"] = data["as_of"]
    return out


def build_vars(target: str, manifest_path: Path | None = None) -> dict:
    v: dict = {
        "thresholds": load_thresholds(),
        "windows": load_windows(),
        "tag_aliases": load_tag_aliases(),
        "share_floor": load_share_floor(),
        "coverage_floor": load_coverage_floor(),
        "mask_user_identities": load_mask_user_identities(),
        "query_source": load_query_source(),
    }
    if target == "dev":
        v.update(load_as_of(manifest_path))
        # DEC-48: masked-key grains can collide on a real snapshot; never fail the dev build on it.
        v["grain_severity"] = "warn"
    return v


def resolve_snapshot_dir(target: str, env: dict) -> str:
    if target == "test":
        return str(ROOT / "tests" / "fixtures" / "parquet")
    # dev: hard-default to the real snapshot dir, but let an already-set env var win.
    return env.get("AUDIT_SNAPSHOT_DIR") or str(ROOT / "snapshot")


def construct_dbt_command(
    target: str,
    select: str | None = None,
    exclude: str | None = None,
    vars_dict: dict | None = None,
) -> list[str]:
    """Builds the `dbt build ...` argv. Kept as a pure function (no subprocess, no env access) so
    tests can assert --select/--exclude pass through unchanged (DEC-12) by inspecting the
    constructed command line, with no live dbt invocation needed."""
    cmd = [
        "dbt",
        "build",
        "--project-dir",
        str(DBT_PROJECT_DIR),
        "--profiles-dir",
        str(DBT_PROFILES_DIR),
        "--target",
        target,
    ]
    if vars_dict is not None:
        cmd += ["--vars", json.dumps(vars_dict)]
    if select:
        cmd += ["--select", select]
    if exclude:
        cmd += ["--exclude", exclude]
    return cmd


def _atomic_swap(tmp_path: Path, final_path: Path, attempts: int = 5, delay: float = 0.5) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: OSError | None = None
    for i in range(attempts):
        try:
            os.replace(tmp_path, final_path)
            return
        except OSError as e:  # a reader may hold the file open (Windows)
            last_err = e
            if i < attempts - 1:
                time.sleep(delay)
    raise SystemExit(f"dbt_run.py: could not swap {tmp_path} -> {final_path}: {last_err}")


def _model_outcome_counts(run_results_path: Path) -> tuple[int, int]:
    """(n_success, n_failed) counted over `model.*` nodes only in target/run_results.json --
    tests, seeds and other resource types are ignored, because usability of a dev build is about
    whether a finding model actually landed in the swapped-in database, not whether a schema test
    happened to pass or fail alongside it. n_failed counts anything that did NOT land (status
    "error" or "skipped" -- a model skipped because an upstream node errored is just as absent
    from the built database as one that errored directly). Returns (0, 0) when run_results.json
    does not exist (a parse/compile failure before a single node ran)."""
    if not run_results_path.exists():
        return 0, 0
    data = json.loads(run_results_path.read_text(encoding="utf-8"))
    n_success = 0
    n_failed = 0
    for r in data.get("results", []):
        uid = r.get("unique_id", "")
        if not uid.startswith("model."):
            continue
        if r.get("status") == "success":
            n_success += 1
        else:
            n_failed += 1
    return n_success, n_failed


def _print_scorecard(project_dir: Path) -> None:
    run_results_path = project_dir / "target" / "run_results.json"
    if not run_results_path.exists():
        print("scorecard: no run results")
        return
    data = json.loads(run_results_path.read_text(encoding="utf-8"))
    counts = {"pass": 0, "fail": 0, "skip": 0, "error": 0, "other": 0}
    status_map = {
        "success": "pass",
        "pass": "pass",
        "fail": "fail",
        "error": "error",
        "skipped": "skip",
        "skip": "skip",
        "warn": "other",
    }
    for r in data.get("results", []):
        status = r.get("status", "")
        counts[status_map.get(status, "other")] += 1
    print(
        f"scorecard: pass={counts['pass']} fail={counts['fail']} "
        f"skip={counts['skip']} error={counts['error']} other={counts['other']}"
    )


def run_build(target: str, select: str | None, exclude: str | None) -> int:
    dbt = dbt_executable()
    if dbt is None:
        print(
            f"dbt is not installed for this Python ({sys.executable}): activate the venv you "
            f"installed into, or run \"{sys.executable}\" -m pip install -r requirements-dbt.txt",
            file=sys.stderr,
        )
        return 2

    env = os.environ.copy()
    env["AUDIT_SNAPSHOT_DIR"] = resolve_snapshot_dir(target, env)
    # T-89 (F4): read the as_of anchor from THIS build's own snapshot folder, not always the
    # hard-coded default ROOT/snapshot -- a `first_run.py --out <folder>` snapshot otherwise left
    # the as_of window silently anchored on a stale (or absent) ./snapshot/manifest.json. A
    # relative AUDIT_SNAPSHOT_DIR (from the surrounding environment) is anchored at ROOT the same
    # way every other relative default in this repo is.
    snapshot_dir = Path(env["AUDIT_SNAPSHOT_DIR"])
    if not snapshot_dir.is_absolute():
        snapshot_dir = ROOT / snapshot_dir
        env["AUDIT_SNAPSHOT_DIR"] = str(snapshot_dir)
    vars_dict = build_vars(target, manifest_path=snapshot_dir / "manifest.json")

    tmp_db: Path | None = None
    final_db: Path | None = None
    if target == "dev":
        raw = env.get("AUDIT_DB", "data/db_audit.duckdb")
        final_db = Path(raw)
        if not final_db.is_absolute():
            final_db = ROOT / final_db
        final_db.parent.mkdir(parents=True, exist_ok=True)
        # Dot-free stem (DEC-45): dbt-duckdb derives the catalog name from the file stem via
        # os.path.splitext, which keeps a leading dot, while DuckDB itself strips it. Appending
        # ".tmp-<pid>" AFTER the ".duckdb" extension (the previous approach) produced a dotted
        # stem once DuckDB parsed "db_audit.duckdb.tmp-123" -- catalog "db_audit.duckdb" not
        # found. Insert the tmp marker BEFORE the extension instead: "db_audit-tmp-<pid>.duckdb".
        tmp_db = final_db.with_name(f"{final_db.stem}-tmp-{os.getpid()}{final_db.suffix}")
        # Seed the tmp file from the current final db (when one already exists) so a selective
        # build (--select/--exclude, DEC-12/DEC-33 "dims only") does not silently drop every
        # table it did not rebuild -- the swap below must be additive, never destructive.
        if final_db.exists():
            shutil.copy2(final_db, tmp_db)
        env["AUDIT_DB"] = str(tmp_db)

    # A build that errors before any node runs (e.g. a parse error) leaves the PREVIOUS run's
    # run_results.json on disk; without removing it first, the scorecard below would silently
    # report stale counts from an earlier, unrelated build. For --target dev, that previous file
    # is also the one app/core/data.py's finding_status() reads for EVERY finding, on every
    # request (DEC-27) -- a completely unusable rebuild (discarded below, previous database left
    # untouched) must not leave a NEW, all-failed run_results.json sitting there, since that would
    # make every finding read NOT_ASSESSED against a database that is otherwise perfectly fine.
    # So for dev the previous file is parked at run_results.prev.json first, and restored after
    # the build if this run turns out to be a full discard; --target test (no swap concept at all)
    # keeps the old direct-unlink behaviour.
    rr = DBT_PROJECT_DIR / "target" / "run_results.json"
    prev = rr.with_name("run_results.prev.json")
    prev.unlink(missing_ok=True)
    # Review fix (F2): also clear a run_results.failed.json left by an EARLIER discarded attempt,
    # so that file only ever exists when THIS attempt was the one discarded -- otherwise a build
    # that fails before writing its own run_results.json (a parse/compile failure) would leave the
    # stale message below pointing at a stale, unrelated attempt's per-model detail.
    rr.with_name("run_results.failed.json").unlink(missing_ok=True)
    if target == "dev" and rr.exists():
        os.replace(rr, prev)
    else:
        rr.unlink(missing_ok=True)
    discarded = False

    cmd = construct_dbt_command(target, select=select, exclude=exclude, vars_dict=vars_dict)
    cmd[0] = dbt
    print(f"$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=ROOT, env=env)

    exit_code = result.returncode

    swapped = False
    if target == "dev" and tmp_db is not None and final_db is not None:
        if result.returncode == 0:
            # dbt's own contract: exit 0 means no node errored, full stop. Always swap -- this is
            # also the path a selective --select/--exclude build takes, and the tmp db was seeded
            # from the current final db precisely so that stays additive.
            if tmp_db.exists():
                _atomic_swap(tmp_db, final_db)
                swapped = True
        else:
            n_success, n_failed = _model_outcome_counts(rr)
            usable = rr.exists() and n_success > 0
            if usable:
                exit_code = 3
                if tmp_db.exists():
                    _atomic_swap(tmp_db, final_db)
                    swapped = True
                print(
                    f"\ndbt_run.py: dev database WAS updated -- {n_success} model(s) built "
                    f"successfully, {n_failed} model(s) failed or were skipped. Those findings "
                    "will show as NOT_ASSESSED in the app; see the Coverage & Gaps page for "
                    "which query and why (detail in dbt/target/run_results.json).",
                    flush=True,
                )
            else:
                # Nothing usable: a parse/compile failure before any node ran (no
                # run_results.json), or every model that did run errored/was skipped. Discard the
                # tmp db -- the previous database, if any, is left exactly as it was. The just-run
                # (all-failed or absent) run_results.json is parked at run_results.failed.json and
                # the PREVIOUS good run's run_results.json is restored below (after the scorecard
                # above has already reported on this attempt) -- app/core/data.py's
                # finding_status() must keep reading the previous, still-accurate build status,
                # not a fresh sea of NOT_ASSESSED against an unchanged, perfectly good database.
                discarded = True
                exit_code = result.returncode if result.returncode not in (0, 3) else 1
                if tmp_db.exists():
                    tmp_db.unlink()
                print(
                    "\ndbt_run.py: dev database NOT updated -- the build produced no usable "
                    "models (see the dbt output above for the failure). Any previous database is "
                    "unchanged (per-model detail: dbt/target/run_results.failed.json).",
                    file=sys.stderr,
                    flush=True,
                )

    if swapped:
        # Without this, current_db.txt (once anything else has written it -- load_direct_results
        # or a prior refresh) outranks this fresh build forever (app/core/data._db_path); a build
        # here must become what the app opens next, the same as any other db switch.
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app.core import datasource

        datasource.set_current_db(final_db)

    _print_scorecard(DBT_PROJECT_DIR)

    if discarded:
        # This attempt's own run_results.json (all-failed, or absent) is kept on disk for anyone
        # who wants the per-model detail, under its own name so it never shadows the restored
        # file below.
        if rr.exists():
            os.replace(rr, rr.with_name("run_results.failed.json"))
        if prev.exists():
            os.replace(prev, rr)
    else:
        prev.unlink(missing_ok=True)

    return exit_code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dbt_run.py",
        epilog=(
            "build --target dev exit codes: 0 = clean build (every node ok); 3 = built, but some "
            "models failed or were skipped -- the database WAS updated with everything that did "
            "build, failed models show as NOT_ASSESSED in the app (see Coverage & Gaps); any "
            "other nonzero = the build produced nothing usable, the database was NOT updated. "
            "build --target test always propagates dbt's own exit code unchanged."
        ),
    )
    sub = ap.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="dbt build, with vars/env/atomic-swap wiring")
    build.add_argument("--target", required=True, choices=["dev", "test"])
    build.add_argument("--select", default=None)
    build.add_argument("--exclude", default=None)

    args = ap.parse_args(argv)

    if args.command == "build":
        return run_build(args.target, args.select, args.exclude)

    ap.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
