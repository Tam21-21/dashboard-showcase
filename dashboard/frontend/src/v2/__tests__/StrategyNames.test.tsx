// UI31 w1: strategyLabel turns a strategy id into the text and the hover a page shows.
import { describe, expect, it } from "vitest";
import { ApiError } from "../../lib/api";
import { strategyLabel } from "../data/strategyNames";

const ROWS = [
  { id: 7, name: "ma_cross", display_name: "Moving average cross", status: "active" },
  { id: 8, name: "old_one", display_name: "Old one", status: "deleted" },
  { id: 9, name: "bare_one", display_name: null, status: "active" },
];
const ready = { data: ROWS, isError: false, error: null };

describe("strategyLabel", () => {
  it("a found id gives its display name, the stored name on hover", () => {
    expect(strategyLabel(ready, 7)).toEqual({ text: "Moving average cross", hover: "ma_cross" });
  });
  it("a deleted strategy adds ', deleted' to the hover", () => {
    expect(strategyLabel(ready, 8)).toEqual({ text: "Old one", hover: "old_one, deleted" });
  });
  it("a found id with no display name gives a dash, never a derived name", () => {
    expect(strategyLabel(ready, 9)).toEqual({ text: "-", hover: "No display name" });
  });
  it("an id not in a loaded list gives Strategy <id>", () => {
    expect(strategyLabel(ready, 99)).toEqual({ text: "Strategy 99", hover: "This strategy is not in the strategy list" });
  });
  it("loading gives a dash and its hover", () => {
    expect(strategyLabel({ data: undefined, isError: false, error: null }, 7)).toEqual({ text: "-", hover: "Loading the strategy names" });
  });
  it("a failed read gives a dash and the server's words", () => {
    const err = new ApiError("GET /x 500", 500, { detail: "store_locked: busy" });
    expect(strategyLabel({ data: undefined, isError: true, error: err }, 7)).toEqual({
      text: "-", hover: "Strategy names could not be read: Busy",
    });
  });
});
