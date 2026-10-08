// P4-44: the one place the app reads/writes location.hash,
// so every view (tab/subtab/focus, window/workspaces/attributes/tag, and a job focus panel) has a
// URL that reproduces it -- paste it anywhere and it opens to the same place. Plain "key=value"
// pairs in the hash (never JSON, so a pasted link stays readable), merged additively: a caller's
// own set() only ever touches the keys it passes, so App.tsx's own filter sync and names.tsx's
// job-focus sync can never clobber each other's keys.

import React from "react";

export const HashState = (function () {
  function get() {
    const raw = (window.location.hash || "").replace(/^#/, "");
    return new URLSearchParams(raw);
  }
  function write(params: URLSearchParams) {
    const next = params.toString();
    const url = window.location.pathname + window.location.search + (next ? `#${next}` : "");
    // replaceState, not a location.hash assignment -- a filter change (or a job panel opening)
    // must never push a new browser-history entry, the same reasoning every filter picker here
    // already follows for its own state.
    window.history.replaceState(null, "", url);
  }
  // Sets/clears the given keys (a null/undefined/"" value removes that key), leaving every other
  // key already in the hash untouched.
  function set(patch: Record<string, string | number | null | undefined>) {
    const params = get();
    Object.keys(patch || {}).forEach((k) => {
      const v = patch[k];
      if (v === null || v === undefined || v === "") params.delete(k);
      else params.set(k, String(v));
    });
    write(params);
  }
  // Replaces every existing "<prefix>*" key with exactly the ones in `obj` -- for a variable-size
  // group (the attribute-filter keys, cost_center/team/business_unit/domain/...) where `set` alone
  // could never know which of a PREVIOUS build's keys to clear.
  function setGroup(prefix: string, obj: Record<string, string | null | undefined>) {
    const params = get();
    Array.from(params.keys()).forEach((k) => { if (k.indexOf(prefix) === 0) params.delete(k); });
    Object.keys(obj || {}).forEach((k) => {
      const v = obj[k];
      if (v !== null && v !== undefined && v !== "") params.set(prefix + k, v);
    });
    write(params);
  }
  return { get, set, setGroup };
})();

// <CopyLinkButton label /> -- copies the page's CURRENT address (location.href) to the clipboard.
// Every view that can be reached through HashState's own keys already has that exact state in the
// address bar by the time this renders (App.tsx and FindingsTable/JobFocus write their own keys as
// they change, not on a timer), so this needs no props of its own -- one button, reused wherever a
// "send this" affordance is wanted (FindingDetail, the job focus panel).
export function CopyLinkButton({ label }: { label?: string }) {
  const [copied, setCopied] = React.useState(false);
  const copy = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(window.location.href).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }).catch(() => {});
      }
    } catch (err) { /* clipboard unavailable in this context -- the affordance silently no-ops */ }
  };
  return (
    <button
      type="button"
      className="copy-link-btn"
      title="Copy a link to this exact view -- tab, filters and whatever is open here"
      onClick={copy}
    >
      {copied ? "Link copied" : (label || "Copy link")}
    </button>
  );
}
