// DBG-1: a failed request shows the server's own words, never the raw ApiError.message.
import { describe, it, expect } from "vitest";
import { ApiError } from "./api";
import { plainDetail, serverDetail } from "./errorText";

describe("serverDetail", () => {
  it("gives the server's detail string from an ApiError body", () => {
    expect(serverDetail(new ApiError("GET /x 422: http://h/x", 422, { detail: "x refused" }))).toBe("x refused");
  });

  it("gives a plain sentence when an ApiError has no detail", () => {
    expect(serverDetail(new ApiError("GET http://h/x 500", 500, null))).toBe("The server refused this request.");
    expect(serverDetail(new ApiError("GET http://h/x 500", 500, { detail: "" }))).toBe("The server refused this request.");
  });

  it("gives the message of a plain Error", () => {
    expect(serverDetail(new Error("Failed to fetch"))).toBe("Failed to fetch");
  });

  it("gives a plain sentence for anything else", () => {
    expect(serverDetail("boom")).toBe("Something went wrong.");
    expect(serverDetail(undefined)).toBe("Something went wrong.");
  });
});

describe("serverDetail: PF3 failures with no words a person can use", () => {
  it("e1 a body that is not JSON or a network failure reads as one plain sentence", () => {
    for (const e of [
      new SyntaxError("Unexpected end of JSON input"),
      new TypeError("Failed to fetch"),
      new TypeError("Failed to fetch (api.example)"),
      new TypeError("Load failed"),
      new TypeError("NetworkError when attempting to fetch resource."),
    ]) expect(serverDetail(e)).toBe("Could not load this from the server.");
  });
  it("e2 a code bug keeps its message, and a plain Error is untouched", () => {
    expect(serverDetail(new TypeError("Cannot read properties of undefined (reading 'x')"))).toBe(
      "Cannot read properties of undefined (reading 'x')",
    );
    expect(serverDetail(new Error("Failed to fetch"))).toBe("Failed to fetch");
  });
});

describe("plainDetail", () => {
  it("drops a leading code prefix and upper-cases the rest", () => {
    expect(plainDetail("cscv-split-count-not-frozen: the split count moved")).toBe("The split count moved");
  });
  it("turns a wholly code text into words", () => {
    expect(plainDetail("cannot_update_while_fetching")).toBe("Cannot update while fetching");
  });
  it("leaves anything else unchanged", () => {
    expect(plainDetail("ticker: AAPL not found")).toBe("ticker: AAPL not found");
    expect(plainDetail("Run 4 not found")).toBe("Run 4 not found");
    expect(plainDetail("none")).toBe("none");
  });
  it("serverDetail runs the detail and an Error's message through it", () => {
    expect(serverDetail(new ApiError("GET /x 409", 409, { detail: "no_data_yet: nothing stored" }))).toBe("Nothing stored");
    expect(serverDetail(new Error("job_refused: busy"))).toBe("Busy");
  });
});
