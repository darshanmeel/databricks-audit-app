// A chart is drawn only from an ok_rows result; every other outcome shows its note instead.
import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { ChartNote, type NoteState } from "./charts";
import { rowsOf } from "./hooks";
import type { FindingData, FindingState, Outcome } from "../types";

afterEach(cleanup);

const ready = (outcome: Outcome, data: Partial<FindingData> = {}): FindingState =>
  ({ phase: "ready", outcome, data: data as FindingData, error: null });

describe("rowsOf", () => {
  it("returns rows only for a finished ok_rows fetch", () => {
    expect(rowsOf(ready("ok_rows", { rows: [{ a: 1 }] }))).toEqual([{ a: 1 }]);
    for (const outcome of ["ok_empty_window", "ok_empty_filters", "not_assessed", "error"] as Outcome[]) {
      expect(rowsOf(ready(outcome, { rows: [{ a: 1 }] }))).toBeNull();
    }
    expect(rowsOf({ phase: "loading", outcome: null, data: null, error: null })).toBeNull();
  });
});

describe("ChartNote", () => {
  const cases: [string, NoteState, string][] = [
    ["loading", { phase: "loading" }, "Loading..."],
    ["empty window", ready("ok_empty_window"), "Nothing in this window (not a verified zero"],
    ["empty filters", ready("ok_empty_filters"), "the current filters exclude all of them"],
    ["read error", ready("error", { error: "Could not open the table." }), "Could not open the table."],
    ["fetch error", { phase: "error", error: "500 Internal Server Error" }, "500 Internal Server Error"],
  ];
  for (const [name, state, text] of cases) {
    it(`shows a note, not a chart, for ${name}`, () => {
      const { container } = render(<ChartNote state={state} />);
      expect(container.textContent).toContain(text);
      expect(container.querySelector("svg")).toBeNull();
    });
  }

  it("says why a check was not assessed", () => {
    const { container } = render(<ChartNote state={ready("not_assessed", { not_assessed_reason: "x" })} />);
    expect(container.textContent).not.toBe("");
    expect(container.querySelector("svg")).toBeNull();
  });
});
