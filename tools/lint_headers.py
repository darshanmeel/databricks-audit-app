#!/usr/bin/env python3
"""Header-schema + hygiene linter for the query library.

Checks, over every app/queries/app/<domain>/*.sql:
  - all 14 v2 header fields present and non-empty
  - query_id == filename; domain == folder; tier in enum; confidence in enum
  - params declares :period_days; the SQL body uses :period_days and NOT :lookback_days
  - every `next:` target resolves to a real query_id (app-owned or vendored)
  - no internal-pipeline vocabulary leaks (collector, verifier, --share, findings/, feeds:,
    lookback_days, the databricks_audit: marker)
  - manifest.json is in sync (delegates to build_manifest --check)

    python tools/lint_headers.py

Exit code 0 = clean, 1 = violations. Stdlib only.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import header_schema as hs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "app" / "queries" / "app"
QUERIES_VENDORED = ROOT / "app" / "queries" / "vendored"

# Vocabulary that must never appear in a public query file (header or body).
FORBIDDEN = [
    (re.compile(r"\bcollector\b", re.I), "internal 'collector'"),
    (re.compile(r"\bverifier\b", re.I), "internal 'verifier'"),
    (re.compile(r"--share\b"), "internal '--share' build flag"),
    (re.compile(r"findings/"), "internal 'findings/...' path"),
    (re.compile(r"databricks_audit:"), "internal 'databricks_audit:' marker"),
    (re.compile(r"^--\s*feeds\s*:", re.M), "internal 'feeds:' header"),
    (re.compile(r"lookback_days"), "old ':lookback_days' param (use :period_days)"),
]

# H4: queries that emit a `status` column must only emit these labels, so the runner scorecard
# (which tallies exactly this set) can never silently miss an off-contract band like 'HIGH'.
STATUS_ENUM = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
# Isolate ONLY the status CASE: the negative lookahead stops the span from crossing an earlier
# column's `END AS <x>`, so we never harvest another column's THEN/ELSE literals.
_STATUS_CASE_RE = re.compile(r"CASE\b((?:(?!\bEND\s+AS\b).)*?)\bEND\s+AS\s+status\b", re.I | re.S)
_STATUS_VALUE_RE = re.compile(r"\b(?:THEN|ELSE)\s+'([^']*)'", re.I)
_NESTED_CASE_RE = re.compile(r"\bCASE\b(?:(?!\bCASE\b|\bEND\b).)*?\bEND\b", re.I | re.S)


def status_labels(span: str) -> list[str]:
    """The status CASE's own THEN/ELSE labels, with nested CASE...END blocks stripped first so a
    categorical CASE used inside a WHEN condition (e.g. `(CASE ... ELSE 'none' END) = 'none'`)
    can't leak its branch literals into the status label set."""
    prev = None
    while prev != span:
        prev = span
        span = _NESTED_CASE_RE.sub(" ", span)
    return _STATUS_VALUE_RE.findall(span)

# A2: hard-coded time windows that must have become :period_days.
WINDOW_BAD = [
    (re.compile(r"INTERVAL\s+\d+\s+DAYS?", re.I), "hard-coded 'INTERVAL <n> DAYS' (use :period_days)"),
    (re.compile(r"date_add\(\s*current_date\(\)\s*,\s*-\d+"), "hard-coded date_add() window (use :period_days)"),
    (re.compile(r"dateadd\(\s*day\s*,\s*-\d+", re.I), "hard-coded dateadd() window (use :period_days)"),
    (re.compile(r"date_sub\(\s*current_date\(\)\s*,\s*\d+"), "hard-coded date_sub() window (use :period_days)"),
]


def lint_file(path: Path, all_ids: set[str]) -> list[str]:
    errs: list[str] = []
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT)

    try:
        hdr = hs.parse_header(text, path=str(rel))
    except hs.HeaderError as e:
        return [str(e)]

    for f in hs.FIELDS:
        if f == "domain":
            continue  # domain + tier ride on the combined 'domain:' line, validated below
        if not str(hdr.get(f, "")).strip():
            errs.append(f"{rel}: field '{f}' is empty")

    if hdr["query_id"] != path.stem:
        errs.append(f"{rel}: query_id '{hdr['query_id']}' != filename '{path.stem}'")
    if hdr["domain"] != path.parent.name:
        errs.append(f"{rel}: domain '{hdr['domain']}' != folder '{path.parent.name}'")
    if hdr["tier"] not in hs.TIER_ENUM:
        errs.append(f"{rel}: tier '{hdr['tier']}' not in {sorted(hs.TIER_ENUM)}")
    if hdr["confidence"] not in hs.CONFIDENCE_ENUM:
        errs.append(f"{rel}: confidence '{hdr['confidence']}' not in {sorted(hs.CONFIDENCE_ENUM)}")
    rn = hdr["_raw"].get("runnable")
    if rn is not None and not rn.strip().lower().startswith(("true", "false")):
        errs.append(f"{rel}: optional 'runnable' must start with 'true' or 'false', got {rn!r}")
    for tok in hdr["empty_if"]:
        if tok not in hs.EMPTY_IF_VOCAB:
            errs.append(f"{rel}: empty_if token '{tok}' not in vocabulary {sorted(hs.EMPTY_IF_VOCAB)}")

    # SQL body = everything after the leading comment block.
    body = "\n".join(l for l in text.splitlines() if not l.startswith("--"))

    # P4-03: a declared not_assessed_reasons code that the query body never emits is stale header
    # text (the query changed and the header did not) -- caught here rather than trusted forever.
    for code in hdr["not_assessed_reasons"]:
        if f"'{code}'" not in body:
            errs.append(f"{rel}: not_assessed_reasons code '{code}' not found in the query body")

    # A2 window hygiene: no hard-coded windows anywhere. The only allowed rolling window is
    # :period_days. Genuine point-in-time snapshots (no window at all) are allowed to omit it.
    # NOTE: WINDOW_BAD scans `body` (SQL only), NOT the header — an "INTERVAL 30 DAYS" or a
    # "not populated before Nov 2025" phrase in a caveats: sentence is prose, not an executable
    # window. FORBIDDEN (below) deliberately scans the FULL file text, header included.
    for rx, label in WINDOW_BAD:
        if rx.search(body):
            errs.append(f"{rel}: {label}")
    param_names = {p["name"] for p in hdr["params"]}
    if ":period_days" in body and "period_days" not in param_names:
        errs.append(f"{rel}: body uses :period_days but params does not declare it")

    for qid, cond in [(n["query_id"], n["if"]) for n in hdr["next"]]:
        if qid not in all_ids:
            errs.append(f"{rel}: next target '{qid}' is not a real query_id")

    for rx, label in FORBIDDEN:
        if rx.search(text):
            errs.append(f"{rel}: forbidden {label}")

    # H4: any query that emits a `status` column may only emit the runner's four band labels,
    # so the scorecard (which tallies exactly this set) can never silently drop an off-band value.
    for span in _STATUS_CASE_RE.findall(body):
        for lit in status_labels(span):
            if lit not in STATUS_ENUM:
                errs.append(f"{rel}: status label '{lit}' not in status enum {sorted(STATUS_ENUM)}")

    return errs


def main() -> int:
    files = list(hs.iter_query_files(QUERIES))
    # An app-owned query's `next:` field is allowed to point at a vendored library query id
    # (see T-10's header), so the valid-id set is the union of app-owned and vendored ids.
    # hs.iter_query_files uses Path.rglob("*.sql"), which yields nothing (does not raise) on a
    # directory that does not exist yet -- app/queries/vendored is only created by T-02, so this
    # set is simply empty until then.
    vendored_files = list(hs.iter_query_files(QUERIES_VENDORED))
    all_ids = {p.stem for p in files} | {p.stem for p in vendored_files}
    errs: list[str] = []
    for p in files:
        errs.extend(lint_file(p, all_ids))

    print(f"linted {len(files)} query files")
    if errs:
        print(f"\n{len(errs)} violation(s):", file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)

    # Generated-artifact sync check: manifest.json is generated from these headers, so it must
    # be in sync (a drift means someone hand-edited a generated file). Unlike the public library,
    # this app has no build_sqlfluff_params.py / build_lineage.py -- those are not copied here.
    def _sync_ok(script: str) -> bool:
        r = subprocess.run(
            [sys.executable, str(ROOT / "tools" / script), "--check"],
            capture_output=True, text=True,
        )
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)  # advisory "note:" lines (e.g. conflicting param defaults) show here
        return r.returncode == 0

    manifest_ok = _sync_ok("build_manifest.py")

    if errs or not manifest_ok:
        return 1
    print("all header + manifest checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
