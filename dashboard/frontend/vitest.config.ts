import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

// Component-test harness (DECISIONS #23): jsdom + Testing Library, render assertions over
// mocked API data. Kept separate from vite.config.ts so the production build is untouched.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: "./src/test/setup.ts",
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
    },
  }),
);
