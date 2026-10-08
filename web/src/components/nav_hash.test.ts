import { afterEach, describe, expect, it } from "vitest";
import { applyOldKeyRedirects, filtersFromHash, navHref, readNavHash } from "./nav_hash";

const params = (hash: string) => Object.fromEntries(new URLSearchParams(hash.replace(/^#/, "")));

afterEach(() => {
  window.location.hash = "";
});

describe("navHref", () => {
  it("merges into the current hash and leaves keys it does not name", () => {
    window.location.hash = "#tab=cost&w=30";
    expect(params(navHref({ tab: "guide", focus: "x" }))).toEqual({ tab: "guide", w: "30", focus: "x" });
  });

  it("deletes a key given as null", () => {
    window.location.hash = "#tab=guide&focus=x&w=30";
    expect(params(navHref({ focus: null }))).toEqual({ tab: "guide", w: "30" });
  });

  it("takes the field name or the short hash key", () => {
    window.location.hash = "#tab=guide";
    expect(navHref({ focusQueryId: "y" })).toBe(navHref({ focus: "y" }));
  });
});

describe("readNavHash", () => {
  it("reads tab, sub-tab, view and focus", () => {
    window.location.hash = "#tab=cost&subtab=trend&view=critical&focus=cost_daily_spikes";
    expect(readNavHash(["cost"])).toEqual({
      tab: "cost", subtab: "trend", view: "critical", focusQueryId: "cost_daily_spikes", domain: null,
    });
  });

  it("ignores a tab it does not know", () => {
    window.location.hash = "#tab=nope";
    expect(readNavHash(["cost"])).toBeNull();
  });

  it("drops a hand-edited value with odd characters or an unknown view", () => {
    window.location.hash = "#tab=cost&subtab=%3Cscript%3E&view=everything";
    const nav = readNavHash(["cost"]);
    expect(nav!.subtab).toBeNull();
    expect(nav!.view).toBeNull();
  });
});

describe("applyOldKeyRedirects", () => {
  it("sends an old tab key to its new tab", () => {
    expect(applyOldKeyRedirects({ tab: "query", subtab: null })!.tab).toBe("queries");
    expect(applyOldKeyRedirects({ tab: "domains", subtab: null })!.tab).toBe("findings");
  });

  it("opens the old Lineage tab as Governance > Lineage", () => {
    expect(applyOldKeyRedirects({ tab: "lineage", subtab: null })).toMatchObject({ tab: "governance", subtab: "lineage" });
  });

  it("sends an old sub-tab key to its new sub-tab", () => {
    expect(applyOldKeyRedirects({ tab: "jobs", subtab: "timing" })!.subtab).toBe("slow");
  });

  it("keeps a key that is a real sub-tab of the area today", () => {
    expect(applyOldKeyRedirects({ tab: "cost", subtab: "chargeback" })!.subtab).toBe("chargeback");
  });
});

describe("filtersFromHash", () => {
  it("reads window, workspaces, env, attribute and tag filters", () => {
    const tags = JSON.stringify([["cost_center", "Cost center", ["finance"]]]);
    const f = filtersFromHash(new URLSearchParams(`w=30&ws=1,2&env=prod&attr_team=a,b&tags=${encodeURIComponent(tags)}`));
    expect(f.window).toBe(30);
    expect([...f.wsIds!]).toEqual(["1", "2"]);
    expect([...f.env]).toEqual(["prod"]);
    expect([...f.attr.team]).toEqual(["a", "b"]);
    expect(f.tag).toEqual({ groups: [{ key: "cost_center", display_key: "Cost center", values: ["finance"] }] });
  });

  it("falls back to no filter for a missing or broken value", () => {
    const f = filtersFromHash(new URLSearchParams("w=0&tags=%5Bbroken"));
    expect(f.window).toBeNull();
    expect(f.wsIds).toBeNull();
    expect(f.env.size).toBe(0);
    expect(f.tag).toBeNull();
  });
});
