"""Merge graph/nodes.base.jsonl + graph/extra/*.nodes.jsonl into graph/nodes.jsonl (same for edges).

python merge.py
- Nodes dedupe by id; keep the entry with the best status, then the most filled fields
  (ties go to the earlier file, base first). Tags are unioned.
- Edges dedupe by (from, to, type); keep the best status.
- A dangling edge endpoint is first matched to an existing id by normalizing it (function<->class,
  function<->test_case, endpoint path params, tab_x plural/singular, e.g. tab_queries -> tab_query). Only if that fails does it get
  a stub node with status "unclear".
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GRAPH = Path(__file__).resolve().parents[1] / "graph"
RANK = {"extracted": 0, "inferred": 1, "unclear": 2}


def read(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def filled(row):
    return sum(1 for v in row.values() if v not in (None, "", [], {}))


def better(new, old):
    a, b = RANK.get(new.get("status"), 3), RANK.get(old.get("status"), 3)
    return a < b or (a == b and filled(new) > filled(old))


def candidates(i):
    kind, _, rest = i.partition(":")
    swap = {"function": ["class", "test_case"], "class": ["function"], "test_case": ["function"]}
    for k in swap.get(kind, []):
        yield f"{k}:{rest}"
    if kind == "endpoint":
        yield re.sub(r"\{[^}]*\}", "{}", i)
    if kind == "ui_screen":
        yield i[:-3] + "y" if i.endswith("ies") else i[:-1] if i.endswith("s") else i + "s"


def main():
    files = [GRAPH / "nodes.base.jsonl"] + sorted((GRAPH / "extra").glob("*.nodes.jsonl"))
    nodes = {}
    for f in files:
        for n in read(f):
            old = nodes.get(n["id"])
            if old is None:
                nodes[n["id"]] = n
                continue
            keep = n if better(n, old) else old
            keep["tags"] = sorted(set(old.get("tags") or []) | set(n.get("tags") or []))
            nodes[n["id"]] = keep

    # some generators counted split("\n") lines, one past the last line; clamp to the real length
    clamped = 0
    for n in nodes.values():
        p = REPO / (n.get("path") or "")
        if n.get("path") and p.is_file() and n.get("line_end"):
            size = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            if size and n["line_end"] == size + 1:
                n["line_end"], clamped = size, clamped + 1

    # endpoint ids compared with params blanked, so {query_id} vs {id} still match
    norm = {re.sub(r"\{[^}]*\}", "{}", i): i for i in nodes if i.startswith("endpoint:")}
    remap, stubs = {}, {}

    def resolve(i, evidence):
        if i in nodes:
            return i
        if i in remap:
            return remap[i]
        for c in candidates(i):
            hit = c if c in nodes else norm.get(c)
            if hit:
                remap[i] = hit
                return hit
        path, _, line = evidence.rpartition(":")
        stubs[i] = {"id": i, "type": i.split(":")[0], "name": i.split(":", 1)[-1], "path": path,
                    "line_start": int(line) if line.isdigit() else None, "line_end": None,
                    "summary": "stub: referenced by an edge, no source node found",
                    "tags": ["stub"], "status": "unclear"}
        nodes[i] = stubs[i]
        return i

    edges = {}
    for f in [GRAPH / "edges.base.jsonl"] + sorted((GRAPH / "extra").glob("*.edges.jsonl")):
        for e in read(f):
            e["from"], e["to"] = resolve(e["from"], e["evidence"]), resolve(e["to"], e["evidence"])
            key = (e["from"], e["to"], e["type"])
            if key not in edges or better(e, edges[key]):
                edges[key] = e

    with open(GRAPH / "nodes.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for n in sorted(nodes.values(), key=lambda n: n["id"]):
            f.write(json.dumps(n, ensure_ascii=False) + "\n")
    with open(GRAPH / "edges.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for e in sorted(edges.values(), key=lambda e: (e["from"], e["to"], e["type"])):
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"nodes={len(nodes)} edges={len(edges)} remapped={len(remap)} stubs={len(stubs)} "
          f"line_end_clamped={clamped}")
    for a, b in sorted(remap.items()):
        print(f"  remap {a} -> {b}")
    for s in sorted(stubs):
        print(f"  stub {s}")


if __name__ == "__main__":
    main()
