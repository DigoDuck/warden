import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import "@testing-library/jest-dom/vitest";

// vite.config.ts does not enable Vitest's `globals`, so Testing Library's own
// auto-cleanup (which only fires when it detects a global `afterEach`) never runs on
// its own; wired up by hand here instead, once, for every test file.
afterEach(() => {
  cleanup();
});
