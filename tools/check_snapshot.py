#!/usr/bin/env python3
"""tools/check_snapshot.py -- read-only local diagnostic over an already-captured snapshot/.

    python tools/check_snapshot.py [--snapshot-dir DIR] [--manifest PATH] [--windows 7,30,90]

Never connects to Databricks and never imports the connector (PLAN.md section 4 / 11 point 2:
"Only tools/snapshot.py touches Databricks. Agents executing the plan never run it, you do.").
This tool only reads local files: a snapshot folder's manifest.json and its <schema>__<table>/
parquet files, plus the generated tests/fixtures/ddl.py (ARROW_SCHEMA, the per-source column
list; T-03), config/snapshot_plan.yml (per-table retention caps; T-05) and the query registry
(app/core/registry.py; T-03) for the final "will build as NOT_ASSESSED" list. It runs with zero
required arguments and works with no snapshot present at all.

DEC-39 (binding, tasks/DECISIONS.md) fixes this tool's flag surface and its state vocabulary:

    --snapshot-dir DIR   default "snapshot" (repo-root relative), overridable by the
                          AUDIT_SNAPSHOT_DIR env var (same precedence tools/dbt_run.py's
                          resolve_snapshot_dir() already uses for --target dev: an explicit
                          value wins, then the env var, then the hard default).
    --manifest PATH       override the manifest.json path (default: <snapshot-dir>/manifest.json;
                          needed to point at tests/fixtures/manifest.json, which sits next to,
                          not inside, tests/fixtures/parquet/).
    --windows 7,30,90      the app's window set (default matches dbt/dbt_project.yml's own
                          `vars.windows` default); a table's effective captured window shorter
                          than max(windows) gets one WARN line per table.

State vocabulary printed (DEC-39, must match the Coverage & Gaps page, T-28, exactly):
    state ok,   rows > 0  -> populated
    state ok,   rows == 0 -> empty
    state partial          -> populated (partial, N slice(s) failed)
    state not_assessed
    state error             -> missing, with the manifest's `reason`
    no manifest entry / no manifest file at all -> missing (no manifest / no manifest entry)

Exit codes: 0 always, except 2 for a genuine usage error (e.g. an unparsable --windows value).
This is a diagnostic, not a gate -- a NOT_ASSESSED-heavy report is an expected, non-error outcome
(most visibly true on the very first run, before any snapshot exists at all).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
DDL_MODULE_PATH = ROOT / "tests" / "fixtures" / "ddl.py"
SNAPSHOT_PLAN_PATH = ROOT / "config" / "snapshot_plan.yml"
DEFAULT_SNAPSHOT_DIR = ROOT / "snapshot"
MANIFEST_NAME = "manifest.json"
DEFAULT_WINDOWS = (7, 30, 90)

# Fixed domain print order, reused here only for readability of the final NOT_ASSESSED summary.
DOMAIN_ORDER = (
    "cost", "compute", "jobs_pipelines", "performance", "governance_access", "storage",
    "serving_ai",
)

# Reasons that mean "the source is unavailable" vs "the export itself failed" (tools/snapshot.py,
# docs/SNAPSHOT.md). A table in either state is a blocking source for a finding that reads it.
BLOCKING_STATES = frozenset({"not_assessed", "error"})


# --------------------------------------------------------------------------------------------
# Inputs: ARROW_SCHEMA / FLAGS (tests/fixtures/ddl.py, T-03), config/snapshot_plan.yml (T-05),
# the query registry (app/core/registry.py, T-03). All three are read-only here.
# --------------------------------------------------------------------------------------------
def load_arrow_schemas(path: Path = DDL_MODULE_PATH) -> tuple[dict, dict]:
    """Load ARROW_SCHEMA and FLAGS from the generated tests/fixtures/ddl.py by file path (same
    technique tools/snapshot.py's own load_arrow_schemas uses -- tests/ carries no __init__.py,
    so a plain package import is not reliable across every invocation style)."""
    if not path.exists():
        raise SystemExit(
            f"check_snapshot.py: {path} not found; run `python tools/build_fixture_ddl.py` "
            "first (T-03) -- this tool cannot report anything without the source column lists."
        )
    spec = importlib.util.spec_from_file_location("check_snapshot_ddl_module", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"check_snapshot.py: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    schemas = dict(getattr(mod, "ARROW_SCHEMA"))
    flags = dict(getattr(mod, "FLAGS", {}))
    return schemas, flags


def load_plan(path: Path = SNAPSHOT_PLAN_PATH) -> dict:
    """config/snapshot_plan.yml, read only for the retention_days annotation printed per table.
    Never required (DEC-10): an absent or unparsable file just means no retention annotation."""
    if not path.exists():
        return {}
    try:
        import yaml  # pyyaml, in requirements.txt
    except ImportError:
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a broken plan file must never crash this diagnostic
        return {}
    if isinstance(doc, dict) and isinstance(doc.get("tables"), dict):
        doc = doc["tables"]
    return doc if isinstance(doc, dict) else {}


def load_registry_specs() -> tuple[list, str | None, object | None]:
    """app/core/registry.py's load_registry(), imported the same way tools/generate_models.py
    does (sys.path.insert(0, ROOT) then a normal package import), plus app/core/data.py's
    _optional_name_sources(spec) classifier (a name-lookup-only source must not count as blocking
    here, matching app/core/data.finding_status()). Both imports share this one guarded try -- a
    failure to load either (e.g. duckdb/pandas not installed) must not crash this diagnostic; it
    is reported as a warning instead, the final NOT_ASSESSED summary is skipped, and the third
    return value is None (callers then treat every source as non-name-lookup, the pre-fix
    behaviour)."""
    sys.path.insert(0, str(ROOT))
    try:
        from app.core.registry import load_registry  # noqa: PLC0415
        from app.core.data import _optional_name_sources  # noqa: PLC0415
        return load_registry(), None, _optional_name_sources
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}", None


# --------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------
def _metastore_text(info) -> str:
    """T-71: the manifest's `metastore` ({cloud, region, id_fingerprint} or null) as one phrase."""
    if isinstance(info, dict) and info.get("region"):
        return f"{info.get('cloud') or '?'} {info['region']}"
    return "not recorded"


def load_manifest(path: Path) -> tuple[dict | None, str | None]:
    """Returns (doc, None) or (None, <human reason>). Never raises: a missing or malformed
    manifest is exactly the state README.md's readers will hit on their first run."""
    if not path.exists():
        return None, f"not found at {path}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not be read ({type(exc).__name__}: {exc})"
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return None, f"is not valid JSON ({type(exc).__name__}: {exc})"
    if not isinstance(doc, dict) or not isinstance(doc.get("tables"), dict):
        return None, "is missing the top-level 'tables' object (not the T-04 manifest shape)"
    return doc, None


# --------------------------------------------------------------------------------------------
# Per-source parquet inspection
# --------------------------------------------------------------------------------------------
def read_folder_columns(folder: Path) -> tuple[set[str] | None, int]:
    """Union of column names across every *.parquet file in `folder` (real snapshot folders hold
    part-NNNNN.parquet; fixture folders hold one file per builder, unioned by name at dbt-read
    time the same way). Returns (None, 0) when the folder is absent or holds no parquet files."""
    if not folder.is_dir():
        return None, 0
    files = sorted(folder.glob("*.parquet"))
    if not files:
        return None, 0
    cols: set[str] = set()
    unreadable = 0
    for f in files:
        try:
            cols.update(pq.ParquetFile(f).schema_arrow.names)
        except Exception:  # noqa: BLE001 - a corrupt file must not crash the whole report
            unreadable += 1
    if unreadable == len(files):
        return None, len(files)
    return cols, len(files)


def classify_state(entry: dict | None) -> tuple[str, bool]:
    """(label, is_blocking) per DEC-39's exact vocabulary. is_blocking = True means a finding
    that reads this source will build as NOT_ASSESSED."""
    if entry is None:
        return "missing (no manifest entry for this source)", True
    state = entry.get("state")
    rows = entry.get("rows") or 0
    reason = entry.get("reason")
    error_class = entry.get("error_class")
    if state == "ok":
        return ("populated" if rows > 0 else "empty"), False
    if state == "partial":
        n = len(entry.get("slices_failed") or [])
        return f"populated (partial, {n} slice(s) failed)", False
    if state in BLOCKING_STATES:
        detail = reason or error_class or state
        return f"missing ({state}: {detail})", True
    return f"missing (unrecognised state {state!r})", True


# --------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------
def format_source_line(
    key: str,
    entry: dict | None,
    folder: Path,
    arrow_schema: dict,
    flags: dict,
    plan: dict,
    windows: list[int],
) -> list[str]:
    lines: list[str] = []
    label, _ = classify_state(entry)
    live_cols, n_files = read_folder_columns(folder)
    on_disk = "on disk" if folder.is_dir() else "NO FOLDER on disk"
    detail_bits = [f"files={n_files}"]
    if entry is not None:
        detail_bits.append(f"rows={entry.get('rows')}")
        tc = entry.get("time_column")
        if tc:
            detail_bits.append(
                f"time_column={tc} days_requested={entry.get('days_requested')} "
                f"days_effective={entry.get('days_effective')} "
                f"min={entry.get('min_time')} max={entry.get('max_time')}"
            )
        else:
            detail_bits.append("time_column=n/a (full history / reference table)")
    lines.append(f"  {key}: {label} [{on_disk}] ({', '.join(detail_bits)})")

    # Folder <-> manifest consistency (the "a source folder missing" scenario).
    if entry is not None and entry.get("state") in ("ok", "partial") and live_cols is None:
        lines.append(
            f"    ERROR: manifest says state={entry.get('state')!r} but no readable parquet "
            f"file was found under {folder} -- wrong --snapshot-dir, or the folder was deleted "
            "after export?"
        )

    # Column superset check against tests/fixtures/ddl.py's ARROW_SCHEMA (T-03).
    expected = arrow_schema.get(key)
    if live_cols is not None and expected is not None:
        missing_cols = sorted(set(expected.names) - live_cols)
        if missing_cols:
            if flags.get(key) == "MISSING_FROM_DUMP":
                lines.append(
                    f"    GAP: referenced column(s) missing from the parquet (this source has "
                    f"no catalog-dump schema, so ARROW_SCHEMA is the referenced-columns list "
                    f"itself): {missing_cols}"
                )
            else:
                lines.append(
                    f"    warning: column(s) in the catalog-dump schema not present in this "
                    f"parquet, not necessarily referenced by any query (may have been dropped "
                    f"or renamed upstream since the dump): {missing_cols}"
                )

    # Window: requested vs effective vs retention cap vs the app's own --windows set (DEC-39:
    # "warns per table when the effective window is shorter than max(windows)").
    if entry is not None and entry.get("time_column"):
        plan_entry = plan.get(key) or {}
        retention = plan_entry.get("retention_days")
        days_eff = entry.get("days_effective")
        cap_note = f" (retention cap {retention})" if retention is not None else ""
        if days_eff is not None and days_eff < max(windows):
            lines.append(
                f"    WARN: effective window is {days_eff} day(s){cap_note}, shorter than the "
                f"largest requested window ({max(windows)}); the {max(windows)}-day (and any "
                f"wider requested) window will show partial coverage for this source."
            )

    return lines


def build_report(snapshot_dir: Path, manifest_path: Path, windows: list[int]) -> int:
    arrow_schema, flags = load_arrow_schemas()
    plan = load_plan()

    print(f"Snapshot directory: {snapshot_dir}")
    manifest, manifest_error = load_manifest(manifest_path)
    if manifest is None:
        print(f"Manifest: NOT USABLE ({manifest_path} {manifest_error})")
        print(
            "  No snapshot has been captured yet (or the manifest could not be read). Run:\n"
            "    python tools/snapshot.py --out "
            f"{snapshot_dir} --days 90 --billing-days 365\n"
            "  Every source below is treated as not captured; every finding "
            "query that reads one will build as NOT_ASSESSED."
        )
        tables = {}
    else:
        print(
            f"Manifest: found at {manifest_path}\n"
            f"  as_of={manifest.get('as_of')} as_of_date={manifest.get('as_of_date')} "
            f"days={manifest.get('days')} billing_days={manifest.get('billing_days')}\n"
            f"  workspaces={manifest.get('workspace_ids') or 'all (unfiltered)'} "
            f"host_fingerprint={manifest.get('host_fingerprint')} "
            f"connector_version={manifest.get('connector_version')}"
        )
        print(
            f"  metastore={_metastore_text(manifest.get('metastore'))} -- regional system tables "
            "cover this metastore's region only; billing covers the whole account"
        )
        tables = manifest["tables"]

    print(f"\nSources ({len(arrow_schema)}):")
    counts = {"populated": 0, "empty": 0, "partial": 0, "missing": 0}
    missing_keys: list[str] = []
    empty_keys: list[str] = []
    for key in sorted(arrow_schema):
        entry = tables.get(key)
        label, blocking = classify_state(entry)
        if blocking:
            counts["missing"] += 1
            missing_keys.append(key)
        elif label.startswith("populated (partial"):
            counts["partial"] += 1
        elif label == "populated":
            counts["populated"] += 1
        elif label == "empty":
            counts["empty"] += 1
            empty_keys.append(key)
        folder = snapshot_dir / key
        for line in format_source_line(key, entry, folder, arrow_schema, flags, plan, windows):
            print(line)

    # Two separate lines because DEC-39's vocabulary distinguishes them: "missing" (blocking --
    # a finding reading this source will build as NOT_ASSESSED) and "empty" (ok, 0 rows -- a
    # finding reading only empty sources still builds, just "OK, empty in window", per PLAN.md
    # 6.2) are both "not populated" in the everyday sense but must never be merged into one
    # count/list -- doing so previously produced a self-contradictory "Sources not populated (0
    # of 47): (none)" next to "Summary: ... 40 empty" on the fixture.
    print(
        f"\nSources missing (blocking, will be NOT_ASSESSED) ({len(missing_keys)} of "
        f"{len(arrow_schema)}): {missing_keys if missing_keys else '(none)'}"
    )
    print(
        f"Sources empty (ok, 0 rows) ({len(empty_keys)} of {len(arrow_schema)}): "
        f"{empty_keys if empty_keys else '(none)'}"
    )

    print("\nFindings that will build as NOT_ASSESSED, grouped by domain:")
    specs, registry_error, optional_name_sources = load_registry_specs()
    total_findings = 0
    total_not_assessed = 0
    if registry_error is not None:
        print(f"  (skipped: could not load the query registry -- {registry_error})")
    else:
        executable = [s for s in specs if s.executable]
        total_findings = len(executable)
        by_domain: dict[str, list[tuple[str, list[str]]]] = {}
        for spec in executable:
            # A name-lookup-only source (e.g. system.lakeflow.jobs feeding just job_name) never
            # blocks -- app/core/data.finding_status() degrades it instead, so it must not be
            # counted here either, or this report overstates what the app will grey out.
            name_lookup = optional_name_sources(spec) if optional_name_sources else {}
            blocking_sources = [
                f"{schema}__{table}"
                for (schema, table) in spec.sources
                if classify_state(tables.get(f"{schema}__{table}"))[1]
                and f"system.{schema}.{table}" not in name_lookup
            ]
            if blocking_sources:
                by_domain.setdefault(spec.domain, []).append((spec.query_id, blocking_sources))
        total_not_assessed = sum(len(v) for v in by_domain.values())
        domains_in_order = list(DOMAIN_ORDER) + sorted(set(by_domain) - set(DOMAIN_ORDER))
        for domain in domains_in_order:
            entries = sorted(by_domain.get(domain, []))
            domain_total = sum(1 for s in executable if s.domain == domain)
            if not entries:
                continue
            print(f"  {domain} ({len(entries)}/{domain_total}):")
            for query_id, blocking_sources in entries:
                print(f"    - {query_id}: blocked by {blocking_sources}")

    print(
        f"\nSummary: {len(arrow_schema)} sources "
        f"({counts['populated']} populated, {counts['empty']} empty, "
        f"{counts['partial']} partial, {counts['missing']} missing) | "
        f"{total_not_assessed} of {total_findings} finding queries will build as NOT_ASSESSED"
    )
    return 0


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def _parse_windows(s: str) -> list[int]:
    try:
        values = [int(part.strip()) for part in s.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--windows must be a comma-separated list of integers, e.g. 7,30,90 (got {s!r})"
        ) from exc
    if not values or any(v <= 0 for v in values):
        raise argparse.ArgumentTypeError(f"--windows values must be positive integers (got {s!r})")
    return values


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="check_snapshot.py",
        description=(
            "Read-only local diagnostic over an already-captured snapshot/ folder: reports "
            "manifest presence, per-source state, column-schema gaps against "
            "tests/fixtures/ddl.py's ARROW_SCHEMA, window coverage, and which finding queries "
            "will build as NOT_ASSESSED. Never connects to Databricks (see README.md for the "
            "exporter this tool inspects the output of)."
        ),
    )
    ap.add_argument(
        "--snapshot-dir", default=None, metavar="DIR",
        help="snapshot folder to inspect (default: the AUDIT_SNAPSHOT_DIR env var, else 'snapshot')",
    )
    ap.add_argument(
        "--manifest", default=None, metavar="PATH",
        help="override the manifest.json path (default: <snapshot-dir>/manifest.json)",
    )
    ap.add_argument(
        "--windows", default="7,30,90", type=_parse_windows, metavar="N,N,...",
        help="comma-separated app window set in days, for the short-window warning (default: 7,30,90)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.snapshot_dir is not None:
        snapshot_dir = Path(args.snapshot_dir)
    else:
        env_dir = os.environ.get("AUDIT_SNAPSHOT_DIR")
        snapshot_dir = Path(env_dir) if env_dir else DEFAULT_SNAPSHOT_DIR
    if not snapshot_dir.is_absolute():
        snapshot_dir = (ROOT / snapshot_dir).resolve()

    manifest_path = Path(args.manifest) if args.manifest is not None else snapshot_dir / MANIFEST_NAME
    if not manifest_path.is_absolute():
        manifest_path = (ROOT / manifest_path).resolve()

    return build_report(snapshot_dir, manifest_path, args.windows)


if __name__ == "__main__":
    raise SystemExit(main())
