"""Validate the merged graph: python validate.py [N_SAMPLES] [--base]

Checks: unique node ids, required fields, every edge endpoint is a node, every path exists (repo
first, then the graph folder), line numbers within file length, evidence path:line valid (a line,
a range a-b, or a comma list). Prints counts by type and N random evidence
lines (default 15) so a human can eyeball that each edge's cited line really says what it claims.
--base checks nodes.base.jsonl / edges.base.jsonl instead of the merged files.
"""
import json
import random
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KG = Path(__file__).resolve().parents[1]
GRAPH = KG / "graph"
NODE_KEYS = {"id", "type", "name", "path", "line_start", "line_end", "summary", "tags", "status"}
EDGE_KEYS = {"from", "to", "type", "evidence", "status"}
STATUSES = {"extracted", "inferred", "unclear"}
_lines = {}


def where(rel):
    # repo paths first; flow/process pages written by the model live in the graph folder itself
    return REPO / rel if (REPO / rel).exists() or not (KG / rel).exists() else KG / rel


def line_ok(spec, n):
    # evidence may be one line, a range "a-b", or a list "a-b,c"
    parts = spec.split(",")
    try:
        nums = [[int(x) for x in part.split("-")] for part in parts]
    except ValueError:
        return False
    return all(len(r) <= 2 and r[0] <= r[-1] and all(1 <= x <= max(1, n) for x in r) for r in nums)


def file_lines(rel):
    if rel not in _lines:
        p = where(rel)
        _lines[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else None
    return _lines[rel]


def load(name):
    rows = []
    for i, ln in enumerate(open(GRAPH / name, encoding="utf-8"), 1):
        if ln.strip():
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError as e:
                print(f"BAD JSON {name}:{i}: {e}")
    return rows


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n_samples = int(args[0]) if args else 15
    suffix = ".base.jsonl" if "--base" in sys.argv else ".jsonl"
    nodes, edges = load("nodes" + suffix), load("edges" + suffix)
    errors = []
    ids = Counter(n.get("id") for n in nodes)
    errors += [f"duplicate node id {i} x{c}" for i, c in ids.items() if c > 1]
    for n in nodes:
        if NODE_KEYS - n.keys():
            errors.append(f"node {n.get('id')} missing {sorted(NODE_KEYS - n.keys())}")
        if n.get("status") not in STATUSES:
            errors.append(f"node {n.get('id')} bad status {n.get('status')}")
        p = n.get("path")
        if p and p != "." and not where(p).exists():
            errors.append(f"node {n['id']} path missing: {p}")
        lines = file_lines(p) if p and p != "." and where(p).is_file() else None
        ls, le = n.get("line_start"), n.get("line_end")
        if lines is not None and ls is not None:
            if not (1 <= ls <= max(1, len(lines))) or (le is not None and not (ls <= le <= max(1, len(lines)))):
                errors.append(f"node {n['id']} lines {ls}-{le} outside 1-{len(lines)}")
    for e in edges:
        if EDGE_KEYS - e.keys():
            errors.append(f"edge {e} missing {sorted(EDGE_KEYS - e.keys())}")
            continue
        for end in (e["from"], e["to"]):
            if end not in ids:
                errors.append(f"edge endpoint not a node: {end} ({e['type']} {e['evidence']})")
        if e.get("status") not in STATUSES:
            errors.append(f"edge {e['from']}->{e['to']} bad status")
        path, _, line = e["evidence"].rpartition(":")
        lines = file_lines(path) if path else None
        if lines is None:
            errors.append(f"edge evidence file missing: {e['evidence']}")
        elif not line_ok(line, len(lines)):
            errors.append(f"edge evidence line out of range: {e['evidence']}")
    print(f"nodes={len(nodes)} edges={len(edges)}")
    print("nodes by type:", dict(Counter(n.get("type") for n in nodes).most_common()))
    print("edges by type:", dict(Counter(e.get("type") for e in edges).most_common()))
    print("node status:", dict(Counter(n.get("status") for n in nodes)),
          "edge status:", dict(Counter(e.get("status") for e in edges)))
    print(f"\n{len(errors)} errors")
    for msg in errors[:50]:
        print("  " + msg)
    print(f"\n{n_samples} random evidence lines:")
    for e in random.sample(edges, min(n_samples, len(edges))):
        path, _, line = e["evidence"].rpartition(":")
        lines = file_lines(path) or []
        first = line.split(",")[0].split("-")[0]
        text = lines[int(first) - 1].strip() if first.isdigit() and 0 < int(first) <= len(lines) else "<n/a>"
        print(f"  [{e['type']}] {e['from']} -> {e['to']}\n      {e['evidence']}: {text[:140]}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
