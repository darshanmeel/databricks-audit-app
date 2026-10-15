// P2-NOBUILD: the friendly screen App.tsx renders in place of
// the whole shell whenever GET /api/status says the app is not ready, so a fresh checkout (no
// database yet), a build that failed or never finished (dims.dim_workspace unreadable), or a typo
// in config/settings.yml never reads as "Could not load 500 Internal Server Error" -- the exact
// bug this task fixes. Each screen names the ONE next command (README.md's own "First run"
// section; never a generic "check the logs"), so a first-time user has something concrete to type
// instead of a stack trace.
//
// Reuses the same "honest-card" look every other "could not X" state in this app already uses
// (finding_detail.tsx, primitives.tsx's ErrorBoundary) -- .fr-card only enlarges it for a
// full-page screen; .honest-card.info/.warn (styles.css) are two new tone modifiers alongside the
// existing not_assessed/empty_filters/error ones.
//
// Ownership note: this file only words the FOUR states GET /api/status can report --
// "settings_invalid" | "no_database" | "build_incomplete" | "ready" (app/api/app.py's own
// docstring). It decides nothing; the API already has.

import React from "react";

import { Api, REFRESH_DAYS } from "../api";
import { navHref } from "./nav_hash";

// Refresh status, polled every 3 s while one runs; the page reloads when it is done.
export function useRefresh() {
  const [st, setSt] = React.useState<any>(null);
  React.useEffect(() => { Api.refreshStatus().then(setSt).catch(() => {}); }, []);
  React.useEffect(() => {
    if (!st || !st.running) return undefined;
    const t = setTimeout(() => Api.refreshStatus().then((s) => {
      setSt(s);
      if (s.step === "done") window.location.reload();
    }).catch(() => {}), 3000);
    return () => clearTimeout(t);
  }, [st]);
  const start = (days: number) => Api.refreshStart(days).then(setSt);
  return { st, start };
}

// Pulls data from the app itself, so a Databricks App (no terminal) can fill an empty database.
function GetData() {
  const { st, start } = useRefresh();
  const [days, setDays] = React.useState(30);
  if (st && st.running) {
    const mins = st.elapsed_s != null ? Math.floor(st.elapsed_s / 60) : 0;
    const step = st.step === "load" ? "Loading into the app" : "Running the checks on Databricks";
    return <div className="h-note">{`${step}… ${mins} min so far. The page reloads when it is done.`}</div>;
  }
  return (
    <div className="fr-get">
      <div className="ws-actions">
        {REFRESH_DAYS.map((d) => (
          <button key={d} type="button" className={d === days ? "on" : ""} onClick={() => setDays(d)}>{`Last ${d} days`}</button>
        ))}
      </div>
      {st && st.step === "failed" && <div className="h-note refresh-error">{`Last try failed: ${st.error}`}</div>}
      <div className="tag-actions">
        <button type="button" className="tag-apply" onClick={() => start(days)}>{`Get the last ${days} days`}</button>
      </div>
    </div>
  );
}

function CommandBlock({ command }: LooseProps) {
  return <pre className="fr-command mono">{command}</pre>;
}

function firstRunGuideLinkProps() {
  const build = () => navHref({ tab: "guide", focusQueryId: "how-flow" });
  return {
    href: build(),
    onClick: (e: any) => { e.preventDefault(); window.location.hash = build(); },
  };
}

function FirstRunShell({ brandName, tone, title, children }: LooseProps) {
  return (
    <div className="fr-shell">
      <div className="brand fr-brand">
        <span className="brand-title">{brandName}</span>
        <span className="brand-dot">*</span>
      </div>
      <div className={`honest-card fr-card ${tone}`}>
        <div className="h-title">{title}</div>
        {children}
      </div>
      <a className="guide-linklike" {...firstRunGuideLinkProps()}>How this app works -&gt;</a>
    </div>
  );
}

function SettingsInvalidScreen({ brandName, settingsError }: LooseProps) {
  const err = settingsError || {};
  return (
    <FirstRunShell brandName={brandName} tone="error" title="config/settings.yml needs a fix">
      <div className="h-note">
        {err.key && (
          <React.Fragment>
            Problem with <span className="mono">{err.key}</span>
            {err.line ? <span> (line {err.line})</span> : null}:{" "}
          </React.Fragment>
        )}
        {err.message || "The file could not be read."}
      </div>
      <CommandBlock command="config/settings.yml" />
      <div className="h-note">
        Open that file, fix the line above, save it, then reload this page. Nothing else in the
        app can load while this file does not match the documented shape -- every route reads it.
      </div>
    </FirstRunShell>
  );
}

function NoDatabaseScreen({ brandName }: LooseProps) {
  return (
    <FirstRunShell brandName={brandName} tone="info" title="No data yet">
      <div className="h-note">
        Pick how many days to pull. It runs every check on your Databricks SQL warehouse with the
        credentials in <span className="mono">.env</span> (see README.md), and takes minutes.
      </div>
      <GetData />
      <div className="h-note">Or from a terminal in the repo root, then reload this page:</div>
      <CommandBlock command="python tools/first_run.py" />
    </FirstRunShell>
  );
}

function BuildIncompleteScreen({ brandName, database }: LooseProps) {
  const detail = database && database.detail;
  return (
    <FirstRunShell brandName={brandName} tone="warn" title="The database build didn't finish">
      <div className="h-note">
        A database file exists, but the app could not read <span className="mono">dims.dim_workspace</span> from
        it -- the build failed before it finished, or the file is not a usable database. Pull the
        data again:
      </div>
      <GetData />
      <div className="h-note">Or from a terminal in the repo root, then reload this page:</div>
      <CommandBlock command="python tools/first_run.py" />
      {detail && <div className="h-note mono">{detail}</div>}
    </FirstRunShell>
  );
}

function UnknownStateScreen({ brandName, state }: LooseProps) {
  return (
    <FirstRunShell brandName={brandName} tone="error" title="Could not load">
      <div className="h-note">
        The app is not ready and did not say why (state: <span className="mono">{state || "unknown"}</span>).
        Reload this page, or check the terminal running <span className="mono">python -m app.api</span> for
        an error.
      </div>
    </FirstRunShell>
  );
}

// The one entry point App.tsx renders: appState is GET /api/status's own body, or null while it
// is still loading (never rendered in that case -- App.tsx keeps its own lightweight loading note
// for that, so this component only ever sees a resolved, non-"ready" state).
export function FirstRunScreen({ appState }: LooseProps) {
  const state = appState && appState.state;
  const brandName = (appState && appState.brand && appState.brand.name) || window.APP_BRAND || "Crosshire";
  React.useEffect(() => {
    document.title = brandName;
  }, [brandName]);
  if (state === "settings_invalid") {
    return <SettingsInvalidScreen brandName={brandName} settingsError={appState.settings_error} />;
  }
  if (state === "no_database") return <NoDatabaseScreen brandName={brandName} />;
  if (state === "build_incomplete") {
    return <BuildIncompleteScreen brandName={brandName} database={appState.database} />;
  }
  return <UnknownStateScreen brandName={brandName} state={state} />;
}
