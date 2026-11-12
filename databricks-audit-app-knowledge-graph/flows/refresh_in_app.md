# Flow: Refresh in the app

The top-bar Refresh (or "Get the last N days" on an empty app) calls `POST /api/refresh?days=N`; a background thread runs the export and the load as subprocesses into a **new** `audit-<stamp>.duckdb`, points the app at it, deletes old files, and the page reloads (`app/api/refresh.py:1-5`, `README.md:95-103`).

```mermaid
sequenceDiagram
  autonumber
  actor V as Viewer
  participant UI as web/src (RefreshControl / GetData)
  participant API as app/api/app.py
  participant R as app/api/refresh.py
  participant EX as tools/export_direct_results.py
  participant WH as Databricks SQL warehouse
  participant LD as tools/load_direct_results.py
  participant DS as app/core/datasource.py
  participant D as app/core/data.py
  V->>UI: pick days (7/15/30/60/90), click
  UI->>API: POST /api/refresh?days=N
  opt Databricks App with rls on
    API->>API: user in refresh.admins or unrestricted? else 403
  end
  API->>R: start(N)
  R-->>UI: status {running:true, step:"export"}
  R->>EX: subprocess --windows N --max-rows 500000 --out results/<stamp>
  EX->>WH: run checks (system.*)
  EX-->>R: exit 0
  R->>LD: subprocess results/<stamp> --db audit-<stamp>.duckdb
  LD-->>R: exit 0
  opt rls apply=export on a Databricks App
    R->>R: rls.build_sets(db) (step "split")
  end
  R->>DS: set_current_db(db)
  R->>R: os.environ["AUDIT_DB"]=db, remove old dbs/results
  loop every 3 s while running
    UI->>API: GET /api/refresh
    API-->>UI: {running, step, elapsed_s, error}
  end
  UI->>UI: step=="done" → window.location.reload()
  UI->>API: next requests
  API->>D: _db_path() → AUDIT_DB (new file), new shared connection
```

## Steps

1. Refresh control lists `REFRESH_DAYS = [7, 15, 30, 60, 90]` (`web/src/api.ts:7-8`); top bar `RefreshControl` (`web/src/App.tsx:212-232`) and empty-app `GetData` (`web/src/components/first_run.tsx:25-47`) both use `useRefresh` (`web/src/components/first_run.tsx:9-23`).
2. `Api.refreshStart(days)` → `POST /api/refresh?days=N` (`web/src/api.ts:78`).
3. `post_refresh(days: int = Query(30, ge=1, le=90))` → `refresh.start(days)` (`app/api/app.py:402-412`). In a Databricks App with rls on, only emails in `refresh.admins` (config/app.yml, app.local.yml wins) or users whose grants cover everything may start it; others get 403 (`app/api/app.py:391-399`, `app/api/app.py:405-410`, `config/app.yml:23-26`).
4. `start` returns current status if one is running; else sets `{running: True, step: "export", days, started_at}` under a lock and starts `_run` in a daemon thread (`app/api/refresh.py:34-41`).
5. `_run` names `stamp = %Y%m%d-%H%M%S` (local time), `out = <data>/results/<stamp>`, `db = <data>/audit-<stamp>.duckdb` (`app/api/refresh.py:51-55`).
6. Export: `python tools/export_direct_results.py --windows N --max-rows 500000 --out <out>` with `cwd=ROOT`; non-zero → `RuntimeError` with last 3 output lines; killed after `EXPORT_TIMEOUT_S` = 4 h (`app/api/refresh.py:57-58`, `app/api/refresh.py:44-48`, `app/api/refresh.py:20-21`). Export internals: `flows/export_load_serve.md` steps 4-9.
7. `step = "load"`; `python tools/load_direct_results.py <out> --db <db>`, killed after `LOAD_TIMEOUT_S` = 30 min (`app/api/refresh.py:59-60`, `app/api/refresh.py:22`). A timeout ends as `step: "failed"` (step 13).
8. Keep-set = new db + previous `AUDIT_DB` (`app/api/refresh.py:61-62`).
9. If `rls.mode() == "export"` and running as a Databricks App: `step = "split"`, `rls.build_sets(db)` writes one filtered db per grant set; their files join the keep-set (`app/api/refresh.py:63-66`, `app/core/rls.py:456-461`, `app/core/rls.py:515-547`).
10. `datasource.set_current_db(db)` writes `current_db.txt` atomically so a restart opens the new db (`app/api/refresh.py:67`, `app/core/datasource.py:55-62`).
11. `os.environ["AUDIT_DB"] = db` switches this process (`app/api/refresh.py:68`); `_base_db_path` reads `AUDIT_DB` first on every call (`app/core/data.py:203-209`).
12. Cleanup: every `audit-*.duckdb` except the new and the previous db (plus RLS set files) and orphan sidecars deleted; only the newest two `results/<stamp>` folders kept (`app/api/refresh.py:69-70`, `app/api/refresh.py:76-99`).
13. State → `{running: False, step: "done"}`; any exception → `step: "failed"`, `error: str(exc)`; old data stays (`app/api/refresh.py:71-73`, `README.md:100`).
14. UI polls `GET /api/refresh` every 3 s while running and reloads the page on `done` (`web/src/components/first_run.tsx:13-20`, `app/api/app.py:386-388`, `app/api/refresh.py:28-31`).
15. Next request: `_connect` sees a new file identity, opens a new shared connection and retires handles of old db versions: `_retire` parks them and closes them after 120 s (or past 32 parked), so requests still reading through them finish and Windows can then delete the file (`app/core/data.py:304-329`, `app/core/data.py:332-339`, `app/core/data.py:283-290`).
16. Caches invalidate: `_data_version()` includes the db identity (`app/api/app.py:1018-1023`).

## Status payload

`refresh.status()` (`app/api/refresh.py:24-31`):

```json
{"running": true, "step": "export", "days": 15, "started_at": 1762762500.0,
 "finished_at": null, "error": null, "elapsed_s": 42}
```

`step` ∈ `export` → `load` → (`split`) → `done` | `failed` (`app/api/refresh.py:38`, `app/api/refresh.py:59`, `app/api/refresh.py:64`, `app/api/refresh.py:71-73`). Values above are illustrative.

## Example (from tests)

`tests/test_refresh.py:21-48` (subprocesses faked):
- Before: `audit-20000101-000000.duckdb` and three `results/` stamps in `tmp_path`.
- `refresh.start(15)` → final status `step == "done"`, `days == 15`, `error is None`.
- Calls made: `["tools/export_direct_results.py", "tools/load_direct_results.py"]`.
- `AUDIT_DB` now points at a new `.duckdb` under `tmp_path`; the old db is deleted; `current_db()` equals it; two `results/` folders remain, the oldest (`19991230-000000`) is gone.
- `tests/test_refresh.py:51-81`: the shared DuckDB connection switches to the new db file after refresh.

## Differences from `first_run.py`

| | first_run.py | Refresh |
|---|---|---|
| Rows kept per check | 50,000 (`tools/export_direct_results.py:183`) | 500,000 (`app/api/refresh.py:57`) |
| Db file | `audit.duckdb` (`app/core/datasource.py:11`) | `audit-<stamp>.duckdb` (`app/api/refresh.py:55`) |
| Stamp | UTC `%Y%m%dT%H%M%SZ` (`tools/first_run.py:99`) | local `%Y%m%d-%H%M%S` (`app/api/refresh.py:52`) |
| App running? | Stop it first on Windows (`README.md:80-81`) | Runs inside the app |
