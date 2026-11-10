import { describe, expect, it } from "vitest";
import { csvField } from "./findings_table";
import { allocSplit } from "./tag_rollup";

describe("csvField", () => {
  it("neutralises text that Excel would run as a formula", () => {
    expect(csvField("=HYPERLINK(\"x\")")).toBe("\"'=HYPERLINK(\"\"x\"\")\"");
    expect(csvField("+1")).toBe("'+1");
    expect(csvField("-job")).toBe("'-job");
    expect(csvField("@sum")).toBe("'@sum");
    expect(csvField("\tx")).toBe("'\tx");
  });

  it("keeps numbers numeric and quotes as before", () => {
    expect(csvField(-12.5)).toBe("-12.5");
    expect(csvField("a,b")).toBe("\"a,b\"");
    expect(csvField(null)).toBe("");
  });
});

describe("allocSplit", () => {
  it("reads the discounted usd_disc, falling back to usd", () => {
    const view: any = {
      phase: "ready", outcome: "ok_rows",
      data: {
        total: { usd: 100, usd_disc: 80 },
        by_value: [
          { value: "__untagged__", label: "Untagged", metrics: { usd: 30, usd_disc: 24 } },
          { value: "a", label: "A", metrics: { usd: 70, usd_disc: 56 } },
          { value: "b", label: "B", metrics: { usd: 5 } },
        ],
      },
    };
    expect(allocSplit(view)).toEqual({ total: 80, untagged: 24, items: [{ name: "A", value: 56 }, { name: "B", value: 5 }] });
  });
});
