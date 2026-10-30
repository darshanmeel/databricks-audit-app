// Roles: which pages each persona sees, in which order, and where it lands.

import { AREA_ORDER } from "./tab_registry";

// ─────────── Roles ("View as", top bar): a persona, never a permission boundary -- it only picks
// which areas the sidebar shows, their order, the landing area, and (CTO) whether an area opens
// its summary or its full drill-down. Kept in the hash (role=) and localStorage, same convention
// theme family/mode already use below.
export const ROLES = ["finops", "data_engineer", "governance", "cto", "cfo"];

export const ROLE_LABEL: Record<string, string> = { finops: "FinOps", data_engineer: "Data engineer", governance: "Governance", cto: "CTO", cfo: "CFO" };

// Every role lands on its Overview (the CFO's is the money page).
export function homeForRole(role: string): string {
  return role === "cfo" ? "money" : "overview";
}

// `priority` first (in the order given), then the rest of `order` in its existing relative order.
function reorderPriority(order: string[], priority: string[]): string[] {
  const first = new Set(priority);
  return [...priority, ...order.filter((k) => !first.has(k))];
}

// Every content area (AREA_ORDER) minus "overview" and "findings" -- sidebarKeysForRole adds
// those two back itself, at a fixed position, so ROLE_AREAS only ever lists the areas a role's
// own picture of the app actually differs on.
export const CONTENT_AREAS = AREA_ORDER.filter((k) => k !== "overview" && k !== "findings");

// roleAreasFor (contract H): the area keys a role sees -- passed into AreaPage so a registered
// Content and "All findings" (tab_findings.tsx) can filter to the same set the sidebar itself
// shows, and never disagree about which areas belong to which role.
export const ROLE_AREAS: Record<string, string[]> = {
  finops: ["actions", "cost", "waste", "mlai", "genie", "compute", "tags", "coverage"],
  data_engineer: reorderPriority(CONTENT_AREAS, ["actions", "jobs", "compute", "queries"]),
  cto: CONTENT_AREAS,
  governance: ["actions", "governance", "tags"],
  // The CFO's own sidebar shows the Money page, not these areas (sidebarKeysForRole below) --
  // this list is still what "All findings" would scope to, were a CFO ever to open it.
  cfo: ["cost", "waste", "mlai"],
};

export function roleAreasFor(role: string): string[] {
  return ROLE_AREAS[role] || ROLE_AREAS.finops;
}

// The sidebar's own area list per role: CFO one page, everyone else Overview + their own areas +
// All findings + Guide.
export function sidebarKeysForRole(role: string): string[] {
  if (role === "cfo") return ["money", "guide"];
  return ["overview", ...roleAreasFor(role), "findings", "guide"];
}
