// Vitest + Testing Library setup (DECISIONS #23 component-test harness).
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Unmount React trees between tests so they do not leak DOM into each other.
afterEach(() => {
  cleanup();
  sessionStorage.clear();
});
