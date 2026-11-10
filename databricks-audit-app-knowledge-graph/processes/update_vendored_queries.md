# Process: update vendored queries

The 103 vendored checks in `app/queries/vendored/` are our own copies of the public library, pinned by `config/vendored.lock`; fix them in place and relock, or re-sync from an upstream clone with `--commit`.

```mermaid
flowchart TB
  START{What changed?}
  START -->|fix a vendored query here| E[edit app/queries/vendored/&lt;domain&gt;/&lt;id&gt;.sql]
  E --> RL[python tools/sync_queries.py --relock --item &lt;item id&gt;]
  RL --> LC[add or update entry in config/library_corrections.yml]
  START -->|pull new upstream version| UP[local clone of crosshire-audit-databricks-admin<br/>or AUDIT_QUERY_SOURCE]
  UP --> CM[python tools/sync_queries.py --commit &lt;sha&gt; [--allow-dirty]]
  CM -->|diverged file would change| REF[refused; list item ids<br/>--force discards the fix]
  CM --> LOCK[config/vendored.lock rewritten]
  RL --> LOCK
  LC --> REGEN
  LOCK --> REGEN[generate_models.py, generate_direct_models.py,<br/>build_direct_sql.py]
  REGEN --> CHK[python tools/sync_queries.py --check<br/>part of gate --quick]
  CHK --> COMMIT[commit file + lock together]
```

## Steps: in-place fix (usual)

1. Edit the vendored `.sql` (rule DEC-66.2: fixes go in our copy, never upstream) (`tools/sync_queries.py:5-11`).
2. `python tools/sync_queries.py --relock --item <id>`: recomputes `files` hashes and `diverged`; an uncommitted vendored change needs `--item` or it exits 1 (`tools/sync_queries.py:39-56`).
3. Disclose the defect in `config/library_corrections.yml` (`app/core/library_corrections.py:3-17`, example entry `config/library_corrections.yml:3-14`).
4. Regenerate downstream: `python tools/generate_models.py --only <id>`, `python tools/generate_direct_models.py --only <id>`, `python tools/build_direct_sql.py` (inferred from `flows/add_a_check.md` steps 5-7; generator inputs `tools/generate_models.py:12-37`).
5. `python tools/sync_queries.py --check` passes (`tools/sync_queries.py:58-63`), then commit the file and the lock together (`tools/sync_queries.py:49-51`).

## Steps: re-sync from upstream

1. Clean upstream clone: `python tools/sync_queries.py --commit <sha> [--source <path>]` extracts via `git archive` (`tools/sync_queries.py:13-27`).
2. Dirty clone: needs `--allow-dirty`; `<sha>` must match HEAD; lock records `dirty: true` and `dirty_files` (`tools/sync_queries.py:28-34`). Current lock is dirty (`config/vendored.lock:16-17`).
3. Refuses to overwrite a diverged file unless `--force` (`tools/sync_queries.py:17-23`).
4. Copies 7 domains' `.sql`, `queries/manifest.json`, `lineage/sources.yml`, the catalog schema dump and LICENSE (`tools/sync_queries.py:14-16`).

## Lock facts

- `commit` `f700dd5`, `branch` `long-running-runs`, 103 queries (`config/vendored.lock:2-15`).
- `diverged`: 51 files with fixes (`config/vendored.lock:33`); `files`: 107 hashes (`config/vendored.lock:195`).
- `url` is the upstream git remote, never a local path (`config/vendored.lock:305`, `tools/sync_queries.py:65-67`).
- Hashes over LF-normalised bytes (`tools/sync_queries.py:86-93`).

## Related

- `tools/build_fixture_ddl.py` reads `app/queries/vendored/databricks_system_catalog_schema.txt`; rerun if the dump changes (`tools/build_fixture_ddl.py:1-10`).
- `tools/build_sources.py` reads `app/queries/vendored/lineage/sources.yml` -> `dbt/models/sources/sources.yml` (`tools/build_sources.py:1-14`).
