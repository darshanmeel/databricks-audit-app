// the screen the reader is on, in location.hash, so a link
// opens the same tab, sub-tab, chart topic and finding. Keys a caller does not name are untouched.
import type React from "react";
import type { TagFilter } from "../types";
import { AREA_REGISTRY, OLD_SUBTAB_REDIRECTS, OLD_TAB_REDIRECTS } from "./tab_registry";

/** The screen in the hash: tab, sub-tab, findings view, focused check and domain. */
export interface Nav {
  tab: string;
  subtab: string | null;
  view: string | null;
  focusQueryId: string | null;
  domain: string | null;
}

/** The filters a link carries (filtersFromHash). */
export interface HashFilters {
  window: number | null;
  wsIds: Set<string> | null;
  env: Set<string>;
  attr: Record<string, Set<string>>;
  tag: TagFilter | null;
}

const NAV_HASH_KEYS: [keyof Nav, string][] = [["tab", "tab"], ["subtab", "subtab"], ["view", "view"], ["focusQueryId", "focus"], ["domain", "domain"]];
const NAV_VIEW_KEYS = ["overview", "critical", "warning", "ok", "not_assessed", "reference"];
const NAV_TOKEN_RE = /^[A-Za-z0-9_.:-]{1,200}$/;

export function readNavHash(validTabs: string[]): Nav | null {
  let p: URLSearchParams;
  try { p = new URLSearchParams((window.location.hash || "").replace(/^#/, "")); } catch (e) { return null; }
  const tab = p.get("tab");
  if (!tab || !(validTabs || []).includes(tab)) return null;
  const tok = (k: string) => { const v = p.get(k); return v && NAV_TOKEN_RE.test(v) ? v : null; };
  const view = p.get("view");
  return {
    tab,
    subtab: tok("subtab"),
    view: view && NAV_VIEW_KEYS.includes(view) ? view : null,
    focusQueryId: tok("focus"),
    domain: tok("domain"),
  };
}

function writeNavHash(nav: Partial<Nav> | null) {
  let p: URLSearchParams;
  try { p = new URLSearchParams((window.location.hash || "").replace(/^#/, "")); } catch (e) { p = new URLSearchParams(); }
  NAV_HASH_KEYS.forEach(([field, key]) => {
    const v = nav && nav[field];
    if (v === null || v === undefined || v === "") p.delete(key); else p.set(key, String(v));
  });
  const next = "#" + p.toString();
  if (window.location.hash !== next) {
    try { window.history.replaceState(null, "", next); } catch (e) { /* file:// or a sandbox: skip */ }
  }
}

// `patch` merges into the current hash; a key it does not name is left as-is, a null value deletes it.
export function navHref(patch?: Record<string, unknown> | null): string {
  let p: URLSearchParams;
  try { p = new URLSearchParams((window.location.hash || "").replace(/^#/, "")); } catch (e) { p = new URLSearchParams(); }
  const given: Record<string, unknown> = patch || {};
  NAV_HASH_KEYS.forEach(([fieldName, key]) => {
    // a caller may spell the patch key either as the nav field name or the short hash key
    const k = fieldName in given ? fieldName : (key in given ? key : null);
    if (k === null) return; // not named by this patch -- leave as-is
    const v = given[k];
    if (v === null || v === undefined || v === "") p.delete(key); else p.set(key, String(v));
  });
  return "#" + p.toString();
}

// A link within the Guide itself (a check's article, or a home topic) -- always a real hash
// change, so back/forward and a shared link both work exactly like any other in-app jump.
export function guideLinkProps(focusId?: string | null): { href: string; onClick: (e: React.MouseEvent) => void } {
  const href = navHref({ tab: "guide", focus: focusId || null });
  return {
    href,
    onClick: (e) => {
      e.preventDefault();
      if (href === window.location.hash) return; // already there
      window.location.hash = href;
    },
  };
}

// The filters a link carries: window, workspace ids, env, attribute filters and tag filter.
export function filtersFromHash(p: URLSearchParams): HashFilters {
  const w = Number(p.get("w"));
  const ws = p.get("ws");
  const envRaw = p.get("env");
  const attr: Record<string, Set<string>> = {};
  Array.from(p.keys()).forEach((k) => {
    const v = p.get(k);
    if (k.indexOf("attr_") === 0 && v) attr[k.slice(5)] = new Set(v.split(","));
  });
  let tag: TagFilter | null = null;
  try {
    const groups = JSON.parse(p.get("tags") || "null");
    if (Array.isArray(groups) && groups.length) tag = { groups: groups.map(([key, display_key, values, all]: [string, string, string[] | null, number?]) => ({ key, display_key, values: values || [], ...(all ? { all: true } : {}) })) };
  } catch (e) { /* a hand-edited link with a broken tags value just drops the tag filter */ }
  return {
    window: w > 0 ? w : null,
    wsIds: ws ? new Set(ws.split(",").filter(Boolean)) : null,
    env: envRaw ? new Set(envRaw.split(",").filter(Boolean)) : new Set<string>(),
    attr,
    tag,
  };
}

// section 4.2: a link carrying an old tab/sub-tab key still lands on the right place.
export function applyOldKeyRedirects<T extends { tab: string; subtab: string | null }>(nav: T | null): T | null {
  if (!nav) return nav;
  let tab = nav.tab;
  let subtab = nav.subtab;
  if (tab === "lineage") {
    tab = "governance";
    subtab = "lineage";
  } else if (OLD_TAB_REDIRECTS[tab]) {
    tab = OLD_TAB_REDIRECTS[tab];
  }
  // A key in the map can later become a real sub-tab of the resolved area (e.g. Cost's own
  // "chargeback") -- the real key always wins over the stale redirect.
  const area = AREA_REGISTRY[tab];
  const isLiveSubtab = area && area.subtabs.some((s) => s.key === subtab);
  if (subtab && !isLiveSubtab && OLD_SUBTAB_REDIRECTS[subtab]) subtab = OLD_SUBTAB_REDIRECTS[subtab];
  return { ...nav, tab, subtab };
}
