# Crosshire

A read-only audit of a Databricks account. It shows where the money goes, what looks wasted, and
how compute, jobs, queries, storage, model serving and access are set up.

It runs 151 SQL checks on the Databricks `system.*` tables through one SQL warehouse. The results
are saved in a local DuckDB file, and a web app shows them, on your own machine or as a
Databricks App. Nothing is ever written to Databricks. Only the export (and Refresh in the app)
queries the warehouse; browsing the app does not.

**Contents:** [What you need](#what-you-need) · [Set up once](#set-up-once) · [Run](#run) ·
[Run as a Databricks App](#run-as-a-databricks-app) · [Update to a new version](#update-to-a-new-version) ·
[What the app shows](#what-the-app-shows) · [What the numbers mean](#what-the-numbers-mean) ·
[Settings](#settings) · [Tags](#tags) ·
[Other ways to load data](#other-ways-to-load-data) · [If something fails](#if-something-fails) ·
[Repository](#repository) · [Tests](#tests)

## What you need

- **Python 3.11 or newer** (tested on 3.12). pandas 3 does not install on 3.10.
- **A Databricks workspace** with Unity Catalog and system tables enabled.
- **A SQL warehouse** and its HTTP path. The export sends 4 statements at a time.
- **A personal access token** for a user who can `SELECT` the system schemas:
  - `billing`, `compute`, `lakeflow`, `query`, `access`, `serving`, `storage` and
    `information_schema`;
  - also `data_classification` and `ai_gateway`, where you have them.

  A check on a schema you cannot read fails on its own, and the rest still run.

Node is not needed: the built UI is in the repo (`app/web/dist`).

## Set up once

```
git clone https://github.com/darshanmeel/databricks-audit-app.git
cd databricks-audit-app
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env
```

Fill in the three values in `.env`:

- `DATABRICKS_SERVER_HOSTNAME`: the workspace host, without `https://`.
- `DATABRICKS_HTTP_PATH`: SQL Warehouses › your warehouse › Connection details › HTTP path.
- `DATABRICKS_TOKEN`: the personal access token.

`.env` is git-ignored. Environment variables with the same names work too.

## Run

```
python tools/first_run.py         # last 30 days; add --windows 90 for 90
python -m app.api
```

Then open `http://127.0.0.1:8000/`. Stop the app with Ctrl+C.

`first_run.py` does two steps, and stops at the first one that fails, naming it:

1. **Export:** runs every check on the warehouse and writes the results to files. This takes
   minutes to tens of minutes.
2. **Load:** builds the local `audit.duckdb` that the app reads.

Stop the app before you run `first_run.py` again. On Windows the load cannot replace
`audit.duckdb` while the app has it open. Refresh in the app does not have this problem.

**Days.** The app has 7-, 30- and 90-day windows. `--windows` sets how many days the export
takes:

- A window longer than the export shows the same days and reads "partial" in the top bar.
- `--windows 1` exports today only; `--windows 2` yesterday and today.
- More than 90 adds nothing.

**Today is included, but partial.** The export takes in today up to the moment it runs, and the
top bar then shows "today partial". Databricks adds usage to its billing table a few hours late,
so the last hours before a run can be missing. To export complete days only, set
`include_today: false`.

**Refresh** in the top bar runs the export and the load from the app:

- Pick 7, 15, 30, 60 or 90 days. It runs every check on your warehouse, so it uses warehouse time
  and takes minutes.
- It writes a new database and switches the app to it. The page reloads when it is done.
- If it fails, the error shows in the same panel and the old data stays.
- It keeps the newest two results folders and deletes older ones, including those from
  `first_run.py`.
- It keeps up to 500,000 rows per check; `first_run.py` keeps 50,000.

**Export options.** `first_run.py` takes only `--windows`. For the rest, run the two steps by hand:

```
python tools/export_direct_results.py --out <folder> [options]
python tools/load_direct_results.py <folder>
```

| Option | What it does |
|---|---|
| `--out DIR` | Output folder, outside the repo. |
| `--windows DAYS` | Days to export (default: `default_window`). |
| `--max-rows N` | Rows kept per check, worst first (50,000). A check that hits it reads "truncated" in Coverage & Gaps. |
| `--threads N` | Statements sent at once (4). |
| `--only ID ...` | Only these checks. Skips the names and tag tables. |
| `--domain NAME` | Only one group of checks, e.g. `cost` or `jobs_pipelines`. Skips the names and tag tables. |
| `--mask-users` | Mask user emails, whatever `privacy.mask_user_identities` says. |

`load_direct_results.py` with no folder loads the newest export in the data folder. `--db PATH`
writes the database somewhere else.

### Where the files go

Files go to the data folder:

| System | Folder |
|---|---|
| Windows | `%LOCALAPPDATA%\databricks-audit` |
| macOS | `~/Library/Application Support/databricks-audit` |
| Linux | `~/.local/share/databricks-audit` |

To use another folder, set `AUDIT_DATA_DIR`. It must be outside the repo.

- Each export goes to `results/<time>/`: one file per check, the name and tag tables,
  `manifest.json`, and `report.md` with one line per check.
- A stopped or failed export never touches an earlier folder.

**Which database the app opens**, the first that exists:

1. `AUDIT_DB`, if set;
2. the database the last load into the data folder, or the last Refresh, made;
3. `audit.duckdb` in the data folder;
4. `data/db_audit.duckdb` in the repo (from the snapshot and dbt build).

The first line `python -m app.api` prints names the database it opened.

These files hold your account's data. Keep them out of the repo and do not share them.

## Run as a Databricks App

The same code runs as a Databricks App, so others can open it with a link. The app runs the
export on your SQL warehouse and keeps the database on its own disk, as on a laptop. This is for
sharing a trial; production is further down.

Once, create `config/app.local.yml` (git-ignored):

```yaml
name: my-audit   # the app's name: lowercase letters, numbers, dashes
```

Then, from the repo root, with the Databricks CLI installed (no login needed):

```
python tools/deploy_app.py
```

It takes the host, HTTP path and token from `.env` or the environment, as the export does, and:

1. downloads the DuckDB wheel once. The app installs nothing from PyPI or a volume: DuckDB is the
   one package it lacks, and FastAPI, uvicorn, pandas, pyarrow, PyYAML and the SQL connector come
   pre-installed in Databricks Apps;
2. copies `app/`, `config/`, `dbt/` and `tools/` to `app-deploy/<name>` in the data folder, with an
   `app.yaml` holding the three credentials, and the wheel in parts under 10 MB (an app file can be
   no bigger). At start-up `tools/app_start.py` joins and unpacks them;
3. syncs that to `/Workspace/Users/<you>/<name>`, creates the app if needed, and deploys it;
4. prints the link. Open it, pick the days and click Get.

Run it again after an update.

What differs from a laptop:

- The token is in `app.yaml` in your workspace folder. Anyone who can read that folder can read it.
- Everyone who can open the app sees everything your token can see. Share it in the app's
  Permissions (Can use).
- The app's disk is wiped when it restarts or is redeployed: click Get or Refresh again.
- A Medium app has 2 CPUs and 6 GB. Set `load.threads: 2` to match.
- Tested with the versions Databricks Apps pre-install: FastAPI 0.115, pandas 2.2, pyarrow 16 and
  SQL connector 3.4. The code has no Python 3.12-only syntax.

**For production (not built yet):**

- **Own schema and own warehouse.** A small warehouse only this app uses and a schema such as
  `<catalog>.audit`, so its access and its cost sit in one place.
- **A separate job copies the system tables** the checks read into that schema, as a user or
  service principal that can read `system.*`.
- **The app's service principal reads only that schema and uses only that warehouse.** No personal
  token and no access to `system.*`. The checks name `system.*` tables today, so the export
  needs a switch to read the copies.
- **Where the app reads from**, two ways:
  - the job writes `audit.duckdb` to a Unity Catalog volume, and the app downloads it at start-up.
    Pages read the local file, so viewing costs nothing and a restart needs no export;
  - the checks write their results as tables in the schema and the app queries them. Unity Catalog
    row filters then decide who sees what, but every page view runs on the warehouse.
- **Who sees what.** The app knows who is signed in, so it can show each person only their
  workspaces or cost centers.

## Update to a new version

1. Download the new zip, or `git pull`.
2. Keep your own files. `.env` and `config/settings.local.yml` are not in the zip, so copy them
   into the new folder if you unpack it somewhere new.
   - `config/tag_aliases.yml`, `config/thresholds.yml` and `config/materiality.yml` are in the
     zip and get replaced. Keep a copy if you changed them.
3. Run `pip install -r requirements.txt` again.
4. Stop the app, then run `python tools/first_run.py --windows <days>` with the days you
   export, for example `--windows 62`. New or changed checks show only after a new export.
5. Start the app again with `python -m app.api`.

The data folder is outside the repo, so an update never touches it.

## What the app shows

**Pages**

| Page | Sub-tabs |
|---|---|
| Overview | – |
| Actions | – |
| Cost | Trend, By product & SKU, Allocation, Chargeback, By resource, Pricing & policy |
| Waste & savings | Priced, Flagged not priced, How it's counted |
| ML & AI | Spend, Endpoints, AI Gateway |
| Genie | – |
| Compute | Warehouses, Clusters & pools, Configuration |
| Jobs | Failures, Slow & queued, Hygiene, Compute fit, Pipelines |
| Queries | Heavy queries, Efficiency, Reliability, Capacity, By team |
| Governance & PII | Access & grants, Admin activity, Sensitive data, Lineage, Sharing |
| Storage | Tables, Maintenance |
| Tags | – |
| All findings | – |
| Coverage & Gaps | Data sources, Not assessed, Known limitations |
| How it works | – |

**Lens** (left rail) picks which pages show. It is not a permission: the data is the same in every
lens.

| Lens | Pages |
|---|---|
| FinOps (default) | Overview, Actions, Cost, Waste & savings, ML & AI, Genie, Compute, Tags, Coverage & Gaps, All findings |
| Data engineer | Overview, Actions, Jobs, Compute, Queries, then every other page |
| Governance | Overview, Actions, Governance & PII, Tags, All findings |
| CTO | Every page as a summary, without sub-tabs or drill-down (Coverage & Gaps keeps its sub-tabs) |
| CFO | One Overview: spend by month, environment, product, workspace, top resources and top tags |

Every lens also has How it works. The CTO's Overview is the CFO's, plus performance.

**Filters** in the top bar:

| Filter | What it does |
|---|---|
| Window | Last 7, 30 or 90 days. Only windows the export holds show; a shorter one reads "partial". |
| Env | prod, uat, dev or unknown (see [Environment](#environment)). |
| Top tags | One filter per top tag that has values, at most 4 (see [Tags](#tags)). |
| Tag | Any tag and value. Values of one tag are OR'ed, different tags AND'ed. "untagged" picks what lacks the tag. |
| Workspaces | By name. Lists only the workspaces of the picked Env. |

Grants, masks, lineage and table checks cover the whole metastore, so the workspace filter does not
narrow them. Each page's scope line says what it covers.

ABAC policies (row filters and column masks set by policy) have no system table. The export runs
`SHOW POLICIES` on the metastore, every catalog and every schema its workspace can see, which needs
READ METADATA or MANAGE on them. Catalogs not bound to that workspace are not covered; Governance ›
Sensitive data says how many catalogs and schemas were asked and how many could not be read.

**Also in the top bar:** where the data came from, when it was taken (UTC), "today partial", and
how many checks ran. An amber dot means some checks failed or did not run; Coverage & Gaps lists
them.

**Links.** The address bar holds the page, sub-tab, open check, lens and filters, so a bookmark or
a pasted link opens the same view. A check's details have a Copy link button.

| Key | Meaning |
|---|---|
| `tab`, `subtab` | Page and sub-tab |
| `focus` | The open check |
| `view` | All findings' status list (`critical`, `warning`, `ok`, `not_assessed`, `reference`) |
| `role` | Lens (`finops`, `data_engineer`, `governance`, `cto`, `cfo`) |
| `w` | Window in days |
| `ws` | Workspace ids, comma-separated |
| `env` | Env values |
| `attr_<key>` | A top-tag filter, e.g. `attr_cost_center=finance` |
| `tags` | Tag filter |

Example: `http://127.0.0.1:8000/#tab=cost&subtab=allocation&w=30&env=prod`

**Getting data out:** CSV downloads on Actions, All findings, Tags and each check's rows. For a
PDF, print the page (Ctrl+P, then Save as PDF): it prints in the theme on screen, with full names
and without the menu or filter bar.

**How it works** has a page per screen and per check, the statuses, how dollars and tags are
counted, troubleshooting and a glossary. It opens even before there is any data.

**Theme:** Light or Dark at the bottom of the left rail; it follows the system until you pick one.
Below about 900 px wide, the filters, lens and theme move into the menu button (☰).

## What the numbers mean

**Dollars** are Databricks list price, from `system.billing.list_prices`. They leave out your cloud
provider's VM bill. Set `discount_pct` to see them less a discount; they then read "what-if". No
system table holds your contract rates.

**Statuses** drive every count, colour and total:

| Status | Meaning |
|---|---|
| Critical | The worst kind of problem this check looks for. |
| Warn | Flagged, but not the worst kind. |
| OK | Ran on real data in this window and found nothing. |
| Ranked | Sorted by size only. Big is not the same as wrong. |
| Not assessed | Did not run in this export. Never a pass; its numbers read "–" with the reason. |
| No data | Ran, but nothing happened in this window. Not a verified zero. |
| No data (filtered) | Rows exist, but your filters exclude them all. |
| Reference | A list to look things up in, with no pass or fail. |

Trend and chargeback reports stop at Warn; they never read Critical.

**Possible waste** (Waste & savings) adds up the estimated wasted dollars on rows flagged Critical
or Warn, each resource counted once, from five checks:

- idle SQL warehouse minutes (a gap of over 60 seconds between queries);
- failed statements on warehouses;
- DBUs of failed job runs;
- classic cluster minutes with almost no CPU in use;
- model serving endpoints.

Six more checks are listed without a dollar figure. The checks can overlap, and none of the
figures is a forecast.

**Savings** come from the oversized-jobs check. It looks at classic job clusters over at least 3
runs. A job counts as oversized when its workers' CPU p90 is below 30%, their memory peak is below
40%, and they swap less than 1%. It reads Warn from an estimated $25 saving and Critical from $200.
Jobs on serverless or a SQL warehouse have no hardware data and read Not assessed.

**Materiality floors** in `config/materiality.yml` set a size per check below which a Critical or
Warn row reads OK. Such rows are left out of counts and dollar totals; Waste & savings › Priced
shows how much they leave out. A dollar floor is per 30 days and scales with the window. Floors
apply when the app reads the data, so a change needs only a page refresh.

**Thresholds** in `config/thresholds.yml` change a check's own limits, such as the idle-warehouse
gap. They are built into the export's SQL. After editing the file, run
`python tools/build_direct_sql.py`, then export again. Until then the export stops with
"config/thresholds.yml changed since the SQL was generated".

<a id="environment"></a>**Environment.** Each workspace's environment comes from its name:

- The name is matched, whole words only, against word lists in `dbt/dbt_project.yml`
  (`env_patterns`), in the order prod, uat, dev.
- `-`, `_`, `.` and `/` count as spaces.
- A name that matches nothing reads "unknown".

After editing the lists, rerun `python tools/load_direct_results.py`. Workspace tags and
`dbt/seeds/workspace_env_overrides.csv` count only in the snapshot and dbt build.

**Regions.** Billing covers the whole account. Most other system tables (compute, jobs, queries,
serving, storage, audit) hold only workspaces in the warehouse's region. In a multi-region account,
workspaces elsewhere show spend but no job, query or compute findings; Coverage & Gaps lists them.

**Windows.** A check for a window longer than the export reads Not assessed, not zero. Some checks
show current settings, and two use calendar months (monthly actuals and the 12-month SKU trend).
These do not follow the window filter.

**Row cap.** Each check keeps at most 50,000 rows, worst first (500,000 from Refresh). A capped
check's totals can be low; Coverage & Gaps lists capped checks.

## Settings

`config/settings.yml` explains each setting in a comment. To change one, put just that line in
`config/settings.local.yml` (create it if missing). That file is git-ignored, so an update never
overwrites it, and its values win over `settings.yml`.

| Setting | Default | What it does | Takes effect |
|---|---|---|---|
| `discount_pct` | 0 | Your discount off list price, as a fraction (0.15 = 15%). Figures then read "what-if". | Page refresh |
| `default_window` | 30 | Days exported when `--windows` is not given: 7, 30 or 90. | Next export |
| `include_today` | true | Export today up to the run time, marked partial. false = complete days only. | Next export |
| `ui.max_rows` | 500 | Most rows a table loads at once. | Page refresh |
| `ui.max_chart_categories` | 8 | Most bars or slices in a chart; the rest are grouped as "Other". | Page refresh |
| `mandatory_tag_keys` | cost_center, domain, environment | See [Tags](#tags). | Page refresh |
| `export_tag_keys` | not set (every tag) | See [Tags](#tags). | Next export |
| `load` | memory_limit 1GB, threads 4, top_tag_keys 10 | DuckDB memory and threads for building the local database and for the app reading it, and which tag keys the Tag filter and tag search keep (`tag_keys`, or the `top_tag_keys` with the most spend; mandatory, top and Unity Catalog tags always). Lower memory and threads if the load runs out of memory. | Next load |
| `tag_coverage_floor` | 0.5 | See [Tags](#tags). | Next export |
| `tag_share_floor` | 0.6 | See [Tags](#tags). | Next export |
| `admin_groups` | not set | Groups expected to hold broad grants (your admins). Their grants read OK in Overly broad grants. | Page refresh |
| `brand` | Crosshire | Name, link and contact in the header and footer. | Page refresh |
| `privacy.mask_user_identities` | false | Replace user emails and names with a short code, in the export files and on screen. Groups, service principals and resource names stay readable. | Next export or load |
| `query_source` | the brand name | The "Source" tag on the app's own queries in Databricks query history. Letters, digits, `_` or `-`. | Next export |

"Next export" means `python tools/first_run.py` or Refresh in the app.

- A block (`ui`, `brand`, `privacy`) in `settings.local.yml` replaces the whole block. A line you
  leave out of it gets the built-in default, so copy every line of the block.
- A bad value stops the app with a screen that names the setting and its line.
- Masking cannot be undone for data loaded while it was on. Turn it on before you export.

**Other files you can edit:**

| File | What it holds | After a change |
|---|---|---|
| `config/tag_aliases.yml` | Top tags and their names | See [Tags](#tags) |
| `config/materiality.yml` | Size floors per check | Page refresh |
| `config/thresholds.yml` | Check limits | `python tools/build_direct_sql.py`, then export |
| `dbt/dbt_project.yml` › `env_patterns` | Words that mark prod, uat and dev | `python tools/load_direct_results.py` |

Leave the other files in `config/` alone; the build writes or reads them.

**Environment variables:**

| Variable | What it does |
|---|---|
| `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN` | Connection, as in `.env`. |
| `AUDIT_DBX_CATALOG` | Catalog that holds the system tables (`system`). |
| `AUDIT_DATA_DIR` | Data folder. |
| `AUDIT_DB` | Database the app opens. |
| `AUDIT_API_HOST` | Address the app listens on (`127.0.0.1`, this machine only). The app has no login, so keep it local. |
| `AUDIT_API_PORT` | Port (`8000`). |
| `AUDIT_CONFIG_DIR` | A different `config/` folder. |
| `AUDIT_SNAPSHOT_DIR` | Snapshot folder for the dbt build (`snapshot/`). |

## Tags

The app narrows figures by the Tag filter at the most specific level that has the tag:

1. the query's, job's or pipeline's own tag;
2. then its warehouse's, cluster's, pool's or serverless budget policy's tag, which Databricks
   copies onto the bill (on the Tags page the object's own tag or its bill's, either counts);
3. then the workspace's.

Cost follows each day's bill, so when an object's own tag and its bill disagree the cost goes to
the bill's value. Performance uses the bill's latest value for the whole window.

On the Tags page each object is checked from the most specific level out:

| Level | Object | Its own tag |
|---|---|---|
| 1. Work | Query | its query tag |
| | Job, pipeline | its own tag; for a query, the tag of the job or pipeline that ran it |
| | Notebook | none: Databricks has no notebook tags |
| 2. Compute | Classic or pro SQL warehouse | its own tag |
| | Serverless SQL warehouse | its own tag, or a usage policy when its bill shows one |
| | Cluster | its own tag, with its compute policy's and pool's |
| | Serverless compute | its usage policy's tags, read from the notebook's, job's or pipeline's bill |
| 3. Workspace | Workspace | its own tag; on Azure, the workspace resource's tag |

On Azure the workspace's tag is copied onto every bill row, so a bill value equal to it counts as
the workspace's, not the compute's, unless a usage policy put it there: then it is the policy's.
To give two warehouses different tags, tag each warehouse; a usage policy on a serverless
warehouse's bill counts too. Queries are also counted by where they came from (SQL editor,
dashboards, Genie, jobs, notebooks, other tools), so the page can say, for example, that most
queries without a query tag come from the SQL editor and are covered by their warehouse. For each
warehouse, all-purpose cluster, job and pipeline:

- a mandatory tag on the object counts as "Own tag"; one only on its bill today counts as "… bill",
  with the day its current value has been there since without a break and any earlier values;
- a tag that left the bill reads "gone after <date>" and does not count;
- an object with no bill for over a week reads "last billed <date>";
- an object tagged one way and billed another is listed under its type, with both values;
- otherwise its workspace's tag counts, then it is missing. A workspace whose values split with
  none on most reads "mixed" with its values.

**The three tag settings:**

| Setting | Where | What it does | After a change |
|---|---|---|---|
| `top_tags` | `config/tag_aliases.yml` | Tag filters in the top bar, and "Spend by" on the CFO and CTO Overview and Cost › Allocation, with the name shown. | Name only: page refresh. New key: `python tools/load_direct_results.py` |
| `mandatory_tag_keys` | settings | Up to 5 tags every query, job, pipeline, cluster, warehouse and workspace should carry. The Tags page lists what misses each one. | Page refresh |
| `export_tag_keys` | settings | Keep only these tags in the export, to make it smaller. Not set = every tag. The top and mandatory tags are always kept. See below for what you miss. | Next export |

**What you miss with `export_tag_keys` set,** for each tag you did not keep:

- The Tag filter still lists it, but picking it leaves the check tables on every page empty, with
  $0. That means "not exported", not "nothing carries this tag".
- The CFO and CTO Overview, Cost › Allocation, the top-tag filters and spend totals are not affected.

For all three, case, spaces, `_` and `-` are ignored: `cost_center` also matches `Cost Center`,
`cost-center` and `costcenter`.

**Top tags** look like this:

```yaml
top_tags:
  cost_center: Cost center
  domain: Domain
  team: Team
  business_unit: Business unit
```

- On the left, the tag key as in Databricks: lower case, letters, digits and `_`, starting with a
  letter. Names the app uses itself, such as `env`, `name` or `tag`, are refused.
- On the right, the name shown; leave it empty to show the key.
- Each one with values becomes a filter in the top bar, at most 4, in this order.
- The CFO and CTO Overview and Cost › Allocation show spend by each of them; the first one is the headline.
- One key per line; two keys are never merged into one filter.
- A mistake in the file hides every top-tag filter; the other filters still work.

**How a workspace gets a top-tag value** (for the top-bar filter):

- At least `tag_coverage_floor` (half) of its billed DBUs without a usage policy must carry the tag.
- At least `tag_share_floor` (60%) of those must carry one value.
- Otherwise it reads "not tagged" or "mixed".
- This is worked out over the whole export, not the window you pick.

Any tag works in the Tag filter as it is.

**Where tags come from in Databricks:**

- **SQL warehouses:** the warehouse's own tags. Budget policies do not apply to warehouses.
- **Serverless notebooks and jobs:** the serverless budget policy's tags. They only reach usage
  that runs under that policy.
- **Job compute:** the job's tags, which Databricks copies onto its job clusters, plus the
  cluster's custom tags and its compute policy.
- **Workspace (Azure):** tags on the workspace resource. They reach every bill row, classic and
  serverless, and no system table holds them.

A tag only counts for usage from the moment it is added. It does not reach back.

## Other ways to load data

**Results from someone else's export**, as a folder or a `.zip`:

```
python tools/load_direct_results.py <their results folder or zip>
```

**Files from another tool** load the same way, with no `manifest.json` needed:

- check results in `checks/` (or `findings/`), one `<check id>.parquet` or `.csv` each;
- names in `dims/` (or `names/`), tag tables in `tags/`;
- each check file has a `window_days` column (0, 7, 30 or 90). A missing longer window reuses the
  longest one present and reads partial.

One wrapping folder around these is fine. Parquet wins over CSV.

**Snapshot and dbt (development).** Copies the system tables to local parquet files, then builds
the database with dbt.

```
pip install -r requirements-dbt.txt
python tools/snapshot.py                     # writes snapshot/; --resume continues a stopped run
python tools/dbt_run.py build --target dev   # writes data/db_audit.duckdb
```

- For another folder, run `snapshot.py --out <folder>` and set `AUDIT_SNAPSHOT_DIR` to it for
  `dbt_run.py`.
- The app opens `data/db_audit.duckdb` only when no other database exists, so start it with
  `AUDIT_DB` set to that file.
- In this mode, a change to `config/tag_aliases.yml` needs the `dbt_run.py build` again.

## API

The app's data comes from a local HTTP API under `/api`. Every route only reads, except
`POST /api/refresh`, which starts an export. `http://127.0.0.1:8000/docs` lists them all.

## If something fails

| What you see | What to do |
|---|---|
| Step 1 exits 2 and names a missing credential | Fill in `.env`, or set the three variables. |
| Step 1 exits 1 (login, network or warehouse) | Check the token, the HTTP path, and that the warehouse can start. |
| `ImportError` or "DLL load failed" | Run `python -m pip install -r requirements.txt` with the same `python`. |
| The export ends with "N failed" | Open `results/<time>/report.md`. It has one line per check, with its status and error. Usually a grant is missing or a system schema is not enabled. The app shows those checks as not run. |
| The export stops: "thresholds.yml changed" | Run `python tools/build_direct_sql.py`, then export again. |
| Step 2 fails | The export is kept. Fix the problem, then run `python tools/load_direct_results.py`. |
| Step 2 fails on Windows with a permission error | The app has the database open. Stop it, then rerun the load. |
| The load says `Out of Memory Error` and a tags table is not built | In `config/settings.local.yml`, lower `load.threads` (e.g. 1) or `load.top_tag_keys`, or list the tags you need in `load.tag_keys`, then rerun the load. No new export needed. |
| An export or load refuses a folder (exit 2) | The folder is inside the repo. Pick one outside it. |
| The app shows "No data yet" | Pick the days and click Get, or run `python tools/first_run.py`. |
| The app shows "config/settings.yml needs a fix" | Fix the setting and line it names. |
| The app shows old or unexpected data | The first line `python -m app.api` prints names the database it opened. Set `AUDIT_DB` to pick one. |
| Top-tag filters are missing | Check `config/tag_aliases.yml` for a bad key, and that the tag has values. |
| Port 8000 is busy | Set `AUDIT_API_PORT`, e.g. `$env:AUDIT_API_PORT=8010` in PowerShell. |
| Git Bash turns `/sql/1.0/...` into a file path | Use PowerShell, or unset the `DATABRICKS_*` variables so `.env` is used. |

Nothing retries on its own. A stopped run exits 130 and leaves earlier results as they were.

## Repository

```
app/api          web server (FastAPI)
app/core         filters, prices, tags, data access
app/queries      the SQL checks, one file each
app/direct_sql   the same checks as plain SQL for the export (generated)
web/             UI source (React, TypeScript, Vite)
app/web/dist     the built UI the server sends (committed)
config/          settings, tag aliases, thresholds, materiality floors, check grains
dbt/             models for the snapshot path and the test build
tools/           export, load, build and test scripts
tests/           pytest suite and fixtures
data/            git-ignored; the dbt build's database
snapshot/        git-ignored; the snapshot's parquet files
```

## Tests

```
pip install -r requirements-dev.txt
python tools/gate.py            # full: test database, dbt build, pytest (about 30 min)
python tools/gate.py --quick    # lint, generated files, UI type check and tests, pytest
```

`--quick` needs the test database a full run builds (`tests/db_audit_test.duckdb`). The UI checks
run when `web/node_modules` is installed; on their own: `cd web && npm ci && npm run typecheck && npm test`.

After changing the UI in `web/`, rebuild it with `cd web && npm ci && npm run build` and commit
`app/web/dist` with it; a test fails when the two differ. `npm run dev` serves the UI with live
reload and sends `/api` to the running app (port `AUDIT_API_PORT`, default 8000). This needs
Node and git.
