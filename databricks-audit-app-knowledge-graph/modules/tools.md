# tools/ — command-line scripts

Every `tools/*.py`: what it does, its CLI, what it reads and writes. Runtime path (export → load → serve → deploy) first, then build/generate, dashboards and gates.

```mermaid
flowchart LR
  subgraph runtime["Run the app"]
    FR["first_run.py"] -->|subprocess| EX["export_direct_results.py"]
    FR -->|subprocess| LD["load_direct_results.py"]
    EX -->|connect()| SN["snapshot.py"]
    EX --> TD["table_detail.py"]
    EX --> WA["warehouse_activity.py"]
    EX --> WSA["workspace_activity.py"]
    DEP["deploy_app.py"] -->|writes app.yaml command| AS["app_start.py"]
    AS -->|runpy| API["python -m app.api"]
  end
  subgraph build["Generate SQL / models"]
    SQ["sync_queries.py"] --> GM["generate_models.py"]
    GDM["generate_direct_models.py"] --> BDS["build_direct_sql.py"]
    BS["build_sources.py"]
    BM["build_manifest.py"] --> HS["header_schema.py"]
    LH["lint_headers.py"] --> HS
    FDDL["build_fixture_ddl.py"]
    DBT["dbt_run.py"]
  end
  subgraph dash["Databricks dashboards"]
    BDD["build_databricks_dashboards.py"] --> KIT["dashboard_kit.py"]
    BDIR["build_direct_dashboards.py"] --> DOC["direct_dashboard_doc.py"]
    BDIR --> JH["direct_jobs_health.py"]
    BDIR --> GOV["direct_governance.py"]
    BDIR --> DU["direct_data_usage.py"]
    BDIR --> DL["direct_dashboard_load.py"]
    DOC --> GD["direct_guides.py"]
    DOC --> ST["direct_stories.py"]
    QD["dashboard_query_doc.py"]
  end
  GATE["gate.py"] --> FC["fixture_coverage.py"]
  GATE --> DBT
```

Edge evidence: `tools/first_run.py:112-120`, `tools/export_direct_results.py:42-47`, `tools/export_direct_results.py:1145`, `tools/deploy_app.py:146-150`, `tools/app_start.py:39`, `tools/direct_dashboard_doc.py:14`, `tools/direct_guides.py:9-10`, `tools/direct_dashboard_load.py:9-12`, `tools/direct_governance.py:12-14`, `tools/direct_jobs_health.py:9-13`, `tools/direct_data_usage.py:13-14`, `tools/header_schema.py:3-5`, `tools/gate.py:4-7`.

## Runtime tools

### first_run.py — export + load in one command
- Purpose: run `export_direct_results.py` then `load_direct_results.py`, stop at the first failure, print `python -m app.api` and `http://127.0.0.1:8000/` (`tools/first_run.py:1-35`, `tools/first_run.py:56-57`).
- CLI: `--windows DAYS` (default: settings `default_window` via the export) (`tools/first_run.py:79-83`). Refuses any flag containing token/password/secret/credential (`tools/first_run.py:64`, `tools/first_run.py:84-91`).
- Writes: nothing itself; picks `<data dir>/results/<UTC stamp %Y%m%dT%H%M%SZ>` (`tools/first_run.py:98-106`).
- Calls: `export_argv` → `[python, tools/export_direct_results.py, --out, dir, (--windows N)]` (`tools/first_run.py:112-116`); `load_argv` → `[python, tools/load_direct_results.py, dir]` (`tools/first_run.py:119-120`); both with `cwd=ROOT` (`tools/first_run.py:126-129`).
- Exit: propagates the failing step's code; 2 → hint about `.env.example` (`tools/first_run.py:132-148`, `tools/first_run.py:162-194`).

### export_direct_results.py — run checks on a SQL warehouse
- Purpose: run every Databricks-direct query (`app/direct_sql/*.sql`) on the warehouse, save rows as parquet outside the repo (`tools/export_direct_results.py:1-7`, `tools/export_direct_results.py:50-53`).
- CLI (`tools/export_direct_results.py:1416-1454`): `--out DIR` (default `<data dir>/results`), `--max-rows N` (50,000, `tools/export_direct_results.py:183`), `--windows DAYS` (settings `default_window`), `--threads N` (4, `tools/export_direct_results.py:185`), mutually exclusive `--only ID…` / `--domain NAME` / `--tags TABLE…` / `--sizes-only`, `--no-table-sizes`, `--no-volumes`, `--storage-from DIR`, `--mask-users`.
- Credentials: `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN` (token optional when the app principal is used), optional `AUDIT_DBX_CATALOG`, from env or `.env` (`tools/export_direct_results.py:189-194`, `tools/export_direct_results.py:816-873`). Errors are redacted (`tools/export_direct_results.py:196-200`, `tools/export_direct_results.py:804-813`).
- Reads: `dbt/models/databricks_direct/generated_manifest.json` (`tools/export_direct_results.py:50`, `tools/export_direct_results.py:1130-1135`), `app/direct_sql/**` (`tools/export_direct_results.py:51-53`), settings (`include_today`, `default_window`, `table_sizes`, `query_tags`, `sources`) (`tools/export_direct_results.py:203-216`, `tools/export_direct_results.py:1201`), `config/app.yml rls:` (`tools/export_direct_results.py:1175`). On Databricks: `system.*` tables via the checks; the rls table (`tools/export_direct_results.py:567-584`).
- Writes (under `<out>.tmp`, renamed to `<out>` on success): `findings/<qid>.parquet` (`tools/export_direct_results.py:985`), `dims/<name>.parquet` (`tools/export_direct_results.py:474-485`), tags, policies, `entitlements/entitlements.parquet` (`tools/export_direct_results.py:574-576`), `storage/`, `report.json`, `report.md`, `manifest.json` (`tools/export_direct_results.py:1289-1322`), swap at `tools/export_direct_results.py:1331-1340`. Refuses an `--out` inside the repo (`tools/export_direct_results.py:1125-1129`).
- Main: `run_export(out_dir, *, max_rows, windows, threads, only, domain, tags, mask_users, …)` (`tools/export_direct_results.py:1091-1350`); `main(argv)` exit 0/2 (`tools/export_direct_results.py:1457-1528`).

### load_direct_results.py — build the local DuckDB file
- Purpose: load an export folder or `.zip` into DuckDB: `findings.f_<qid>`, `dims.*` (empty stand-ins when missing), `tags.*`, `governance.*`, `storage.*`, `usage.*`, `warehouse.*`, `direct_export_meta`, `direct_export_findings_meta`, `<db>.run_results.json` (`tools/load_direct_results.py:1-8`).
- CLI: `SRC` (default newest `results/<stamp>`), `--db PATH` (default `<data dir>/audit.duckdb`; outside repo or `data/*.duckdb` other than `db_audit.duckdb`) (`tools/load_direct_results.py:887-905`, `tools/load_direct_results.py:89-97`).
- Steps: build `<db>.tmp` with `load.memory_limit`/`threads` (`tools/load_direct_results.py:517`, `tools/load_direct_results.py:538-547`); findings (`:580`), dims (`:609`, stubs `:620`), tags (`:629-685`), `apply_bill_to_perf` (`:702`), `workspace_file.apply` (`:706-707`), governance (`:717-727`), storage (`:738-765`), usage (`:775-776`), warehouse (`:793-794`), rls entitlements + columns (`:804-808`), meta tables (`:813`, `:836`); laptop rls cut (`:851-852`); `os.replace(tmp, db)` (`:853`); run_results sidecar (`:863`) — all `tools/load_direct_results.py`.
- After load into the data folder: `datasource.set_current_db(db)` (`tools/load_direct_results.py:927-932`).

### snapshot.py — connector + raw system-table snapshot
- Purpose: the one module that opens a Databricks SQL connection; also a standalone raw parquet snapshot for the dbt path (`tools/snapshot.py:1-47`).
- `connect(timeout, dotenv_path)` — token auth, or OAuth M2M as the app's service principal when there is no token but `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` exist (`tools/snapshot.py:170-177`, `tools/snapshot.py:473-555`). Tags the session (`SET QUERY_TAGS`, `SET TIME ZONE 'UTC'`) (`tools/snapshot.py:77-99`); maps `settings.sources` (`tools/snapshot.py:549-552`); reads `current_metastore()` (`tools/snapshot.py:418`, `tools/snapshot.py:554-555`).
- CLI (snapshot mode): `--out` (`snapshot`), `--days` (30), `--billing-days`, `--workspace ID…`, `--only`, `--exclude-schema`, `--max-rows-per-file` (500000), `--timeout` (600), `--resume` (`tools/snapshot.py:770-796`).
- Writes: `<out>/<schema>__<table>/part-NNNNN.parquet`, `_MISSING.json`, `manifest.json` (`tools/snapshot.py:18-38`). `run_export(opts, …)` (`tools/snapshot.py:1458`), `main` exit 0/1/2/130 (`tools/snapshot.py:1577-1593`).

### table_detail.py — table and volume sizes
- `export(connect_fn, out_dir, *, previous, threads=16, …)` runs `DESCRIBE DETAIL` / `DESCRIBE HISTORY` per Delta table (`tools/table_detail.py:1-15`, `tools/table_detail.py:169`); `export_volumes(...)` lists volumes (`tools/table_detail.py:402`); `source: stored` reads the sizes job tables (`tools/table_detail.py:11-12`, `tools/table_detail.py:98`). Writes `storage/table_detail.parquet`, `storage/table_history.parquet` (`tools/table_detail.py:14-15`). No CLI; called by the export (`tools/export_direct_results.py:45`, `tools/export_direct_results.py:1221-1230`).

### warehouse_activity.py — per-warehouse detail
- SQL builders `daily_sql`, `users_sql`, `tables_sql`, `slow_sql` and `export(connect_fn, out_dir, *, end, windows, …)` (`tools/warehouse_activity.py:65-189`); writes `warehouse/<name>.parquet` (`tools/warehouse_activity.py:13`). No CLI; called by the export (`tools/export_direct_results.py:46`).

### workspace_activity.py — who is active where
- `activity_sql`, `catalog_sql`, `table_access_sql`, … , `maintenance_sql` (`tools/workspace_activity.py:144-396`); outputs `workspace_activity`, `catalog_workspace`, `table_access`, `user_access`, `user_tables`, `user_last_seen`, `volume_access` (`tools/workspace_activity.py:1-14`). No CLI; called by the export (`tools/export_direct_results.py:47`).

### deploy_app.py — deploy as a Databricks App
- Purpose: stage `app/ config/ dbt/ tools/` + `app.yaml` + DuckDB wheel parts, sync to the workspace, create/update/start/deploy the app via the Databricks CLI (`tools/deploy_app.py:1-11`).
- CLI: none (`tools/deploy_app.py:4`). Reads `config/app.yml` (+ `app.local.yml`): `name`, `run_as` (`user`|`app`), `rls` (`tools/deploy_app.py:55-76`); credentials via `export_direct_results._preflight_env` (`tools/deploy_app.py:177`); `requirements.txt` `duckdb==` pin (`tools/deploy_app.py:106-110`).
- Writes: `<data dir>/app-deploy/wheels/*.whl` (`tools/deploy_app.py:113-124`, `tools/deploy_app.py:184-186`), `<data dir>/app-deploy/<name>/` with `wheels/*.partNN` (≤ 9 MB) and `app.yaml` (`tools/deploy_app.py:127-151`).
- Remote: `databricks current-user me`, `sync`, `apps get/create/update/start/deploy` (`tools/deploy_app.py:189-207`); prints `GRANT` SQL for `run_as: app` (`tools/deploy_app.py:92-103`, `tools/deploy_app.py:208-213`). Exit 0 / 2 (`tools/deploy_app.py:173-219`). Full process: `processes/deploy_databricks_app.md`.

### app_start.py — Databricks App start command
- `unpack(parts, target)` joins `wheels/*.whl.part*` into the wheel and extracts it to `.pydeps` once (`tools/app_start.py:21-31`); `main()` prepends `.pydeps` and repo root to `sys.path`/`PYTHONPATH` and runs `app.api` as `__main__` (`tools/app_start.py:34-39`). No CLI.

### dbt_run.py — dbt wrapper (dev/test path)
- CLI: `build --target dev|test [--select S] [--exclude S]` (`tools/dbt_run.py:492-508`).
- Builds `--vars` from thresholds, windows, tag aliases, share/coverage floors, masking, `as_of` (`tools/dbt_run.py:10-24`, `tools/dbt_run.py:210`); sets `AUDIT_SNAPSHOT_DIR` / `AUDIT_SEED_DIR` (`tools/dbt_run.py:352-364`); dev builds into `<db>-tmp-<pid>.duckdb` seeded from `AUDIT_DB` (default `data/db_audit.duckdb`) and swaps with `os.replace` (`tools/dbt_run.py:369-386`, `tools/dbt_run.py:262-267`); exit 3 = built with some failures (`tools/dbt_run.py:38-50`). Applies `config/workspace.csv` (`tools/dbt_run.py:325-330`).

## Generate / build tools

| Tool | Purpose | CLI | Reads → writes | Evidence |
|---|---|---|---|---|
| `build_direct_sql.py` | Render Databricks-direct models to plain SQL so the export needs no dbt | `--check` | dbt direct models + `config/thresholds.yml` → `app/direct_sql/` | `tools/build_direct_sql.py:1-7`, `tools/build_direct_sql.py:147-149` |
| `generate_direct_models.py` | Every executable query → Databricks-direct dbt model | `--only ID…` / `--domain`, `--check`, `--force` | registry → `dbt/models/databricks_direct/` (+ `generated_manifest.json`) (folder inferred from `tools/export_direct_results.py:50`) | `tools/generate_direct_models.py:1-13`, `tools/generate_direct_models.py:336-342`, `tools/export_direct_results.py:50` |
| `generate_models.py` | Vendored/app queries → DuckDB dbt models via `translate` | `--only` / `--domain`, `--check`, `--force` | registry, `app/core/translate.py` → dbt models | `tools/generate_models.py:1-13`, `tools/generate_models.py:777-783` |
| `build_sources.py` | `sources.yml` for dbt from vendored lineage | `--plan`, `--check` | `app/queries/vendored/lineage/sources.yml` → `dbt/models/sources/sources.yml`, `config/snapshot_plan.yml` | `tools/build_sources.py:1-14`, `tools/build_sources.py:267-273` |
| `build_manifest.py` | `app/queries/app/manifest.json` from v2 headers | `--check` | `app/queries/app/<domain>/*.sql` → manifest.json | `tools/build_manifest.py:1-11`, `tools/build_manifest.py:104-106` |
| `header_schema.py` | Shared v2 SQL header parser (library) | — | — | `tools/header_schema.py:1-8` |
| `lint_headers.py` | Header schema + hygiene lint | none | `app/queries/app/**` | `tools/lint_headers.py:1-13`, `tools/lint_headers.py:141` |
| `sync_queries.py` | Vendor the public query library, lock file | `--commit`, `--check`, `--relock`, `--item`, `--force`, `--allow-dirty`, `--source` | upstream → `app/queries/vendored/`, `config/vendored.lock` | `tools/sync_queries.py:1-14`, `tools/sync_queries.py:578-609` |
| `build_fixture_ddl.py` | `tests/fixtures/ddl.py` from the catalog dump | none | `app/queries/vendored/databricks_system_catalog_schema.txt` → `tests/fixtures/ddl.py` | `tools/build_fixture_ddl.py:1-14`, `tools/build_fixture_ddl.py:585` |

## Dashboards and docs tools

| Tool | Purpose | CLI | Evidence |
|---|---|---|---|
| `build_databricks_dashboards.py` | Lakehouse Health AI/BI dashboards (one per tab), snapshot notebook, README | `--catalog`, `--out-dir`, `--only TAB`, `--refresh-columns` | `tools/build_databricks_dashboards.py:1-10`, `tools/build_databricks_dashboards.py:1454-1466` |
| `dashboard_kit.py` | Shared page layout for per-tab dashboards (library) | — | `tools/dashboard_kit.py:1-14` |
| `build_direct_dashboards.py` | Dashboards reading `system.*` directly (Waste, Jobs health, Workspace usage, Broad grants, Admin changes, Data usage, Dashboard load) | `--out-dir`, `--query-tags`, `--workspace-class CLASS=PATTERN`, `--sizes-schema` | `tools/build_direct_dashboards.py:1-13`, `tools/build_direct_dashboards.py:952-963` |
| `direct_dashboard_doc.py` | HTML guide per direct dashboard (library, `build()`) | — | `tools/direct_dashboard_doc.py:1-7`, `tools/direct_dashboard_doc.py:709` |
| `direct_dashboard_load.py` | "Dashboard load" dashboard by query tags (library) | — | `tools/direct_dashboard_load.py:1-14` |
| `direct_data_usage.py` | Data usage dashboard + sizes notebook (library) | — | `tools/direct_data_usage.py:1-10`, `tools/direct_data_usage.py:1672` |
| `direct_governance.py` | Workspace usage / Broad grants / Admin changes dashboards (library) | — | `tools/direct_governance.py:1-9` |
| `direct_jobs_health.py` | Jobs health dashboard (library) | — | `tools/direct_jobs_health.py:1-6` |
| `direct_guides.py` | Explanatory text for the HTML guides (library) | — | `tools/direct_guides.py:1-6` |
| `direct_stories.py` | "How it works, with an example" sections, made-up ids (library) | — | `tools/direct_stories.py:1-9` |
| `dashboard_query_doc.py` | HTML guide to one dashboard's queries | positional argv (unclear beyond default) | `tools/dashboard_query_doc.py:1-5`, `tools/dashboard_query_doc.py:471` |

## Gates

| Tool | Purpose | CLI | Evidence |
|---|---|---|---|
| `gate.py` | Quick gate (lint, sync/generate checks, web checks, pytest) or full gate (fixtures + `dbt build --target test` + pytest + coverage) | `--quick` | `tools/gate.py:1-14`, `tools/gate.py:205-208` |
| `fixture_coverage.py` | Every executable check has a grain entry and a pytest assertion | `--check` | `tools/fixture_coverage.py:1-14`, `tools/fixture_coverage.py:379-385` |

Data files in `tools/`: `dashboard_columns.json` (rewritten by `--refresh-columns`, `tools/build_databricks_dashboards.py:1462-1463`), `dashboard_screens/` (per-tab screens, `tools/build_databricks_dashboards.py:3-4`).
