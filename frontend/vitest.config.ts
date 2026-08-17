import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: [
      {
        find: "@platform/agent-ui-protocol/host",
        replacement: fileURLToPath(
          new URL("./packages/agent-ui-protocol/src/host.ts", import.meta.url),
        ),
      },
      {
        find: "@platform/agent-ui-protocol",
        replacement: fileURLToPath(
          new URL("./packages/agent-ui-protocol/src/index.ts", import.meta.url),
        ),
      },
      {
        find: "@platform/agent-ui-client",
        replacement: fileURLToPath(
          new URL("./packages/agent-ui-client/src/client.ts", import.meta.url),
        ),
      },
      {
        find: "@platform/agent-ui-catalog",
        replacement: fileURLToPath(
          new URL("./packages/agent-ui-catalog/src/index.tsx", import.meta.url),
        ),
      },
      {
        find: "@platform/agent-ui-react",
        replacement: fileURLToPath(
          new URL("./packages/agent-ui-react/src/index.tsx", import.meta.url),
        ),
      },
    ],
  },
  test: {
    environment: "node",
    env: {
      // React 19 only exports `act` from its development build; forcing a
      // non-production NODE_ENV keeps jsdom component tests working even when
      // the invoking shell exports NODE_ENV=production.
      NODE_ENV: "test",
    },
    include: [
      "packages/**/*.test.ts",
      "packages/**/*.test.tsx",
      "examples/**/*.test.ts",
      "examples/**/*.test.tsx",
    ],
    setupFiles: ["./test/setup.ts"],
  },
});
