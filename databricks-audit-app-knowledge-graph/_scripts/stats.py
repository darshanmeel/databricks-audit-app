"""Recompute counts from graph/*.jsonl into README.md (Stats section) and meta.json."""
import json, re
from collections import Counter
from pathlib import Path

KG = Path(__file__).resolve().parent.parent
load = lambda f: [json.loads(l) for l in (KG / "graph" / f).read_text(encoding="utf-8").splitlines() if l.strip()]
nodes, edges = load("nodes.jsonl"), load("edges.jsonl")
nt, et = Counter(n["type"] for n in nodes), Counter(e["type"] for e in edges)
ns, es = Counter(n.get("status", "extracted") for n in nodes), Counter(e.get("status", "extracted") for e in edges)

table = lambda title, c: f"| {title} | Count |\n|---|---|\n" + "".join(f"| {k} | {v} |\n" for k, v in c.most_common())
stats = (f"## Stats (merged graph)\n\nNodes: {len(nodes)} (extracted {ns['extracted']}, inferred {ns['inferred']}). "
         f"Edges: {len(edges)} (extracted {es['extracted']}, inferred {es['inferred']}).\n\n"
         f"{table('Node type', nt)}\n{table('Edge type', et)}\n")
readme = KG / "README.md"
s = readme.read_text(encoding="utf-8")
s = re.sub(r"## Stats \(merged graph\)\n.*?(?=\n## )", stats, s, flags=re.S)
s = re.sub(r"\d+ nodes and \d+ edges", f"{len(nodes)} nodes and {len(edges)} edges", s, count=1)
readme.write_text(s, encoding="utf-8")

meta = json.loads((KG / "meta.json").read_text(encoding="utf-8"))
meta["counts"].update(nodes=len(nodes), edges=len(edges), nodes_by_type=dict(nt), edges_by_type=dict(et),
                      node_status=dict(ns), edge_status=dict(es))
(KG / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
print(len(nodes), len(edges))
