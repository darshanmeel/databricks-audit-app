# Run as a Databricks App

The same app as on a laptop, running inside Databricks, so others open it with a link. It runs the
export on your SQL warehouse and keeps the results on its own disk. Nothing is written to your
catalogs.

This is one of three ways to see the data; [the main README](../README.md#three-ways-to-use-it)
lists the other two.

**Contents:** [What you need](#what-you-need) · [1. Pick who the app runs as](#1-pick-who-the-app-runs-as) ·
[2. Name the app](#2-name-the-app) · [3. Deploy](#3-deploy) ·
[4. Grant the service principal access](#4-grant-the-service-principal-access) ·
[5. Open it and load data](#5-open-it-and-load-data) · [Update](#update) · [Limit what each person sees](#limit-what-each-person-sees) ·
[What differs from a laptop](#what-differs-from-a-laptop) · [If something fails](#if-something-fails) ·
[For production](#for-production-not-built-yet)

## What you need

- **The repo set up on your machine:** Python 3.11 or newer and `pip install -r requirements.txt`
  ([Set up once](../README.md#set-up-once)).
- **`.env` filled in:** the workspace host, a SQL warehouse's HTTP path and a token. The deploy always
  uses this token; `run_as` decides whether the app keeps it.
- **The Databricks CLI** installed. No `databricks auth login`: the deploy passes the token itself.
- **Permission to create apps** in the workspace.

## 1. Pick who the app runs as

| `run_as` | The app's queries run as | Token stored in the app | Access to set up |
| --- | --- | --- | --- |
| `user` (default) | The owner of the token in `.env`: you, or another user or service principal whose token you put there | Yes, in `app.yaml` in your workspace folder | None: the token already reads the system tables |
| `app` | The app's own service principal, which Databricks creates with the app | No | A metastore admin grants it `SELECT` on the system schemas, once ([step 4](#4-grant-the-service-principal-access)) |

- **`user`** is quickest for a trial.
- **`app`** suits sharing: no personal token sits in the workspace, and access belongs to one
  principal you can see and revoke.

## 2. Name the app

`config/app.yml` comes with the repo and works as it is:

```yaml
name: lakehouse-health   # 2-30 lowercase letters, numbers or dashes
run_as: user             # user or app
```

To change a value, edit it there, or put just that line in `config/app.local.yml` (git-ignored),
which wins and survives an update.

## 3. Deploy

From the repo root:

```
python tools/deploy_app.py
```

It takes a few minutes the first time. What it does:

1. **Downloads the DuckDB wheel**, once. It's the one package the app lacks. FastAPI, uvicorn, pandas,
   pyarrow, PyYAML, the SQL connector and the Databricks SDK come pre-installed in Databricks Apps.
   Nothing is installed from PyPI or a volume.
2. **Copies** `app/`, `config/`, `dbt/` and `tools/` to `app-deploy/<name>` in the data folder. It
   adds an `app.yaml` and the wheel in parts under 10 MB, the Apps file limit; `tools/app_start.py`
   joins the parts at start-up.
3. **Syncs** that folder to `/Workspace/Users/<you>/<name>`.
4. **Creates the app** if it doesn't exist. With `run_as: app`, it also adds your warehouse as an app
   resource, which gives the service principal **Can use** on it.
5. **Deploys** and prints the link. With `run_as: app`, it also prints the grants for step 4.

## 4. Grant the service principal access

Only for `run_as: app`. A metastore admin runs the printed statements once in the SQL editor. They
look like this, one line per system schema:

```sql
GRANT USE CATALOG ON CATALOG system TO `<service principal id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.billing TO `<service principal id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.compute TO `<service principal id>`;
-- ... access, lakeflow, query, serving, storage, data_classification, ai_gateway
```

- Governance & PII and Storage show only objects the service principal can see. To cover a catalog,
  also run `GRANT BROWSE ON CATALOG <catalog> TO ...`.
- A schema it can't read fails only the checks that read it; Coverage & Gaps lists them.

## 5. Open it and load data

1. Open the printed link, or **Compute** › **Apps** › your app.
2. Pick the days and click **Get**. The export runs on the warehouse and takes minutes to tens of
   minutes.
3. To share: the app's **Permissions** › add people or groups with **Can use**.

## Update

1. `git pull` (or unpack the new zip).
2. `python tools/deploy_app.py` again. A changed `run_as` applies from this deploy.
3. Click **Get** in the app: a redeploy wipes its disk.

## Limit what each person sees

Off by default: everyone who opens the app sees everything its principal can read. Turn it on in
`config/app.yml` (or `config/app.local.yml`); the deploy carries the `rls:` block into the app.

- **Entitlements table:** one row per grant, columns `principal` (the person's email),
  `workspace_id`, `warehouse_id`, `environment`. `*` or empty in a column means all.
- **`rls.enabled`:** `false` by default.
- **`rls.table`:** the 3-part name, default `main.governance.audit_entitlements`.
- **`rls.columns`:** which columns filter, any of `workspace`, `warehouse`, `environment`; default `[workspace]`.
- **`rls.apply`:** `query` (default) filters per signed-in user while they browse; `export` builds one
  data file per distinct grant set at refresh, so people with identical grants share one file and the
  disk holds one copy per set. A laptop run always filters at load, for the token's user.
- **Matching:** a row is visible when one grant row passes every configured column. A row with no value
  in a configured column (account-level billing has no workspace) needs `*` for that column. Emails
  compare case-insensitively.
- **Example:** `config/entitlements.example.sql` creates the table with one row per kind of grant.
- **Grant:** `INSERT INTO main.governance.audit_entitlements VALUES ('ana@example.com', '1234567890', '*', 'prod');`
  then click **Get**; the table is read once per refresh.
- **Access to the table:** the app's principal (the token's user, or the service principal with
  `run_as: app`, whose grants `deploy_app.py` prints) needs `SELECT` on it.
- **Fail closed:** if the table cannot be read, or the signed-in person has no grant, nobody sees rows
  and the app says why.
- **The export folder is unfiltered:** `results/<stamp>` on the app's disk holds everyone's rows; only
  the data file the app serves is filtered.

## What differs from a laptop

- **Everyone who can open the app sees everything its principal can see,** unless you turn on
  [row-level access](#limit-what-each-person-sees).
- **With `run_as: user`, the token is in `app.yaml`** in your workspace folder. Anyone who can read
  that folder can read it.
- **The disk is wiped** when the app restarts or is redeployed: click Get or Refresh again.
- **A Medium app has 2 CPUs and 6 GB.** Set `load.threads: 2` in `config/settings.local.yml` to match;
  the deploy copies that file too.
- **Tested with the versions Databricks Apps pre-install:** FastAPI 0.115, pandas 2.2, pyarrow 16 and
  SQL connector 3.4.

## If something fails

| What you see | What to do |
| --- | --- |
| "the Databricks CLI is not installed" | [Install it](https://docs.databricks.com/dev-tools/cli/install.html), then deploy again. |
| "config/app.yml is missing" | Restore it from the repo ([step 2](#2-name-the-app)). |
| "name in config/app.yml" | 2-30 lowercase letters, numbers or dashes. |
| "run_as in config/app.yml" | Use `user` or `app`. |
| "run_as: app needs a SQL warehouse HTTP path" | Put a SQL warehouse's HTTP path in `.env`, not a cluster's. |
| `databricks apps create failed` with a permission error | Ask a workspace admin for permission to create apps. |
| Get fails with `PERMISSION_DENIED` on `system.*` | With `run_as: app`, run the step 4 grants. With `user`, the token's owner lacks `SELECT`. |
| The app shows "No data yet" | Its disk was wiped by a restart or redeploy: click Get. |
| Anything else | The app's page › **Logs** shows the export's output. |

## For production (not built yet)

- **Own schema and own warehouse.** A small warehouse only this app uses and a schema such as
  `<catalog>.audit`, so its access and cost sit in one place.
- **A separate job copies the system tables** the checks read into that schema, as a user or
  service principal that can read `system.*`.
- **The app's service principal reads only that schema** and uses only that warehouse. The checks
  name `system.*` tables today, so the export needs a switch to read the copies.
- **Where the app reads from**, two ways:
  - the job writes `audit.duckdb` to a Unity Catalog volume, and the app downloads it at start-up.
    Pages read the local file, so viewing costs nothing and a restart needs no export;
  - the checks write their results as tables in the schema and the app queries them. Unity Catalog
    row filters then decide who sees what, but every page view runs on the warehouse.
- **Who sees what.** The app knows who is signed in, so it can show each person only their
  workspaces or cost centers.
