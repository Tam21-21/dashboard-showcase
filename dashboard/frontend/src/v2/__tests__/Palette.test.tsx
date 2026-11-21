// UI31 w12: the palette's failure note shows the server's own words, never a code prefix.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../lib/api";
import Palette from "../shell/Palette";

const runAll = vi.fn();
vi.mock("../../lib/refresh-registry", async (orig) => ({
  ...(await orig<typeof import("../../lib/refresh-registry")>()),
  runAllRefreshers: () => runAll(),
}));

function open() {
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <Palette live="open" go={() => undefined} onClose={() => undefined} />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText("Search commands"), { target: { value: "Refresh everything" } });
  fireEvent.keyDown(screen.getByLabelText("Search commands"), { key: "Enter" });
}

afterEach(() => {
  cleanup();
  runAll.mockReset();
});

describe("Palette failure note", () => {
  it("w12 an ApiError detail loses its code prefix", async () => {
    runAll.mockRejectedValue(new ApiError("POST /x 409", 409, { detail: "job-refused: busy" }));
    open();
    await waitFor(() => expect(screen.getByText("Could not run: Busy")).toBeInTheDocument());
  });

  it("w12 a non-error rejection reads Something went wrong.", async () => {
    runAll.mockRejectedValue("x");
    open();
    await waitFor(() => expect(screen.getByText("Could not run: Something went wrong.")).toBeInTheDocument());
  });
});
