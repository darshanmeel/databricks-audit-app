// root orchestrator: filters (window + workspace, ALL workspaces selected by
// default), the shell (logo, a left sidebar listing every area, top-bar window/env/cost-center/
// tag/workspace controls plus as-of date/theme, a menu button below ~900px), and cross-tab
// navigation (next:-chips, Overview's top-findings list and every "How is this worked out?" link
// land on the right area via QUERY_HOME / homeForQuery, tab_registry.ts). Every area (except
// Guide) renders through AreaPage (primitives.tsx) -- this file owns the shell only, never an
// area's own content.
//
// P2-FILTERS' tag/attribute-filter plumbing, P4-T-IDX's tag seam and P4-44's shareable-hash-state
// discipline are unchanged from before this redesign -- only the shell around them is new.

import { Tip, type TipPoint } from "./components/tip";
import React from "react";
import type { Workspace } from "./types";
import { Api, REFRESH_DAYS } from "./api";
import { fmtInt, fmtSnapshotWhen } from "./format";
import { HashState } from "./components/hash_state";
import { setCanDrill } from "./components/names";
import { useDims } from "./components/hooks";
import { FirstRunScreen, useRefresh } from "./components/first_run";
import { AttributeFilters, AttributePicker, NO_WORKSPACE_MATCH } from "./components/filters";
import { MandatoryTagPicker, TagPicker, TagScopeBanner, mandatoryFilterKeys, narrowingTag } from "./components/tag_picker";
import { AREA_ORDER, AREA_REGISTRY, OLD_TAB_REDIRECTS, bandOf, homeForQuery } from "./components/tab_registry";
import { ROLES, ROLE_LABEL, homeForRole, roleAreasFor, sidebarKeysForRole } from "./components/roles";
import { AreaPage, ErrorBoundary, assessedCounts } from "./components/primitives";
import { RegionScopeBanner } from "./components/scope";
import { setAppMasking } from "./components/finding_detail";
import { MoneyTab } from "./tabs/tab_money";
import { applyOldKeyRedirects, filtersFromHash, readNavHash } from "./components/nav_hash";
import { GuideTab } from "./components/guide";
import { GuideDrawer, openGuideDrawer } from "./components/guide_drawer";

// New area keys (tab_registry.ts's AREA_REGISTRY) plus "guide", the one tab with no AreaPage --
// the sidebar's own row order (every area, not a curated subset).
const TAB_KEYS = [...AREA_ORDER, "guide"];
// "money" is CFO-only in the sidebar (sidebarKeysForRole below) but still a real, linkable tab --
// accepted here so a shared link naming it, or a bookmark from before a role switch, still opens it.
// A hash may still carry an old tab/sub-tab key (section 4.2) -- accepted here, translated once
// below, so a bookmark or a shared link from before the redesign still lands on the right place.
const ALL_ACCEPTED_HASH_TABS = [...TAB_KEYS, "money", ...Object.keys(OLD_TAB_REDIRECTS)];

function areaLabel(key: any) {
  if (key === "guide") return "How it works";
  if (key === "money") return "Overview"; // the CFO's overview
  return (AREA_REGISTRY[key] && AREA_REGISTRY[key].label) || key;
}

function getStoredRole() {
  try {
    const v = localStorage.getItem("role");
    return ROLES.includes(v!) ? v : null;
  } catch (e) {
    return null; // localStorage unavailable -- FinOps stays the default
  }
}
// A link or bookmark to a page this role does not show lands on the role's home page instead.
function clampTabForRole(tab: any, role: any) {
  return sidebarKeysForRole(role).includes(tab) ? tab : homeForRole(role);
}

// ─────────── Theme: light or dark; with nothing stored it follows the OS. ───────────
function getStoredThemeMode() {
  try {
    const v = localStorage.getItem("theme");
    return v === "light" || v === "dark" ? v : null;
  } catch (e) {
    return null;
  }
}
function prefersDark() {
  return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
}
function applyTheme(mode: any) {
  const dark = mode === "dark" || (mode === null && prefersDark());
  if (dark) document.documentElement.setAttribute("data-theme", "dark");
  else document.documentElement.removeAttribute("data-theme");
  // Charts that read CSS variables at render time re-render on this event.
  window.dispatchEvent(new Event("themechange"));
}

// One choice for the page: the rail's toggle and the mobile drawer's both read and set it.
let themeMode = getStoredThemeMode(); // null = follow the OS

// Shared by both theme controls below -- the two-button group (mobile drawer) and the single
// icon button next to Guide (desktop rail).
function useThemeState() {
  const [mode, setMode] = React.useState(themeMode);
  const [osDark, setOsDark] = React.useState(prefersDark);
  React.useEffect(() => { applyTheme(mode); }, [mode, osDark]);
  React.useEffect(() => {
    const onTheme = () => setMode(themeMode);
    window.addEventListener("themechange", onTheme);
    const mq = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setOsDark(mq.matches);
    if (mq) { if (mq.addEventListener) mq.addEventListener("change", onChange); else mq.addListener(onChange); }
    return () => {
      window.removeEventListener("themechange", onTheme);
      if (mq) { if (mq.removeEventListener) mq.removeEventListener("change", onChange); else mq.removeListener(onChange); }
    };
  }, []);
  const isDark = mode === "dark" || (mode === null && osDark);
  const choose = (next: any) => {
    try { localStorage.setItem("theme", next); } catch (e) { /* this session's state still drives it */ }
    themeMode = next;
    setMode(next);
  };
  return { isDark, choose };
}

function ThemeToggle() {
  const { isDark, choose } = useThemeState();
  return (
    <div className="c-theme" role="group" aria-label="Theme">
      <button type="button" aria-pressed={!isDark} onClick={() => choose("light")}>Light</button>
      <button type="button" aria-pressed={isDark} onClick={() => choose("dark")}>Dark</button>
    </div>
  );
}

function ThemeIconToggle() {
  const { isDark, choose } = useThemeState();
  return (
    <button
      type="button"
      className="c-theme-icon"
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      title={isDark ? "Switch to light theme" : "Switch to dark theme"}
      onClick={() => choose(isDark ? "light" : "dark")}
    >
      {isDark ? (
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="8" cy="8" r="3.4" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path d="M8 1.6v1.6M8 12.8v1.6M1.6 8h1.6M12.8 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
      ) : (
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M13 9.5A5.5 5.5 0 116.5 3a4.5 4.5 0 006.5 6.5z" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinejoin="round" />
        </svg>
      )}
    </button>
  );
}

// The role ("lens") picks which pages the rail lists.
function LensSelect({ role, onChange }: LooseProps) {
  return (
    <label className="c-lens" title="Picks which pages you see; data is the same">
      <span className="c-rail-label">Lens</span>
      <select value={role} onChange={(e) => onChange(e.target.value)} aria-label="View as">
        {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
      </select>
    </label>
  );
}

// The server puts the settings brand into the page, so the name is right before /api/meta answers.
const BRAND_NAME = window.APP_BRAND || "Crosshire";

function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
        <path d="M8 1V15M1 8H15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    </span>
  );
}

// ─────────── Snapshot status: the common rule's own line -- source, as-of and checks that ran, all
// visible (not hidden behind a hover), plus an amber dot when any model failed or wasn't built. ──
function SnapshotStatus({ meta, checksAssessed }: LooseProps) {
  if (!meta) return <span className="muted">loading…</span>;
  const direct = meta.direct_export;
  const source = direct ? "Databricks" : (meta.snapshot_available ? "Snapshot" : null);
  const when = direct ? fmtSnapshotWhen(direct.as_of) : (meta.snapshot_available ? fmtSnapshotWhen(meta.as_of) : null);
  const m = meta.models || {};
  // assessedCounts(findings) (contract H) once the findings list has loaded -- the same count
  // Overview reads, so the two can never show a different "X/Y checks"; meta.models is only the
  // fallback while findings are still loading.
  const ranTotal = checksAssessed ? checksAssessed.total : (m.run_results_total || m.registry_total || 0);
  const assessedN = checksAssessed ? checksAssessed.assessed : (m.ok || 0);
  const checksText = ranTotal ? `${fmtInt(assessedN)} of ${fmtInt(ranTotal)} checks ran` : null;
  const dotCls = !source ? "unknown" : (m.failed > 0 || m.not_built > 0) ? "stale" : "";
  // The export took in today up to its run time, so today's figures are not final.
  const partialToday = !!(direct && direct.includes_today);
  const whenText = when ? `Data as of ${partialToday ? `${when} UTC` : when}` : source;
  const parts = [whenText, partialToday ? "today partial" : null].filter(Boolean);
  const notes: TipPoint[] = [];
  if (source && when) notes.push({ label: "Source:", text: source });
  if (m.failed || m.not_built) notes.push({ tone: "bad", label: "Not run:", text: `${fmtInt(m.failed || 0)} failed, ${fmtInt(m.not_built || 0)} not built` });
  if (partialToday) notes.push({ tone: "bad", label: "Today partial:", text: `up to ${when} UTC; billing can lag a few hours` });
  const dot = <span className={`snapshot-dot ${dotCls}`}></span>;
  return (
    <span className="row snapshot-status" style={{ gap: 6 }}>
      {notes.length ? <Tip points={notes}>{dot}</Tip> : dot}
      <span className="muted snapshot-text">
        {parts.length ? parts.join(" · ") : "no snapshot captured yet"}
        {checksText && (
          <React.Fragment>
            {" · "}
            <Tip points={[{ text: "Content tabs only" }, { text: "Reference lists not counted" }, { text: "Differs from the guide's total" }]}>{checksText}</Tip>
          </React.Fragment>
        )}
      </span>
    </span>
  );
}

// Refresh from Databricks: the app is not live, so it says when the data was last pulled and that
// a refresh runs every check on the warehouse (money), then lets the viewer run it anyway.

function agoText(asOf: any) {
  const t = asOf ? Date.parse(String(asOf).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(asOf) ? "" : "Z")) : NaN;
  if (Number.isNaN(t)) return null;
  const h = (Date.now() - t) / 3600000;
  return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`;
}

function RefreshControl({ meta }: LooseProps) {
  const [open, setOpen] = React.useState(false);
  const [days, setDays] = React.useState(30);
  const { st, start } = useRefresh();
  const boxRef = React.useRef<any>(null);
  React.useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e: any) => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const direct = meta && meta.direct_export;
  const when = direct ? fmtSnapshotWhen(direct.as_of) : null;
  const ago = direct ? agoText(direct.as_of) : null;
  const running = !!(st && st.running);
  const mins = running && st.elapsed_s != null ? Math.floor(st.elapsed_s / 60) : 0;
  const stepText = running ? (st.step === "load" ? "Loading into the app" : "Running the checks on Databricks") : null;
  return (
    <div className="ws-picker refresh-control" ref={boxRef}>
      <button type="button" className="ws-trigger" onClick={() => setOpen(!open)} title="Pull fresh data from Databricks">
        {running ? `Refreshing… ${mins} min` : "Refresh"}
      </button>
      {open && (
        <div className="ws-pop refresh-pop">
          <div className="refresh-last">{when ? `Last refresh: ${when} UTC${ago ? ` (${ago})` : ""}.` : "No refresh from Databricks yet."}</div>
          <div className="muted refresh-note">
            This dashboard is not live. A refresh runs every check on your Databricks SQL warehouse, which costs money, and takes several minutes. Refresh only when you need newer data.
          </div>
          {running ? (
            <div className="refresh-running">{`${stepText}… ${mins} min so far. The page reloads when it is done.`}</div>
          ) : (
            <React.Fragment>
              <div className="ws-actions">
                {REFRESH_DAYS.map((d) => (
                  <button key={d} type="button" className={d === days ? "on" : ""} onClick={() => setDays(d)}>{`Last ${d} days`}</button>
                ))}
              </div>
              {st && st.step === "failed" && <div className="refresh-error">{`Last refresh failed: ${st.error}`}</div>}
              <div className="tag-actions">
                <button type="button" className="tag-apply" onClick={() => start(days)}>{`Refresh last ${days} days`}</button>
              </div>
            </React.Fragment>
          )}
        </div>
      )}
    </div>
  );
}

// ─────────── Window control: one segmented button per available GET /api/meta `windows` entry --
// a window this database never exported is HIDDEN (never a dead disabled button); a partially-
// exported one is labelled "partial", so "30d" never silently reads as a full 30 days of money
// when the export only ran 10. ───────────
function WindowControl({ windows, value, onChange }: LooseProps) {
  const list = windows && windows.length ? windows : [7, 30, 90].map((d) => ({ days: d, available: true, covered_days: d, partial: false }));
  // A window this export never captured is HIDDEN, not shown disabled/greyed -- there is nothing
  // useful to tell a reader by leaving a dead button on screen for it.
  const shown = list.filter((w: any) => w.available);
  return (
    <div className="seg window-seg">
      {shown.map((w: any) => (
        <button
          key={w.days}
          type="button"
          className={value === w.days ? "active" : ""}
          title={w.partial ? `Partial: ${fmtInt(w.covered_days)} of ${w.days} days exported` : `${w.days} days`}
          onClick={() => onChange(w.days)}
        >
          {w.days}d{w.partial ? " partial" : ""}
        </button>
      ))}
    </div>
  );
}

// ─────────── Workspaces: a dropdown, not a wall of chips (a real account can carry many). ───────
function WorkspacePickerBody({ workspaces, selected, onToggle, onAll, onNone, onSetMany }: LooseProps) {
  const [q, setQ] = React.useState("");
  const sorted = React.useMemo(() => {
    const copy = workspaces.slice();
    copy.sort((a: any, b: any) => {
      const an = a.name || "", bn = b.name || "";
      if (!!an !== !!bn) return an ? -1 : 1;
      return (an || a.workspace_id).localeCompare(bn || b.workspace_id);
    });
    return copy;
  }, [workspaces]);

  const needle = q.trim().toLowerCase();
  const shown = needle
    ? sorted.filter((w: any) =>
        (w.name || "").toLowerCase().includes(needle) ||
        String(w.workspace_id).includes(needle) ||
        (w.env || "").toLowerCase().includes(needle))
    : sorted;

  const total = workspaces.length;
  const n = selected ? workspaces.filter((w: any) => selected.has(w.workspace_id)).length : 0;

  const envs = React.useMemo(() => {
    const seen = new Map();
    workspaces.forEach((w: any) => seen.set(w.env || "unknown", (seen.get(w.env || "unknown") || 0) + 1));
    return Array.from(seen.entries()).sort((a, b) => b[1] - a[1]);
  }, [workspaces]);

  return (
    <React.Fragment>
      <input
        className="ws-search"
        placeholder="Search name, id or env..."
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="ws-actions">
        <button onClick={needle ? () => onSetMany(shown.map((w: any) => w.workspace_id)) : onAll}>{needle ? `All ${shown.length} shown` : "All"}</button>
        <button onClick={onNone}>None</button>
        {envs.map(([env, count]) => (
          <button
            key={env}
            title={`Select only the ${count} ${env} workspace(s)`}
            onClick={() => onSetMany(workspaces.filter((w: any) => (w.env || "unknown") === env).map((w: any) => w.workspace_id))}
          >
            {env} ({count})
          </button>
        ))}
      </div>
      <div className="ws-list">
        {shown.length === 0 && <div className="ws-empty">No workspace matches "{q}"</div>}
        {shown.map((w: any) => (
          <label key={w.workspace_id} className="ws-row">
            <input type="checkbox" checked={selected ? selected.has(w.workspace_id) : false} onChange={() => onToggle(w.workspace_id)} />
            <span className={`ws-name ${w.name ? "" : "idonly"}`}>{w.name || w.workspace_id}</span>
            <span className={`env env-${w.env || "unknown"}`}>{w.env || "unknown"}</span>
            {w.region_status === "outside_region" && <span className="ws-region" title={w.region_reason || ""}>outside region</span>}
          </label>
        ))}
      </div>
      <div className="ws-foot">{shown.length} shown · {n} of {total} selected</div>
    </React.Fragment>
  );
}

// The workspace list follows the Env filter: only workspaces of the picked environments are offered.
function inEnvs(workspaces: any, envFilter: any) {
  return envFilter && envFilter.size ? workspaces.filter((w: any) => envFilter.has(w.env || "unknown")) : workspaces;
}

// Self-anchored dropdown trigger for the Workspaces control (same .ws-picker/.ws-trigger/.ws-pop
// shape as AttributePicker/TagPicker, so the four top-bar pickers behave identically).
function WorkspacesControl({ workspaces, selected, onToggle, onAll, onNone, onSetMany }: LooseProps) {
  const [open, setOpen] = React.useState(false);
  const boxRef = React.useRef<any>(null);
  React.useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e: any) => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false); };
    const onEsc = (e: any) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => { document.removeEventListener("mousedown", onDocClick); document.removeEventListener("keydown", onEsc); };
  }, [open]);

  const total = workspaces.length;
  const n = selected ? workspaces.filter((w: any) => selected.has(w.workspace_id)).length : 0;
  const label = total > 0 && n === total ? `All ${total}` : n === 0 ? "None" : `${n} of ${total}`;

  return (
    <div className="filter-group ws-picker" ref={boxRef}>
      <span className="filter-label">Workspaces</span>
      <button type="button" className="ws-trigger" onClick={() => setOpen(!open)} title="Filter by workspace">
        {label} <span className="caret">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <div className="ws-pop">
          <WorkspacePickerBody workspaces={workspaces} selected={selected} onToggle={onToggle} onAll={onAll} onNone={onNone} onSetMany={onSetMany} />
        </div>
      )}
    </div>
  );
}

// ─────────── Filters: one button for Env, every tag-attribute key and Workspaces, showing what's
// active, opens the existing filter controls. Reuses those same controls verbatim inside its own
// popover, so filtering itself never changes, only how many always-visible controls the top bar
// shows at once. ───────────
function activeFilterCount(attrFilters: any, tagFilter: any, reserved: string[]): number {
  let n = Object.values(attrFilters || {}).filter((s: any) => s && s.size).length;
  const norm = (k: string) => String(k).toLowerCase().replace(/[ _-]+/g, "");
  if (((tagFilter && tagFilter.groups) || []).some((g: any) => !reserved.includes(norm(g.key)))) n += 1;
  return n;
}

// Env and the mandatory tags (settings mandatory_tag_keys) sit in the bar as their own filters.
function MainFilters({ workspaces, envFilter, onEnvChange, tagFilter, onTagChange, meta }: LooseProps) {
  const keys = mandatoryFilterKeys(meta && meta.mandatory_tag_keys, meta && meta.top_tags);
  return (
    <React.Fragment>
      <AttributePicker attrKey="env" workspaces={workspaces} selectedValues={envFilter} onChange={onEnvChange} />
      {keys.map((k) => <MandatoryTagPicker key={k.key} tagKey={k.key} label={k.label} value={tagFilter} onChange={onTagChange} />)}
    </React.Fragment>
  );
}

// Workspace pickers for top tags that already have their own mandatory-tag picker are left out.
function otherAttributeKeys(meta: any): string[] {
  const norm = (k: string) => String(k).toLowerCase().replace(/[ _-]+/g, "");
  const mandatory = mandatoryFilterKeys(meta && meta.mandatory_tag_keys, meta && meta.top_tags).map((k) => k.key);
  return ((meta && meta.attribute_keys_populated) || []).filter((k: string) => !mandatory.includes(norm(k)));
}
function FiltersButton({
  workspaces, pickerSelected, toggleWorkspace, selectAll, selectNone, setManyWorkspaces,
  envFilter, onEnvChange, attrFilters, onAttrChange, tagFilter, onTagChange, meta,
}: LooseProps) {
  const [open, setOpen] = React.useState(false);
  const boxRef = React.useRef<any>(null);
  const triggerRef = React.useRef<any>(null);
  React.useEffect(() => {
    if (!open) return undefined;
    // Closing hands focus back to the button so keyboard users keep their place.
    const close = () => { setOpen(false); if (triggerRef.current) triggerRef.current.focus(); };
    const onDocClick = (e: any) => { if (boxRef.current && !boxRef.current.contains(e.target)) close(); };
    const onEsc = (e: any) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => { document.removeEventListener("mousedown", onDocClick); document.removeEventListener("keydown", onEsc); };
  }, [open]);
  const total = workspaces.length;
  const n = pickerSelected ? pickerSelected.size : 0;
  const wsText = total === 0 ? "no workspaces" : n === total ? `all ${fmtInt(total)} workspaces` : n === 0 ? "no workspaces" : `${fmtInt(n)} of ${fmtInt(total)} workspaces`;
  const reserved = mandatoryFilterKeys(meta && meta.mandatory_tag_keys, meta && meta.top_tags).map((k) => k.key);
  const active = activeFilterCount(attrFilters, tagFilter, reserved);
  return (
    <div className="filter-group ws-picker filters-picker" ref={boxRef}>
      <button type="button" ref={triggerRef} className="ws-trigger filters-trigger" aria-expanded={open} onClick={() => setOpen(!open)} title="Filter by other tags or workspaces">
        Filters <span className="filters-trigger-sum">{`${active ? `${fmtInt(active)} active` : "none"} · ${wsText}`}</span> <span className="caret">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <div className="ws-pop filters-pop">
          <AttributeFilters
            workspaces={workspaces}
            populatedKeys={otherAttributeKeys(meta)}
            topTags={(meta && meta.top_tags) || []}
            selected={attrFilters}
            onChange={onAttrChange}
          />
          <TagPicker value={tagFilter} onChange={onTagChange} reservedKeys={reserved} />
          <div className="filters-pop-divider" />
          <WorkspacesControl
            workspaces={inEnvs(workspaces, envFilter)} selected={pickerSelected} onToggle={toggleWorkspace}
            onAll={selectAll} onNone={selectNone} onSetMany={setManyWorkspaces}
          />
        </div>
      )}
    </div>
  );
}

// ─────────── Top bar: brand, window, Filters, as-of date,
// theme -- area navigation itself lives in the left sidebar (SideNav), not here. ───────────
function TopBar({
  meta, onOpenNavDrawer,
  filtersWindow, windows, onSetWindow,
  workspaces, pickerSelected, toggleWorkspace, selectAll, selectNone, setManyWorkspaces,
  envFilter, onEnvChange, attrFilters, onAttrChange, tagFilter, onTagChange,
  role, onRoleChange, checksAssessed, regionGap, solo, activeArea, onSelectArea,
}: LooseProps) {
  // Sticky panels below the bar offset themselves by its real height; it wraps on narrow screens.
  const barRef = React.useRef<any>(null);
  React.useEffect(() => {
    const el = barRef.current;
    if (!el || !window.ResizeObserver) return undefined;
    const ro = new ResizeObserver(() => document.documentElement.style.setProperty("--topbar-h", `${el.offsetHeight}px`));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <div className="topbar" ref={barRef}>
      <button type="button" className="topbar-menu-btn" onClick={onOpenNavDrawer} aria-label="Open areas menu">☰</button>
      <div className="brand topbar-brand">
        <BrandMark />
        <span className="brand-title">{(meta && meta.brand && meta.brand.name) || BRAND_NAME}</span>
      </div>

      <div className="topbar-controls">
        {solo && <LensSelect role={role} onChange={onRoleChange} />}
        <WindowControl windows={windows} value={filtersWindow} onChange={onSetWindow} />
        <MainFilters workspaces={workspaces} envFilter={envFilter} onEnvChange={onEnvChange} tagFilter={tagFilter} onTagChange={onTagChange} meta={meta} />
        <FiltersButton
          workspaces={workspaces} pickerSelected={pickerSelected} toggleWorkspace={toggleWorkspace}
          selectAll={selectAll} selectNone={selectNone} setManyWorkspaces={setManyWorkspaces}
          envFilter={envFilter} onEnvChange={onEnvChange} attrFilters={attrFilters} onAttrChange={onAttrChange}
          tagFilter={tagFilter} onTagChange={onTagChange} meta={meta}
        />
        <RegionScopeBanner meta={meta} regionGap={regionGap} />
        <SnapshotStatus meta={meta} checksAssessed={checksAssessed} />
        <RefreshControl meta={meta} />
        {solo && (
          <React.Fragment>
            {activeArea !== "money" && (
              <button type="button" className="topbar-guide-link" onClick={() => onSelectArea("money")}>Money</button>
            )}
            <button type="button" className="topbar-guide-link" onClick={() => onSelectArea("guide")}>Guide</button>
            <ThemeIconToggle />
          </React.Fragment>
        )}
      </div>
    </div>
  );
}

// ─────────── Left sidebar: top-level areas only -- sub-tabs show once, as tabs on the page
// itself (AreaPage's own SubTabs bar), never duplicated here too. Hidden below ~900px --
// SideNavDrawer takes over, and keeps its own sub-tab tree for a one-tap deep link. ───────────
function SideNav({ keys, activeArea, onSelectArea, criticalAreas }: LooseProps) {
  return (
    <nav className="side-nav" aria-label="Areas">
      {(keys || TAB_KEYS).map((key: any, i: any, all: any) => {
        const isActive = activeArea === key;
        return (
          <div key={key} className="side-nav-area">
            {navSectionLabel(all, i) && <div className="c-rail-label side-nav-label">{navSectionLabel(all, i)}</div>}
            <button
              type="button"
              className={`side-nav-link ${isActive ? "active" : ""}`}
              aria-current={isActive ? "page" : undefined}
              onClick={() => onSelectArea(key)}
            >
              {areaLabel(key)}
              {criticalAreas && criticalAreas.has(key) && (
                <span className="dot critical side-nav-dot" title="Has critical findings">
                  <span className="visually-hidden">Has critical findings</span>
                </span>
              )}
            </button>
          </div>
        );
      })}
    </nav>
  );
}

// Rail section labels: Explore before the first content area, Reference before the meta pages.
const NAV_META_KEYS = ["findings", "coverage", "guide"];
const NAV_TOP_KEYS = ["overview", "actions", "money"];
function navSectionLabel(keys: any, i: any) {
  const key = keys[i];
  const prev = i > 0 ? keys[i - 1] : null;
  if (NAV_META_KEYS.includes(key)) return prev === null || !NAV_META_KEYS.includes(prev) ? "Reference" : null;
  if (NAV_TOP_KEYS.includes(key)) return null;
  return prev === null || NAV_TOP_KEYS.includes(prev) ? "Explore" : null;
}

// The dark left rail: brand, lens, every page for the role, then "How it works" and the theme pinned
// at the bottom, so they stay on screen for every lens however long its page list is.
function Rail({ meta, role, onRoleChange, keys, activeArea, onSelectArea, criticalAreas }: LooseProps) {
  const pageKeys = (keys || []).filter((k: any) => k !== "guide");
  const helpActive = activeArea === "guide";
  // CFO's own "page list" is one entry (Money) it is always already on -- a dead link to the page
  // you're on reads as a sidebar with nothing in it, so this role shows none. Once a Money jump
  // (e.g. "See untagged spend") has moved the CFO off Money, that same single link is exactly
  // how they get back, so it reappears.
  const showPageNav = activeArea !== "money" || !(pageKeys.length === 1 && pageKeys[0] === "money");
  return (
    <aside className="c-rail">
      <div className="brand c-brand">
        <BrandMark />
        <span className="brand-title">{(meta && meta.brand && meta.brand.name) || BRAND_NAME}</span>
      </div>
      <LensSelect role={role} onChange={onRoleChange} />
      {showPageNav && (
        <div className="c-rail-nav">
          <SideNav keys={pageKeys} activeArea={activeArea} onSelectArea={onSelectArea} criticalAreas={criticalAreas} />
        </div>
      )}
      <div className="c-rail-foot">
        <div className="c-rail-foot-row">
          {(keys || []).includes("guide") && (
            <button
              type="button"
              className={`side-nav-link c-rail-help ${helpActive ? "active" : ""}`}
              aria-current={helpActive ? "page" : undefined}
              onClick={() => onSelectArea("guide")}
            >
              <span className="c-rail-help-mark" aria-hidden="true">?</span>
              {areaLabel("guide")}
            </button>
          )}
          <ThemeIconToggle />
        </div>
      </div>
    </aside>
  );
}

// ─────────── Mobile nav drawer (below ~900px): the same area/sub-tab tree as SideNav, plus the
// window control, every filter and the theme menu -- .topbar-controls (their usual home) is
// hidden at this width, so without these a shared link's env/attribute/tag filter, or the theme
// switch itself, would have nothing on screen to show or clear it. ───────
function SideNavDrawer({
  keys, activeArea, activeSubtab, onSelectArea, onSelectSubtab, onClose, windows, filtersWindow, onSetWindow, meta,
  workspaces, pickerSelected, toggleWorkspace, selectAll, selectNone, setManyWorkspaces,
  envFilter, onEnvChange, attrFilters, onAttrChange, tagFilter, onTagChange,
  role, onRoleChange, criticalAreas,
}: LooseProps) {
  const panelRef = React.useRef<any>(null);
  React.useEffect(() => {
    const panel = panelRef.current;
    const first = panel && panel.querySelector(".mobile-nav-close");
    if (first) first.focus();
    const onKey = (e: any) => {
      if (e.key === "Escape") { onClose(); return; }
      if (e.key !== "Tab" || !panel) return;
      // Keep Tab inside the open drawer.
      const items = Array.from(panel.querySelectorAll("button, select, input, a[href]")).filter((el: any) => !el.disabled && el.offsetParent !== null) as any[];
      if (!items.length) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) { e.preventDefault(); lastEl.focus(); }
      else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); firstEl.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const opener: any = document.querySelector(".topbar-menu-btn");
      if (opener) opener.focus();
    };
  }, []);
  return (
    <React.Fragment>
      <div className="mobile-nav-backdrop" onClick={onClose}></div>
      <div className="mobile-nav" ref={panelRef} role="dialog" aria-modal="true" aria-label="Areas menu">
        <button type="button" className="mobile-nav-close" onClick={onClose} aria-label="Close">✕</button>
        <WindowControl windows={windows} value={filtersWindow} onChange={onSetWindow} />
        <div className="mobile-nav-filters">
          <MainFilters workspaces={workspaces} envFilter={envFilter} onEnvChange={onEnvChange} tagFilter={tagFilter} onTagChange={onTagChange} meta={meta} />
          <AttributeFilters
            workspaces={workspaces}
            populatedKeys={otherAttributeKeys(meta)}
            topTags={(meta && meta.top_tags) || []}
            selected={attrFilters}
            onChange={onAttrChange}
          />
          <TagPicker value={tagFilter} onChange={onTagChange}
            reservedKeys={mandatoryFilterKeys(meta && meta.mandatory_tag_keys, meta && meta.top_tags).map((k) => k.key)} />
          <WorkspacesControl
            workspaces={inEnvs(workspaces, envFilter)} selected={pickerSelected} onToggle={toggleWorkspace}
            onAll={selectAll} onNone={selectNone} onSetMany={setManyWorkspaces}
          />
        </div>
        <div className="mobile-nav-row">
          <LensSelect role={role} onChange={onRoleChange} />
          <ThemeToggle />
        </div>
        {(keys || TAB_KEYS).map((key: any) => {
          const area = key === "guide" || key === "money" ? null : AREA_REGISTRY[key];
          const subtabs = area && (role !== "cto" || key === "coverage") ? (area.subtabs || []) : [];
          const isActive = activeArea === key;
          const currentSub = subtabs.some((s) => s.key === activeSubtab) ? activeSubtab : (subtabs[0] && subtabs[0].key);
          return (
            <React.Fragment key={key}>
              <button
                type="button"
                className={`mobile-nav-link ${isActive ? "active" : ""}`}
                onClick={() => { onSelectArea(key); if (subtabs.length <= 1) onClose(); }}
              >
                {areaLabel(key)}
                {criticalAreas && criticalAreas.has(key) && (
                  <span className="dot critical side-nav-dot" title="Has critical findings">
                    <span className="visually-hidden">Has critical findings</span>
                  </span>
                )}
              </button>
              {isActive && subtabs.length > 1 && subtabs.map((s) => (
                <button
                  key={s.key}
                  type="button"
                  className={`mobile-nav-link mobile-nav-sub ${currentSub === s.key ? "active" : ""}`}
                  onClick={() => { onSelectSubtab(s.key); onClose(); }}
                >
                  {s.label}
                </button>
              ))}
            </React.Fragment>
          );
        })}
      </div>
    </React.Fragment>
  );
}

// P4-44: this page's own address bar at mount time, read exactly once -- every "restore on load"
// below reads off this ONE snapshot rather than location.hash directly.
function useInitialHash() {
  return React.useState(() => {
    try { return HashState.get(); } catch (e) { return new URLSearchParams(); }
  })[0];
}

function workspaceSetFromHash(wsIds: Set<string> | null, ws: Workspace[]): Set<string> {
  const matched = wsIds ? ws.filter((w) => wsIds.has(String(w.workspace_id))).map((w) => w.workspace_id) : [];
  return new Set(matched.length ? matched : ws.map((w) => w.workspace_id));
}

const sameSet = (a: any, b: any) => !!a && !!b && a.size === b.size && Array.from(a).every((x) => b.has(x));
const attrFiltersKey = (a: any) => JSON.stringify(Object.keys(a).filter((k) => a[k] && a[k].size).sort()
  .map((k) => [k, Array.from(a[k]).sort()]));
const tagFilterStateKey = (t: any) => (t ? JSON.stringify(t.groups) : "");

// An empty brand part is dropped with its separator, so the line never shows a gap.
function BrandFooter({ meta }: LooseProps) {
  const brand = (meta && meta.brand) || {};
  const parts: React.ReactNode[] = [`© ${new Date().getFullYear()} ${brand.name || BRAND_NAME}`];
  if (brand.url) {
    const bare = brand.url.replace(/^https?:\/\//, "");
    const href = bare === brand.url ? `https://${bare}` : brand.url;
    parts.push(<a key="url" href={href} target="_blank" rel="noreferrer">{bare}</a>);
  }
  if (brand.contact_name) parts.push(brand.contact_name);
  if (brand.contact_email) {
    parts.push(<a key="mail" href={`mailto:${brand.contact_email}`}>{brand.contact_email}</a>);
  }
  return (
    <div className="footer-note">
      {parts.map((part, i) => (
        <React.Fragment key={i}>
          {i > 0 && " · "}
          {part}
        </React.Fragment>
      ))}
    </div>
  );
}

export default function App() {
  const initHash = useInitialHash();
  const [meta, setMeta] = React.useState<any>(null);
  const [workspaces, setWorkspaces] = React.useState<any[]>([]);
  const [findings, setFindings] = React.useState<any>(null);
  const [regionGap, setRegionGap] = React.useState<any>(null);
  const [loadError, setLoadError] = React.useState<any>(null);
  const [appState, setAppState] = React.useState<any>(null);
  const [navDrawerOpen, setNavDrawerOpen] = React.useState(false);
  // Where the Guide rail's search box and "Tabs" list portal into (App.tsx owns the dark rail;
  // GuideTab owns the Guide's own data) -- set once the <aside> mounts, cleared if it unmounts.
  const [guideRailNode, setGuideRailNode] = React.useState<HTMLElement | null>(null);
  const initFilters = React.useMemo(() => filtersFromHash(initHash), [initHash]);
  const initHashWindow = initFilters.window;
  const [window_, setWindow_] = React.useState(initHashWindow || 30);
  const [workspaceIdSet, setWorkspaceIdSet] = React.useState<Set<string> | null>(null);
  const workspacesRef = React.useRef<any[]>([]);
  workspacesRef.current = workspaces;
  const [envFilter, setEnvFilter] = React.useState<Set<string>>(initFilters.env);
  const [attrFilters, setAttrFilters] = React.useState(initFilters.attr);
  const [tagFilter, setTagFilterState] = React.useState(initFilters.tag);
  const initialRole = React.useMemo(() => {
    const fromHash = initHash.get("role");
    if (ROLES.includes(fromHash!)) return fromHash;
    return getStoredRole() || "finops";
  }, [initHash]);
  const [role, setRoleState] = React.useState(initialRole);
  const [nav, setNav] = React.useState(() => {
    const fromHash = applyOldKeyRedirects(readNavHash(ALL_ACCEPTED_HASH_TABS));
    return fromHash
      ? { tab: clampTabForRole(fromHash.tab, initialRole), subtab: fromHash.subtab, domain: fromHash.domain, focusQueryId: fromHash.focusQueryId }
      : { tab: homeForRole(initialRole!), subtab: null, domain: null, focusQueryId: null };
  });
  // Where the Guide's "← Back to …" link returns to -- the last area the reader was actually on,
  // kept in a ref since it only ever needs to be right at the moment nav.tab flips to "guide".
  const lastGuideBackRef = React.useRef<{ tab: string; subtab: string | null }>({ tab: homeForRole(initialRole!), subtab: null });
  React.useEffect(() => {
    if (nav.tab !== "guide") lastGuideBackRef.current = { tab: nav.tab, subtab: nav.subtab || null };
  }, [nav.tab, nav.subtab]);
  // `navigateHome: false` keeps the current area (CTO's "Full view" switches persona without
  // jumping away from whatever area was open); the picker (applyRole, the role menu's onChange)
  // always wants the new persona's own landing area.
  const applyRole = (next: any, opts: any) => {
    try { localStorage.setItem("role", next); } catch (e) { /* this session's state still drives it */ }
    setRoleState(next);
    if (!opts || opts.navigateHome !== false) {
      setNav({ tab: homeForRole(next), subtab: null, domain: null, focusQueryId: null });
    }
  };
  // Safety net for every path that can hand CFO a tab outside its own lens (a pasted link,
  // browser back/forward) -- applyRole already sends the picker itself to the right place. Money's
  // own jumps (e.g. "See untagged spend" -> Cost > Allocation) land on roleAreasFor("cfo"), so
  // those stay put; the rail's single page link (above) is the way back from them.
  React.useEffect(() => {
    if (role === "cfo" && nav.tab !== "money" && nav.tab !== "guide" && !roleAreasFor("cfo").includes(nav.tab)) {
      setNav((prev) => ({ ...prev, tab: "money", subtab: null, focusQueryId: null }));
    }
  }, [role, nav.tab]);
  React.useEffect(() => {
    HashState.set({ role: role === "finops" ? null : role });
    // Switching to a role that does not show the current page goes to that role's home page.
    if (!sidebarKeysForRole(role!).includes(nav.tab)) {
      setNav((prev) => ({ ...prev, tab: homeForRole(role!), subtab: null, focusQueryId: null }));
    }
  }, [role]);
  React.useEffect(() => {
    const onHash = () => {
      const n = applyOldKeyRedirects(readNavHash(ALL_ACCEPTED_HASH_TABS));
      if (!n) return;
      setNav((prev) => ({ ...prev, tab: n.tab, subtab: n.subtab, domain: n.domain, focusQueryId: n.focusQueryId }));
      const f = filtersFromHash(HashState.get());
      if (f.window) setWindow_(f.window);
      setEnvFilter((prev) => (sameSet(prev, f.env) ? prev : f.env));
      setAttrFilters((prev) => (attrFiltersKey(prev) === attrFiltersKey(f.attr) ? prev : f.attr));
      setTagFilterState((prev) => (tagFilterStateKey(prev) === tagFilterStateKey(f.tag) ? prev : f.tag));
      const hRole = HashState.get().get("role");
      if (ROLES.includes(hRole!)) setRoleState((prev) => (hRole !== prev ? hRole : prev));
      const ws = workspacesRef.current;
      if (ws.length) {
        const next = workspaceSetFromHash(f.wsIds, ws);
        setWorkspaceIdSet((prev) => (sameSet(prev, next) ? prev : next));
      }
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const dims = useDims();

  // A name with every value ticked stays in the picker but filters nothing.
  const activeTag = React.useMemo(() => narrowingTag(tagFilter), [tagFilter]);
  Api.setTagFilter(activeTag);
  // canDrill (contract H): off for the whole CTO persona, not only its own summary pages -- a
  // module flag (names.tsx) so every existing <Ref>/JobFocus.open call site respects it without
  // this file threading a prop through tables it does not own. Set during render (like the two
  // Api calls above), not an effect, so the very first render after a role change already reads it.
  setCanDrill(role !== "cto");

  React.useEffect(() => {
    Api.status().then(setAppState).catch((e) => setLoadError(String(e)));
  }, []);

  const ready = !!appState && appState.state === "ready";

  React.useEffect(() => {
    if (!ready) return;
    Api.meta().then((m) => {
      setMeta(m);
      setAppMasking(!!m.mask_user_identities);
      // The app opens on the window GET /api/meta says this db actually has
      // (open_window), never a fixed default the db might not have -- unless the hash already
      // named one.
      if (!initHashWindow) setWindow_(m.open_window != null ? m.open_window : m.default_window);
      if (m.brand && m.brand.name) document.title = m.brand.name;
    }).catch((e) => setLoadError(String(e)));
    Api.workspaces().then((ws) => {
      setWorkspaces(ws);
      setWorkspaceIdSet(workspaceSetFromHash(initFilters.wsIds, ws));
    }).catch((e) => setLoadError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready]);

  const attrMatchedIds = React.useMemo(() => {
    const activeKeys = Object.keys(attrFilters).filter((k) => attrFilters[k] && attrFilters[k].size > 0);
    if (activeKeys.length === 0) return null;
    const matched = new Set();
    workspaces.forEach((w) => {
      if (activeKeys.every((k) => attrFilters[k].has(w[k]))) matched.add(w.workspace_id);
    });
    return matched;
  }, [workspaces, attrFilters]);

  const workspaceIds = React.useMemo(() => {
    if (!workspaceIdSet) return [];
    // "None" picked (an explicitly empty selection, not "not loaded yet") must never fall through
    // to [], this app's own "no filter = every workspace" convention -- checked before either
    // branch below, so Workspaces > None reads as no spend under every filter combination.
    if (workspaceIdSet.size === 0) return [NO_WORKSPACE_MATCH];
    if (attrMatchedIds === null) {
      if (workspaceIdSet.size === workspaces.length) return [];
      return Array.from(workspaceIdSet);
    }
    const narrowed = Array.from(workspaceIdSet).filter((id) => attrMatchedIds.has(id));
    return narrowed.length === 0 ? [NO_WORKSPACE_MATCH] : narrowed;
  }, [workspaceIdSet, workspaces.length, attrMatchedIds]);

  const effectiveWorkspaceIdSet = React.useMemo((): Set<string> => {
    if (!workspaceIdSet) return new Set<string>();
    if (attrMatchedIds === null) return workspaceIdSet;
    return new Set(Array.from(workspaceIdSet).filter((id) => attrMatchedIds.has(id)));
  }, [workspaceIdSet, attrMatchedIds]);

  const envs = React.useMemo(() => Array.from(envFilter), [envFilter]);

  React.useEffect(() => {
    if (workspaceIdSet === null) return;
    setFindings(null);
    setRegionGap(null);
    Api.findings(window_, workspaceIds, envs)
      .then((d) => { setFindings(d.findings); setRegionGap(d.region_gap || null); })
      .catch((e) => setLoadError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [window_, JSON.stringify(workspaceIds), workspaceIdSet === null, envs.join(","), Api.tagFilterKey()]);

  React.useEffect(() => {
    HashState.set({
      tab: nav.tab, subtab: nav.subtab || null, domain: nav.domain || null, focus: nav.focusQueryId || null,
      w: String(window_),
    });
  }, [nav, window_]);

  React.useEffect(() => {
    if (!workspaceIdSet) return;
    const allSelected = workspaces.length > 0 && workspaceIdSet.size === workspaces.length;
    HashState.set({ ws: allSelected ? null : Array.from(workspaceIdSet).join(",") });
  }, [workspaceIdSet, workspaces.length]);

  React.useEffect(() => {
    HashState.set({ env: envFilter.size ? envs.join(",") : null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [envs.join(",")]);

  React.useEffect(() => {
    const obj: Record<string, any> = {};
    Object.keys(attrFilters).forEach((k) => {
      if (attrFilters[k] && attrFilters[k].size > 0) obj[k] = Array.from(attrFilters[k]).join(",");
    });
    HashState.setGroup("attr_", obj);
  }, [attrFilters]);

  React.useEffect(() => {
    HashState.set({
      tags: tagFilter ? JSON.stringify(tagFilter.groups.map((g) => (g.all ? [g.key, g.display_key, g.values, 1] : [g.key, g.display_key, g.values]))) : null,
    });
  }, [tagFilter]);

  const toggleWorkspace = (id: any) => {
    setWorkspaceIdSet((prev) => {
      // With every workspace ticked (the default), a click means "just this one", not "all but this one".
      if (prev && workspaces.length > 1 && prev.size === workspaces.length) return new Set([id]);
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const selectAll = () => setWorkspaceIdSet(new Set(workspaces.map((w) => w.workspace_id)));
  const selectNone = () => setWorkspaceIdSet(new Set());
  const setManyWorkspaces = (ids: any) => setWorkspaceIdSet(new Set(ids));

  const onAttrChange = (key: any, nextSet: any) => {
    setAttrFilters((prev) => {
      const next = { ...prev };
      if (nextSet && nextSet.size > 0) next[key] = nextSet; else delete next[key];
      return next;
    });
  };

  const filters = { window: window_, workspaceIds, envs, workspaceIdSet: effectiveWorkspaceIdSet, tag: activeTag };
  const pickerSelected = workspaceIdSet || new Set();
  const maxCat = (meta && meta.max_chart_categories) || 8;
  const sidebarKeys = sidebarKeysForRole(role!);
  const checksAssessed = findings ? assessedCounts(findings) : null;
  // Sidebar dot: which top-level areas carry at least one Critical finding right now.
  const criticalAreas = React.useMemo(() => {
    const s = new Set<string>();
    (findings || []).forEach((f: any) => { if (bandOf(f) === "CRITICAL") s.add(homeForQuery(f).tab); });
    return s;
  }, [findings]);

  const allFindingsIndex = React.useMemo(() => {
    const idx: Record<string, any> = {};
    (findings || []).forEach((f: any) => { idx[f.query_id] = f; });
    return idx;
  }, [findings]);

  const goTo = React.useCallback((target: any) => {
    // The CTO lens shows area summaries only, so a link to one check opens its Guide drawer.
    if (role === "cto" && target.focusQueryId && !["guide", "findings", "overview", "money"].includes(target.tab || "findings")) {
      openGuideDrawer(target.focusQueryId, allFindingsIndex[target.focusQueryId] || null, window_);
      return;
    }
    setNav({
      tab: target.tab || "findings",
      subtab: target.subtab || null,
      domain: target.domain || null,
      focusQueryId: target.focusQueryId || null,
    });
  }, [role, allFindingsIndex, window_]);

  const onExternalJump = React.useCallback((queryId: any) => {
    const row = allFindingsIndex[queryId];
    if (!row) return;
    const home = homeForQuery(row);
    goTo({ ...home, focusQueryId: queryId, domain: row.domain });
  }, [allFindingsIndex, goTo]);

  const selectArea = (key: any) => setNav({ tab: key, subtab: null, domain: null, focusQueryId: null });
  const onSubtabChange = (key: any) => setNav((prev) => ({ ...prev, subtab: key, focusQueryId: null }));

  const brandOf = (src: any) => src && src.brand && src.brand.name;
  const brandName = brandOf(meta) || brandOf(appState) || BRAND_NAME;

  if (appState && appState.state !== "ready") {
    if (nav.tab === "guide") {
      return (
        <div className="shell">
          <div className="topbar">
            <div className="brand">
              <span className="brand-title">{brandName}</span>
            </div>
          </div>
          <ErrorBoundary><GuideTab focus={nav.focusQueryId} filters={filters} /></ErrorBoundary>
        </div>
      );
    }
    return <FirstRunScreen appState={appState} />;
  }

  if (appState === null && !loadError) {
    return (
      <div className="fr-shell">
        <div className="brand fr-brand">
          <span className="brand-title">{brandName}</span>
        </div>
        <div className="loading-note">Checking setup...</div>
      </div>
    );
  }

  // CFO is one page: no sidebar, the controls and Guide link live in the top bar.
  const soloLens = role === "cfo" && nav.tab !== "guide";
  const windowOptions = (meta && meta.window_options) || [7, 30, 90];
  const windows = (meta && meta.windows) || windowOptions.map((d: number) => ({ days: d, available: true, covered_days: d, partial: false }));

  return (
    <div className={`c-app${soloLens ? " c-app--solo" : ""}${nav.tab === "guide" ? " c-app--guide" : ""}`}>
      {nav.tab === "guide" ? (
        // In the Guide, the same dark rail keeps the brand but swaps its page list for the
        // Guide's own menu (GuideTab portals it into the div below) -- never a second, cream
        // column inside the content.
        <aside className="c-rail">
          <div className="brand c-brand">
            <BrandMark />
            <span className="brand-title">{brandName}</span>
            <span className="c-rail-guide-badge">Guide</span>
          </div>
          <div className="c-rail-guide-nav" ref={setGuideRailNode} />
          <div className="c-rail-foot">
            <div className="c-rail-foot-row">
              <ThemeIconToggle />
            </div>
          </div>
        </aside>
      ) : !soloLens && (
        <Rail
          meta={meta} role={role} onRoleChange={applyRole} keys={sidebarKeys}
          activeArea={nav.tab} onSelectArea={selectArea} criticalAreas={criticalAreas}
        />
      )}
      <div className="c-main">
      <TopBar
        meta={meta}
        onOpenNavDrawer={() => setNavDrawerOpen(true)}
        filtersWindow={window_}
        windows={windows}
        onSetWindow={setWindow_}
        workspaces={workspaces}
        pickerSelected={pickerSelected}
        toggleWorkspace={toggleWorkspace}
        selectAll={selectAll}
        selectNone={selectNone}
        setManyWorkspaces={setManyWorkspaces}
        envFilter={envFilter}
        onEnvChange={setEnvFilter}
        attrFilters={attrFilters}
        onAttrChange={onAttrChange}
        tagFilter={tagFilter}
        onTagChange={setTagFilterState}
        role={role}
        onRoleChange={applyRole}
        checksAssessed={checksAssessed}
        regionGap={regionGap}
        solo={soloLens}
        activeArea={nav.tab}
        onSelectArea={selectArea}
      />
      {navDrawerOpen && (
        <SideNavDrawer
          keys={sidebarKeys}
          activeArea={nav.tab}
          activeSubtab={nav.subtab}
          onSelectArea={selectArea}
          onSelectSubtab={onSubtabChange}
          onClose={() => setNavDrawerOpen(false)}
          windows={windows}
          filtersWindow={window_}
          onSetWindow={setWindow_}
          meta={meta}
          workspaces={workspaces}
          pickerSelected={pickerSelected}
          toggleWorkspace={toggleWorkspace}
          selectAll={selectAll}
          selectNone={selectNone}
          setManyWorkspaces={setManyWorkspaces}
          envFilter={envFilter}
          onEnvChange={setEnvFilter}
          attrFilters={attrFilters}
          onAttrChange={onAttrChange}
          tagFilter={tagFilter}
          onTagChange={setTagFilterState}
          role={role}
          onRoleChange={applyRole}
          criticalAreas={criticalAreas}
        />
      )}

      <div className="c-content">
      {/* Desktop shows this notice in the top bar; phones hide the bar's controls, so it shows here. */}
      <div className="c-region-mobile"><RegionScopeBanner meta={meta} regionGap={regionGap} /></div>
      <TagScopeBanner tag={activeTag} />

      {loadError && (
        <div className="honest-card error" style={{ marginBottom: 16 }}>
          <div className="h-title">Could not load</div>
          <div className="h-note mono">{loadError}</div>
        </div>
      )}

      <div className="app-body">
        {/* Overviews and Actions get the roomier executive look (c_exec.css). */}
        <div className={`app-main${["overview", "money", "actions"].includes(nav.tab) ? " exec" : ""}`}>
          {findings === null && !loadError && <div className="loading-note">Loading data...</div>}

          {findings !== null && nav.tab !== "guide" && nav.tab !== "money"
            && !(role === "cto" && nav.tab === "overview") && (
            <ErrorBoundary>
              <AreaPage
                areaKey={nav.tab}
                findings={findings}
                filters={filters}
                dims={dims}
                maxCat={maxCat}
                meta={meta}
                allFindingsIndex={allFindingsIndex}
                onExternalJump={onExternalJump}
                initialFocusId={nav.focusQueryId || undefined}
                initialSubtab={nav.subtab}
                onSubtabChange={onSubtabChange}
                setManyWorkspaces={setManyWorkspaces}
                goTo={goTo}
                summaryOnly={role === "cto" && nav.tab !== "coverage"}
                role={role!}
                roleAreas={roleAreasFor(role!)}
              />
            </ErrorBoundary>
          )}

          {/* The one executive overview component: CFO's Money home, and CTO's Overview home
              (role === "cto" still gets every OTHER area's own summary above, via AreaPage). */}
          {findings !== null && (nav.tab === "money" || (role === "cto" && nav.tab === "overview")) && (
            <ErrorBoundary>
              <MoneyTab
                filters={filters} meta={meta} findingsById={allFindingsIndex} goTo={goTo}
                role={role!} showPerformance={role === "cto"}
              />
            </ErrorBoundary>
          )}

          {nav.tab === "guide" && (
            <ErrorBoundary>
              <GuideTab
                focus={nav.focusQueryId} filters={filters} findingsById={allFindingsIndex} meta={meta}
                role={role} goTo={goTo} backTo={lastGuideBackRef.current} railNode={guideRailNode}
                themeToggle={<ThemeIconToggle />}
              />
            </ErrorBoundary>
          )}
        </div>
      </div>

      <BrandFooter meta={meta} />
      </div>
      </div>
      <GuideDrawer />
    </div>
  );
}

