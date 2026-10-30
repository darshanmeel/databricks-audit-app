// Start-up waits beyond each query's wait added up: the clock time queries waited and the
// warehouses' own starts, per warehouse over the window (perf_daily_by_resource).
import React from "react";

import { fmtDuration, fmtInt } from "../format";
import { numOrZero, useFindingAgg } from "../components/hooks";
import type { AggState } from "../types";

export interface Startup { clock: number; starts: number; startS: number; longest: number; failed: number }

const QID = "perf_daily_by_resource";
const GROUP = ["resource_type", "resource_key"];

function perWarehouse(s: AggState): Map<string, number> | null {
  if (s.phase !== "ready" || s.outcome !== "ok_rows" || !s.data) return null;
  const out = new Map<string, number>();
  s.data.groups.forEach((g) => { if (g.key[0] === "warehouse") out.set(String(g.key[1]), numOrZero(g.value)); });
  return out;
}

/** "workspace_id:warehouse_id" -> its start-up numbers, plus the total; null until loaded or on an older export. */
export function useStartupWaits(window_: number, workspaceIds: string[] | null, envs: string[] | null, on = true):
  { byWh: Map<string, Startup>; total: Startup } | null {
  const q = on ? QID : null;
  const clock = useFindingAgg(q, window_, workspaceIds, envs, GROUP, "sum", "provision_clock_s");
  const starts = useFindingAgg(q, window_, workspaceIds, envs, GROUP, "sum", "starts");
  const startS = useFindingAgg(q, window_, workspaceIds, envs, GROUP, "sum", "start_s");
  const longest = useFindingAgg(q, window_, workspaceIds, envs, GROUP, "max", "start_s_max");
  const failed = useFindingAgg(q, window_, workspaceIds, envs, GROUP, "sum", "failed_starts");
  return React.useMemo(() => {
    const maps = [clock, starts, startS, longest, failed].map(perWarehouse);
    if (maps.some((x) => !x)) return null;
    const [c, n, s, l, f] = maps as Map<string, number>[];
    const byWh = new Map<string, Startup>();
    const total: Startup = { clock: 0, starts: 0, startS: 0, longest: 0, failed: 0 };
    new Set([...c.keys(), ...n.keys()]).forEach((k) => {
      const w = { clock: c.get(k) || 0, starts: n.get(k) || 0, startS: s.get(k) || 0, longest: l.get(k) || 0, failed: f.get(k) || 0 };
      byWh.set(k, w);
      total.clock += w.clock; total.starts += w.starts; total.startS += w.startS; total.failed += w.failed;
      total.longest = Math.max(total.longest, w.longest);
    });
    return { byWh, total };
  }, [clock, starts, startS, longest, failed]);
}

/** "started 12 times, 3 m starting, longest 40 s; 1 never reached running" */
export function startsText(t: Pick<Startup, "starts" | "startS" | "longest" | "failed">): string {
  if (!t.starts) return "no warehouse start recorded";
  return `started ${fmtInt(t.starts)} time${t.starts === 1 ? "" : "s"}, ${fmtDuration(t.startS)} starting, longest ${fmtDuration(t.longest)}`
    + (t.failed ? `; ${fmtInt(t.failed)} never reached running` : "");
}
