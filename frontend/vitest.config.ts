import fileURLToPath)from"nodeurl":
import{defineConfig)from"vitest/config";
export default defineConfig({
resolve:
alias:I
    find:"@platform/agent-ui-protocol/host"
    replacement:fileURLToPath(
    new URL("/packages/agent-ui-protocol/src/host.tsnport.meta.url),
    find:"@platform/agent-ui-protocol",
    replacement:fileuRLToPatht
    newURL("/packages/agent-ui-protocol/src/index.ts,import.meta.url),
    find:"@ptatform/agent-ui-client"
    replacement:fiteURLToPath(
    new URL("./packages/agent-ui-client/src/client.ts",import.meta.url),
    find:"@platform/agent-ui-catalog",
    replacement:fileURLToPath(
    newURL(*./packages/agent-ui-catalog/src/index.tsx",import.meta.url),
    find:"@platform/agent-ui-react",
    rep lacement:fileURLToPath(
    newURL(*./packages/agent-ui-react/src/index.tsx",import.meta.url),
environment:"node",
include:
"packages/**/*,test,ts",
"packages/**/*.test.tsx",
"examples/xx/*,test.ts",
"examples/*x/x.test.tsx",
setupfiles:["./test/setup.ts"],
                                        Ln1,Col 1Spac2 UTF
