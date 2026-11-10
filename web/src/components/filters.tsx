// One picker per workspace tag key that at least one workspace carries (GET /api/meta attribute_keys_populated);
// an unpopulated key renders nothing. Takes everything as props and fetches nothing.

import React from "react";
import type { Workspace } from "../types";

const ATTRIBUTE_OUTCOME_VALUES: Record<string, boolean> = { not_tagged: true, no_usage: true, mixed: true };

function attributeLabel(key: string): string {
  // "cost_center" -> "Cost center": keys come from config, never hard-coded.
  const spaced = String(key).replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function AttributeValueOption({ value, count, avgShare, selected, onToggle }: {
  value: string; count: number; avgShare: number | null; selected: boolean; onToggle: () => void;
}) {
  const isOutcome = value in ATTRIBUTE_OUTCOME_VALUES;
  const pct = typeof avgShare === "number" ? `${Math.round(avgShare * 100)}%` : null;
  const title = isOutcome
    ? `${count} workspace(s) currently read "${value}" for this key`
    : `${count} workspace(s)` + (pct ? `, average dominant share ${pct}` : "");
  return (
    <label className="ws-row" title={title}>
      <input type="checkbox" checked={selected} onChange={onToggle} />
      <span className={isOutcome ? "muted" : ""}>{value}</span>
      <span className="muted"> ({count})</span>
    </label>
  );
}

export function AttributePicker({ attrKey, label: shownLabel, workspaces, selectedValues, onChange }: {
  attrKey: string; label?: string; workspaces: Workspace[] | null; selectedValues: Set<string> | null;
  onChange: (next: Set<string>) => void;
}) {
  // A dropdown, not a wall of chips: an account can carry many values.
  const [open, setOpen] = React.useState(false);
  const boxRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  // One row per value a workspace carries; 'no_usage' is not a tag so it is left out; 'not_tagged' and 'mixed' sort last.
  const valueCounts = React.useMemo(() => {
    const counts = new Map<string, number>();
    const shares = new Map<string, number[]>();
    (workspaces || []).forEach((w) => {
      const raw = w ? w[attrKey] : undefined;
      if (raw === undefined || raw === null || raw === "no_usage") return;
      const v = raw as string;
      counts.set(v, (counts.get(v) || 0) + 1);
      const share = w[`${attrKey}_share`];
      if (typeof share === "number") {
        const list = shares.get(v) || [];
        list.push(share);
        shares.set(v, list);
      }
    });
    const entries = Array.from(counts.entries());
    entries.sort((a, b) => {
      const aOutcome = a[0] in ATTRIBUTE_OUTCOME_VALUES;
      const bOutcome = b[0] in ATTRIBUTE_OUTCOME_VALUES;
      if (aOutcome !== bOutcome) return aOutcome ? 1 : -1;
      return String(a[0]).localeCompare(String(b[0]));
    });
    return entries.map(([value, count]) => {
      const list = shares.get(value) || [];
      const avgShare = list.length ? list.reduce((s, x) => s + x, 0) / list.length : null;
      return { value, count, avgShare };
    });
  }, [workspaces, attrKey]);

  const selectedSet = selectedValues || new Set<string>();
  const n = selectedSet.size;
  const label = n === 0 ? "All" : `${n} selected`;

  const toggleValue = (value: string) => {
    const next = new Set(selectedSet);
    if (next.has(value)) next.delete(value); else next.add(value);
    onChange(next);
  };

  return (
    <div className="filter-group ws-picker" ref={boxRef}>
      <span className="filter-label">{shownLabel || attributeLabel(attrKey)}</span>
      <button
        className="ws-trigger"
        onClick={() => setOpen(!open)}
        title={attrKey === "env" ? "Filter by Env (read from the workspace name, an env tag, or an override)" : `Filter by ${shownLabel || attributeLabel(attrKey)}`}
      >
        {label} <span className="caret">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <div className="ws-pop">
          <div className="ws-actions">
            <button onClick={() => onChange(new Set())}>All</button>
          </div>
          <div className="ws-list">
            {valueCounts.length === 0 && (
              <div className="ws-empty">No {(shownLabel || attributeLabel(attrKey)).toLowerCase()} values yet</div>
            )}
            {valueCounts.map(({ value, count, avgShare }) => (
              <AttributeValueOption
                key={value}
                value={value}
                count={count}
                avgShare={avgShare}
                selected={selectedSet.has(value)}
                onToggle={() => toggleValue(value)}
              />
            ))}
          </div>
          <div className="ws-foot">{valueCounts.length} value(s)</div>
        </div>
      )}
    </div>
  );
}

export function AttributeFilters({ workspaces, populatedKeys, topTags, selected, onChange }: {
  workspaces: Workspace[] | null; populatedKeys: string[] | null; topTags: { key: string; label: string }[] | null;
  selected: Record<string, Set<string>> | null; onChange: (key: string, next: Set<string>) => void;
}) {
  // `selected` is {canonicalKey: Set(values)}; `onChange(key, nextSet)` mirrors App.tsx's own
  // toggleWorkspace/setManyWorkspaces callback shape (set-by-key, not one big reducer) so a
  // caller can lift this state with minimal glue once it wires this component in.
  const keys = populatedKeys || [];
  if (keys.length === 0) return null;
  return (
    <React.Fragment>
      {keys.map((key) => (
        <AttributePicker
          key={key}
          attrKey={key}
          label={(topTags || []).find((t) => t.key === key)?.label}
          workspaces={workspaces}
          selectedValues={(selected && selected[key]) || new Set<string>()}
          onChange={(nextSet) => onChange(key, nextSet)}
        />
      ))}
    </React.Fragment>
  );
}

// A workspace_ids value no real workspace_id can ever equal -- see the `workspaceIds` memo inside
// App() for why an attribute filter matching none of the currently-picked workspaces must send
// this rather than [] (this app's own "no filter = every workspace" convention).
export const NO_WORKSPACE_MATCH = "__no_workspace_matches_filters__";
