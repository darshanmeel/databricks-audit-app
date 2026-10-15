#!/usr/bin/env python3
"""tools/first_run.py -- the one wrapper that turns "get the data" into a single command.

    python tools/first_run.py [--windows 90]

Runs, in order, the two commands the direct (no-dbt) path needs:

    1. python tools/export_direct_results.py --out <data folder>/results/<stamp> [--windows N]
    2. python tools/load_direct_results.py <data folder>/results/<stamp>

streaming each step's own output live (house style: tools/gate.py's `_run`) and stopping at the
first step that fails, with a message naming the step, rather than plowing on into a load that has
nothing to read. On success it ends by printing the exact command to open the app --
`python -m app.api` -- and the URL to open, `http://127.0.0.1:8000/`.

Credentials: this script never accepts a token argument, never prints a credential, and never
reads one itself. Credential loading belongs to tools/export_direct_results.py alone (the three
DATABRICKS_* environment variables, or its own git-ignored dotenv file at the repo root) -- this
script only spawns it as a subprocess and reads its exit code, exactly as it reached that process
through the inherited environment. If it exits 2 (its configuration-error code, e.g. a missing
credential), this script adds one line pointing at .env.example without repeating or reformatting
the message tools/export_direct_results.py already printed.

Exit code: propagates the first failing step's own exit code unchanged (2 = configuration error,
1 = connector or load failure, 130 = interrupted); 0 only when both steps succeeded.

No git calls anywhere in this file. Every path this script itself touches is resolved from its
own file location (ROOT = Path(__file__).resolve().parent.parent), not from the process's current
working directory, and every subprocess it spawns is explicitly run with cwd=ROOT -- the same
"repo root is the cwd for every command" convention the other tools already assume for their own
relative-path defaults. That makes this script work identically from an unpacked zip opened
anywhere on disk, regardless of the shell's own directory when it is invoked.

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import datasource  # noqa: E402

try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

APP_CMD = "python -m app.api"
APP_URL = "http://127.0.0.1:8000/"

# Defensive: any option string containing one of these substrings would let a credential be
# passed on the command line (and therefore end up in shell history, a CI log, etc.) -- this repo
# never has a --token flag anywhere and this wrapper must not be the place that adds one. Checked
# at parser-construction time in build_parser(), and tests/test_first_run.py asserts it directly
# against the constructed parser.
_FORBIDDEN_FLAG_SUBSTRINGS = ("token", "password", "secret", "credential")

RunFn = Callable[..., "subprocess.CompletedProcess"]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="first_run.py",
        description=(
            "Run the first-run pipeline end to end: tools/export_direct_results.py (query the "
            "account's system tables straight from a SQL warehouse; no dbt) -> "
            "tools/load_direct_results.py (build the local DuckDB file the app reads), stopping "
            "at the first step that fails. On success, prints the command and URL to open the app."
        ),
    )
    ap.add_argument(
        "--windows", type=int, default=None, metavar="DAYS",
        help="days of history to export, passed to tools/export_direct_results.py's own "
             "--windows (default: that tool's own default, config/settings.yml's default_window)",
    )
    for action in ap._actions:  # noqa: SLF001 - a deliberate, tested self-check, not runtime logic
        for opt in action.option_strings:
            lname = opt.lstrip("-").lower()
            if any(bad in lname for bad in _FORBIDDEN_FLAG_SUBSTRINGS):
                raise AssertionError(
                    f"first_run.py must never accept a credential-shaped flag: {opt}"
                )
    return ap


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _stamp(now: dt.datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def export_out_dir(now: dt.datetime) -> Path:
    """A fresh, timestamped folder per run, so an interrupted or superseded export never mixes
    its files with an earlier one under the same path (tools/export_direct_results.py's own
    --out is swapped into place atomically, but only within one run)."""
    return datasource.data_dir() / "results" / _stamp(now)


# -------------------------------------------------------------------------------------------
# The two steps' argv
# -------------------------------------------------------------------------------------------
def export_argv(out_dir: Path, args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, "tools/export_direct_results.py", "--out", str(out_dir)]
    if args.windows is not None:
        cmd += ["--windows", str(args.windows)]
    return cmd


def load_argv(out_dir: Path) -> list[str]:
    return [sys.executable, "tools/load_direct_results.py", str(out_dir)]


# -------------------------------------------------------------------------------------------
# Running and reporting one step
# -------------------------------------------------------------------------------------------
def _stream(cmd: list[str], run: RunFn) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    result = run(cmd, cwd=ROOT)
    return result.returncode


def _advise_export_failure(rc: int) -> str:
    if rc == 2:
        return (
            "tools/export_direct_results.py exited 2 (configuration error) -- see its message "
            "above. If it names a missing credential, set DATABRICKS_SERVER_HOSTNAME, "
            "DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN in your shell, or copy .env.example to "
            ".env at the repo root and fill in the three values (.env is git-ignored; never "
            "commit or share it)."
        )
    if rc == 130:
        return "tools/export_direct_results.py was interrupted. Re-run this command to start over."
    return (
        f"tools/export_direct_results.py exited {rc} -- see the error line above (already "
        "scrubbed of secrets). Usually a connector failure: authentication, network, or an "
        "unreachable warehouse. An ImportError or \"DLL load failed\" there means a broken "
        f"install instead: re-run \"{sys.executable}\" -m pip install -r requirements.txt."
    )


def _advise_load_failure(rc: int, out_dir: Path) -> str:
    return (
        f"tools/load_direct_results.py exited {rc} -- see the message above. The export at "
        f"{out_dir} is untouched; re-run just the load once the problem is fixed: "
        f"python tools/load_direct_results.py {out_dir}"
    )


# -------------------------------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------------------------------
def main(
    argv: Sequence[str] | None = None, run: RunFn = subprocess.run, now: dt.datetime | None = None,
) -> int:
    args = parse_args(argv)
    out_dir = export_out_dir(now if now is not None else dt.datetime.now(dt.timezone.utc))

    print(
        "== step 1/2: tools/export_direct_results.py (queries the account's system tables "
        "straight from a SQL warehouse; read-only, no dbt, can take minutes to tens of "
        "minutes) ==",
        flush=True,
    )
    rc = _stream(export_argv(out_dir, args), run)
    if rc != 0:
        print(f"\nfirst_run.py: step 1 (tools/export_direct_results.py) failed, exit {rc}.", file=sys.stderr)
        print(_advise_export_failure(rc), file=sys.stderr)
        return rc

    print(
        "\n== step 2/2: tools/load_direct_results.py (builds the local DuckDB file the app "
        "reads) ==",
        flush=True,
    )
    rc = _stream(load_argv(out_dir), run)
    if rc != 0:
        print(f"\nfirst_run.py: step 2 (tools/load_direct_results.py) failed, exit {rc}.", file=sys.stderr)
        print(_advise_load_failure(rc, out_dir), file=sys.stderr)
        return rc

    print("\nDone -- open the app:\n", flush=True)
    print(APP_CMD, flush=True)
    print(f"then open {APP_URL} in a browser", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
