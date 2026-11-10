# databricks-audit-app knowledge graph

A committed map of the Crosshire repo: 8227 nodes and 23076 edges, each pointing at `path:line` in the code, plus hand-written pages on modules, flows, rules, data and processes.

- Built: 2026-11-10
- Commit: `b8d2db1` (`b8d2db1d5b78cfbebc05c75715cf7467c5ab6f17`)
- Per-file SHA-256 of the 1227 tracked files at build time: `meta.json` (`file_hashes`).

## How to use it

```mermaid
flowchart LR
  Q[Question] --> I[index.md<br/>pick a page]
  I --> P[page<br/>modules / flows / logic / data / processes / documents]
  Q --> G[grep graph/nodes.jsonl<br/>by id or name]
  G --> E[grep graph/edges.jsonl<br/>by from / to]
  P --> C[open path:line<br/>in the repo]
  E --> C
```

1. Start at [index.md](index.md): big picture, entry points, and a "where to look" table for every page.
2. Look up a node by id: `grep '"id": "function:app/core/data.py:' graph/nodes.jsonl`.
3. Find what touches it: `grep '"to": "table:system.billing.usage"' graph/edges.jsonl` (callers, readers) or `"from": ...` (what it calls or reads).
4. Open the cited `path:line` in the repo to confirm. `evidence` can be a line, a range `a-b`, or a list `a-b,c`.

**ID patterns**

| Kind | Example |
|---|---|
| File / folder | `file:app/api/app.py`, `folder:web/src/components` |
| Python | `function:app/core/rollup.py:_build_tree`, `class:app/api/app.py:_PrincipalMiddleware` (methods `Class.method`) |
| Test | `test_case:tests/test_api.py:test_x` |
| API | `endpoint:GET /api/status` |
| dbt | `dbt_model:cost_alloc`, `dbt_source:system_billing.usage`, `dbt_test:<model>:<test>[:<col>]`, `dbt_macro:<name>`, `dbt_seed:workspace_env_overrides` |
| Data | `table:system.billing.usage`, `table:databricks_audit_dashboard.<x>`, `column:<model>.<col>` |
| UI | `ui_screen:tab_cost`, `ui_screen:tab_cost:trend`, `ui_screen:dashboard:lh06_cost` |
| Semantic | `business_rule:<name>`, `flow:<name>`, `process:<name>`, `document:<path>`, `example_data:<path>` |
| Config | `config:<path>`, `config:env:<NAME>`, `secret_ref:<NAME>` (name only, never a value) |

Status: `extracted` = read straight from code; `inferred` = derived (e.g. fixture writes, screen -> `f_<id>` reads); `unclear` = not settled.

## Layout

| Path | What | Made by |
|---|---|---|
| `index.md` | Entry page | model |
| `modules/`, `flows/`, `logic/`, `data/`, `processes/`, `documents/` | Reference pages, `path:line` on every fact | model |
| `data/model.json` | Entities, keys, relations of the DuckDB data model | model-run script |
| `graph/nodes.base.jsonl`, `graph/edges.base.jsonl` | Static extraction of every tracked file | `_scripts/extract.py` |
| `graph/extra/*.jsonl` | Flows, data model, logic, web, dashboards/processes/docs additions | model-run scripts |
| `graph/nodes.jsonl`, `graph/edges.jsonl` | Merged graph (use these) | `_scripts/merge.py` |
| `meta.json` | Build date, commit, file hashes, counts, what made each file | script |

## Stats (merged graph)

Nodes: 8227 (extracted 8184, inferred 43). Edges: 23076 (extracted 22331, inferred 745).

| Node type | Count |
|---|---|
| function | 2618 |
| test_case | 1546 |
| file | 956 |
| column | 883 |
| dbt_test | 645 |
| dbt_model | 341 |
| table | 252 |
| module | 245 |
| ui_screen | 187 |
| config | 144 |
| class | 96 |
| folder | 71 |
| dbt_source | 49 |
| example_data | 42 |
| business_rule | 40 |
| endpoint | 35 |
| document | 21 |
| process | 20 |
| dbt_macro | 10 |
| flow | 8 |
| external_system | 7 |
| secret_ref | 6 |
| directory | 2 |
| database | 1 |
| dbt_seed | 1 |
| repo | 1 |

| Edge type | Count |
|---|---|
| calls | 6501 |
| contains | 6140 |
| reads | 3266 |
| part_of | 1388 |
| derived_from | 1186 |
| imports | 895 |
| configures | 889 |
| depends_on | 808 |
| illustrates | 782 |
| writes | 415 |
| documents | 414 |
| implements | 257 |
| exposes | 68 |
| deploys | 23 |
| fk | 17 |
| ref | 16 |
| triggers | 8 |
| grants | 2 |
| schedules | 1 |


## Known gaps

- Python calls: only direct names, module aliases, imported `Class.method` and `self.method`; calls on instances, callbacks and `importlib` loads by path are missed.
- TypeScript: only top-level exports are nodes; no TS-to-TS call graph beyond `Api.*` and direct `/api` fetches.
- SQL reads: `FROM`/`JOIN` on `system.*` or `databricks_audit_dashboard.*` only; comma joins are missed.
- Screen -> `dbt_model:f_<id>` reads are inferred (the UI only calls `/api/finding/{id}`).
- The steps in `flows/add_a_check.md` are inferred; the repo does not document their order.

## How to update

Run from the repo root:

```
python databricks-audit-app-knowledge-graph/_scripts/extract.py     # rebuild graph/*.base.jsonl (~40 s)
python databricks-audit-app-knowledge-graph/_scripts/merge.py       # base + extra -> graph/nodes.jsonl, edges.jsonl
python databricks-audit-app-knowledge-graph/_scripts/validate.py    # 0 errors expected; prints random evidence lines to eyeball
python databricks-audit-app-knowledge-graph/_scripts/stats.py       # recompute counts in README.md and meta.json
```

- `merge.py` dedupes nodes by id and edges by (from, to, type), keeping the best status. It maps a dangling id to an existing one where it can (function<->class, `tab_queries` -> `tab_query`, endpoint params), else adds an `unclear` stub. It also trims `line_end` values that run one line past the end of the file.
- The extractor does not regenerate `graph/extra/*.jsonl` or the Markdown pages. After a code change, compare the `file_hashes` in `meta.json` with the new files to find the stale pages. Re-check their `path:line` citations, or regenerate them, before you trust them.
- After the update, recompute `meta.json` (hashes, counts, commit).
