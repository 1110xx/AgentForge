import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

const fromProjectRoot = (path: string): string =>
  fileURLToPath(new URL(`../../${path}`, import.meta.url));

/**
 * Resolve the workspace packages directly from source so the example runs and
 * builds without requiring the packages to be pre-built (mirrors the vitest
 * aliases in frontend/vitest.config.ts).
 */
export const sourceAliases = [
  {
    find: /^@platform\/agent-ui-protocol\/host$/,
    replacement: fromProjectRoot("packages/agent-ui-protocol/src/host.ts"),
  },
  {
    find: /^@platform\/agent-ui-protocol$/,
    replacement: fromProjectRoot("packages/agent-ui-protocol/src/index.ts"),
  },
  {
    find: /^@platform\/agent-ui-client$/,
    replacement: fromProjectRoot("packages/agent-ui-client/src/client.ts"),
  },
  {
    find: /^@platform\/agent-ui-catalog$/,
    replacement: fromProjectRoot("packages/agent-ui-catalog/src/index.tsx"),
  },
  {
    find: /^@platform\/agent-ui-react$/,
    replacement: fromProjectRoot("packages/agent-ui-react/src/index.tsx"),
  },
] as const;

const backendTarget =
  process.env.AGENT_PLATFORM_BASE_URL ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [...sourceAliases],
  },
  server: {
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true,
        // The SDK talks to "/api/agent-platform/v1/*" while the backend
        // serves "/v1/*" — strip the SDK prefix before forwarding.
        rewrite: (path) => path.replace(/^\/api\/agent-platform/, ""),
        // SSE must not be buffered by the proxy.
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            proxyRes.headers["X-Accel-Buffering"] = "no";
            proxyRes.headers["Cache-Control"] = "no-cache, no-transform";
          });
        },
      },
    },
  },
});
