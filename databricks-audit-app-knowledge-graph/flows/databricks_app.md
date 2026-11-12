# Flow: running as a Databricks App

The same FastAPI app runs inside Databricks Apps: `tools/app_start.py` unpacks DuckDB and starts `app.api` on `0.0.0.0:$DATABRICKS_APP_PORT`; the warehouse queries run either as the `.env` token's owner (`run_as: user`) or as the app's own service principal via OAuth M2M (`run_as: app`); the signed-in viewer's email (`X-Forwarded-Email`) is used only for optional row-level access (`docs/databricks-app.md:26-35`, `tools/app_start.py:1-7`, `app/api/app.py:188-203`).

```mermaid
sequenceDiagram
  autonumber
  actor V as Viewer (browser)
  participant P as Databricks Apps platform / proxy
  participant AS as tools/app_start.py
  participant API as app.api (uvicorn)
  participant MW as _PrincipalMiddleware + rls
  participant R as app/api/refresh.py
  participant EX as export_direct_results → snapshot.connect
  participant WH as SQL warehouse (system.*)
  participant DB as App disk: data dir / audit-<stamp>.duckdb
  P->>AS: command: python tools/app_start.py (app.yaml env: HOST, HTTP_PATH[, TOKEN])
  AS->>AS: join wheels/*.part → unzip to .pydeps, set PYTHONPATH
  AS->>API: runpy app.api → host 0.0.0.0, port DATABRICKS_APP_PORT
  V->>P: open app URL (signed in)
  P->>API: GET / , GET /api/status (+ X-Forwarded-Email)
  API->>MW: rls.set_principal(email)
  API-->>V: state "no_database" → first-run screen with "Get"
  V->>API: POST /api/refresh?days=30
  API->>R: start(30) → thread
  R->>EX: subprocess export (inherits env)
  alt run_as user (DATABRICKS_TOKEN in env)
    EX->>WH: dbsql.connect(access_token=token)
  else run_as app (no token; DATABRICKS_CLIENT_ID/SECRET from platform)
    EX->>WH: dbsql.connect(credentials_provider=OAuth M2M)
  end
  WH-->>EX: rows → results/<stamp>
  R->>DB: load → audit-<stamp>.duckdb (+ rls sets if apply=export)
  V->>API: GET /api/findings … (page reload)
  API->>MW: principal → rls.mode(): off / query / export
  MW->>DB: read full db, scoped views (query) or per-grant-set file (export)
  API-->>V: JSON
```

## Steps

1. Deploy writes `app.yaml` with `command: ["python", "tools/app_start.py"]` and env `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, plus `DATABRICKS_TOKEN` only when `run_as: user` (`tools/deploy_app.py:148-153`). Deploy steps: `processes/deploy_databricks_app.md`.
2. Platform starts the command; `app_start.unpack` joins `wheels/*.whl.part*` and extracts DuckDB to `.pydeps` once; `main` prepends `.pydeps` and the root to `sys.path` and `PYTHONPATH` so child processes (the export) inherit it, then `runpy.run_module("app.api")` (`tools/app_start.py:21-39`).
3. `__main__.main` sees `DATABRICKS_APP_PORT`: host `0.0.0.0`, port `DATABRICKS_APP_PORT` (`app/api/__main__.py:15-17`). `rls.is_app()` is true for the whole process (`app/core/rls.py:106-107`).
4. Data folder: `AUDIT_DATA_DIR` or the Linux default `~/.local/share/databricks-audit` (inferred: Apps run Linux per `tools/deploy_app.py:44-46`; `app/core/datasource.py:25-31`). The disk is wiped on restart/redeploy (`docs/databricks-app.md:134`).
5. Each request: `_PrincipalMiddleware` reads `x-forwarded-email` ("set by Databricks Apps", per the code comment) and binds it (`app/api/app.py:188-203`, `app/core/rls.py:434-443`).
6. Empty disk → `GET /api/status` returns `no_database` (`app/core/data.py:397-399`); `FirstRunScreen` shows `GetData` ("Pulls data from the app itself, so a Databricks App (no terminal) can fill an empty database") (`web/src/components/first_run.tsx:25-47`).
7. "Get" → `POST /api/refresh?days=N` → `refresh._run` spawns the export and load as subprocesses (`app/api/app.py:402-412`, `app/api/refresh.py:51-60`). With rls on, only `refresh.admins` or users granted everything may start it (403 otherwise). Same as `flows/refresh_in_app.md`.
8. The export's `_preflight_env` requires host + HTTP path, and the token only when `uses_app_principal` is false (`tools/export_direct_results.py:886-895`).
9. `snapshot.connect` picks auth:
   - **run_as user**: `DATABRICKS_TOKEN` present → `access_token=token` (`tools/snapshot.py:535`). Queries run as the token's owner (`docs/databricks-app.md:28-30`).
   - **run_as app**: no token, `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` present ("Databricks sets these in a Databricks App") → `credentials_provider` from `databricks.sdk.core.Config(auth_type="oauth-m2m")` (`tools/snapshot.py:170-177`, `tools/snapshot.py:473-482`, `tools/snapshot.py:499-500`, `tools/snapshot.py:535`).
10. With rls enabled, the export also reads the entitlements table and `current_user()` (`tools/export_direct_results.py:1213-1214`, `tools/export_direct_results.py:596-613`); the loader loads `rls.entitlements` and adds `workspace_id/warehouse_id/env` columns (`tools/load_direct_results.py:827-831`). On an app the loader does **not** cut rows to one user (`tools/load_direct_results.py:874-875`).
11. If `rls.apply == "export"`: refresh step `split` builds one filtered db per grant set + `<db>.rls_sets.json` (`app/api/refresh.py:63-66`, `app/core/rls.py:515-547`).
12. Serving by `rls.mode()` (`app/core/rls.py:456-461`):
    - `off`: everyone reads the full db (`docs/databricks-app.md:101`).
    - `query`: `_connect` returns `rls.scoped_connection(base, principal)` — in-memory views over the full db limited to the principal's grants (`app/core/data.py:308-309`, `app/core/rls.py:587-612`).
    - `export`: `_db_path` → `rls.resolve` → that user's grant-set file, else the empty set, else a non-existent file (fail closed) (`app/core/data.py:198-200`, `app/core/rls.py:481-491`).
13. Caches are per user in `query` mode (`rls.scope_key`) and the start-up warm-up is skipped (`app/core/rls.py:464-466`, `app/api/app.py:1122-1123`).
14. `/api/meta.rls` reports `{enabled, apply, user, granted, error}` (`app/api/app.py:487`, `app/core/rls.py:655-683`).

## Auth: user vs service principal

| | `run_as: user` (default) | `run_as: app` |
|---|---|---|
| Queries run as | Owner of `DATABRICKS_TOKEN` in `.env` (`docs/databricks-app.md:30`) | App's service principal (`docs/databricks-app.md:31`) |
| Token stored | Yes, in `app.yaml` env (`tools/deploy_app.py:150-151`) | No (`tests/test_deploy_app.py:133-138`) |
| Connector auth | `access_token` (`tools/snapshot.py:535`) | OAuth M2M via `DATABRICKS_CLIENT_ID`/`DATABRICKS_CLIENT_SECRET` (`tools/snapshot.py:473-482`) |
| Warehouse access | Token owner's own | App resource `sql-warehouse` with `CAN_USE` (`tools/deploy_app.py:81-86`) |
| System table access | Token owner's grants | Metastore admin runs printed `GRANT`s (`tools/deploy_app.py:94-105`, `docs/databricks-app.md:70-84`) |
| Viewer identity | `X-Forwarded-Email`, only for rls | same |

- Not built: queries never run as the signed-in viewer (no on-behalf-of auth found in `tools/snapshot.py:485-555`); the viewer only filters rows.

## Example (from tests)

- `tests/test_deploy_app.py:98-117`: `stage(out, ENV, wheel)` writes `app.yaml` with `command == ["python", "tools/app_start.py"]` and env equal to `ENV`; the wheel is split into ≤ `PART_BYTES` parts that rejoin byte-identical; `app/web/dist/index.html` is copied; no `__pycache__` and no `config/app.local.yml`.
- `tests/test_deploy_app.py:133-138`: with `run_as="app"`, env names are exactly `["DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH"]` and the fake token is absent.
- `tests/test_rls_api.py:68-98`: with `DATABRICKS_APP_PORT=8000` and `rls: {enabled: true, apply: query, columns: [workspace]}`, header `x-forwarded-email: alice@x.com` sees only workspace `1111`, `BOB@x.com` only `2222` (case-insensitive), an unknown or missing email sees nothing.
- Illustrative `app.yaml` for `run_as: user` (values are placeholders):

```yaml
command: [python, tools/app_start.py]
env:
- {name: DATABRICKS_SERVER_HOSTNAME, value: dbc-xxxxxxxx-xxxx.cloud.databricks.com}
- {name: DATABRICKS_HTTP_PATH, value: /sql/1.0/warehouses/xxxxxxxxxxxx}
- {name: DATABRICKS_TOKEN, value: <secret, not shown>}
```
