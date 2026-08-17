/**
 * Shared vitest setup.
 *
 * Extends expect with jest-dom matchers (used by jsdom-rendered component
 * tests); harmless in node-environment tests. Component tests opt into jsdom
 * per file with `// @vitest-environment jsdom`.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// vitest globals are off; register DOM cleanup explicitly so jsdom-rendered
// component tests do not leak nodes across test files.
afterEach(() => {
  cleanup();
});
