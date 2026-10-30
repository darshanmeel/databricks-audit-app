import { describe, expect, it } from "vitest";
import { dropMandatoryAttrs } from "./tag_picker";

describe("dropMandatoryAttrs", () => {
  it("drops attribute filters whose normalised key is mandatory", () => {
    const attr = { cost_center: new Set(["a"]), team: new Set(["b"]) };
    expect(Object.keys(dropMandatoryAttrs(attr, ["costcenter"]))).toEqual(["team"]);
  });
});
