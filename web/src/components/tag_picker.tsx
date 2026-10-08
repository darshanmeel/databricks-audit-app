// the top bar's tag filter: a "Tag name" dropdown and a
// "Tag value" dropdown, both checkbox lists, applied together with one Apply button. Also the
// banner shown while a tag filter is active (TagScopeBanner).
//
// `value` is null or {groups: [{key, display_key, values: [..], all?}]}: values OR within one name,
// names AND together, an empty `values` means any value of that name (untagged left out), and
// `all` means every value ticked, untagged too, so the name narrows nothing. A picked name starts
// at `all`. GET /api/tags lists the names and, per name, its values; no account-specific setup.

import React from "react";

import { Api } from "../api";
import { fmtInt } from "../format";
import type { TagFilter, TagGroup, TagKeyEntry } from "../types";

const TAG_SEARCH_DEBOUNCE_MS = 250;
const TAG_UNTAGGED = "__untagged__";

export function tagValueLabel(v: string | null | undefined): string {
  if (v === TAG_UNTAGGED) return "untagged";
  if (v === "__empty__" || v === "" || v === null || v === undefined) return "(no value)";
  return v;
}

// Closes a popover on an outside click or Escape.
function usePopoverClose(open: boolean, setOpen: (open: boolean) => void, ref: React.RefObject<HTMLElement>) {
  React.useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);
}

function TagNameList({ draft, onToggle }: { draft: TagGroup[]; onToggle: (k: TagKeyEntry) => void }) {
  const [q, setQ] = React.useState("");
  const [state, setState] = React.useState<{ loading: boolean; keys: TagKeyEntry[]; truncated: boolean; error: string | null }>(
    { loading: true, keys: [], truncated: false, error: null });
  React.useEffect(() => {
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: null }));
    const t = setTimeout(() => {
      Api.tags(q, null, 200, 1)
        .then((d) => { if (!cancelled) setState({ loading: false, keys: d.keys || [], truncated: !!d.truncated, error: null }); })
        .catch((e) => { if (!cancelled) setState({ loading: false, keys: [], truncated: false, error: e.message || String(e) }); });
    }, TAG_SEARCH_DEBOUNCE_MS);
    return () => { cancelled = true; clearTimeout(t); };
  }, [q]);
  return (
    <React.Fragment>
      <input className="ws-search" autoFocus placeholder="Search tag names..." value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="ws-list">
        {state.loading && <div className="ws-empty">Loading tag names...</div>}
        {state.error && <div className="ws-empty">{/tag index/i.test(state.error)
          ? "Tags aren't in this export; the next export loads them. Filter by Workspaces meanwhile."
          : `Could not load tags: ${state.error}`}</div>}
        {!state.loading && !state.error && state.keys.length === 0 && <div className="ws-empty">{q ? `No tag names match "${q}".` : "No tags in this export."}</div>}
        {!state.loading && state.keys.map((k) => (
          <label key={k.tag_key} className="ws-row">
            <input type="checkbox" checked={draft.some((g) => g.key === k.tag_key)} onChange={() => onToggle(k)} />
            <span className="ws-name">{k.display_key || k.tag_key}</span>
            <span className="muted">{`${fmtInt(k.value_count)} value${k.value_count === 1 ? "" : "s"}`}</span>
          </label>
        ))}
      </div>
      {state.truncated && <div className="ws-foot">More names exist: search to find them.</div>}
    </React.Fragment>
  );
}

// The groups that narrow anything: a name with every value ticked is left out.
export function narrowingTag(tag: TagFilter | null): TagFilter | null {
  const groups = ((tag && tag.groups) || []).filter((g) => !g.all);
  return groups.length ? (groups.length === tag!.groups.length ? tag : { groups }) : null;
}

// `group` with `val` ticked or unticked, given every value the name has; at least one stays ticked.
// allValues is null when only part of the values loaded: then a tick names a value to keep, since
// "all but this one" can't be written without the rest, and unticking the last returns to all.
function toggleGroupValue(group: TagGroup, val: string, allValues: string[] | null): TagGroup {
  const base = { key: group.key, display_key: group.display_key };
  if (group.none) return { ...base, values: [val] };
  if (!allValues) {
    const kept = group.all || !group.values.length ? [] : group.values;
    const picked = kept.includes(val) ? kept.filter((x) => x !== val) : [...kept, val];
    return picked.length ? { ...base, values: picked } : { ...base, values: [], all: true };
  }
  const every = [...allValues, TAG_UNTAGGED];
  const ticked = group.all ? every : group.values.length ? group.values : allValues;
  const next = ticked.includes(val) ? ticked.filter((x) => x !== val) : [...ticked, val];
  if (!next.length) return { ...base, values: [], none: true };
  if (every.every((x) => next.includes(x))) return { ...base, values: [], all: true };
  if (!next.includes(TAG_UNTAGGED) && allValues.every((x) => next.includes(x))) return { ...base, values: [] };
  return { ...base, values: next };
}

function TagValueSection({ group, q, onToggleValue, onSetAll }: {
  group: TagGroup; q: string; onToggleValue: (key: string, val: string, allValues: string[] | null) => void;
  onSetAll: (key: string, all: boolean) => void;
}) {
  const [state, setState] = React.useState<{ loading: boolean; values: NonNullable<TagKeyEntry["values"]>; truncated: boolean }>(
    { loading: true, values: [], truncated: false });
  React.useEffect(() => {
    let cancelled = false;
    Api.tags("", group.key, 1, 500)
      .then((d) => {
        const row = (d.keys || [])[0];
        if (!cancelled) setState({ loading: false, values: (row && row.values) || [], truncated: !!(row && row.values_truncated) });
      })
      .catch(() => { if (!cancelled) setState({ loading: false, values: [], truncated: false }); });
    return () => { cancelled = true; };
  }, [group.key]);
  const needle = q.trim().toLowerCase();
  const partial = state.truncated;
  const shown = state.values.filter((v) => !needle || tagValueLabel(v.tag_value).toLowerCase().includes(needle));
  const row = (val: string, label: string, count: number | null) => (
    <label key={val} className="ws-row tag-value-row">
      <input type="checkbox" checked={group.none ? false : partial ? group.values.includes(val) : !!group.all || (group.values.length ? group.values.includes(val) : val !== TAG_UNTAGGED)}
        onChange={() => onToggleValue(group.key, val, partial ? null : state.values.map((v) => v.tag_value))} />
      <span className="ws-name" title={label}>{label}</span>
      {count != null && <span className="muted">{fmtInt(count)}</span>}
    </label>
  );
  return (
    <div className="tag-key-block">
      <div className="tag-key-head">
        <span className="tag-key-name">{group.display_key || group.key}</span>
        <span className="muted">{group.none ? "none" : group.all ? "all" : group.values.length ? `${group.values.length} selected` : "tagged only"}</span>
        <span className="tag-all-none">
          <button type="button" onClick={() => onSetAll(group.key, true)}>All</button>
          <button type="button" onClick={() => onSetAll(group.key, false)}>None</button>
        </span>
      </div>
      {state.loading && <div className="ws-empty">Loading values...</div>}
      {!state.loading && shown.map((v) => row(v.tag_value, tagValueLabel(v.tag_value), v.object_count))}
      {!state.loading && !needle && row(TAG_UNTAGGED, "untagged (no such tag)", null)}
      {!state.loading && state.truncated && <div className="ws-empty">Showing the first {fmtInt(state.values.length)} values: tick the ones to keep, or search to narrow.</div>}
    </div>
  );
}

export function TagPicker({ value, onChange }: { value: TagFilter | null; onChange: (tag: TagFilter | null) => void }) {
  const applied = (value && value.groups) || [];
  const [draft, setDraft] = React.useState<TagGroup[]>(applied);
  const [open, setOpen] = React.useState<"name" | "value" | null>(null);
  const [valueQ, setValueQ] = React.useState("");
  const boxRef = React.useRef<HTMLDivElement>(null);
  usePopoverClose(!!open, () => setOpen(null), boxRef);
  React.useEffect(() => { setDraft((value && value.groups) || []); }, [JSON.stringify(value)]);

  const toggleName = (k: TagKeyEntry) => setDraft((d) => (d.some((g) => g.key === k.tag_key)
    ? d.filter((g) => g.key !== k.tag_key)
    : [...d, { key: k.tag_key, display_key: k.display_key || k.tag_key, values: [], all: true }]));
  const toggleValue = (key: string, val: string, allValues: string[] | null) =>
    setDraft((d) => d.map((g) => (g.key !== key ? g : toggleGroupValue(g, val, allValues))));
  const setAll = (key: string, all: boolean) =>
    setDraft((d) => d.map((g) => (g.key !== key ? g
      : all ? { key: g.key, display_key: g.display_key, values: [], all: true }
        : { key: g.key, display_key: g.display_key, values: [], none: true })));
  // A name left with nothing ticked narrows nothing, so it is dropped.
  const apply = () => {
    const groups = draft.filter((g) => !g.none);
    onChange(groups.length ? { groups } : null); setOpen(null);
  };
  const clear = () => { setDraft([]); onChange(null); setOpen(null); };
  const toggleOpen = (which: "name" | "value") => setOpen(open === which ? null : which);

  const nameLabel = applied.length === 0 ? "any"
    : applied.length === 1 ? applied[0].display_key || applied[0].key : `${applied.length} names`;
  const narrowing = applied.filter((g) => !g.all);
  const pickedValues = narrowing.reduce((n, g) => n + g.values.length, 0);
  const valueLabel = applied.length === 0 ? "any" : narrowing.length === 0 ? "all" : pickedValues === 0 ? "tagged only"
    : pickedValues === 1 ? tagValueLabel(narrowing.find((g) => g.values.length)!.values[0]) : `${pickedValues} values`;
  const actions = (
    <div className="tag-actions">
      <button type="button" className="tag-apply" onClick={apply}>Apply</button>
      <button type="button" onClick={clear}>Clear</button>
    </div>
  );

  return (
    <div className="filter-group ws-picker tag-picker" ref={boxRef}>
      <span className="filter-label">Tag</span>
      <button className="ws-trigger" onClick={() => toggleOpen("name")} title="Pick tag names">
        {`Name: ${nameLabel}`} <span className="caret">{open === "name" ? "▴" : "▾"}</span>
      </button>
      <button className="ws-trigger" onClick={() => toggleOpen("value")} disabled={!draft.length}
        title={draft.length ? "Pick values of the chosen tag names" : "Pick a tag name first"}>
        {`Value: ${valueLabel}`} <span className="caret">{open === "value" ? "▴" : "▾"}</span>
      </button>
      {open === "name" && (
        <div className="ws-pop tag-pop">
          <TagNameList draft={draft} onToggle={toggleName} />
          {actions}
        </div>
      )}
      {open === "value" && (
        <div className="ws-pop tag-pop">
          <input className="ws-search" autoFocus placeholder="Search values..." value={valueQ} onChange={(e) => setValueQ(e.target.value)} />
          <div className="ws-list">
            {draft.map((g) => <TagValueSection key={g.key} group={g} q={valueQ} onToggleValue={toggleValue} onSetAll={setAll} />)}
          </div>
          <div className="ws-foot">Every value starts ticked: untick the ones to leave out, or click None and tick the ones to keep. Several names must all match.</div>
          {actions}
        </div>
      )}
    </div>
  );
}

// The banner shown under RegionScopeBanner while a tag filter is active.
export function TagScopeBanner({ tag }: { tag: TagFilter | null }) {
  const groups = (tag && tag.groups) || [];
  if (!groups.length) return null;
  const parts = groups.map((g) => `${g.display_key || g.key} = ${g.values.length ? g.values.map(tagValueLabel).join(" or ") : "any value, untagged left out"}`);
  return (
    <div className="scope-banner">
      {`Tag filter: ${parts.join("; and ")}. `}
      {"Each check is filtered at the most specific level it can see: query tag, then job tag, "}
      {"then compute tag, then workspace tag. Checks that cannot see tags are not filtered and "}
      {'are marked "tag n/a". Exact dollars for this tag: Cost › Allocation.'}
    </div>
  );
}

