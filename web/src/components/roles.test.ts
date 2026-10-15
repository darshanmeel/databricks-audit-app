import { describe, expect, it } from "vitest";
import { AREA_REGISTRY } from "./tab_registry";
import { ROLES, ROLE_AREAS, homeForRole, roleAreasFor, sidebarKeysForRole } from "./roles";

describe("roles", () => {
  it("gives the CFO one page plus the Guide", () => {
    expect(sidebarKeysForRole("cfo")).toEqual(["money", "guide"]);
    expect(homeForRole("cfo")).toBe("money");
  });

  it("gives every other role Overview, its own areas, All findings and the Guide", () => {
    for (const role of ROLES.filter((r) => r !== "cfo")) {
      expect(sidebarKeysForRole(role)).toEqual(["overview", ...ROLE_AREAS[role], "findings", "guide"]);
      expect(homeForRole(role)).toBe("overview");
    }
  });

  it("lists only areas that exist", () => {
    for (const role of ROLES) for (const area of roleAreasFor(role)) expect(AREA_REGISTRY[area]).toBeTruthy();
  });

  it("shows the CTO every content area and the data engineer its own first", () => {
    expect(ROLE_AREAS.cto).not.toContain("overview");
    expect(ROLE_AREAS.cto).not.toContain("findings");
    expect(ROLE_AREAS.data_engineer.slice(0, 4)).toEqual(["actions", "jobs", "compute", "queries"]);
  });

  it("treats an unknown role as FinOps", () => {
    expect(roleAreasFor("nobody")).toEqual(ROLE_AREAS.finops);
  });
});
