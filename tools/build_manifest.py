#!/usr/bin/env python3
"""Generate app/queries/app/manifest.json FROM the v2 SQL headers (single source of truth).

The headers in app/queries/app/<domain>/*.sql are authoritative; this script parses them and
emits a deterministic, domain-keyed manifest. Never hand-edit manifest.json.

    python tools/build_manifest.py            # regenerate manifest.json in place
    python tools/build_manifest.py --check     # exit 1 if manifest.json is stale (CI)

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import header_schema as hs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "app" / "queries" / "app"
MANIFEST = QUERIES / "manifest.json"

# Manifest entry field order (A8 contract). confidence_note is included as a bonus.
ENTRY_FIELDS = [
    "query_id",
    "title",
    "domain",
    "tier",
    "stars",
    "runnable",
    "empty_if",
    "reads",
    "requires",
    "params",
    "confidence",
    "confidence_note",
    "read_this",
    "healthy",
    "investigate_if",
    "actions",
    "next",
    "sql_url",
    "learn_url",
]


def entry_for(hdr: dict) -> dict:
    qid = hdr["query_id"]
    domain = hdr["domain"]
    e = {
        "query_id": qid,
        "title": hdr["title"],
        "domain": domain,
        "tier": hdr["tier"],
        "stars": hdr["stars"],
        "runnable": hdr["runnable"],
        "empty_if": hdr["empty_if"],
        "reads": hdr["reads"],
        "requires": hdr["requires"],
        "params": hdr["params"],
        "confidence": hdr["confidence"],
        "confidence_note": hdr["confidence_note"],
        "read_this": hdr["read_this"],
        "healthy": hdr["healthy"],
        "investigate_if": hdr["investigate_if"],
        "actions": hdr["actions"],
        "next": hdr["next"],
        # DEC-05: this app's sql_url is a repo-relative path to the app-owned query, not the
        # public library's GitHub REPO constant; learn_url is left empty (the public LEARN
        # constant is not used here). header_schema.REPO / .LEARN are still imported unchanged
        # above but are simply not consulted for these two fields any more.
        "sql_url": f"app/queries/app/{domain}/{qid}.sql",
        "learn_url": "",
    }
    return {k: e[k] for k in ENTRY_FIELDS}


def build() -> dict:
    manifest: dict[str, list] = {}
    for p in hs.iter_query_files(QUERIES):
        hdr = hs.parse_header(p.read_text(encoding="utf-8"), path=str(p))
        manifest.setdefault(hdr["domain"], []).append(entry_for(hdr))
    # Every domain must be a known schema domain (the linter also enforces domain == folder).
    # Fail loudly on an unknown domain rather than bucketing it to a non-deterministic 99 tail
    # that would make the byte-for-byte --check gate flaky across a rename.
    unknown = sorted(set(manifest) - hs.DOMAINS)
    if unknown:
        raise hs.HeaderError(f"unknown domain(s) {unknown} — not in header_schema.DOMAINS")
    # Deterministic ordering: domains in schema order, entries by query_id.
    domain_order = sorted(hs.DOMAINS)
    ordered = {}
    for domain in sorted(manifest, key=domain_order.index):
        ordered[domain] = sorted(manifest[domain], key=lambda e: e["query_id"])
    return ordered


def serialize(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if manifest.json is stale")
    args = ap.parse_args()

    text = serialize(build())
    if args.check:
        # newline="" so a CRLF-corrupted file on disk (e.g. from a write before this fix) shows up
        # as stale instead of silently comparing equal after universal-newline translation.
        # (Path.read_text has no newline= param before Python 3.13, so this uses open() directly.)
        current = ""
        if MANIFEST.exists():
            with open(MANIFEST, "r", encoding="utf-8", newline="") as f:
                current = f.read()
        if current != text:
            print("manifest.json is STALE — run: python tools/build_manifest.py", file=sys.stderr)
            return 1
        print("manifest.json is in sync.")
        return 0
    # newline="\n": the repo is LF-only. Without this, Path.write_text's universal-newline
    # translation turns every "\n" into os.linesep on Windows, corrupting manifest.json to CRLF.
    MANIFEST.write_text(text, encoding="utf-8", newline="\n")
    n = sum(len(v) for v in json.loads(text).values())
    print(f"wrote {MANIFEST} ({n} queries across {len(json.loads(text))} domains)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
