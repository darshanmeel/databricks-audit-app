// Names, not ids, with a Databricks deep link, everywhere a job/cluster/warehouse/pipeline/run/workspace id renders,
// and the store that lets a job click anywhere open the Jobs focus panel.
// GET /api/dims and /api/workspaces are fetched once into a module cache (Refs live in unconnected tables).
// Job and pipeline ids are per workspace (key workspace_id:id); cluster and warehouse ids are global.
// A job with no name in its own workspace borrows the name from another workspace only when no second workspace
// uses the same id.

import React from "react";

import { Api } from "../api";
import { HashState } from "./hash_state";
import type { DimCluster, DimJob, DimNotebook, DimPipeline, DimWarehouse, Id, Workspace } from "../types";

/** A job id's one name and the workspaces it occurs in, when only the id is known. */
interface JobIdEntry {
  workspaces: Set<string>;
  name: string | null;
}

const _jobIdFallbackCache = new WeakMap<Map<string, { name?: string | null }>, Map<string, JobIdEntry>>();
function jobIdFallbackIndex(jobsMap: Map<string, { name?: string | null }>): Map<string, JobIdEntry> {
  const cached = _jobIdFallbackCache.get(jobsMap);
  if (cached) return cached;
  const byId = new Map<string, JobIdEntry>();
  jobsMap.forEach((rec, key) => {
    const sep = String(key).indexOf(":");
    if (sep < 0) return;
    const jobId = key.slice(sep + 1);
    const wsId = key.slice(0, sep);
    let entry = byId.get(jobId);
    if (!entry) { entry = { workspaces: new Set(), name: null }; byId.set(jobId, entry); }
    entry.workspaces.add(wsId);
    if (rec && rec.name && !entry.name) entry.name = rec.name;
  });
  _jobIdFallbackCache.set(jobsMap, byId);
  return byId;
}
// The fallback name alone (or null), for a jobs Map keyed "workspace_id:job_id" -- shared by
// resolveName's "job" case above.
function fallbackJobName(jobsMap: Map<string, { name?: string | null }>, jobId: Id): string | null {
  const entry = jobIdFallbackIndex(jobsMap).get(String(jobId));
  return (entry && entry.workspaces.size === 1 && entry.name) || null;
}
// jobNameFallback(dims, workspaceId, jobId) -- same rule, for hooks.ts's useDims() map shape
// (tab_jobs.tsx/tab_compute.tsx's own `dims` prop). Always returns a display string, matching
// hooks.ts's own jobName() "job <id>" final fallback, since chart labels need a plain string.
export function jobNameFallback(dims: { jobs?: Map<string, DimJob> } | null, workspaceId: Id, jobId: Id): string {
  if (!dims || !dims.jobs) return `job ${jobId}`;
  const rec = dims.jobs.get(`${workspaceId}:${jobId}`);
  return (rec && rec.name) || fallbackJobName(dims.jobs, jobId) || `job ${jobId}`;
}

export const Names = (function () {
  let dims = {
    jobs: new Map<string, DimJob>(), clusters: new Map<string, DimCluster>(), warehouses: new Map<string, DimWarehouse>(),
    pipelines: new Map<string, DimPipeline>(), notebooks: new Map<string, DimNotebook>(),
  };
  let whNameCount: Map<string, number> | null = null;
  let workspaces = new Map<string, Workspace>(); // workspace_id (string) -> full /api/workspaces record
  let ready = false;
  let loadPromise: Promise<void> | null = null;
  const listeners = new Set<() => void>();

  function notify() {
    listeners.forEach((fn) => {
      try { fn(); } catch (e) { /* one bad listener never blocks the others */ }
    });
  }

  function load() {
    if (loadPromise) return loadPromise;
    loadPromise = Promise.all([Api.dims(), Api.workspaces()])
      .then(([d, ws]) => {
        const jobs = new Map<string, DimJob>();
        (d.jobs || []).forEach((j) => jobs.set(`${j.workspace_id}:${j.job_id}`, j));
        const clusters = new Map<string, DimCluster>();
        (d.clusters || []).forEach((c) => clusters.set(String(c.cluster_id), c));
        const warehouses = new Map<string, DimWarehouse>();
        (d.warehouses || []).forEach((w) => warehouses.set(String(w.warehouse_id), w));
        const pipelines = new Map<string, DimPipeline>();
        (d.pipelines || []).forEach((p) => pipelines.set(`${p.workspace_id}:${p.pipeline_id}`, p));
        const notebooks = new Map<string, DimNotebook>();
        (d.notebooks || []).forEach((n) => notebooks.set(`${n.workspace_id}:${n.notebook_id}`, n));
        dims = { jobs, clusters, warehouses, pipelines, notebooks };
        whNameCount = null; // recount shared warehouse names for the new list

        const wsMap = new Map<string, Workspace>();
        (ws || []).forEach((w) => wsMap.set(String(w.workspace_id), w));
        workspaces = wsMap;
      })
      .catch(() => {
        // Names never load -> every id column falls back to its raw value (Ref's own contract),
        // never a blank and never a thrown render error.
      })
      .finally(() => {
        ready = true;
        notify();
      });
    return loadPromise;
  }

  function isReady() { return ready; }
  function subscribe(fn: () => void) { listeners.add(fn); return () => { listeners.delete(fn); }; }

  function workspaceRecord(workspaceId: Id): Workspace | null {
    if (workspaceId === undefined || workspaceId === null) return null;
    return workspaces.get(String(workspaceId)) || null;
  }

  function workspaceUrl(workspaceId: Id): string | null {
    const w = workspaceRecord(workspaceId);
    return (w && w.url) || null;
  }

  function jobRecord(workspaceId: Id, jobId: Id): DimJob | null {
    if (workspaceId === undefined || workspaceId === null || jobId === undefined || jobId === null) return null;
    return dims.jobs.get(`${workspaceId}:${jobId}`) || null;
  }

  // True when more than one warehouse carries this name; counted once per names load.
  function warehouseNameShared(name: string): boolean {
    if (!whNameCount) {
      const counts = new Map<string, number>();
      dims.warehouses.forEach((w) => { if (w && w.warehouse_name) counts.set(w.warehouse_name, (counts.get(w.warehouse_name) || 0) + 1); });
      whNameCount = counts;
    }
    return (whNameCount.get(name) || 0) > 1;
  }

  function resolveName(kind: string, workspaceId: Id, id: Id): string | null {
    if (id === undefined || id === null || id === "") return null;
    switch (kind) {
      case "workspace": {
        // 0 is the account-level pseudo-workspace id a few billing/audit rows carry for usage
        // that is not tied to any one workspace -- not a real id, so it never has a dim row and
        // would otherwise read as a lookup failure ("no name: not in system tables") forever.
        if (String(id) === "0") return "Account level";
        const w = workspaceRecord(id);
        return (w && w.name) || null;
      }
      case "job": {
        const j = jobRecord(workspaceId, id);
        return (j && j.name) || fallbackJobName(dims.jobs, id) || null;
      }
      case "cluster": {
        const c = dims.clusters.get(String(id));
        return (c && c.cluster_name) || null;
      }
      case "warehouse": {
        const w = dims.warehouses.get(String(id));
        if (!w || !w.warehouse_name) return null;
        return warehouseNameShared(w.warehouse_name) && w.workspace_id != null
          ? `${w.warehouse_name} · ${workspaceRecord(w.workspace_id)?.name || w.workspace_id}`
          : w.warehouse_name;
      }
      case "pipeline": {
        const p = dims.pipelines.get(`${workspaceId}:${id}`);
        return (p && p.pipeline_name) || null;
      }
      case "notebook": {
        const n = dims.notebooks.get(`${workspaceId}:${id}`);
        return (n && n.notebook_path) || null;
      }
      // "run" has no dim -- a run has no name, only an id -- resolveName intentionally always
      // returns null so <Ref kind="run"> falls back to the run id itself, never a fabricated name.
      default:
        return null;
    }
  }

  // Mirrors app/core/links.py exactly (job_url/run_url/cluster_url/warehouse_url): no
  // workspace_url on record -> null, never a broken/bare link built from a missing host.
  function deepLink(kind: string, workspaceId: Id, id: Id, runId?: Id): string | null {
    const wUrl = workspaceUrl(workspaceId);
    if (!wUrl) return null;
    const base = String(wUrl).replace(/\/+$/, "");
    const hasId = id !== undefined && id !== null && id !== "";
    const hasRun = runId !== undefined && runId !== null && runId !== "";
    const o = `?o=${encodeURIComponent(String(workspaceId))}`;
    switch (kind) {
      case "job":
        return hasId ? `${base}/jobs/${encodeURIComponent(String(id))}${o}` : null;
      // links.py run_url(workspace_url, workspace_id, job_id, run_id) -- `id` here IS the job_id
      // (the path needs it to build the nested /jobs/<job_id>/runs/<run_id> URL), `runId` is the
      // run's own id.
      case "run":
        return hasId && hasRun
          ? `${base}/jobs/${encodeURIComponent(String(id))}/runs/${encodeURIComponent(String(runId))}${o}`
          : null;
      case "cluster":
        return hasId ? `${base}/compute/clusters/${encodeURIComponent(String(id))}${o}` : null;
      case "warehouse":
        return hasId ? `${base}/sql/warehouses/${encodeURIComponent(String(id))}${o}` : null;
      // Serving endpoints are addressed by name in the UI.
      case "endpoint":
        return hasId ? `${base}/ml/endpoints/${encodeURIComponent(String(id))}${o}` : null;
      case "notebook":
        return hasId ? `${base}/${o}#notebook/${encodeURIComponent(String(id))}` : null;
      // links.py query_url: the path already carries a query string, so "o" rides as "&o=" here
      // instead of the "?o=" every other kind above uses.
      case "query":
        return hasId ? `${base}/sql/history?queryId=${encodeURIComponent(String(id))}&o=${encodeURIComponent(String(workspaceId))}` : null;
      default:
        return null; // pipeline/workspace: no known Databricks UI URL shape (links.py has none) --
        // never invent one.
    }
  }

  load(); // kick off the one fetch immediately, at script-load time
  return {
    load, isReady, subscribe, resolveName, deepLink,
    workspaceRecord, workspaceUrl, jobRecord,
  };
})();

// Subscribes a component to Names' cache so it re-renders once dims/workspaces have loaded
// (before that, every <Ref> already renders correctly off the raw id -- this only upgrades the
// same cell in place once names are ready, never blocks first paint on the fetch).
export function useNames() {
  const [, force] = React.useReducer((x: number) => x + 1, 0);
  React.useEffect(() => {
    if (Names.isReady()) return undefined;
    return Names.subscribe(force);
  }, []);
  return Names;
}

// ---------------------------------------------------------------------------------------------
// JobFocus -- open/close store for the Jobs focus panel, reachable from any job Ref; it also keeps the "job" key
// in location.hash, so a copied link reopens the panel.
// ---------------------------------------------------------------------------------------------
/** The job focus panel: open or closed, and for which job. */
export interface JobFocusState {
  open: boolean;
  workspaceId: Id;
  jobId: Id;
}

export const JobFocus = (function () {
  let state: JobFocusState = { open: false, workspaceId: null, jobId: null };
  const listeners = new Set<(s: JobFocusState) => void>();
  function emit() { listeners.forEach((fn) => { try { fn(state); } catch (e) { /* noop */ } }); }
  function syncHash() {
    try { HashState.set({ job: state.open ? `${state.workspaceId}:${state.jobId}` : null }); } catch (e) { /* no hash on this page */ }
  }
  // Restored once, at load, before anything subscribes -- JobFocusOverlay's own initial
  // useState(JobFocus.getState()) (tab_jobs.tsx) then opens straight into it.
  (function restoreFromHash() {
    try {
      const raw = HashState.get().get("job");
      if (!raw) return;
      const sep = raw.indexOf(":");
      if (sep <= 0) return; // malformed -- start closed, same as no link at all
      state = { open: true, workspaceId: raw.slice(0, sep), jobId: raw.slice(sep + 1) };
    } catch (e) { /* no hash on this page -- start closed */ }
  })();
  return {
    open(workspaceId: Id, jobId: Id) {
      if (!getCanDrill()) return; // CTO's summary-only view: no drill-down, however it was reached
      state = { open: true, workspaceId, jobId };
      syncHash();
      emit();
    },
    close() {
      state = { open: false, workspaceId: null, jobId: null };
      syncHash();
      emit();
    },
    getState(): JobFocusState { return state; },
    subscribe(fn: (s: JobFocusState) => void) { listeners.add(fn); return () => { listeners.delete(fn); }; },
  };
})();

export function resolveName(kind: string, workspaceId: Id, id: Id): string | null { return Names.resolveName(kind, workspaceId, id); }
export function deepLink(kind: string, workspaceId: Id, id: Id, runId?: Id): string | null { return Names.deepLink(kind, workspaceId, id, runId); }

// canDrill (contract H): true everywhere except the CTO persona's summary-only view, set once by
// App.tsx as the role changes. A module flag rather than a prop on every caller -- <Ref> and
// JobFocus.open sit inside tables and panels this app does not thread every prop through (job_
// panel.tsx, tab_jobs.tsx, finding_detail.tsx, ...), so every existing call site respects it for
// free instead of needing its own canDrill plumbing.
let _canDrill = true;
export function setCanDrill(v: boolean | undefined) { _canDrill = v !== false; }
function getCanDrill() { return _canDrill; }

// The one "Open in Databricks" affordance for a job/run/cluster/warehouse/query id -- null (never
// a dead link) when canDrill is off or no URL can be built.
export function DbxLink({ kind, workspaceId, id, runId, label }: { kind: string; workspaceId: Id; id: Id; runId?: Id; label?: string }) {
  if (!getCanDrill()) return null;
  const url = deepLink(kind, workspaceId, id, runId);
  if (!url) return null;
  return (
    <a
      className="ref-ext"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title="Link shape not confirmed against your workspace"
      onClick={(e) => e.stopPropagation()}
    >
      {label || "Open in Databricks ↗"}
    </a>
  );
}

// wsPhrase: "name (id)"'s plain-text sibling for a sentence, not a table cell -- the resolved
// workspace name, else "workspace <id>", else (a row with no workspace_id at all, e.g. an
// account-level line) "an unknown workspace" instead of the literal string "workspace null".
// tab_waste.tsx keeps its own nullable wsLabel (a different contract -- callers there supply
// their own fallback text); this is the always-a-string sibling every other caller wants.
export function wsPhrase(workspaceId: Id): string {
  return resolveName("workspace", workspaceId, workspaceId)
    || (workspaceId === null || workspaceId === undefined || workspaceId === "" ? "an unknown workspace" : `workspace ${workspaceId}`);
}

// "warehouse · workspace", with the workspace once: a name shared across workspaces already has it.
export function warehouseInWorkspace(workspaceId: Id, warehouseId: Id): string {
  const name = resolveName("warehouse", workspaceId, warehouseId) || `warehouse ${warehouseId}`;
  const ws = wsPhrase(workspaceId);
  return name.endsWith(` · ${ws}`) ? name : `${name} · ${ws}`;
}

// ---------------------------------------------------------------------------------------------
// <Ref kind id workspaceId runId label /> -- renders the resolved NAME as the visible text
// (falling back to the raw id, never a blank, when there is no dim row or Names has not loaded
// yet); links the name to Databricks when a url can be built; always keeps the raw id available
// as a title tooltip and a small copy affordance, because the id is what gets pasted into a
// Databricks search box and it must never be lost behind a friendly name.
//
// kind="job" is special-cased: the primary click opens the in-app job focus panel (top runs +
// diagnosis, tab_jobs.tsx) rather than immediately leaving the app, since that is the single most
// useful action on a job per this task's brief; a small separate "open in Databricks" arrow is
// still rendered alongside whenever a url exists, so the direct Databricks jump from bullet 1
// ("clickable Databricks links") is never lost, only demoted to a secondary affordance.
// ---------------------------------------------------------------------------------------------
// The five kinds a real Databricks UI URL exists for that also get the shared, uniform "Open in
// Databricks" trailing link (contract H) -- pipeline/notebook/workspace keep their own, unchanged
// Ref treatment (pipeline/workspace have no known URL shape at all; notebook's name is the link).
const DBX_LINK_KINDS = new Set(["job", "run", "cluster", "warehouse", "query"]);

export function Ref({ kind, id, workspaceId, runId, label }: { kind: string; id: Id; workspaceId?: Id; runId?: Id; label?: string | null }) {
  useNames();
  // Hook-order bug (NAMES-SPEC, DESIGN-DIRECTION.md section 6): this used to sit AFTER the
  // leafId-empty early return below, so a Ref instance whose id later became empty (or vice
  // versa) called a different number of hooks across renders -- exactly what React's "hooks must
  // not be called conditionally" rule exists to catch. Every hook this component uses now runs
  // before any return, unconditionally.
  const [copied, setCopied] = React.useState(false);

  const leafId = kind === "run" && runId !== undefined && runId !== null && runId !== "" ? runId : id;
  if (leafId === undefined || leafId === null || leafId === "") {
    return <span className="ref-empty">{"—"}</span>;
  }
  const idStr = String(leafId);
  // Section 6 "Ref 'name (id)'": three name states, each styled differently below --
  //   resolved  -- came from Names' own system-tables dim: full ink, shown as "name (id)".
  //   unverified -- a caller-supplied `label` that did NOT go through that lookup (a generated
  //                 or assumed name): a dotted --text-faint underline and a tooltip say so.
  //   none      -- no name at all: falls back to the bare id alone (never "id (id)"), muted.
  const resolved = resolveName(kind, workspaceId, id);
  const name = label || resolved;
  const hasName = !!name;
  const unverified = !!label && label !== resolved;
  const url = deepLink(kind, workspaceId, id, runId);

  const copyId = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(idStr).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }).catch(() => {});
      }
    } catch (err) { /* clipboard unavailable in this context -- the affordance silently no-ops */ }
  };

  const display = name || idStr;
  // The base tooltip names which of the three states this is, before any kind-specific caveat is
  // appended below -- so hovering a name always says AT LEAST what it is, verified or not.
  const nameTitle = !hasName
    ? `${idStr} -- no name: not in system tables`
    : unverified
      ? `${idStr} -- name not verified against system tables`
      : idStr;
  const nameCls = ["ref-name", unverified ? "ref-unverified" : "", !hasName ? "ref-noname" : ""]
    .filter(Boolean).join(" ");

  // canDrill off (CTO's summary-only view): plain text, no link, no job-focus button, no copy --
  // there is nothing here to drill into.
  if (!getCanDrill()) {
    return (
      <span className="ref">
        <span className={nameCls} title={nameTitle}>{display}</span>
        {hasName && <span className="ref-id" title={idStr}>{`(${idStr})`}</span>}
      </span>
    );
  }

  let nameEl;
  if (kind === "job") {
    nameEl = (
      <button
        type="button"
        className={`${nameCls} ref-job-open`}
        title={`${nameTitle} -- open job focus (top runs, diagnosis)`}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); JobFocus.open(workspaceId, id); }}
      >
        {display}
      </button>
    );
  } else if (DBX_LINK_KINDS.has(kind)) {
    // The Databricks link rides as a separate DbxLink after the name (below), same split as job's
    // own name/link -- so run/cluster/warehouse/query read consistently with it.
    nameEl = <span className={nameCls} title={nameTitle}>{display}</span>;
  } else if (url) {
    nameEl = (
      <a
        className={`${nameCls} ref-link`}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        title={`${nameTitle} -- opens Databricks (link shape is a best guess)`}
        onClick={(e) => e.stopPropagation()}
      >
        {display}
      </a>
    );
  } else {
    nameEl = <span className={nameCls} title={nameTitle}>{display}</span>;
  }

  return (
    <span className="ref">
      {nameEl}
      {/* Section 6 "name (id)": the id rides alongside the name, in Plex Mono, muted -- only when
          there IS a separate name to pair it with; the no-name fallback above already reads as
          the bare id on its own, so this never doubles it up as "id (id)". */}
      {hasName && <span className="ref-id" title={idStr}>{`(${idStr})`}</span>}
      {DBX_LINK_KINDS.has(kind) && <DbxLink kind={kind} workspaceId={workspaceId} id={id} runId={runId} />}
      <button type="button" className="ref-copy" title={`Copy id: ${idStr}`} onClick={copyId}>
        {copied ? "✓" : "⧉"}
      </button>
    </span>
  );
}
